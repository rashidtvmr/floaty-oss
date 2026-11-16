import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/socket_stream_desktop/ui"))
from audio_devices import list_audio_devices


class AudioDevicesTests(unittest.TestCase):
    def test_playback_choices_only_include_monitors_and_do_not_open_audio(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps([
                {"name": "headphones.monitor", "description": "Headphones", "monitor_of_sink": 7},
                {"name": "microphone", "description": "Your voice", "monitor_of_sink": "n/a"}]))
        self.assertEqual([v["id"] for v in list_audio_devices("system", run=run)], ["default", "headphones.monitor"])
        self.assertEqual([v["id"] for v in list_audio_devices("microphone", run=run)], ["default", "microphone"])
        self.assertTrue(all(call == ["pactl", "-f", "json", "list", "sources"] for call in calls))

    def test_invalid_inventory_is_rejected(self):
        for stdout in ("{}", "not json"):
            with self.assertRaises(ValueError):
                list_audio_devices("system", run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout))
