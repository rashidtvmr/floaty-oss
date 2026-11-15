"""macOS live-output handoff contract: readiness reads and self-test wiring."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "apps/socket_stream_desktop/ui/mac-app.swift"
GUI = ROOT / "target/native-helpers/socket_stream_gui"


@unittest.skipUnless(SOURCE.is_file(), "mac-app.swift is not part of this package")
class LiveHandoffSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = SOURCE.read_text(encoding="utf-8")

    def test_live_handler_uses_a_readiness_read(self):
        # Foundation's buffered FileHandle read waits for the full buffer on
        # macOS, which delays small live events; the handler must use a POSIX
        # readiness read instead.
        self.assertNotIn(".read(upToCount", self.source)
        self.assertIn("readAvailablePipeBytes(outputFD)", self.source)

    def test_self_test_regresses_short_event_delivery(self):
        self.assertIn("enum PipeReadSelfTest", self.source)
        self.assertIn("short live event delivered while the writer stays open",
                      self.source)
        self.assertIn("PipeReadSelfTest.run()", self.source)


@unittest.skipUnless(GUI.is_file(), "the macOS GUI helper is not built")
class BuiltGuiSelfTest(unittest.TestCase):
    def test_self_test_passes(self):
        result = subprocess.run([str(GUI), "--self-test"],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("macOS panel self-test: ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
