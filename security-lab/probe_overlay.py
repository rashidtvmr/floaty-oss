#!/usr/bin/env python3
"""Clearly labeled, harmless test window. No AI, evasion or secret capture."""
import sys


def main():
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
    except (ImportError, ValueError) as error:
        raise SystemExit(f"Install python3-gi and gir1.2-gtk-3.0: {error}")

    window = Gtk.Window(title="SocketStream Capture Test — Visible Marker")
    window.set_default_size(460, 210)
    window.set_resizable(False)
    window.set_keep_above(True)
    window.set_position(Gtk.WindowPosition.CENTER)
    window.set_border_width(18)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    window.add(box)
    heading = Gtk.Label()
    heading.set_markup(
        '<span size="xx-large" weight="bold" foreground="#dc3737">'
        'CAPTURE TEST MARKER</span>'
    )
    box.pack_start(heading, True, True, 0)
    info = Gtk.Label(label=(
        "Security QA only — no AI answers or private data.\n"
        "Compare this window with the browser recording preview."
    ))
    info.set_justify(Gtk.Justification.CENTER)
    box.pack_start(info, True, True, 0)
    close = Gtk.Button(label="Close test marker")
    close.connect("clicked", lambda *_: Gtk.main_quit())
    box.pack_start(close, False, False, 0)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
