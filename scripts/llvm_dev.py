#!/usr/bin/env python3
"""Small, explicit interface to a machine-local LLVM build."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAMP = ".llvm-dev.json"
GIB = 1024**3
SUITES = {
    "llvm": ("llvm/test", "test", "llvm-dev-llvm-test-depends"),
    "clang": ("clang/test", "tools/clang/test", "llvm-dev-clang-test-depends"),
    "mlir": ("mlir/test", "tools/mlir/test", "llvm-dev-mlir-test-depends"),
    "clang-tools": (
        "clang-tools-extra/test",
        "tools/clang/tools/extra/test",
        "llvm-dev-clang-tools-test-depends",
    ),
}


class DevError(Exception):
    """An actionable user/configuration error, without a Python traceback."""


def capture(command):
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise DevError(f"{shlex.join(map(str, command))}: {result.stderr.strip()}")
    return result.stdout.strip()


def executable(name):
    path = shutil.which(name)
    if not path:
        raise DevError(f"Missing executable {name!r}; enter `nix develop` first.")
    # Do not resolve clang++'s symlink: its argv[0] selects the C++ driver.
    return os.path.abspath(path)


def positive(value, name):
    if isinstance(value, bool):
        raise DevError(f"{name} must be a positive integer.")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise DevError(f"{name} must be a positive integer.") from exc
    if number < 1 or str(number) != str(value):
        raise DevError(f"{name} must be a positive integer.")
    return number


def resource_limits():
    """Conservative heuristic, also honoring Linux cgroup-v2 memory/CPU limits."""
    try:
        import psutil
    except ImportError as exc:
        raise DevError("Missing psutil; enter `nix develop`.") from exc
    memory = psutil.virtual_memory()
    total, available = memory.total, memory.available
    cpus = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        cpus = min(cpus, len(os.sched_getaffinity(0)))
    if platform.system() == "Linux":
        bases = [Path("/sys/fs/cgroup")]
        try:
            for line in Path("/proc/self/cgroup").read_text().splitlines():
                if line.startswith("0::"):
                    group = (bases[0] / line[3:].lstrip("/")).resolve()
                    if group.is_relative_to(bases[0]):
                        bases = [group, *group.parents[: len(group.parts) - 4]]
        except OSError:
            pass
        for base in bases:
            try:
                limit = int((base / "memory.max").read_text().strip())
                used = int((base / "memory.current").read_text().strip())
                total = min(total, limit)
                available = min(available, max(0, limit - used))
            except (OSError, ValueError):
                pass
            try:
                quota, period = (base / "cpu.max").read_text().split()
                cpus = min(cpus, max(1, int(quota) // int(period)))
            except (OSError, ValueError):
                pass
    # Leave room for the OS, editor, and one linker. This is not an OOM guarantee.
    jobs = max(1, min(cpus, int(max(0, available - 4 * GIB) // (2 * GIB))))
    return {
        "cpus": cpus,
        "total_gib": round(total / GIB, 1),
        "available_gib": round(available / GIB, 1),
        "jobs": jobs,
        "link_jobs": 2 if total >= 48 * GIB else 1,
        "test_jobs": min(jobs, 8),
    }


def absolute_path(value, root):
    if not isinstance(value, str) or not value:
        raise DevError("Paths must be nonempty strings.")
    path = Path(value).expanduser()
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


@dataclass(frozen=True)
class Config:
    root: Path
    source: Path
    build: Path
    profile: str
    jobs: int
    link_jobs: int
    test_jobs: int
    cache_dir: Path
    cache_size: str
    projects: tuple[str, ...] | None
    targets: tuple[str, ...] | None

    def env(self):
        return {
            **os.environ,
            "CCACHE_DIR": str(self.cache_dir),
            "CCACHE_MAXSIZE": self.cache_size,
            "CCACHE_COMPRESS": "true",
        }


def load_config(args, root=ROOT, limits=None):
    root = root.resolve()
    path = root / ".local/config.toml"
    try:
        data = tomllib.loads(path.read_text()) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DevError(f"Invalid {path}: {exc}") from exc
    allowed = {
        "source",
        "build_dir",
        "profile",
        "jobs",
        "link_jobs",
        "test_jobs",
        "cache_dir",
        "cache_size",
        "projects",
        "targets",
    }
    if unknown := set(data) - allowed:
        raise DevError(f"Unknown local settings: {', '.join(sorted(unknown))}")
    limits = limits or resource_limits()

    def setting(key, default):
        cli = getattr(args, key, None)
        return (
            cli
            if cli is not None
            else os.environ.get("LLVM_DEV_" + key.upper(), data.get(key, default))
        )

    profile = setting("profile", "dev")
    if profile not in ("dev", "debug", "validate"):
        raise DevError("profile must be dev, debug, or validate.")
    source = absolute_path(setting("source", "llvm-project"), root)
    build = absolute_path(setting("build_dir", f"build/{profile}"), root)
    if (
        build == root
        or build in root.parents
        or build.is_relative_to(source)
        or source.is_relative_to(build)
    ):
        raise DevError("Build must be separate from the source and not contain the workspace.")
    # External worktrees get their own explicit build directory.
    if source != root / "llvm-project" and setting("build_dir", None) is None:
        raise DevError("A source override requires an explicit build_dir for isolation.")

    def names(key):
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or any(
            not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", v) for v in value
        ):
            raise DevError(f"{key} must be an array of project/backend names.")
        if key == "targets" and not value:
            raise DevError("targets must contain at least one backend.")
        return tuple(value)

    cache_dir = absolute_path(
        setting(
            "cache_dir",
            os.environ.get(
                "CCACHE_DIR",
                str(Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ccache"),
            ),
        ),
        root,
    )
    cache_size = setting("cache_size", "100Gi")
    if not isinstance(cache_size, str) or not re.fullmatch(
        r"[1-9][0-9]*(?:Ki|Mi|Gi|Ti|K|M|G|T)", cache_size
    ):
        raise DevError("cache_size must be a positive ccache size, for example '100Gi'.")
    return Config(
        root,
        source,
        build,
        profile,
        positive(setting("jobs", limits["jobs"]), "jobs"),
        positive(setting("link_jobs", limits["link_jobs"]), "link_jobs"),
        positive(setting("test_jobs", limits["test_jobs"]), "test_jobs"),
        cache_dir,
        cache_size,
        names("projects"),
        names("targets"),
    )


def read_cache(build):
    path = build / "CMakeCache.txt"
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        match = re.match(r"([^/#][^:]*):[^=]+=(.*)", line)
        if match:
            values[match[1]] = match[2]
    return values


def identity(cfg):
    files = [
        cfg.root / "flake.lock",
        cfg.root / "cmake/common.cmake",
        cfg.root / "cmake/test-dependencies.cmake",
        cfg.root / f"cmake/{cfg.profile}.cmake",
    ]
    return {
        "schema": 1,
        "source": str(cfg.source),
        "profile": cfg.profile,
        "platform": f"{platform.system()}-{platform.machine()}",
        "cc": executable(os.environ.get("CC", "clang")),
        "cxx": executable(os.environ.get("CXX", "clang++")),
        "ccache": executable("ccache"),
        "projects": list(cfg.projects) if cfg.projects is not None else None,
        "targets": list(cfg.targets) if cfg.targets is not None else None,
        "policy": hashlib.sha256(b"\0".join(p.read_bytes() for p in files)).hexdigest(),
    }


def read_stamp(cfg):
    try:
        data = json.loads((cfg.build / STAMP).read_text())
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("identity"), dict)
            or not isinstance(data.get("configured"), bool)
            or not isinstance(data.get("cache", {}), dict)
        ):
            raise ValueError("malformed ownership marker")
        return data
    except (OSError, ValueError) as exc:
        raise DevError(
            f"Invalid or missing build ownership marker in {cfg.build}; select a fresh build_dir."
        ) from exc


def check_build(cfg, *, configuring=False):
    if not (cfg.source / "llvm/CMakeLists.txt").is_file():
        raise DevError(f"No LLVM monorepo at {cfg.source}; clone it manually or set source.")
    current = identity(cfg)
    marker = cfg.build / STAMP
    if not marker.exists():
        if cfg.build.exists() and any(cfg.build.iterdir()):
            raise DevError(
                f"Refusing unmanaged/nonempty build: {cfg.build}. Select a fresh build_dir; existing files were not changed."
            )
        if configuring:
            return current
        raise DevError("Build is not configured; run `just configure` first.")
    saved = read_stamp(cfg)
    previous = saved.get("identity", {})
    changes = [key for key in current if previous.get(key) != current[key]]
    immutable = {"schema", "source", "profile", "platform", "cc", "cxx", "ccache"}
    if any(key in immutable for key in changes):
        raise DevError(
            f"Incompatible build ({', '.join(changes)}); select a fresh build_dir. Nothing was removed."
        )
    if not configuring and (changes or not saved.get("configured")):
        raise DevError("Build configuration changed or was interrupted; run `just configure`.")
    if saved.get("configured"):
        cache = read_cache(cfg.build)
        expected = saved.get("cache", {})
        if any(cache.get(k) != v for k, v in expected.items()):
            raise DevError("CMake cache changed outside llvm-dev; select a fresh build_dir.")
    return current


@contextmanager
def build_lock(cfg):
    cfg.build.mkdir(parents=True, exist_ok=True)
    with (cfg.build / ".llvm-dev.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DevError(
                "Another llvm-dev command is using this build; wait for it to finish."
            ) from exc
        yield


def run_logged(cfg, command, label):
    """Stream ordinary output and keep complete evidence without recording secrets/env."""
    logs = cfg.root / ".local/logs"
    logs.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    log_path = logs / f"{timestamp}-{label}.log"
    command = list(map(str, command))
    print(f"+ {shlex.join(command)}\nLog: {log_path}", flush=True)
    with log_path.open("w") as log:
        log.write(
            f"command: {shlex.join(command)}\nsource: {cfg.source}\nbuild: {cfg.build}\nprofile: {cfg.profile}\n"
        )
        revision = subprocess.run(
            ["git", "-C", str(cfg.source), "rev-parse", "HEAD"], capture_output=True, text=True
        )
        status = subprocess.run(
            ["git", "-C", str(cfg.source), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
        )
        log.write(
            f"revision: {revision.stdout.strip()}\ntracked_changes: {status.stdout.strip() or '(none)'}\n"
        )
        log.flush()
        with subprocess.Popen(
            command,
            cwd=cfg.root,
            env=cfg.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        ) as process:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    log.write(line)
                    log.flush()
                result = process.wait()
            except BaseException:
                # Terminate this command's process group, including Ninja's compilers.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                except ProcessLookupError:
                    pass
                log.write("\ninterrupted: true\n")
                raise
        log.write(f"\nexit_status: {result}\n")
    if result:
        raise subprocess.CalledProcessError(result, command)
    return log_path


def configure(cfg):
    current = check_build(cfg, configuring=True)
    cache_keys = (
        "CMAKE_HOME_DIRECTORY",
        "CMAKE_GENERATOR",
        "CMAKE_C_COMPILER",
        "CMAKE_CXX_COMPILER",
        "CMAKE_C_COMPILER_LAUNCHER",
        "CMAKE_CXX_COMPILER_LAUNCHER",
        "CMAKE_BUILD_TYPE",
        "LLVM_ENABLE_PROJECTS",
        "LLVM_TARGETS_TO_BUILD",
        "LLVM_ENABLE_ASSERTIONS",
        "LLVM_ENABLE_RTTI",
        "LLVM_ENABLE_EH",
        "CMAKE_C_FLAGS_RELEASE",
        "CMAKE_CXX_FLAGS_RELEASE",
        "CMAKE_DISABLE_PRECOMPILE_HEADERS",
        "CMAKE_C_FLAGS_RELWITHDEBINFO",
        "CMAKE_CXX_FLAGS_RELWITHDEBINFO",
        "CMAKE_C_FLAGS_DEBUG",
        "CMAKE_CXX_FLAGS_DEBUG",
        "BUILD_SHARED_LIBS",
        "LLVM_USE_LINKER",
    )
    with build_lock(cfg):
        marker = cfg.build / STAMP
        marker.write_text(json.dumps({"identity": current, "configured": False}, indent=2) + "\n")
        command = [
            executable("cmake"),
            "-S",
            cfg.source / "llvm",
            "-B",
            cfg.build,
            "-G",
            "Ninja",
            "-C",
            cfg.root / f"cmake/{cfg.profile}.cmake",
            f"-DCMAKE_C_COMPILER={current['cc']}",
            f"-DCMAKE_CXX_COMPILER={current['cxx']}",
            f"-DCMAKE_C_COMPILER_LAUNCHER={current['ccache']}",
            f"-DCMAKE_CXX_COMPILER_LAUNCHER={current['ccache']}",
            f"-DPython3_EXECUTABLE={sys.executable}",
            f"-DLLVM_PARALLEL_COMPILE_JOBS={cfg.jobs}",
            f"-DLLVM_PARALLEL_LINK_JOBS={cfg.link_jobs}",
            f"-DLLVM_PARALLEL_TABLEGEN_JOBS={cfg.jobs}",
            f"-DLLVM_LIT_ARGS=-sv -j {cfg.test_jobs}",
        ]
        if cfg.projects is not None:
            command.append(f"-DLLVM_ENABLE_PROJECTS={';'.join(cfg.projects)}")
        if cfg.targets is not None:
            command.append(f"-DLLVM_TARGETS_TO_BUILD={';'.join(cfg.targets)}")
        if platform.system() == "Linux":
            # A previously missing optional library is cached as absent by CMake.
            # Re-probe when configuring after adding libpfm to the shell.
            command += ["-UHAVE_LIBPFM_INITIALIZE", "-UHAVE_PERFMON_*"]
            executable("ld.lld")
            command.append("-DLLVM_USE_LINKER=lld")
            if runtime_path := os.environ.get("LLVM_DEV_RUNTIME_LIBRARY_PATH"):
                command.append(f"-DCMAKE_BUILD_RPATH={runtime_path.replace(os.pathsep, ';')}")
        run_logged(cfg, command, "configure")
        cache = read_cache(cfg.build)
        marker.write_text(
            json.dumps(
                {
                    "identity": current,
                    "configured": True,
                    "cache": {key: cache[key] for key in cache_keys if key in cache},
                },
                indent=2,
            )
            + "\n"
        )


def build(cfg, targets):
    if not targets:
        targets = ["opt", "FileCheck", "count", "not", "split-file", "llvm-config"]
    if any(not re.fullmatch(r"[A-Za-z0-9_.+/-]+", t) or t.startswith("-") for t in targets):
        raise DevError("Use explicit CMake target names, not build-system flags.")
    run_logged(
        cfg,
        [
            executable("cmake"),
            "--build",
            cfg.build,
            "--parallel",
            str(cfg.jobs),
            "--target",
            *targets,
        ],
        "build",
    )


def suite_for_path(cfg, value):
    path = absolute_path(value, cfg.source)
    for name, (source_dir, build_dir, depends) in SUITES.items():
        source_root = (cfg.source / source_dir).resolve()
        if path.is_relative_to(source_root) and path.exists():
            if "Inputs" in path.relative_to(source_root).parts:
                raise DevError("Inputs/ holds fixtures; select a test file or test directory.")
            if not (cfg.build / build_dir / "lit.site.cfg.py").is_file():
                raise DevError(
                    f"Suite {name} is not configured in this build; enable its project and configure."
                )
            return name, path, depends
    raise DevError(
        "Select an existing test under llvm/test, clang/test, mlir/test, or clang-tools-extra/test in the selected source tree."
    )


def test(cfg, args):
    if args.command == "check":
        source_dir, _, _ = SUITES[args.suite]
        name, path, depends = suite_for_path(cfg, str(cfg.source / source_dir))
    else:
        name, path, depends = suite_for_path(cfg, args.path)
    if not args.no_build:
        print(
            f"Building {name} suite prerequisites ({depends}); the first run can be large.",
            flush=True,
        )
        build(cfg, [depends])
    else:
        print(
            "Explicit --no-build: prerequisite freshness is the caller's responsibility.",
            flush=True,
        )
    runner = cfg.build / "bin/llvm-lit"
    if not runner.is_file():
        raise DevError("Missing build/bin/llvm-lit; reconfigure this build.")
    command = [sys.executable, runner, "-sv", "-j", str(cfg.test_jobs)]
    if args.filter:
        command += ["--filter", args.filter]
    command += ["--timeout", str(args.timeout), path]
    run_logged(cfg, command, "test")


def compilation_database(cfg):
    target = cfg.build / "compile_commands.json"
    if not target.is_file():
        raise DevError("No compile_commands.json; configure the build first.")
    link = cfg.root / "compile_commands.json"
    record = cfg.root / ".local/compdb-link.json"
    if link.exists() or link.is_symlink():
        try:
            owned = json.loads(record.read_text())
        except (OSError, ValueError):
            owned = None
        if not link.is_symlink() or str(link.resolve()) != owned:
            raise DevError(
                "Existing compilation database is not managed by llvm-dev; leave it intact and set clangd --compile-commands-dir explicitly."
            )
        link.unlink()
    record.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    record.write_text(json.dumps(str(target)) + "\n")
    print(f"{link} -> {target}")


def developed_tool(cfg, name):
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", name):
        raise DevError("Select a tool name from this build's bin directory.")
    tool = cfg.build / "bin" / name
    if not tool.resolve().is_relative_to(cfg.build) or not os.access(tool, os.X_OK):
        raise DevError(f"Tool not built: {tool}. Run `just build {name}`.")
    return tool


def debugger():
    if platform.system() == "Darwin":
        return capture(["/usr/bin/xcrun", "--find", "lldb"])
    return executable("lldb")


def doctor(cfg, as_json=False):
    report = {
        "source": str(cfg.source),
        "build": str(cfg.build),
        "profile": cfg.profile,
        "system": f"{platform.system()} {platform.machine()}",
        "nix_shell": os.environ.get("LLVM_DEV_SHELL") == "1",
        "resources": resource_limits(),
        "jobs": cfg.jobs,
        "link_jobs": cfg.link_jobs,
        "test_jobs": cfg.test_jobs,
        "cache_dir": str(cfg.cache_dir),
        "cache_size": cfg.cache_size,
        "tools": {},
        "warnings": [],
        "errors": [],
    }
    for tool, flag in {
        "cmake": "--version",
        "ninja": "--version",
        "ccache": "--version",
        "just": "--version",
        "clangd": "--version",
        "git": "--version",
        "uv": "--version",
        "ty": "--version",
        "ruff": "--version",
        "z3": "--version",
        "hyperfine": "--version",
        "creduce": "--version",
    }.items():
        try:
            path = executable(tool)
            report["tools"][tool] = {"path": path, "version": capture([path, flag]).splitlines()[0]}
        except DevError as exc:
            report["errors"].append(str(exc))
    for key, fallback in (("CC", "clang"), ("CXX", "clang++")):
        try:
            path = executable(os.environ.get(key, fallback))
            report["tools"][key] = {
                "path": path,
                "version": capture([path, "--version"]).splitlines()[0],
            }
        except DevError as exc:
            report["errors"].append(str(exc))
    try:
        path = debugger()
        report["tools"]["lldb"] = {
            "path": path,
            "version": capture([path, "--version"]).splitlines()[0],
        }
    except DevError as exc:
        report["errors"].append(str(exc))
    if platform.system() == "Darwin":
        try:
            report["apple_sdk"] = capture(["/usr/bin/xcrun", "--show-sdk-path"])
            report["nix_sdkroot"] = os.environ.get("SDKROOT", "(provided by compiler wrapper)")
        except DevError as exc:
            report["errors"].append(str(exc))
    elif platform.system() == "Linux":
        report["wsl"] = "microsoft" in platform.release().lower()
        if report["wsl"] and any(
            str(p).startswith("/mnt/") for p in (cfg.source, cfg.build, cfg.cache_dir)
        ):
            report["warnings"].append(
                "Use WSL's Linux filesystem for source/build/cache, not /mnt/."
            )
        try:
            report["tools"]["linker"] = executable("ld.lld")
            perf = executable("perf")
            report["tools"]["perf"] = {"path": perf, "version": capture([perf, "--version"])}
        except DevError as exc:
            report["errors"].append(str(exc))
        try:
            report["perf_event_paranoid"] = (
                Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip()
            )
        except OSError:
            report["warnings"].append(
                "Kernel perf policy is unavailable; hardware counters need host verification."
            )
    report["alive2_revision"] = os.environ.get("LLVM_DEV_ALIVE2_REVISION", "not in the Nix shell")
    if (cfg.source / "llvm/CMakeLists.txt").is_file():
        try:
            report["llvm_revision"] = capture(["git", "-C", str(cfg.source), "rev-parse", "HEAD"])
            report["llvm_status"] = capture(
                ["git", "-C", str(cfg.source), "status", "--short", "--branch"]
            )
        except DevError as exc:
            report["warnings"].append(str(exc))
    else:
        report["warnings"].append("LLVM checkout is absent; clone it manually before configuring.")
    report["configured_cache"] = {
        k: v
        for k, v in read_cache(cfg.build).items()
        if k
        in {
            "CMAKE_BUILD_TYPE",
            "CMAKE_C_COMPILER",
            "CMAKE_CXX_COMPILER",
            "LLVM_TARGETS_TO_BUILD",
            "LLVM_ENABLE_PROJECTS",
            "LLVM_ENABLE_RTTI",
            "LLVM_ENABLE_EH",
            "LLVM_ENABLE_LIBPFM",
            "HAVE_LIBPFM_INITIALIZE",
            "LLVM_PARALLEL_COMPILE_JOBS",
            "LLVM_PARALLEL_LINK_JOBS",
        }
    }
    if cfg.build.exists():
        try:
            check_build(cfg)
            report["build_status"] = "managed, configured"
        except DevError as exc:
            report["warnings"].append(str(exc))
    else:
        report["build_status"] = "not configured"
    report["developed_tools"] = {
        t: str(cfg.build / "bin" / t)
        for t in ("opt", "clang", "llc", "mlir-opt", "llvm-reduce", "llvm-mca", "llvm-exegesis")
        if (cfg.build / "bin" / t).exists()
    }
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        for key, value in report.items():
            print(
                f"{key}: {json.dumps(value, indent=2) if isinstance(value, (dict, list)) else value}"
            )
    return 1 if report["errors"] else 0


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    for name in ("source", "build-dir", "profile", "jobs", "link-jobs", "test-jobs"):
        common.add_argument(f"--{name}")
    commands = result.add_subparsers(dest="command", required=True)
    for name in (
        "doctor",
        "configure",
        "build",
        "test",
        "check",
        "cache-stats",
        "compdb",
        "run",
        "debug",
    ):
        sub = commands.add_parser(name, parents=[common])
        if name == "doctor":
            sub.add_argument("--json", action="store_true")
        elif name == "build":
            sub.add_argument("targets", nargs="*")
        elif name in ("test", "check"):
            if name == "test":
                sub.add_argument(
                    "path", help="test path relative to the LLVM monorepo, or absolute"
                )
            else:
                sub.add_argument("suite", choices=SUITES)
            sub.add_argument(
                "--no-build", action="store_true", help="explicitly skip prerequisite builds"
            )
            sub.add_argument("--filter", help="lit test-name regular expression")
            sub.add_argument("--timeout", type=int, default=120, help="per-test timeout in seconds")
        elif name in ("run", "debug"):
            sub.add_argument("tool")
            sub.add_argument("args", nargs=argparse.REMAINDER)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        cfg = load_config(args)
        if args.command == "doctor":
            return doctor(cfg, args.json)
        if args.command == "cache-stats":
            return subprocess.run([executable("ccache"), "--show-stats"], env=cfg.env()).returncode
        if args.command == "configure":
            configure(cfg)
            return 0
        check_build(cfg)
        if args.command in ("run", "debug"):
            tool = developed_tool(cfg, args.tool)
            extra = args.args[1:] if args.args[:1] == ["--"] else args.args
            command = [str(tool), *extra]
            if args.command == "debug":
                command = [debugger(), "--", *command]
            return subprocess.run(command, cwd=cfg.root, env=cfg.env()).returncode
        with build_lock(cfg):
            if args.command == "build":
                build(cfg, args.targets)
            elif args.command in ("test", "check"):
                positive(args.timeout, "timeout")
                test(cfg, args)
            elif args.command == "compdb":
                compilation_database(cfg)
        return 0
    except (DevError, OSError) as exc:
        print(f"llvm-dev: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        return exc.returncode if exc.returncode > 0 else 128 - exc.returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
