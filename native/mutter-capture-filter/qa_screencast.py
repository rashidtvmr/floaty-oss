#!/usr/bin/env python3
"""Capture one synthetic-session frame through Mutter's real PipeWire API."""
import argparse
import subprocess
from gi.repository import Gio, GLib

NAME = "org.gnome.Mutter.ScreenCast"
ROOT = "/org/gnome/Mutter/ScreenCast"


def call(bus, path, iface, method, args=None):
    return bus.call_sync(NAME, path, iface, method, args, None,
                         Gio.DBusCallFlags.NONE, 15000, None)


def main(out, timeout, source="monitor", frames=1):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    session = call(bus, ROOT, NAME, "CreateSession",
                   GLib.Variant("(a{sv})", ({},))).unpack()[0]
    try:
        props = {"is-recording": GLib.Variant("b", True)}
        if source == "monitor":
            state = bus.call_sync(
                "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                "org.gnome.Mutter.DisplayConfig", "GetCurrentState", None, None,
                Gio.DBusCallFlags.NONE, 15000, None).unpack()
            connector = state[1][0][0][0]
            method, args = "RecordMonitor", GLib.Variant("(sa{sv})", (connector, props))
        elif source == "area":
            method, args = "RecordArea", GLib.Variant("(iiiia{sv})", (0, 0, 1280, 720, props))
        else:
            # With no window-id, Mutter selects the focused window.
            method, args = "RecordWindow", GLib.Variant("(a{sv})", (props,))
        stream = call(bus, session, NAME + ".Session", method, args).unpack()[0]
        nodes = []
        loop = GLib.MainLoop()

        def node_callback(_bus, _sender, _path, _iface, _signal, args, _user):
            nodes.append(args.unpack()[0])
            loop.quit()

        sid = bus.signal_subscribe(NAME, NAME + ".Stream", "PipeWireStreamAdded",
                                   stream, None, Gio.DBusSignalFlags.NONE, node_callback, None)
        timer = GLib.timeout_add_seconds(timeout, lambda: (loop.quit(), False)[1])
        try:
            call(bus, session, NAME + ".Session", "Start")
            loop.run()
        finally:
            bus.signal_unsubscribe(sid)
            if nodes:
                GLib.source_remove(timer)
        if not nodes:
            raise RuntimeError("No PipeWire node")
        subprocess.run([
            "gst-launch-1.0", "-q", "pipewiresrc", f"path={nodes[0]}", f"num-buffers={frames}",
            "!", "videoconvert", "!", "pngenc", "!",
            "multifilesink" if frames > 1 else "filesink", f"location={out}",
        ], check=True, timeout=timeout)
        print("saved", source, out, flush=True)
    finally:
        try:
            call(bus, session, NAME + ".Session", "Stop")
        except GLib.Error:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--source", choices=("monitor", "area", "window"), default="monitor")
    parser.add_argument("--frames", type=int, default=1,
                        help="For multiple frames, output must contain a %%d placeholder")
    args = parser.parse_args()
    if args.frames < 1 or (args.frames > 1 and "%d" not in args.output):
        parser.error("positive --frames and a %d output placeholder are required for a sequence")
    main(args.output, args.timeout, args.source, args.frames)
