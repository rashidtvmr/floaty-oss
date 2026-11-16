import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/socket_stream_desktop/ui"))
from source_preflight import check_sources, _probe


class SourcePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        worker = self.root / "target/native-helpers/moonshine/socket_stream_moonshine/socket_stream_moonshine"
        worker.parent.mkdir(parents=True)
        worker.write_text("fixture")
        worker.chmod(0o700)
        self.calls = []

    def probe(self, args, **kwargs):
        self.calls.append(args)
        self.assertLessEqual(kwargs["timeout"], 3)
        if args[:3] == ["pactl", "list", "short"]:
            output = "1 mic0 fixture\n2 sink0.monitor fixture\n"
        elif args[-1] == "get-default-sink":
            output = "sink0\n"
        elif args[-1] == "get-default-source":
            output = "mic0\n"
        else:
            output = ""
        return SimpleNamespace(returncode=0, stdout=output)

    def test_selected_audio_checks_devices_without_capture(self):
        errors, notes = check_sources(True, False, "system", "default", self.root,
                                     run=self.probe, which=lambda _: "/fixture")
        self.assertEqual(errors, [])
        self.assertTrue(any("not a permission grant" in note for note in notes))
        self.assertTrue(all(args[0] == "pactl" for args in self.calls))

    def test_unselected_audio_dependencies_do_not_block_screen(self):
        errors, _ = check_sources(False, True, "microphone", "default", self.root,
                                  run=self.probe, which=lambda _: None)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("getDesktop", " ".join(self.calls[0]))

    def test_missing_device_and_timeout_are_actionable_without_recording(self):
        errors, _ = check_sources(True, False, "microphone", "missing", self.root,
                                  run=self.probe, which=lambda _: "/fixture")
        self.assertIn("Audio options", " ".join(errors))

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("fixture", 3)

        errors, _ = check_sources(True, False, "microphone", "default", self.root,
                                  run=timeout, which=lambda _: "/fixture")
        self.assertIn("timed out", " ".join(errors))

    def test_no_sources_does_not_probe(self):
        errors, _ = check_sources(False, False, "microphone", "default", self.root,
                                  run=self.probe, which=lambda _: None)
        self.assertTrue(errors)
        self.assertEqual(self.calls, [])

    def test_real_probe_bounds_output_and_time_without_sensor_access(self):
        with self.assertRaises(OSError):
            _probe([sys.executable, "-c", "print('x' * 100000)"], timeout=2)
        with self.assertRaises(subprocess.TimeoutExpired):
            _probe([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)
