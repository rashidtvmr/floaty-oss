"""P0 end-to-end acceptance: intentionally RED until LIVE-EXPERIENCE-004.

Run independently of the existing green baseline:
  python3 -m unittest discover -s tests/acceptance -v
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "apps/context-desktop/ui/live_state.py"


class LiveWorkflowContract(unittest.TestCase):
    def test_live_state_exists_and_has_usable_interface(self):
        self.assertTrue(CONTROLLER.is_file(),
            "Implement GTK-independent live_state.py as specified in docs/AGENT-HANDOFF-LIVE-UX.md")
        spec = importlib.util.spec_from_file_location("live_state", CONTROLLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        state = module.LiveState()
        self.assertEqual(state.snapshot()["status"], "idle")
        self.assertTrue(callable(state.apply))


if __name__ == "__main__":
    unittest.main()
