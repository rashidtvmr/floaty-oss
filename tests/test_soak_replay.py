"""Tests for the bounded replay soak harness path resolution."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("soak_replay", ROOT / "scripts/soak_replay.py")
SOAK_REPLAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SOAK_REPLAY)


class CliPathTests(unittest.TestCase):
    def test_finds_packaged_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bin/socket_stream"
            binary.parent.mkdir()
            binary.touch()
            self.assertEqual(SOAK_REPLAY.cli_path(root), binary)

    def test_finds_development_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "target/debug/socket_stream"
            binary.parent.mkdir(parents=True)
            binary.touch()
            self.assertEqual(SOAK_REPLAY.cli_path(root), binary)


if __name__ == "__main__":
    unittest.main()
