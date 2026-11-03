"""Tests for the pure capture-exclusion strategy module."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps" / "context-desktop" / "ui"
if str(UI) not in sys.path:
    sys.path.insert(0, str(UI))

import overlay_guard as guard  # noqa: E402


class SessionDetection(unittest.TestCase):
    def test_wayland_gnome(self):
        session = guard.detect_session({
            "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
        })
        self.assertEqual(session.session_type, guard.SESSION_WAYLAND)
        self.assertEqual(session.compositor, "gnome")
        self.assertTrue(session.has_wayland_display)

    def test_x11_kde(self):
        session = guard.detect_session({
            "XDG_SESSION_TYPE": "x11", "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "KDE",
        })
        self.assertEqual(session.session_type, guard.SESSION_X11)
        self.assertEqual(session.compositor, "kde")
        self.assertTrue(session.has_x11_display)

    def test_niri_is_recognised(self):
        session = guard.detect_session({
            "WAYLAND_DISPLAY": "wayland-1", "XDG_CURRENT_DESKTOP": "niri",
        })
        self.assertEqual(session.compositor, "niri")

    def test_unknown_session_does_not_raise(self):
        session = guard.detect_session({})
        self.assertEqual(session.session_type, guard.SESSION_UNKNOWN)
        self.assertEqual(session.compositor, "unknown")


class PlanSelection(unittest.TestCase):
    def _session(self, session_type, compositor="x11"):
        return guard.SessionInfo(session_type, compositor, compositor, True, False)

    def test_x11_plan_is_best_effort_only(self):
        plan = guard.build_plan(self._session(guard.SESSION_X11))
        self.assertEqual(plan.strategy, "x11-override-redirect")
        self.assertTrue(plan.override_redirect)
        self.assertTrue(plan.keep_above)
        self.assertTrue(plan.skip_taskbar)
        self.assertEqual(plan.window_type, "_NET_WM_WINDOW_TYPE_NOTIFICATION")
        states = plan.atoms["_NET_WM_STATE"]
        self.assertIn("_NET_WM_STATE_SKIP_TASKBAR", states)
        self.assertNotIn("_NET_WM_STATE_SKIP_CAPTURE", states)
        self.assertFalse(plan.guaranteed)

    def test_x11_skip_capture_is_opt_in(self):
        plan = guard.build_plan(self._session(guard.SESSION_X11), compositor_skip_capture=True)
        self.assertIn("_NET_WM_STATE_SKIP_CAPTURE", plan.atoms["_NET_WM_STATE"])
        self.assertTrue(plan.guaranteed)

    def test_wayland_plan_uses_layer_shell_and_is_not_guaranteed(self):
        plan = guard.build_plan(self._session(guard.SESSION_WAYLAND, "gnome"))
        self.assertEqual(plan.strategy, "wayland-layer-shell")
        self.assertEqual(plan.layer, "overlay")
        self.assertFalse(plan.override_redirect)
        self.assertFalse(plan.guaranteed)
        self.assertTrue(any("whole output" in note for note in plan.notes))

    def test_wayland_guaranteed_only_with_compositor_rule(self):
        plan = guard.build_plan(
            self._session(guard.SESSION_WAYLAND, "niri"), compositor_skip_capture=True,
        )
        self.assertTrue(plan.guaranteed)

    def test_unknown_session_has_no_strategy(self):
        plan = guard.build_plan(self._session(guard.SESSION_UNKNOWN, "unknown"))
        self.assertEqual(plan.strategy, "none")
        self.assertFalse(plan.guaranteed)

    def test_plan_is_json_serialisable(self):
        payload = guard.build_plan(self._session(guard.SESSION_X11)).as_dict()
        self.assertEqual(json.loads(json.dumps(payload))["strategy"], "x11-override-redirect")


class CapabilityReport(unittest.TestCase):
    def test_report_is_honest(self):
        report = guard.capability_report(
            guard.SessionInfo(guard.SESSION_X11, "x11", "x11", True, False)
        )
        self.assertFalse(report["guaranteed"])
        self.assertTrue(report["mechanism"])
        self.assertGreaterEqual(len(report["limitations"]), 3)
        self.assertIn("auto-hide", report["recommended"])
        self.assertIn("WDA_EXCLUDEFROMCAPTURE", " ".join(report["limitations"]))

    def test_niri_report_recommends_the_compositor_rule(self):
        report = guard.capability_report(
            guard.SessionInfo(guard.SESSION_WAYLAND, "niri", "niri", False, True)
        )
        self.assertIn("block-out-from=screencast", report["recommended"])


class ShouldHide(unittest.TestCase):
    def test_truth_table(self):
        plan = guard.build_plan(guard.SessionInfo(guard.SESSION_X11, "x11", "x11", True, False))
        self.assertFalse(guard.should_hide(plan, False, False))
        self.assertTrue(guard.should_hide(plan, True, False))
        self.assertTrue(guard.should_hide(plan, False, True))
        self.assertTrue(guard.should_hide(plan, True, True))
        self.assertFalse(guard.should_hide(None, False, False))


class Watcher(unittest.TestCase):
    def test_detects_an_active_share(self):
        watcher = guard.ScreenShareWatcher(probe=lambda: True)
        self.assertTrue(watcher.poll())
        self.assertEqual(watcher.status(), {
            "active": True, "checked": True, "detail": "screencast detected",
        })

    def test_reports_clean_screen(self):
        watcher = guard.ScreenShareWatcher(probe=lambda: False)
        self.assertFalse(watcher.poll())
        self.assertTrue(watcher.status()["checked"])

    def test_probe_failure_is_not_a_crash(self):
        def broken():
            raise guard.ProbeUnavailable("no pw-dump")

        watcher = guard.ScreenShareWatcher(probe=broken)
        self.assertFalse(watcher.poll())
        status = watcher.status()
        self.assertFalse(status["checked"])
        self.assertIn("probe unavailable", status["detail"])

    def test_env_override_forces_the_probe(self):
        import os

        previous = os.environ.get("CONTEXT_LENS_SCREEN_SHARE")
        try:
            os.environ["CONTEXT_LENS_SCREEN_SHARE"] = "1"
            self.assertTrue(guard.default_probe())
            os.environ["CONTEXT_LENS_SCREEN_SHARE"] = "0"
            self.assertFalse(guard.default_probe())
        finally:
            if previous is None:
                os.environ.pop("CONTEXT_LENS_SCREEN_SHARE", None)
            else:
                os.environ["CONTEXT_LENS_SCREEN_SHARE"] = previous


if __name__ == "__main__":
    unittest.main()
