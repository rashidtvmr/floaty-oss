"""Offline Linux ScreenCast helper tests; no desktop capture is performed."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("portal_capture", ROOT / "scripts/linux-portal-capture.py")
portal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portal)


class PortalTests(unittest.TestCase):
    def test_request_paths(self):
        self.assertEqual(
            portal.request_path(":1.42", "cl123"),
            "/org/freedesktop/portal/desktop/request/1_42/cl123")
        with self.assertRaises(ValueError):
            portal.request_path(":1.42", "../evil")

    def test_interval_limits(self):
        self.assertEqual(portal.interval_to_fraction(5), "1/5")
        for value in (0, 1, 3601):
            with self.assertRaises(ValueError):
                portal.interval_to_fraction(value)

    def test_atomic_image_overwrites_without_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "current.png"
            portal.atomic_write(path, b"first")
            portal.atomic_write(path, b"second")
            self.assertEqual(path.read_bytes(), b"second")
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_requires_consent(self):
        with self.assertRaises(SystemExit) as result:
            portal.main(["--output", "/tmp/unused.png"])
        self.assertEqual(result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
