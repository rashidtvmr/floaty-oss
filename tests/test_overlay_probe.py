"""Headless checks for the human-run overlay QA probe.

The probe must never hide itself on a screen share and must report a
compositor rule as configured separately from runtime-verified.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "apps" / "socket_stream_desktop" / "ui"
if str(UI) not in sys.path:
    sys.path.insert(0, str(UI))

import capture_rules as rules  # noqa: E402
import overlay_guard as guard  # noqa: E402

PROBE_PATH = ROOT / "security-lab" / "overlay_probe.py"
SPEC = importlib.util.spec_from_file_location("overlay_probe", PROBE_PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ProbeNeverHides(unittest.TestCase):
    def test_source_has_no_auto_hide_path(self):
        source = PROBE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("self.hide()", source)
        self.assertNotIn("auto_hidden", source)
        self.assertNotIn("hiding", source)


class ProbeReport(unittest.TestCase):
    def test_configured_rule_is_reported_and_runtime_stays_unverified(self):
        session = guard.SessionInfo(
            guard.SESSION_WAYLAND, "niri", "niri", False, True,
        )
        status = rules.RuleStatus(
            backend="niri",
            supported=True,
            configured=True,
            verified=False,
            config_validated=True,
            detail='niri window rule is installed (block-out-from "screen-capture")',
            guidance="The panel stays visible locally.",
            path="/home/user/.config/niri/config.kdl",
            can_install=False,
            can_remove=True,
        )
        plan = guard.build_plan(session, compositor_skip_capture=True)
        capability = guard.capability_report(
            session, compositor_skip_capture=True,
        )
        report = probe._format_report(
            guard, session, plan, capability, ["keep-above"], status,
        )
        self.assertIn("COMPOSITOR CAPTURE RULE", report)
        self.assertIn("configured           : yes", report)
        self.assertIn("config validated     : yes", report)
        self.assertIn("runtime verified      : no", report)
        self.assertIn("/home/user/.config/niri/config.kdl", report)

    def test_runtime_field_does_not_imply_config_validation(self):
        session = guard.SessionInfo(guard.SESSION_WAYLAND, "kde", "kde", False, True)
        status = rules.RuleStatus(backend="kde", supported=True, configured=True,
                                  verified=True, config_validated=False, detail="fixture")
        report = probe._format_report(guard, session, guard.build_plan(session),
                                      guard.capability_report(session), [], status)
        self.assertIn("config validated     : no", report)

    def test_unavailable_rule_is_reported_honestly(self):
        session = guard.SessionInfo(
            guard.SESSION_WAYLAND, "gnome", "gnome", False, True,
        )
        status = rules.RuleStatus(
            backend="gnome", supported=False, configured=False, verified=False,
            detail="GNOME Shell has no per-window screen-capture exclusion API",
            guidance="Share a single application window instead.",
        )
        plan = guard.build_plan(session)
        capability = guard.capability_report(session)
        report = probe._format_report(
            guard, session, plan, capability, [], status,
        )
        self.assertIn("supported            : no", report)
        self.assertIn("runtime verified      : no", report)
        self.assertIn("single application window", report)


if __name__ == "__main__":
    unittest.main()
