#!/usr/bin/env python3
"""Human-run GTK3 probe for the SocketStream stealth overlay capture boundary.

This is a security-QA harness, not a product feature. It opens one clearly
labelled marker window and applies the *same* ExclusionPlan that the real
floating overlay would apply, so a human can compare what they see on their
desktop with what an explicitly approved browser screen share captures.

Honest scope (see docs/STEALTH-OVERLAY.md sections 2 and 3.1):

* The window is a normal, identifiable process. It does not disguise itself,
  masquerade as a system component, or bypass any OS permission prompt.
* On Linux there is no public API equivalent to Windows
  ``SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`` or macOS
  ``NSWindow.sharingType = .none``. Window hints are best effort only.
* The marker never hides. Focus changes and detected screen shares do not
  change its visibility; only the tester closing the probe does.
* niri, Hyprland and KDE Plasma/KWin 6.7+ document a per-window compositor rule
  (``capture_rules.py`` writes it). GNOME Shell/mutter, X11 and sway do not.
  A configured rule is reported separately from runtime verification, which
  still requires a real share.

No AI, no capture, no network, no secrets. Nothing is read from the screen and
nothing is uploaded. The marker text is fixed placeholder text.

Usage:
    python3 overlay_probe.py                      # apply hints, never hide
    python3 overlay_probe.py --no-stealth         # A/B control: no hints
    python3 overlay_probe.py --exclusion-status   # print rule status and exit
    python3 overlay_probe.py --setup-exclusion    # install the compositor rule
    python3 overlay_probe.py --remove-exclusion   # remove the compositor rule
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The frozen, pure-stdlib overlay_guard module lives in the desktop UI package.
UI_DIR = Path(__file__).resolve().parent.parent / "apps" / "socket_stream_desktop" / "ui"
if str(UI_DIR) not in sys.path:
    sys.path.insert(0, str(UI_DIR))

WINDOW_TITLE = "SocketStream Stealth Overlay Probe — NOT a real answer panel"

MARKER_BODY = (
    "SECURITY QA MARKER — this is NOT a real answer panel.\n"
    "It contains no AI output, no captured content and no private data.\n"
    "Compare this window with the browser capture preview."
)


def _load_overlay_guard():
    """Import the frozen overlay_guard interface, or explain why we cannot."""
    try:
        import overlay_guard  # noqa: WPS433 (deliberate late import)
    except ImportError as exc:
        return None, (
            f"overlay_guard is unavailable ({exc}).\n"
            f"Expected it at {UI_DIR / 'overlay_guard.py'}.\n"
            "This probe needs the frozen overlay_guard module described in "
            "docs/STEALTH-OVERLAY.md section 3.1."
        )
    return overlay_guard, None


def _load_capture_rules():
    """Import the compositor-rule module, or report that it is missing."""
    try:
        import capture_rules  # noqa: WPS433 (deliberate late import)
    except ImportError as exc:
        return None, f"capture_rules is unavailable ({exc})."
    return capture_rules, None


def _load_gtk():
    """Import GTK3, or explain why we cannot."""
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, GLib, Gtk
    except (ImportError, ValueError) as exc:
        return None, (
            f"GTK3 is unavailable ({exc}).\n"
            "Install python3-gi and gir1.2-gtk-3.0 (Debian/Ubuntu) or "
            "python3-gobject and gtk3 (Fedora/Arch)."
        )
    return (Gtk, Gdk, GLib), None


def _apply_plan(window, Gdk, plan, applied):
    """Apply an ExclusionPlan to a GTK window. Best effort, never fatal."""
    if getattr(plan, "keep_above", False):
        window.set_keep_above(True)
        applied.append("keep-above")

    if getattr(plan, "skip_taskbar", False):
        window.set_skip_taskbar_hint(True)
        window.set_skip_pager_hint(True)
        applied.append("skip-taskbar + skip-pager")

    window_type = getattr(plan, "window_type", "") or ""
    if window_type:
        # "_NET_WM_WINDOW_TYPE_NOTIFICATION" -> Gdk.WindowTypeHint.NOTIFICATION
        hint_name = window_type.rsplit("_", 1)[-1]
        hint = getattr(Gdk.WindowTypeHint, hint_name, None)
        if hint is not None:
            window.set_type_hint(hint)
            applied.append(f"type-hint={hint_name.lower()}")
        else:
            applied.append(f"type-hint {hint_name} (unsupported by GTK)")

    if getattr(plan, "override_redirect", False):
        # Must be set before the window is realized. Makes the window
        # unmanaged; the WM will not add decorations or focus it. GTK3 does
        # not always expose set_override_redirect, so fall back to POPUP.
        applied_flag = False
        setter = getattr(window, "set_override_redirect", None)
        if callable(setter):
            try:
                setter(True)
                applied_flag = True
            except Exception:  # noqa: BLE001 - best effort, report only
                applied_flag = False
        if not applied_flag:
            try:
                window.set_type_hint(Gdk.WindowTypeHint.POPUP)
                applied_flag = True
            except Exception:  # noqa: BLE001 - best effort, report only
                pass
        try:
            window.set_decorated(False)
        except Exception:  # noqa: BLE001 - best effort, report only
            pass
        applied.append(
            "override-redirect (unmanaged)" if applied_flag
            else "override-redirect unavailable"
        )

    # Never steal focus: the marker must not pull focus away from the tester.
    window.set_focus_on_map(False)
    window.set_accept_focus(False)
    applied.append("never-steal-focus")


def _apply_atoms(window, Gdk, atoms, applied):
    """Best-effort application of raw _NET_WM_* atoms after realize()."""
    gdk_window = window.get_window()
    if gdk_window is None:
        applied.append("raw atoms: no GdkWindow yet")
        return
    for prop, values in (atoms or {}).items():
        try:
            Gdk.property_change(
                gdk_window,
                Gdk.Atom.intern(prop, False),
                Gdk.Atom.intern("ATOM", False),
                32,
                Gdk.PropMode.REPLACE,
                [Gdk.Atom.intern(str(value), False) for value in values],
            )
            applied.append(f"{prop} ({len(values)} atom(s))")
        except Exception as exc:  # noqa: BLE001 - best effort, report only
            applied.append(f"{prop} not applied: {exc}")


def _format_report(guard, session, plan, capability, applied, rule_status=None):
    """Build the honest, human-readable capability text shown in the window."""
    lines = [
        "SESSION",
        f"  session type : {session.session_type}",
        f"  desktop      : {session.desktop or '(unset)'}",
        f"  compositor   : {session.compositor}",
        f"  X11 display  : {session.has_x11_display}",
        f"  Wayland      : {session.has_wayland_display}",
        "",
        "CAPABILITY REPORT (honest verdict)",
        f"  guaranteed exclusion : {'YES' if capability.get('guaranteed') else 'NO'}",
        f"  mechanism            : {capability.get('mechanism', '(none)')}",
        f"  recommended          : {capability.get('recommended', '(none)')}",
        "  limitations:",
    ]
    limitations = capability.get("limitations") or ["(none reported)"]
    lines.extend(f"    - {item}" for item in limitations)
    lines.extend(["", "COMPOSITOR CAPTURE RULE"])
    if rule_status is None:
        lines.append("  status unavailable (capture_rules import failed)")
    else:
        lines.extend([
            f"  backend              : {rule_status.backend}",
            f"  supported            : {'yes' if rule_status.supported else 'no'}",
            f"  configured           : {'yes' if rule_status.configured else 'no'}",
            f"  config validated     : {'yes' if rule_status.config_validated else 'no'}",
            "  runtime verified      : no (requires a real screen share)",
            f"  detail               : {rule_status.detail}",
        ])
        if rule_status.path:
            lines.append(f"  rule file            : {rule_status.path}")
        if rule_status.guidance:
            lines.append(f"  guidance             : {rule_status.guidance}")
    lines.extend(
        [
            "",
            "EXCLUSION PLAN APPLIED",
            f"  strategy          : {plan.strategy}",
            f"  guaranteed        : {'YES' if plan.guaranteed else 'NO'}",
            f"  override-redirect : {plan.override_redirect}",
            f"  keep-above        : {plan.keep_above}",
            f"  skip-taskbar      : {plan.skip_taskbar}",
            f"  window type       : {plan.window_type or '(none)'}",
            f"  layer             : {plan.layer or '(none)'}",
            "  applied hints:",
        ]
    )
    lines.extend(f"    - {item}" for item in applied)
    notes = getattr(plan, "notes", ()) or ()
    if notes:
        lines.append("  plan notes:")
        lines.extend(f"    - {item}" for item in notes)
    return "\n".join(lines)


def _safe_capability(guard, session, compositor_skip_capture=False, xwayland_dock=False,
                     capture_rule_supported=None):
    try:
        return guard.capability_report(
            session, compositor_skip_capture=compositor_skip_capture,
            xwayland_dock=xwayland_dock,
            capture_rule_supported=capture_rule_supported,
        )
    except Exception as exc:  # noqa: BLE001 - report, never crash the probe
        return {
            "guaranteed": False,
            "mechanism": f"capability_report failed: {exc}",
            "recommended": "inspect the compositor configuration manually",
            "limitations": ["capability report unavailable"],
        }


def _print_rule_status(status):
    print(f"backend:          {status.backend}")
    print(f"supported:        {'yes' if status.supported else 'no'}")
    print(f"configured:       {'yes' if status.configured else 'no'}")
    print(f"config validated: {'yes' if status.config_validated else 'no'}")
    print("runtime verified: no (a real screen share is required)")
    print(f"detail:           {status.detail}")
    if status.path:
        print(f"rule file:        {status.path}")
    if status.guidance:
        print(f"guidance:         {status.guidance}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "SocketStream stealth-overlay capture-boundary probe. "
            "Clearly labelled QA marker; no AI, capture, network or secrets."
        )
    )
    parser.add_argument(
        "--no-stealth",
        dest="stealth",
        action="store_false",
        help="Do not apply any window hint (A/B control). The marker never hides.",
    )
    parser.add_argument(
        "--exclusion-status",
        action="store_true",
        help="Print the compositor capture-rule status and exit.",
    )
    parser.add_argument(
        "--setup-exclusion",
        action="store_true",
        help="Install the managed compositor capture-exclusion rule, then exit.",
    )
    parser.add_argument(
        "--remove-exclusion",
        action="store_true",
        help="Remove the managed compositor capture-exclusion rule, then exit.",
    )
    parser.set_defaults(stealth=True)
    args = parser.parse_args(argv)

    guard, guard_error = _load_overlay_guard()
    if guard_error:
        print(guard_error, file=sys.stderr)
        return 2

    rules, rules_error = _load_capture_rules()
    if rules_error:
        print(rules_error, file=sys.stderr)
        return 2

    session = guard.detect_session()
    backend = guard.ensure_panel_backend(session)
    xwayland_dock = guard.backend_is_x11(backend)
    exclusion = rules.CaptureExclusion(session=session)

    requested = [
        name for name, enabled in (
            ("status", args.exclusion_status),
            ("install", args.setup_exclusion),
            ("remove", args.remove_exclusion),
        ) if enabled
    ]
    if len(requested) > 1:
        print("choose only one of --exclusion-status, --setup-exclusion, --remove-exclusion",
              file=sys.stderr)
        return 2
    if requested:
        try:
            status = getattr(exclusion, requested[0])()
        except rules.CaptureRuleError as exc:
            print(f"capture exclusion: {exc}", file=sys.stderr)
            return 1
        _print_rule_status(status)
        note = exclusion.note()
        if note:
            print(f"note:             {note}")
        if requested[0] != "status" and not status.supported:
            return 1
        return 0

    gtk_modules, gtk_error = _load_gtk()
    if gtk_error:
        print(gtk_error, file=sys.stderr)
        return 2
    Gtk, Gdk, GLib = gtk_modules

    rule_status = exclusion.status()
    plan = guard.build_plan(
        session, compositor_skip_capture=rule_status.configured,
        xwayland_dock=xwayland_dock,
    )
    capability = _safe_capability(
        guard, session, rule_status.configured, xwayland_dock, rule_status.supported,
    )
    watcher = guard.ScreenShareWatcher()

    class OverlayProbe(Gtk.Window):
        """A clearly labelled marker that applies the real ExclusionPlan."""

        @staticmethod
        def draw_capture_marker(area, cr):
            """Render the M/C/Y detection marker only in this QA window."""
            width = area.get_allocated_width()
            for band, rgb in enumerate(((237, 10, 169), (9, 203, 232), (246, 215, 43))):
                cr.set_source_rgb(*(component / 255 for component in rgb))
                cr.rectangle(0, band * 40, width, 40)
                cr.fill()
            return False

        def __init__(self):
            # Match the real panel so compositor rules with an exact app-id
            # / WM_CLASS match apply to this probe too.
            GLib.set_prgname(rules.PANEL_APP_ID)
            Gdk.set_program_class(rules.PANEL_WM_CLASS)
            super().__init__(title=WINDOW_TITLE)
            self.stealth = args.stealth
            self.watcher = watcher
            self.exclusion = exclusion
            self.rule_status = rule_status
            self.plan = plan
            self.last_share = None
            self.applied = []
            self.set_default_size(660, 720)
            self.set_border_width(14)
            self.set_position(Gtk.WindowPosition.CENTER)
            try:
                self.set_wmclass(rules.PANEL_WM_CLASS, rules.PANEL_WM_CLASS)
            except Exception:  # noqa: BLE001 - X11 only
                pass
            if self.stealth:
                _apply_plan(self, Gdk, plan, self.applied)
            else:
                self.applied.append("hints disabled (--no-stealth)")

            layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            self.add(layout)

            heading = Gtk.Label()
            heading.set_markup(
                '<span size="x-large" weight="bold" foreground="#dc3737">'
                "STEALTH OVERLAY PROBE</span>"
            )
            heading.set_xalign(0)
            layout.pack_start(heading, False, False, 0)

            subtitle = Gtk.Label(label=WINDOW_TITLE)
            subtitle.set_xalign(0)
            subtitle.set_line_wrap(True)
            layout.pack_start(subtitle, False, False, 0)

            body = Gtk.Label(label=MARKER_BODY)
            body.set_xalign(0)
            body.set_line_wrap(True)
            layout.pack_start(body, False, False, 0)

            marker = Gtk.DrawingArea()
            marker.set_size_request(480, 120)
            marker.connect("draw", self.draw_capture_marker)
            layout.pack_start(marker, False, False, 0)

            self.status = Gtk.Label()
            self.status.set_xalign(0)
            layout.pack_start(self.status, False, False, 0)

            report_frame = Gtk.Frame(label="Capability report — read the honest verdict")
            report_scroll = Gtk.ScrolledWindow()
            report_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            report_scroll.set_min_content_height(300)
            self.report = Gtk.TextView()
            self.report.set_editable(False)
            self.report.set_cursor_visible(False)
            self.report.set_monospace(True)
            self.report.get_buffer().set_text(
                _format_report(
                    guard, session, plan, capability, self.applied, self.rule_status,
                )
            )
            report_scroll.add(self.report)
            report_frame.add(report_scroll)
            layout.pack_start(report_frame, True, True, 0)

            log_frame = Gtk.Frame(label="Event log")
            log_scroll = Gtk.ScrolledWindow()
            log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            log_scroll.set_min_content_height(130)
            self.log = Gtk.TextView()
            self.log.set_editable(False)
            self.log.set_cursor_visible(False)
            self.log.set_monospace(True)
            log_scroll.add(self.log)
            log_frame.add(log_scroll)
            layout.pack_start(log_frame, True, True, 0)

            buttons = Gtk.Box(spacing=8)
            refresh = Gtk.Button(label="Refresh rule status")
            refresh.connect("clicked", self.refresh_rule_status)
            buttons.pack_start(refresh, False, False, 0)
            clear = Gtk.Button(label="Clear log")
            clear.connect("clicked", self.on_clear)
            buttons.pack_start(clear, False, False, 0)
            close = Gtk.Button(label="Close probe")
            close.connect("clicked", lambda *_: Gtk.main_quit())
            buttons.pack_end(close, False, False, 0)
            layout.pack_start(buttons, False, False, 0)

            self.connect("destroy", Gtk.main_quit)
            self.log_line(
                f"Probe started. stealth={'on' if self.stealth else 'off (--no-stealth)'}"
            )
            self.log_line(
                "The marker never hides on focus changes or screen shares. "
                "Close the probe to stop it."
            )
            if not plan.guaranteed:
                self.log_line(
                    "Capture exclusion is NOT guaranteed on this session; "
                    "read the rule/video test result, do not assume it."
                )

        def log_line(self, text):
            import time

            stamp = time.strftime("%H:%M:%S")
            buffer = self.log.get_buffer()
            buffer.insert(buffer.get_end_iter(), f"[{stamp}] {text}\n")
            buffer.place_cursor(buffer.get_end_iter())

        def set_status(self, text, level):
            colors = {"ok": "#1a7f37", "warn": "#b3261e", "info": "#444444"}
            self.status.set_markup(
                f'<span weight="bold" foreground="{colors[level]}">{text}</span>'
            )

        def on_clear(self, *_):
            self.log.get_buffer().set_text("")
            self.log_line("Log cleared.")

        def refresh_rule_status(self, *_):
            self.rule_status = self.exclusion.status()
            capability_now = _safe_capability(
                guard, session, self.rule_status.configured, xwayland_dock, self.rule_status.supported,
            )
            self.report.get_buffer().set_text(
                _format_report(
                    guard, session, self.plan, capability_now,
                    self.applied, self.rule_status,
                )
            )
            self.log_line(
                f"rule status: configured={self.rule_status.configured} "
                f"validated={self.rule_status.config_validated} "
                f"backend={self.rule_status.backend}"
            )

        def tick(self):
            """Poll the share state for reporting only; never hide."""
            try:
                active = bool(self.watcher.poll())
                status = self.watcher.status() or {}
            except Exception as exc:  # noqa: BLE001 - never crash the UI
                self.set_status("Screen share: probe error", "warn")
                self.log_line(f"watcher error: {exc}")
                return True

            checked = bool(status.get("checked", False))
            detail = status.get("detail", "")
            if active:
                self.set_status("Screen share: detected — marker stays visible", "warn")
            elif checked:
                self.set_status("Screen share: none detected", "ok")
            else:
                self.set_status("Screen share: unknown (probe unavailable)", "info")
            if active != self.last_share:
                self.log_line(
                    f"share state changed: {'detected' if active else 'none'}"
                    f"{' · ' + detail if detail else ''}"
                )
                self.last_share = active
            return True

        def start(self):
            self.show_all()
            _apply_atoms(self, Gdk, getattr(self.plan, "atoms", {}), self.applied)
            self.report.get_buffer().set_text(
                _format_report(
                    guard, session, self.plan, capability, self.applied,
                    self.rule_status,
                )
            )
            self.tick()
            GLib.timeout_add_seconds(2, self.tick)

    probe = OverlayProbe()
    probe.start()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
