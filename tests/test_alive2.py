"""Fail-closed proof handling and source/build isolation, without compiling LLVM."""

import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_workspace import WorkspaceFixture

from scripts import alive2


def summary(correct=1, incorrect=0, failed=0, errors=0):
    return (
        f"Summary:\n  {correct} correct transformations\n"
        f"  {incorrect} incorrect transformations\n"
        f"  {failed} failed-to-prove transformations\n  {errors} Alive2 errors\n"
    )


class ProofSummaryTests(unittest.TestCase):
    def test_accepts_only_complete_positive_results(self):
        alive2.check_summary(summary(), 1)
        alive2.check_summary(summary(correct=2), 2)
        for output, expected in (
            (summary(correct=0, incorrect=1), 1),
            (summary(correct=0, failed=1), 1),
            (summary(correct=0, errors=1), 1),
            (summary(correct=0), 0),
            (summary(), 2),  # a silently skipped function is not a proof
            ("Transformation seems to be correct!", 1),
            (summary() + summary(), 1),
        ):
            with self.subTest(output=output):
                with self.assertRaises(alive2.ProofFailure):
                    alive2.check_summary(output, expected)

    def test_canonical_function_names(self):
        ir = """; declarations and globals are not function definitions
declare void @external()
@global = global i32 1
define i32 @plain(i32 %x) { ret i32 %x }
define void @"name with space"() { ret void }
define void @"escaped\\22name"() { ret void }
define i32 @0() { ret i32 0 }
"""
        self.assertEqual(
            alive2.function_names(ir), {"plain", '"name with space"', '"escaped\\22name"', "0"}
        )
        self.assertEqual(alive2.function_names("declare void @external()\n"), set())
        with self.assertRaises(alive2.ProofFailure):
            alive2.function_names("define void @f() {}\ndefine void @f() {}\n")


class IsolationTests(WorkspaceFixture):
    def test_validation_paths_do_not_repurpose_the_dev_build(self):
        cfg = self.config()
        with patch.dict(
            os.environ,
            {
                "LLVM_DEV_ALIVE2_REVISION": "a" * 40,
                "LLVM_DEV_ALIVE2_SOURCE": str(self.source / "llvm"),
            },
        ):
            validation, _, build, _ = alive2.locations(cfg)
            self.assertNotEqual(validation.build, cfg.build)
            self.assertEqual(validation.profile, "validate")
            self.assertIsNone(validation.projects)
            self.assertIsNone(validation.targets)
            self.assertEqual(build.name, "dev-alive2-aaaaaaa")
            other = replace(cfg, build=self.root / "build/topic")
            self.assertNotEqual(alive2.locations(other)[0].build, validation.build)

    def test_missing_pin_fails_before_building(self):
        with patch.dict(os.environ, {"LLVM_DEV_ALIVE2_REVISION": ""}):
            with self.assertRaisesRegex(alive2.dev.DevError, "pinned"):
                alive2.locations(self.config())


class SourceStateTests(unittest.TestCase):
    def test_head_staged_unstaged_and_untracked_changes_are_distinguished(self):
        with tempfile.TemporaryDirectory(prefix="alive2 source ") as directory:
            source = Path(directory)

            def git(*args):
                subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "commit.gpgsign", "false")
            path = source / "source.cpp"
            path.write_text("before\n")
            git("add", "source.cpp")
            git("commit", "-qm", "fixture")
            clean = alive2.source_state(source)
            path.write_text("after\n")
            dirty = alive2.source_state(source)
            self.assertNotEqual(clean, dirty)
            git("add", "source.cpp")
            self.assertEqual(dirty, alive2.source_state(source))
            new = source / "new.h"
            new.write_text("one\n")
            first = alive2.source_state(source)
            new.write_text("two\n")
            self.assertNotEqual(first, alive2.source_state(source))
            git("add", ".")
            git("commit", "-qm", "changed")
            self.assertNotEqual(clean["revision"], alive2.source_state(source)["revision"])


if __name__ == "__main__":
    unittest.main()
