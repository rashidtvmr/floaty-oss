"""Tests for the pure capture-exclusion strategy module."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps" / "socket_stream_desktop" / "ui"
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
        self.assertFalse(plan.guaranteed)

    def test_wayland_plan_is_a_toplevel_and_not_guaranteed(self):
        plan = guard.build_plan(self._session(guard.SESSION_WAYLAND, "gnome"))
        self.assertEqual(plan.strategy, "wayland-toplevel")
        self.assertEqual(plan.layer, "toplevel")
        self.assertFalse(plan.override_redirect)
        self.assertFalse(plan.keep_above)
        self.assertFalse(plan.skip_taskbar)
        self.assertFalse(plan.guaranteed)
        self.assertTrue(any("whole output" in note for note in plan.notes))
        self.assertTrue(any("always-on-top" in note for note in plan.notes))

    def test_wayland_configured_rule_is_not_a_runtime_guarantee(self):
        plan = guard.build_plan(
            self._session(guard.SESSION_WAYLAND, "niri"), compositor_skip_capture=True,
        )
        self.assertFalse(plan.guaranteed)

    def test_unknown_session_has_no_strategy(self):
        plan = guard.build_plan(self._session(guard.SESSION_UNKNOWN, "unknown"))
        self.assertEqual(plan.strategy, "none")
        self.assertFalse(plan.guaranteed)

    def test_plan_is_json_serialisable(self):
        payload = guard.build_plan(self._session(guard.SESSION_X11)).as_dict()
        self.assertEqual(json.loads(json.dumps(payload))["strategy"], "x11-override-redirect")


class XwaylandDockPlan(unittest.TestCase):
    def _session(self):
        return guard.SessionInfo(
            guard.SESSION_WAYLAND, "ubuntu:gnome", "gnome", True, True,
        )

    def test_dock_layer_is_requested_on_gnome_wayland(self):
        plan = guard.build_plan(self._session(), xwayland_dock=True)
        self.assertEqual(plan.strategy, "xwayland-dock")
        self.assertEqual(plan.layer, "dock")
        self.assertEqual(plan.window_type, "_NET_WM_WINDOW_TYPE_DOCK")
        self.assertTrue(plan.keep_above)
        self.assertTrue(plan.skip_taskbar)
        self.assertFalse(plan.override_redirect)
        self.assertFalse(plan.guaranteed)
        states = plan.atoms["_NET_WM_STATE"]
        self.assertIn("_NET_WM_STATE_ABOVE", states)
        self.assertIn("_NET_WM_STATE_SKIP_TASKBAR", states)
        self.assertTrue(any("fullscreen" in note for note in plan.notes))
        self.assertTrue(any("capture" in note for note in plan.notes))

    def test_dock_plan_never_claims_capture_exclusion(self):
        report = guard.capability_report(self._session(), xwayland_dock=True)
        self.assertFalse(report["guaranteed"])
        self.assertIn("dock", report["mechanism"])
        self.assertIn("cannot exclude", report["recommended"])
        self.assertIn("single application window", report["recommended"])


class BackendSelection(unittest.TestCase):
    def test_gnome_wayland_prefers_xwayland(self):
        session = guard.detect_session({
            "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
        })
        self.assertTrue(guard.prefer_x11_backend(session))

    def test_kde_wayland_stays_native(self):
        session = guard.detect_session({
            "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "KDE",
        })
        self.assertFalse(guard.prefer_x11_backend(session))

    def test_x11_session_does_not_switch_backend(self):
        session = guard.detect_session({
            "XDG_SESSION_TYPE": "x11", "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "GNOME",
        })
        self.assertFalse(guard.prefer_x11_backend(session))
        self.assertEqual(session.session_type, guard.SESSION_X11)

    def test_explicit_backend_wins(self):
        import os

        session = guard.detect_session({
            "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
        })
        previous = os.environ.get("GDK_BACKEND")
        try:
            os.environ["GDK_BACKEND"] = "wayland"
            self.assertEqual(guard.ensure_panel_backend(session), "wayland")
            self.assertEqual(os.environ["GDK_BACKEND"], "wayland")
        finally:
            if previous is None:
                os.environ.pop("GDK_BACKEND", None)
            else:
                os.environ["GDK_BACKEND"] = previous

    def test_ensure_panel_backend_sets_x11_for_gnome(self):
        import os

        session = guard.detect_session({
            "XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
        })
        previous = os.environ.pop("GDK_BACKEND", None)
        try:
            self.assertEqual(guard.ensure_panel_backend(session), "x11")
            self.assertEqual(os.environ["GDK_BACKEND"], "x11")
        finally:
            os.environ.pop("GDK_BACKEND", None)
            if previous is not None:
                os.environ["GDK_BACKEND"] = previous

    def test_backend_and_display_helpers(self):
        self.assertTrue(guard.backend_is_x11("x11"))
        self.assertTrue(guard.backend_is_x11("x11,wayland"))
        self.assertFalse(guard.backend_is_x11("wayland"))
        self.assertFalse(guard.backend_is_x11(None))
        self.assertTrue(guard.display_is_x11(":0"))
        self.assertFalse(guard.display_is_x11("wayland-0"))
        self.assertFalse(guard.display_is_x11(""))


class CapabilityReport(unittest.TestCase):
    def test_report_is_honest(self):
        report = guard.capability_report(
            guard.SessionInfo(guard.SESSION_X11, "x11", "x11", True, False)
        )
        self.assertFalse(report["guaranteed"])
        self.assertTrue(report["mechanism"])
        self.assertGreaterEqual(len(report["limitations"]), 3)
        self.assertIn("does not auto-hide", report["recommended"])
        self.assertIn("WDA_EXCLUDEFROMCAPTURE", " ".join(report["limitations"]))

    def test_niri_report_recommends_the_compositor_rule(self):
        report = guard.capability_report(
            guard.SessionInfo(guard.SESSION_WAYLAND, "niri", "niri", False, True)
        )
        self.assertIn("block-out-from", report["recommended"])
        self.assertIn("screen-capture", report["recommended"])
        self.assertTrue(report["capture_rule"]["supported"])
        self.assertFalse(report["capture_rule"]["configured"])
        self.assertFalse(report["capture_rule"]["runtime_verified"])

    def test_gnome_report_reports_no_per_window_rule(self):
        report = guard.capability_report(
            guard.SessionInfo(guard.SESSION_WAYLAND, "gnome", "gnome", False, True)
        )
        self.assertFalse(report["guaranteed"])
        self.assertFalse(report["capture_rule"]["supported"])
        self.assertIn("single application window", report["recommended"])
        self.assertIn("GNOME", " ".join(report["limitations"]))

    def test_kde_report_names_the_hide_from_screencast_rule(self):
        report = guard.capability_report(
            guard.SessionInfo(guard.SESSION_WAYLAND, "kde", "kde", False, True)
        )
        self.assertIn("Hide from Screencast", report["recommended"])
        self.assertEqual(report["capture_rule"]["backend"], "kde")
        self.assertFalse(report["capture_rule"]["supported"])

    def test_kde_report_uses_evaluated_version_support(self):
        for supported in (False, True):
            report = guard.capability_report(
                guard.SessionInfo(guard.SESSION_WAYLAND, "kde", "kde", False, True),
                compositor_skip_capture=True, capture_rule_supported=supported)
            self.assertEqual(report["capture_rule"]["supported"], supported)
            self.assertFalse(report["capture_rule"]["runtime_verified"])

    def test_hyprland_report_names_no_screen_share(self):
        report = guard.capability_report(
            guard.SessionInfo(guard.SESSION_WAYLAND, "hyprland", "hyprland", False, True)
        )
        self.assertIn("no_screen_share", report["recommended"])
        self.assertEqual(report["capture_rule"]["backend"], "hyprland")

    def test_configured_rule_keeps_capture_unverified(self):
        report = guard.capability_report(
            guard.SessionInfo(guard.SESSION_WAYLAND, "niri", "niri", False, True),
            compositor_skip_capture=True,
        )
        self.assertFalse(report["guaranteed"])
        self.assertTrue(report["capture_rule"]["configured"])
        self.assertFalse(report["capture_rule"]["runtime_verified"])


class ShouldHide(unittest.TestCase):
    def test_truth_table(self):
        plan = guard.build_plan(guard.SessionInfo(guard.SESSION_X11, "x11", "x11", True, False))
        self.assertFalse(guard.should_hide(plan, False, False))
        self.assertFalse(guard.should_hide(plan, True, False))
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

        previous = os.environ.get("SOCKET_STREAM_SCREEN_SHARE")
        try:
            os.environ["SOCKET_STREAM_SCREEN_SHARE"] = "1"
            self.assertTrue(guard.default_probe())
            os.environ["SOCKET_STREAM_SCREEN_SHARE"] = "0"
            self.assertFalse(guard.default_probe())
        finally:
            if previous is None:
                os.environ.pop("SOCKET_STREAM_SCREEN_SHARE", None)
            else:
                os.environ["SOCKET_STREAM_SCREEN_SHARE"] = previous


if __name__ == "__main__":
    unittest.main()
