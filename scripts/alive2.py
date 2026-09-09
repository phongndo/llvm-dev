"""Pinned Alive2 built against an isolated LLVM from the selected source tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from . import llvm_dev as dev

MARKER = ".alive2-workspace.json"


class ProofFailure(dev.DevError):
    pass


def source_state(source):
    """Identify HEAD plus tracked and untracked edits; store hashes, not patches."""
    revision = dev.capture(["git", "-C", str(source), "rev-parse", "HEAD"])
    patch = subprocess.check_output(
        ["git", "-C", str(source), "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"]
    )
    untracked = subprocess.check_output(
        ["git", "-C", str(source), "ls-files", "--others", "--exclude-standard", "-z"]
    )
    digest = hashlib.sha256(patch)
    for raw in sorted(name for name in untracked.split(b"\0") if name):
        path = source / os.fsdecode(raw)
        digest.update(raw + b"\0")
        if path.is_symlink():
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise dev.DevError(f"Source changed while reading {path}; retry.")
        digest.update(b"\0")
    return {"revision": revision, "edits_sha256": digest.hexdigest()}


def locations(cfg):
    revision = os.environ.get("LLVM_DEV_ALIVE2_REVISION", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise dev.DevError("Missing pinned Alive2 environment; enter `nix develop`.")
    source = Path(os.environ.get("LLVM_DEV_ALIVE2_SOURCE", "/nonexistent"))
    if not (source / "CMakeLists.txt").is_file():
        raise dev.DevError("Pinned Alive2 source is unavailable; re-enter `nix develop`.")
    validation = replace(
        cfg,
        profile="validate",
        build=cfg.build.with_name(cfg.build.name + "-validation"),
        projects=None,
        targets=None,
        jobs=min(cfg.jobs, 8),
        link_jobs=1,
    )
    build = cfg.build.with_name(cfg.build.name + "-alive2-" + revision[:7])
    return validation, source, build, revision


def read_marker(path):
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("identity"), dict):
            raise ValueError("malformed marker")
        return data
    except (OSError, ValueError) as exc:
        raise dev.DevError(
            f"Missing or invalid {path}; run `just build-alive2` in a fresh build selection."
        ) from exc


def tool_identity(validation, source, revision):
    return {
        "schema": 1,
        "alive2_revision": revision,
        "alive2_source": str(source),
        "llvm_build": str(validation.build),
        "llvm_identity": dev.identity(validation),
        "z3_library": os.environ.get("LLVM_DEV_Z3_LIBRARY"),
    }


def build_alive2(cfg):
    validation, source, build, revision = locations(cfg)
    before = source_state(cfg.source)
    print(f"Validation LLVM: {validation.build}\nAlive2: {build}", flush=True)
    dev.configure(validation)
    with dev.build_lock(validation):
        # These LLVM libraries need RTTI; the everyday build remains unchanged.
        dev.build(validation, ["opt", "llvm-config"])
        identity = tool_identity(validation, source, revision)
        marker = build / MARKER
        if marker.exists():
            saved = read_marker(marker)["identity"]
            # Policy/source edits can be rebuilt, but never change the CMake source
            # directory or compiler underneath an existing Alive2 configuration.
            old_llvm = saved.get("llvm_identity", {})
            new_llvm = identity["llvm_identity"]
            if (
                saved.get("alive2_source") != identity["alive2_source"]
                or saved.get("llvm_build") != identity["llvm_build"]
                or any(old_llvm.get(k) != new_llvm[k] for k in ("cc", "cxx", "platform"))
            ):
                raise dev.DevError("Incompatible Alive2 build; choose a fresh base build_dir.")
        elif build.exists() and any(build.iterdir()):
            raise dev.DevError(f"Refusing unmanaged Alive2 build: {build}")
        alive_cfg = replace(validation, build=build)
        with dev.build_lock(alive_cfg):
            marker.write_text(
                json.dumps({"identity": identity, "configured": False}, indent=2) + "\n"
            )
            command = [
                dev.executable("cmake"),
                "-S",
                source,
                "-B",
                build,
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DBUILD_LLVM_UTILS=ON",
                "-DBUILD_TV=OFF",
                f"-DLLVM_DIR={validation.build / 'lib/cmake/llvm'}",
                f"-DCMAKE_C_COMPILER={identity['llvm_identity']['cc']}",
                f"-DCMAKE_CXX_COMPILER={identity['llvm_identity']['cxx']}",
                f"-DCMAKE_C_COMPILER_LAUNCHER={dev.executable('ccache')}",
                f"-DCMAKE_CXX_COMPILER_LAUNCHER={dev.executable('ccache')}",
                f"-DZ3_INCLUDE_DIR={os.environ['LLVM_DEV_Z3_INCLUDE']}",
                f"-DZ3_LIBRARIES={os.environ['LLVM_DEV_Z3_LIBRARY']}",
            ]
            dev.run_logged(alive_cfg, command, "alive2-configure")
            dev.run_logged(
                alive_cfg,
                [
                    dev.executable("cmake"),
                    "--build",
                    build,
                    "--parallel",
                    str(validation.jobs),
                    "--target",
                    "alive-tv",
                    "alive-exec",
                ],
                "alive2-build",
            )
            after = source_state(cfg.source)
            if before != after:
                raise dev.DevError(
                    "LLVM source changed during the validation build; run `just build-alive2` again."
                )
            marker.write_text(
                json.dumps(
                    {
                        "identity": identity,
                        "configured": True,
                        "llvm_source": after,
                    },
                    indent=2,
                )
                + "\n"
            )
    print("Alive2 is ready. Use `just prove before.ll after.ll` for an explicit IR pair.")


def function_names(ir):
    """Read names from canonical LLVM printer output, not arbitrary user syntax."""
    names = re.findall(
        r'^define\b[^\n]*?@("(?:[^"\\]|\\.)*"|[-a-zA-Z$._0-9]+)\s*\(', ir, re.MULTILINE
    )
    if len(names) != len(set(names)):
        raise ProofFailure("Duplicate function definitions in canonical IR.")
    return set(names)


def check_summary(output, expected):
    # Alive2 may return zero for an incorrect transformation. Exit status alone
    # is insufficient for automation; every requested function must be proved.
    values = []
    for label in (
        "correct transformations",
        "incorrect transformations",
        "failed-to-prove transformations",
        "Alive2 errors",
    ):
        matches = re.findall(r"^\s*(\d+) " + re.escape(label) + r"\s*$", output, re.MULTILINE)
        if len(matches) != 1:
            raise ProofFailure("Missing or ambiguous Alive2 summary; validation is inconclusive.")
        values.append(int(matches[0]))
    if expected < 1 or values != [expected, 0, 0, 0]:
        raise ProofFailure(
            f"Validation did not prove every function: correct/incorrect/unproved/errors = {values}; expected {expected} correct."
        )


def prove(cfg, before, after, timeout, wall_timeout=120):
    validation, source, build, revision = locations(cfg)
    dev.check_build(validation)
    saved = read_marker(build / MARKER)
    if not saved.get("configured") or saved["identity"] != tool_identity(
        validation, source, revision
    ):
        raise dev.DevError("Alive2 configuration is stale; run `just build-alive2`.")
    state = source_state(cfg.source)
    if saved.get("llvm_source") != state:
        raise dev.DevError("LLVM source changed since Alive2 was built; run `just build-alive2`.")
    inputs = [dev.absolute_path(value, cfg.root) for value in (before, after)]
    if any(not path.is_file() for path in inputs):
        raise dev.DevError("Provide two existing LLVM IR or bitcode files.")
    smt_ms = dev.positive(timeout, "timeout") * 1000
    print(
        f"LLVM {state['revision']} / Alive2 {revision}\nChecking the supplied IR pair, not running an internal optimization pipeline.",
        flush=True,
    )
    with dev.build_lock(validation), dev.build_lock(replace(cfg, build=build)):
        work = cfg.root / ".local/proofs"
        work.mkdir(parents=True, exist_ok=True)
        # Keep snapshots even on failure: counterexamples must remain reproducible.
        snapshot = Path(tempfile.mkdtemp(prefix="ir-", dir=work))
        canonical = [snapshot / "before.ll", snapshot / "after.ll"]
        (snapshot / "metadata.json").write_text(
            json.dumps(
                {
                    "llvm_source": state,
                    "alive2_revision": revision,
                    "inputs": list(map(str, inputs)),
                    "smt_timeout_ms": smt_ms,
                },
                indent=2,
            )
            + "\n"
        )
        for original, output in zip(inputs, canonical):
            dev.run_logged(
                validation,
                [
                    dev.developed_tool(validation, "opt"),
                    "-S",
                    "-passes=verify",
                    original,
                    "-o",
                    output,
                ],
                "proof-input",
            )
        left, right = (function_names(path.read_text()) for path in canonical)
        if not left or left != right:
            raise ProofFailure(
                "IR pairs must contain the same nonempty set of defined functions; interprocedural changes are not supported."
            )
        executable = build / "alive-tv"
        if not os.access(executable, os.X_OK):
            raise dev.DevError("alive-tv is missing; run `just build-alive2`.")
        seconds = dev.positive(wall_timeout, "wall-timeout")
        log = dev.run_logged(
            replace(cfg, build=build),
            [
                dev.executable("timeout"),
                "--kill-after=5s",
                str(seconds),
                executable,
                f"-smt-to={smt_ms}",
                *canonical,
            ],
            "prove",
        )
        check_summary(log.read_text(), len(left))
        if source_state(cfg.source) != state:
            raise ProofFailure("LLVM source changed during validation; rebuild and rerun.")
        print(
            f"Validated {len(left)} function(s). Bounded Alive2 validation is not a universal correctness proof.\nEvidence: {log}\nIR snapshots: {snapshot}"
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "prove"):
        sub = commands.add_parser(command)
        for option in ("source", "build-dir", "profile", "jobs", "link-jobs", "test-jobs"):
            sub.add_argument("--" + option)
        if command == "prove":
            sub.add_argument("before")
            sub.add_argument("after")
            sub.add_argument("--timeout", type=int, default=10, help="SMT query timeout in seconds")
            sub.add_argument(
                "--wall-timeout",
                type=int,
                default=120,
                help="whole Alive2 invocation timeout in seconds",
            )
    args = parser.parse_args(argv)
    try:
        cfg = dev.load_config(args)
        if args.command == "build":
            build_alive2(cfg)
        else:
            prove(cfg, args.before, args.after, args.timeout, args.wall_timeout)
        return 0
    except ProofFailure as exc:
        print(f"alive2: {exc}", file=sys.stderr)
        return 1
    except (dev.DevError, OSError, KeyError) as exc:
        print(f"alive2: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        return exc.returncode if exc.returncode > 0 else 128 - exc.returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
