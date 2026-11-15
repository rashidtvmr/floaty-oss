"""Patcher transactions against real upstream 50.1 sources (no GTK required).

Set SOCKET_STREAM_MUTTER_SOURCE to a Git checkout of upstream tag 50.1. Fixtures
are read from HEAD, so a locally patched working tree is safe to use too.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "native/mutter-capture-filter/patch_mutter.py"
spec = importlib.util.spec_from_file_location("mutter_patch", SCRIPT)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)
SOURCE = os.environ.get("SOCKET_STREAM_MUTTER_SOURCE")
FILES = (
    "meson.build", "clutter/clutter/clutter-paint-context.h",
    "clutter/clutter/clutter-actor.h", "clutter/clutter/clutter-actor.c",
    "clutter/clutter/clutter-offscreen-effect.c", "src/compositor/compositor.c",
    "clutter/clutter/clutter-texture-content.c", "clutter/clutter/clutter-texture-content.h",
    "clutter/clutter/clutter-stage.c", "src/compositor/meta-window-group.c",
    "src/compositor/meta-window-actor.c",
    "src/tests/clutter/conform/meson.build",
    "src/backends/meta-screen-cast-monitor-stream-src.c",
    "src/backends/meta-screen-cast-area-stream-src.c",
    "src/backends/meta-screen-cast-virtual-stream-src.c",
)


class VersionGuard(unittest.TestCase):
    def test_wrong_version_does_not_modify_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            version = root / "meson.build"
            version.write_text("project('mutter', 'c', version: '51.0')\n")
            before = version.read_bytes()
            self.assertEqual(patcher.main([str(root), "--apply"]), 1)
            self.assertEqual(version.read_bytes(), before)
            self.assertEqual(list(root.iterdir()), [version])


@unittest.skipUnless(SOURCE, "set SOCKET_STREAM_MUTTER_SOURCE for upstream source integration")
class UpstreamPatch(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in FILES:
            data = subprocess.check_output(["git", "show", f"HEAD:{name}"], cwd=SOURCE)
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_default_is_read_only_and_apply_is_idempotent(self):
        before = self.snapshot()
        self.assertEqual(patcher.main([str(self.root)]), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(patcher.main([str(self.root), "--apply"]), 0)
        applied = self.snapshot()
        self.assertNotEqual(applied, before)
        self.assertEqual(patcher.prepare(self.root), "")
        self.assertEqual(patcher.main([str(self.root), "--apply"]), 0)
        self.assertEqual(self.snapshot(), applied)

    def test_late_incompatible_anchor_leaves_every_file_untouched(self):
        path = self.root / FILES[-1]
        path.write_text(path.read_text().replace(
            "  view_framebuffer = clutter_stage_view_get_framebuffer (view);",
            "  view_framebuffer = NULL; /* deliberately incompatible fixture */"))
        before = self.snapshot()
        self.assertEqual(patcher.main([str(self.root), "--apply"]), 1)
        self.assertEqual(self.snapshot(), before)

    def test_generated_diff_reverses_exactly_and_preserves_other_files(self):
        other = self.root / "local-notes.txt"
        other.write_text("User changes\n")
        before = self.snapshot()
        diff = patcher.prepare(self.root)
        subprocess.run(["git", "apply", "-"], input=diff, text=True,
                       cwd=self.root, check=True)
        subprocess.run(["git", "apply", "--reverse", "-"], input=diff, text=True,
                       cwd=self.root, check=True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(other.read_text(), "User changes\n")

    def test_existing_native_test_is_not_overwritten(self):
        native = self.root / "src/tests/clutter/conform/socket_stream_capture.c"
        native.write_text("/* local test edits */\n")
        before = self.snapshot()
        self.assertEqual(patcher.main([str(self.root), "--apply"]), 1)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
