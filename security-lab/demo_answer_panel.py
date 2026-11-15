#!/usr/bin/env python3
"""Real SocketStream answer window with synthetic, locally generated QA content."""
from __future__ import annotations

import sys
from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[1] / "apps" / "socket_stream_desktop" / "ui"
sys.path.insert(0, str(UI_DIR))

QUESTION = "How can you optimize Two Sum from O(n²) to O(n)?"
ANSWER = (
    "Brute force: compare every pair. O(n²) time, O(1) space.\n\n"
    "Optimized: store previously seen numbers in a hash map. For each number, "
    "look up target - number before inserting it. O(n) average time, O(n) space.\n\n"
    "TypeScript:\n"
    "const seen = new Map<number, number>();\n"
    "for (let i = 0; i < nums.length; i++) {\n"
    "  const other = seen.get(target - nums[i]);\n"
    "  if (other !== undefined) return [other, i];\n"
    "  seen.set(nums[i], i);\n"
    "}\n\n"
    "Demo only: no microphone, AI inference, or screen reading."
)
COLORS = ((237, 10, 169), (9, 203, 232), (246, 215, 43))


def draw_marker(widget, cairo):
    width = widget.get_allocated_width()
    for row, rgb in enumerate(COLORS):
        cairo.set_source_rgb(*(channel / 255 for channel in rgb))
        cairo.rectangle(0, row * 28, width, 28)
        cairo.fill()
    return False


def main():
    from overlay_guard import detect_session, ensure_panel_backend
    session = detect_session()
    ensure_panel_backend(session)
    import gi
    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
    from floating_overlay import FloatingOverlay

    panel = FloatingOverlay(session=session, stealth=False, compact=False, opacity=0.98)
    panel.window.set_title("SocketStream — Practice Answer (Test)")
    panel.window.set_decorated(True)
    panel.window.set_keep_above(False)
    panel.window.set_accept_focus(True)
    panel.window.set_default_size(500, 440)
    layout = panel.window.get_child()

    drag = Gtk.EventBox()
    drag.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
    label = Gtk.Label(label="⇆ DRAG HERE · Real answer panel · Sample Q&A")
    label.set_xalign(0)
    label.set_margin_top(6)
    label.set_margin_bottom(6)
    drag.add(label)

    def move_window(_widget, event):
        if event.button != 1:
            return False
        panel.window.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
        return True

    drag.connect("button-press-event", move_window)
    layout.pack_start(drag, False, False, 0)
    layout.reorder_child(drag, 0)

    marker = Gtk.DrawingArea()
    marker.set_size_request(280, 84)
    marker.connect("draw", draw_marker)
    layout.pack_start(marker, False, False, 0)
    layout.reorder_child(marker, 1)

    panel.set_question(QUESTION)
    panel.set_answer(ANSWER)
    panel.set_status("DEMO · no screen/audio capture active")

    def position():
        try:
            screen = panel.window.get_screen()
            monitor = screen.get_monitor_geometry(screen.get_primary_monitor())
            panel.window.move(monitor.x + max(0, monitor.width - 525), monitor.y + 42)
        except Exception:
            pass  # Native Wayland positioning can be compositor-restricted.
        return False

    panel.window.connect("hide", lambda *_: GLib.idle_add(panel.destroy))
    panel.show()
    GLib.timeout_add(120, position)
    return panel.run()


if __name__ == "__main__":
    raise SystemExit(main())
