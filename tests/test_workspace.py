"""Test the command interface with disposable source/build trees, never real LLVM."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import llvm_dev as dev

LIMITS = {"jobs": 4, "link_jobs": 1, "test_jobs": 2}


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="llvm-dev test ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "llvm-project"
        (self.source / "llvm").mkdir(parents=True)
        (self.source / "llvm/CMakeLists.txt").write_text("# fixture\n")
        for relative in (
            "flake.lock",
            "justfile",
            "pyproject.toml",
            "scripts/llvm_dev.py",
            "cmake/common.cmake",
            "cmake/test-dependencies.cmake",
            "cmake/dev.cmake",
            "cmake/debug.cmake",
            "cmake/validate.cmake",
        ):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dev.ROOT / relative, destination)
        self.env = {
            key: value for key, value in os.environ.items() if not key.startswith("LLVM_DEV_")
        }
        self.env["CCACHE_DIR"] = str(self.root / "cache")
        self.env["CCACHE_CONFIGPATH"] = str(self.root / "empty-ccache.conf")
        (self.root / "empty-ccache.conf").write_text("")
        self.environ = patch.dict(os.environ, self.env, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def config(self, command="doctor", *args):
        return dev.load_config(dev.parser().parse_args([command, *args]), self.root, LIMITS)

    def local(self, text):
        path = self.root / ".local/config.toml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)

    def owned(self, cfg):
        cfg.build.mkdir(parents=True, exist_ok=True)
        (cfg.build / dev.STAMP).write_text(
            json.dumps(
                {
                    "identity": dev.identity(cfg),
                    "configured": True,
                    "cache": {},
                }
            )
        )


class ConfigurationTests(WorkspaceFixture):
    def test_defaults_and_explicit_overrides(self):
        cfg = self.config()
        self.assertEqual(cfg.profile, "dev")
        self.assertEqual(cfg.build, self.root / "build/dev")
        self.assertEqual(cfg.jobs, 4)
        self.assertEqual(cfg.cache_size, "100Gi")
        self.local('profile = "debug"\njobs = 3\nprojects = []\ntargets = ["X86"]\n')
        cfg = self.config("doctor", "--jobs", "2")
        self.assertEqual(cfg.profile, "debug")
        self.assertEqual(cfg.jobs, 2)
        self.assertEqual(cfg.projects, ())
        self.assertEqual(cfg.targets, ("X86",))
        self.assertEqual(cfg.build, self.root / "build/debug")
        with patch.dict(os.environ, {"LLVM_DEV_JOBS": "5"}):
            self.assertEqual(self.config().jobs, 5)
            self.assertEqual(self.config("doctor", "--jobs", "6").jobs, 6)

    def test_invalid_configuration_fails_clearly(self):
        for text in (
            "jobs = 0",
            "jobs = true",
            "jobs = 1.5",
            "jobs = 'oops'",
            "job = 2",
            "profile = 'release'",
            "projects = 'clang'",
            "targets = []",
            "projects = ['clang;mlir']",
            "cache_size = '0'",
            "[broken",
        ):
            with self.subTest(text=text):
                self.local(text)
                with self.assertRaises(dev.DevError):
                    self.config()

    def test_source_and_build_separation(self):
        for value in (".", "..", "llvm-project", "llvm-project/build"):
            with self.subTest(value=value):
                self.local(f'build_dir = "{value}"\n')
                with self.assertRaises(dev.DevError):
                    self.config()
        self.local('source = "worktrees/topic"\n')
        with self.assertRaisesRegex(dev.DevError, "explicit build_dir"):
            self.config()
        self.local('source = "worktrees/topic"\nbuild_dir = "build/topic-dev"\n')
        self.assertEqual(self.config().build.name, "topic-dev")

    def test_unmanaged_build_is_untouched(self):
        cfg = self.config()
        cfg.build.mkdir(parents=True)
        sentinel = cfg.build / "CMakeCache.txt"
        sentinel.write_text("personal build\n")
        with self.assertRaisesRegex(dev.DevError, "unmanaged/nonempty"):
            dev.check_build(cfg, configuring=True)
        self.assertEqual(sentinel.read_text(), "personal build\n")
        self.assertEqual(list(cfg.build.iterdir()), [sentinel])

    def test_ownership_and_policy_changes(self):
        cfg = self.config()
        self.owned(cfg)
        dev.check_build(cfg)
        with self.assertRaisesRegex(dev.DevError, "Incompatible"):
            dev.check_build(replace(cfg, profile="debug"), configuring=True)
        (self.root / "cmake/common.cmake").write_text("# new policy\n")
        with self.assertRaisesRegex(dev.DevError, "configuration changed"):
            dev.check_build(cfg)
        dev.check_build(cfg, configuring=True)

    def test_manual_cache_changes_are_rejected(self):
        cfg = self.config()
        self.owned(cfg)
        marker = cfg.build / dev.STAMP
        data = json.loads(marker.read_text())
        data["cache"] = {"CMAKE_HOME_DIRECTORY": str(cfg.source / "llvm")}
        marker.write_text(json.dumps(data))
        (cfg.build / "CMakeCache.txt").write_text("CMAKE_HOME_DIRECTORY:INTERNAL=/other/source\n")
        with self.assertRaisesRegex(dev.DevError, "cache changed"):
            dev.check_build(cfg)

    def test_malformed_ownership_markers_fail_clearly(self):
        cfg = self.config()
        cfg.build.mkdir(parents=True)
        for data in ("[]", "null", "{}", '{"identity": [], "configured": true}', "broken"):
            with self.subTest(data=data):
                (cfg.build / dev.STAMP).write_text(data)
                with self.assertRaisesRegex(dev.DevError, "ownership marker"):
                    dev.check_build(cfg)

    def test_compiler_argv_zero_is_preserved(self):
        target = self.root / "clang"
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
        driver = self.root / "clang++"
        driver.symlink_to(target)
        self.assertEqual(dev.executable(str(driver)), str(driver))

    def test_build_lock_excludes_another_command(self):
        cfg = self.config()
        with dev.build_lock(cfg):
            with self.assertRaisesRegex(dev.DevError, "Another"):
                with dev.build_lock(cfg):
                    self.fail("lock must exclude concurrent commands")

    def test_compilation_database_protects_user_files(self):
        cfg = self.config()
        cfg.build.mkdir(parents=True)
        (cfg.build / "compile_commands.json").write_text("[]\n")
        link = self.root / "compile_commands.json"
        link.write_text("user content\n")
        with self.assertRaisesRegex(dev.DevError, "not managed"):
            dev.compilation_database(cfg)
        self.assertEqual(link.read_text(), "user content\n")
        link.unlink()
        dev.compilation_database(cfg)
        self.assertEqual(link.resolve(), cfg.build / "compile_commands.json")
        other = replace(cfg, build=self.root / "build/debug")
        other.build.mkdir()
        (other.build / "compile_commands.json").write_text("[]\n")
        dev.compilation_database(other)
        self.assertEqual(link.resolve(), other.build / "compile_commands.json")
        link.unlink()
        link.symlink_to(cfg.build / "compile_commands.json")
        with self.assertRaisesRegex(dev.DevError, "not managed"):
            dev.compilation_database(other)

    def test_suite_paths_and_symlink_escape(self):
        cfg = self.config()
        for name, (source_path, build_path, depends) in dev.SUITES.items():
            source = cfg.source / source_path
            source.mkdir(parents=True, exist_ok=True)
            build = cfg.build / build_path
            build.mkdir(parents=True, exist_ok=True)
            (build / "lit.site.cfg.py").write_text("# fixture\n")
            test = source / "case with spaces.ll"
            test.write_text("; fixture\n")
            selected = dev.suite_for_path(cfg, str(test.relative_to(cfg.source)))
            self.assertEqual(selected, (name, test, depends))
        outside = self.root / "external.ll"
        outside.touch()
        (cfg.source / "llvm/test/escape.ll").symlink_to(outside)
        for path in ("llvm/test/escape.ll", "../external.ll", "llvm/test/missing.ll"):
            with self.assertRaises(dev.DevError):
                dev.suite_for_path(cfg, path)
        inputs = cfg.source / "llvm/test/Inputs"
        inputs.mkdir()
        with self.assertRaisesRegex(dev.DevError, "fixtures"):
            dev.suite_for_path(cfg, str(inputs))

    def test_tool_selection_cannot_escape_build(self):
        cfg = self.config()
        for name in ("../opt", "/bin/sh", "-x", "opt;echo"):
            with self.assertRaises(dev.DevError):
                dev.developed_tool(cfg, name)
        binary_dir = cfg.build / "bin"
        binary_dir.mkdir(parents=True)
        (binary_dir / "escaped").symlink_to(sys.executable)
        with self.assertRaises(dev.DevError):
            dev.developed_tool(cfg, "escaped")


class ResourceTests(unittest.TestCase):
    def test_mac_and_memory_headroom(self):
        import psutil

        with (
            patch.object(dev.platform, "system", return_value="Darwin"),
            patch.object(dev.os, "cpu_count", return_value=12),
            patch.object(dev.os, "sched_getaffinity", return_value=set(range(12)), create=True),
            patch.object(
                psutil,
                "virtual_memory",
                return_value=SimpleNamespace(total=64 * dev.GIB, available=60 * dev.GIB),
            ),
        ):
            result = dev.resource_limits()
        self.assertEqual(result["jobs"], 12)
        self.assertEqual(result["link_jobs"], 2)
        self.assertEqual(result["test_jobs"], 8)

    def test_cgroup_hierarchy_limits_cpu_and_available_memory(self):
        import psutil

        files = {
            "/proc/self/cgroup": "0::/jobs/task\n",
            "/sys/fs/cgroup/jobs/task/memory.max": str(8 * dev.GIB),
            "/sys/fs/cgroup/jobs/task/memory.current": str(2 * dev.GIB),
            "/sys/fs/cgroup/jobs/task/cpu.max": "200000 100000",
            "/sys/fs/cgroup/jobs/memory.max": str(6 * dev.GIB),
            "/sys/fs/cgroup/jobs/memory.current": str(3 * dev.GIB),
            "/sys/fs/cgroup/jobs/cpu.max": "100000 100000",
            "/sys/fs/cgroup/memory.max": "max",
            "/sys/fs/cgroup/cpu.max": "max 100000",
        }

        def read(path, *args, **kwargs):
            if str(path) not in files:
                raise FileNotFoundError(path)
            return files[str(path)]

        with (
            patch.object(dev.platform, "system", return_value="Linux"),
            patch.object(dev.os, "cpu_count", return_value=32),
            patch.object(dev.os, "sched_getaffinity", return_value=set(range(32)), create=True),
            patch.object(
                psutil,
                "virtual_memory",
                return_value=SimpleNamespace(total=64 * dev.GIB, available=60 * dev.GIB),
            ),
            patch.object(Path, "read_text", read),
        ):
            result = dev.resource_limits()
        self.assertEqual(result["cpus"], 1)
        self.assertEqual(result["total_gib"], 6)
        self.assertEqual(result["available_gib"], 3)
        self.assertEqual(result["jobs"], 1)
        self.assertEqual(result["link_jobs"], 1)


@unittest.skipUnless(
    all(shutil.which(t) for t in ("cmake", "ninja", "ccache", "clang", "just")),
    "enter nix develop for real CMake fixture tests",
)
class CommandIntegrationTests(WorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.local("jobs = 2\nlink_jobs = 1\ntest_jobs = 1\n")
        cmake_source = self.source / "llvm"
        (cmake_source / "CMakeLists.txt").write_text("""cmake_minimum_required(VERSION 3.20)
project(LLVM LANGUAGES C CXX)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_executable(hello hello.cpp)
# Match current LLVM's default: projects may request PCH. The workspace's
# cache-first policy must disable it so ccache works without sloppiness.
target_precompile_headers(hello PRIVATE <iostream>)
file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/test")
file(WRITE "${CMAKE_BINARY_DIR}/test/lit.site.cfg.py" "# fixture\\n")
configure_file(fake-lit.py "${CMAKE_BINARY_DIR}/bin/llvm-lit" COPYONLY)
add_custom_target(fixture-test-depends
  COMMAND "${CMAKE_COMMAND}" -E touch "${CMAKE_BINARY_DIR}/test-dependencies-built"
  DEPENDS hello)
# Building prerequisites must never execute the check target's command.
add_custom_target(check-llvm COMMAND "${CMAKE_COMMAND}" -E false)
add_dependencies(check-llvm fixture-test-depends)
""")
        (cmake_source / "hello.cpp").write_text("""#include <iostream>
int main(int argc, char **argv) {
  for (int i = 1; i < argc; ++i) std::cout << argv[i] << "\\n";
  return 0;
}
""")
        (cmake_source / "fake-lit.py").write_text("""import json, sys
from pathlib import Path
build = Path(__file__).resolve().parents[1]
(build / "last-lit-args.json").write_text(json.dumps(sys.argv[1:]))
status = build / "fixture-test-exit"
print("fixture lit runner")
sys.exit(int(status.read_text()) if status.exists() else 0)
""")
        test_dir = cmake_source / "test"
        test_dir.mkdir()
        (test_dir / "case with spaces.ll").write_text("; fixture\n")

    def command(self, *args, expected=0):
        result = subprocess.run(
            ["just", "--justfile", str(self.root / "justfile"), *args],
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_configure_build_run_and_real_ccache_hit(self):
        self.command("configure")
        self.command("build", "hello")
        result = self.command("run", "hello", "one argument", "$(not-executed)")
        self.assertEqual(result.stdout, "one argument\n$(not-executed)\n")
        cfg = self.config()
        cache = dev.read_cache(cfg.build)
        self.assertEqual(cache["CMAKE_BUILD_TYPE"], "RelWithDebInfo")
        self.assertEqual(cache["LLVM_ENABLE_ASSERTIONS"], "ON")
        self.assertEqual(cache["CMAKE_CXX_COMPILER_LAUNCHER"], dev.executable("ccache"))

        def cache_hits():
            stats = dev.capture(["ccache", "--print-stats"])
            counters = dict(line.split() for line in stats.splitlines())
            return int(counters.get("direct_cache_hit", 0)) + int(
                counters.get("preprocessed_cache_hit", 0)
            )

        # Delete only this disposable fixture's object; repeat the exact compile.
        # Compare deltas so unrelated CMake compiler-probe hits cannot hide a bug.
        before = cache_hits()
        obj = cfg.build / "CMakeFiles/hello.dir/hello.cpp.o"
        self.assertTrue(obj.is_file())
        obj.unlink()
        self.command("build", "hello")
        self.assertGreater(cache_hits(), before)
        self.command("compdb")
        self.assertTrue((self.root / "compile_commands.json").is_symlink())
        self.command("configure")  # repeat configuration is safe

    def test_prerequisites_test_arguments_exit_status_and_logs(self):
        self.command("configure")
        self.command("test", "llvm/test/case with spaces.ll", "--filter", "case.*spaces")
        cfg = self.config()
        self.assertTrue((cfg.build / "test-dependencies-built").exists())
        argv = json.loads((cfg.build / "last-lit-args.json").read_text())
        self.assertIn("case.*spaces", argv)
        self.assertEqual(argv[-1], str(cfg.source / "llvm/test/case with spaces.ll"))
        (cfg.build / "fixture-test-exit").write_text("7")
        # just maps a recipe's nonzero exit to its own failure; the helper preserves 7.
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.root / "scripts/llvm_dev.py"),
                "test",
                "llvm/test/case with spaces.ll",
                "--no-build",
            ],
            text=True,
            capture_output=True,
            env=self.env,
        )
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        logs = sorted((self.root / ".local/logs").glob("*-test.log"))
        self.assertIn("exit_status: 7", logs[-1].read_text())
        self.assertIn("fixture lit runner", logs[-1].read_text())
        self.command("test", "llvm/test/missing.ll", expected=2)

    @unittest.skipUnless(shutil.which("clangd"), "clangd is provided by nix develop")
    def test_clangd_finds_the_nix_cpp_standard_headers(self):
        self.command("configure")
        result = subprocess.run(
            [
                "clangd",
                f"--check={self.source / 'llvm/hello.cpp'}",
                f"--compile-commands-dir={self.root / 'build/dev'}",
            ],
            text=True,
            capture_output=True,
            env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0 errors", result.stderr)

    def test_interrupt_stops_build_descendants_and_records_it(self):
        import psutil

        cmake_source = self.source / "llvm"
        with (cmake_source / "CMakeLists.txt").open("a") as file:
            file.write(
                'add_custom_target(slow COMMAND "${Python3_EXECUTABLE}" "${CMAKE_SOURCE_DIR}/slow.py" "${CMAKE_BINARY_DIR}")\n'
            )
        (cmake_source / "slow.py").write_text(
            "import os, sys, time\nfrom pathlib import Path\n"
            "(Path(sys.argv[1]) / 'slow.pid').write_text(str(os.getpid()))\ntime.sleep(90)\n"
        )
        self.command("configure")
        cfg = self.config()
        process = subprocess.Popen(
            [sys.executable, "-B", str(self.root / "scripts/llvm_dev.py"), "build", "slow"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self.env,
        )
        child = None
        try:
            deadline = time.monotonic() + 15
            while not (cfg.build / "slow.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((cfg.build / "slow.pid").exists(), "slow command did not start")
            child = psutil.Process(int((cfg.build / "slow.pid").read_text()))
            process.send_signal(signal.SIGINT)
            output, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, output)
            self.assertTrue(not child.is_running() or child.status() == psutil.STATUS_ZOMBIE)
            log = sorted((self.root / ".local/logs").glob("*-build.log"))[-1]
            self.assertIn("interrupted: true", log.read_text())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            if child is not None and child.is_running():
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass

    def test_refusal_to_repurpose_an_existing_build(self):
        self.command("configure")
        before = (self.root / "build/dev/CMakeCache.txt").read_bytes()
        self.command("configure", "--profile", "debug", "--build-dir", "build/dev", expected=2)
        self.assertEqual((self.root / "build/dev/CMakeCache.txt").read_bytes(), before)
        self.command("configure", "--profile", "debug")
        debug_cache = dev.read_cache(self.root / "build/debug")
        self.assertEqual(debug_cache["CMAKE_BUILD_TYPE"], "Debug")


if __name__ == "__main__":
    unittest.main()
