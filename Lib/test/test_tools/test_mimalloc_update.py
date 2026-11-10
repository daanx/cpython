"""Tests for the mimalloc-update vendoring script."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test.test_tools import basepath


SCRIPT = Path(basepath) / "mimalloc-update"


@unittest.skipUnless(os.name == "posix" and shutil.which("rsync"),
                     "requires a POSIX shell and rsync")
@unittest.skipUnless(SCRIPT.is_file(), "requires the mimalloc-update script")
class MimallocUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "cpython checkout"
        self.source = self.directory / "mimalloc checkout"
        self.root.mkdir()
        shutil.copy2(SCRIPT, self.root / "mimalloc-update")
        self.sources = self.root / "Objects/mimalloc"
        self.includes = self.root / "Include/internal/mimalloc"
        self.write(self.root / "Include/Python.h", "CPython header\n")
        self.write(self.root / "Include/internal/pycore_mimalloc.h", "hooks\n")
        self.write(self.root / "Objects/obmalloc.c", "integration\n")
        self.write(self.root / "Makefile.pre.in", "build rules\n")
        self.write(self.sources / "static.c", "old source\n")
        self.write(self.sources / "obsolete.c", "obsolete source\n")
        self.write(self.sources / "LICENSE", "old license\n")
        self.write(self.includes / "mimalloc.h", "old header\n")
        self.write(self.includes / "old/header.h", "obsolete header\n")
        self.write(self.source / "src/static.c", "new source\n")
        self.write(self.source / "src/alloc/new.c", "new allocation code\n")
        self.write(self.source / "include/mimalloc.h", "new header\n")
        self.write(self.source / "include/mimalloc/types.h", "new types\n")
        self.write(self.source / "LICENSE", "new license\n")
        self.write(self.source / "CMakeLists.txt", "not vendored\n")
        self.write(self.source / "test/test.c", "not vendored\n")

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def snapshot(self, path):
        return {
            file.relative_to(path): (file.read_bytes(), file.stat().st_mtime_ns)
            for file in path.rglob("*") if file.is_file()
        }

    def run_update(self, *args, env=None, success=True):
        result = subprocess.run(
            ["sh", str(self.root / "mimalloc-update"), *map(str, args)],
            cwd=self.directory, env=env, capture_output=True, text=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_local_mirror_and_repeat(self):
        before = self.snapshot(self.root)
        self.run_update("--local", self.source)
        expected_sources = {
            file.relative_to(self.source / "src"): file.read_bytes()
            for file in (self.source / "src").rglob("*") if file.is_file()
        }
        expected_sources[Path("LICENSE")] = (self.source / "LICENSE").read_bytes()
        self.assertEqual(
            {path: data for path, (data, _) in self.snapshot(self.sources).items()},
            expected_sources,
        )
        self.assertEqual(
            {path: data for path, (data, _) in self.snapshot(self.includes).items()},
            {path: data for path, (data, _) in
             self.snapshot(self.source / "include").items()},
        )
        after = self.snapshot(self.root)
        vendor_paths = ("Objects/mimalloc/", "Include/internal/mimalloc/")
        self.assertEqual(
            {p: value for p, value in after.items()
             if not p.as_posix().startswith(vendor_paths)},
            {p: value for p, value in before.items()
             if not p.as_posix().startswith(vendor_paths)},
        )
        # A fresh checkout's timestamps alone must not trigger rebuilds.
        for path in self.source.rglob("*"):
            if path.is_file():
                os.utime(path, (1000000000, 1000000000))
        self.run_update("--local", self.source)
        self.assertEqual(self.snapshot(self.root), after)
        preview = self.run_update("--dry-run", "--local", self.source)
        self.assertNotIn("static.c", preview.stdout)

    def test_dry_run(self):
        before = self.snapshot(self.root)
        result = self.run_update("--dry-run", "--local", self.source)
        self.assertIn("obsolete.c", result.stdout)
        self.assertIn("new.c", result.stdout)
        self.assertEqual(self.snapshot(self.root), before)

    def test_invalid_source_and_arguments_leave_destination_untouched(self):
        before = self.snapshot(self.root)
        for args in [
            ("--local",),
            ("--unknown",),
            ("dev3", "--local", self.source),
            ("--local", self.source, "dev3"),
            ("dev3", "another-branch"),
            ("--local", self.directory / "missing"),
        ]:
            with self.subTest(args=args):
                self.run_update(*args, success=False)
                self.assertEqual(self.snapshot(self.root), before)
        (self.source / "LICENSE").unlink()
        self.run_update("--local", self.source, success=False)
        self.assertEqual(self.snapshot(self.root), before)

    def test_symlink_destination_is_rejected(self):
        outside = self.directory / "outside"
        self.includes.rename(outside)
        self.includes.symlink_to(outside, target_is_directory=True)
        before = self.snapshot(self.root)
        outside_before = self.snapshot(outside)
        self.run_update("--local", self.source, success=False)
        self.assertEqual(self.snapshot(self.root), before)
        self.assertEqual(self.snapshot(outside), outside_before)

    def test_fetch_branch_and_failure(self):
        bin_dir = self.directory / "bin"
        git = bin_dir / "git"
        self.write(git, """#!/bin/sh
set -eu
if [ "$1" = clone ]; then
    printf '%s\\n' "$@" > "$GIT_ARGS"
    [ "${FAIL_FETCH:-0}" = 0 ] || exit 1
    while [ "$#" -gt 1 ]; do shift; done
    cp -R "$MIMALLOC_SOURCE" "$1"
else
    echo 0123456789012345678901234567890123456789
fi
""")
        git.chmod(0o755)
        args_file = self.directory / "git-args"
        env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                   GIT_ARGS=str(args_file), MIMALLOC_SOURCE=str(self.source))
        for args, branch in [((), "dev3"), (("other-branch",), "other-branch")]:
            with self.subTest(branch=branch):
                self.run_update(*args, env=env)
                git_args = args_file.read_text(encoding="utf-8").splitlines()
                self.assertEqual(git_args[git_args.index("--branch") + 1], branch)
                self.assertEqual((self.sources / "static.c").read_bytes(),
                                 (self.source / "src/static.c").read_bytes())
        before = self.snapshot(self.root)
        self.run_update(env=dict(env, FAIL_FETCH="1"), success=False)
        self.assertEqual(self.snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
