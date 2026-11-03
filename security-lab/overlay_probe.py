#!/usr/bin/env python3
"""Human-run GTK3 probe for the ContextLens stealth overlay capture boundary.

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
* The only mechanism that can honestly keep the marker out of a full-screen
  capture is the behavioural guard: detect an active screen share and hide.

No AI, no capture, no network, no secrets. Nothing is read from the screen and
nothing is uploaded. The marker text is fixed placeholder text.

Usage:
    python3 overlay_probe.py                 # guard enabled
    python3 overlay_probe.py --no-stealth    # A/B control: guard disabled
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The frozen, pure-stdlib overlay_guard module lives in the desktop UI package.
UI_DIR = Path(__file__).resolve().parent.parent / "apps" / "context-desktop" / "ui"
if str(UI_DIR) not in sys.path:
    sys.path.insert(0, str(UI_DIR))

WINDOW_TITLE = "ContextLens Stealth Overlay Probe — NOT a real answer panel"

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


def _format_report(guard, session, plan, capability, applied):
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


def _safe_capability(guard, session):
    try:
        return guard.capability_report(session)
    except Exception as exc:  # noqa: BLE001 - report, never crash the probe
        return {
            "guaranteed": False,
            "mechanism": f"capability_report failed: {exc}",
            "recommended": "behavioural auto-hide",
            "limitations": ["capability report unavailable"],
        }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "ContextLens stealth-overlay capture-boundary probe. "
            "Clearly labelled QA marker; no AI, capture, network or secrets."
        )
    )
    parser.add_argument(
        "--no-stealth",
        dest="stealth",
        action="store_false",
        help="Disable the behavioural guard (A/B control: marker never auto-hides).",
    )
    parser.set_defaults(stealth=True)
    args = parser.parse_args(argv)

    guard, guard_error = _load_overlay_guard()
    if guard_error:
        print(guard_error, file=sys.stderr)
        return 2

    gtk_modules, gtk_error = _load_gtk()
    if gtk_error:
        print(gtk_error, file=sys.stderr)
        return 2
    Gtk, Gdk, GLib = gtk_modules

    session = guard.detect_session()
    plan = guard.build_plan(session)
    capability = _safe_capability(guard, session)
    watcher = guard.ScreenShareWatcher()

    class OverlayProbe(Gtk.Window):
        """A clearly labelled marker that applies the real ExclusionPlan."""

        def __init__(self):
            super().__init__(title=WINDOW_TITLE)
            self.stealth = args.stealth
            self.watcher = watcher
            self.auto_hidden = False
            self.applied = []
            self.set_default_size(660, 720)
            self.set_border_width(14)
            self.set_position(Gtk.WindowPosition.CENTER)
            _apply_plan(self, Gdk, plan, self.applied)

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

            self.status = Gtk.Label()
            self.status.set_xalign(0)
            layout.pack_start(self.status, False, False, 0)

            report_frame = Gtk.Frame(label="Capability report — read the honest verdict")
            report_scroll = Gtk.ScrolledWindow()
            report_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            report_scroll.set_min_content_height(240)
            self.report = Gtk.TextView()
            self.report.set_editable(False)
            self.report.set_cursor_visible(False)
            self.report.set_monospace(True)
            self.report.get_buffer().set_text(
                _format_report(guard, session, plan, capability, self.applied)
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
            if not plan.guaranteed:
                self.log_line(
                    "Plan is best effort: exclusion is NOT guaranteed on this session."
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

        def tick(self):
            """GLib 2s timer: poll the guard and hide/restore the marker."""
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
                self.set_status("Screen share: detected — hiding", "warn")
                if self.stealth and self.get_visible():
                    self.hide()
                    self.auto_hidden = True
                    self.log_line(f"share detected; marker hidden. {detail}".strip())
            else:
                if checked:
                    self.set_status("Screen share: none detected", "ok")
                else:
                    self.set_status("Screen share: unknown (probe unavailable)", "info")
                if self.auto_hidden and self.stealth:
                    self.show_all()
                    self.auto_hidden = False
                    self.log_line("share ended; marker restored.")
            return True

        def start(self):
            self.show_all()
            # Raw _NET_WM_* atoms can only be set once a GdkWindow exists.
            _apply_atoms(self, Gdk, getattr(plan, "atoms", {}), self.applied)
            self.report.get_buffer().set_text(
                _format_report(guard, session, plan, capability, self.applied)
            )
            self.tick()
            GLib.timeout_add_seconds(2, self.tick)

    probe = OverlayProbe()
    probe.start()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
