#!/usr/bin/env python3
"""User-authorized XDG ScreenCast/PipeWire frame writer for Linux Wayland.

Requires python3-dbus, python3-gi, Gst 1.0 with pipewiresrc/pngenc/appsink.
A single ScreenCast portal session is kept open; it writes one atomic PNG
snapshot at each sampling interval until terminated. No permission bypass,
capture persistence, or hidden startup.
"""
import argparse
import os
import signal
import sys
import tempfile
import uuid
from pathlib import Path

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENSCAST = "org.freedesktop.portal.ScreenCast"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"


def request_path(sender, token):
    clean = sender.lstrip(":").replace(".", "_")
    if not clean.replace("_", "").isalnum() or not token.isalnum():
        raise ValueError("Invalid D-Bus request path")
    return f"/org/freedesktop/portal/desktop/request/{clean}/{token}"


def interval_to_fraction(seconds):
    if seconds < 2 or seconds > 3600:
        raise ValueError("Interval must be between 2 and 3600 seconds")
    return f"1/{seconds}"


def atomic_write(destination, content):
    destination = Path(destination)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".frame-",
                                     suffix=".png", delete=False) as temporary:
        temporary_name = temporary.name
        try:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        except Exception:
            os.unlink(temporary_name)
            raise
    os.replace(temporary_name, destination)


class PortalCapture:
    def __init__(self, destination, seconds, bus, dbus, GLib, Gst):
        self.destination = Path(destination)
        self.seconds = seconds
        self.bus = bus
        self.dbus = dbus
        self.GLib = GLib
        self.Gst = Gst
        self.loop = GLib.MainLoop()
        self.portal = dbus.Interface(
            bus.get_object(PORTAL_BUS, PORTAL_PATH), SCREENSCAST)
        self.session = None
        self.pipeline = None
        self.remote_fd = None

    def request(self, method_name, args, options, on_response):
        token = "cl" + uuid.uuid4().hex
        path = request_path(self.bus.get_unique_name(), token)

        def response_handler(code, results):
            if int(code) != 0:
                print(f"ScreenCast {method_name} denied/cancelled (code {code})",
                      file=sys.stderr, flush=True)
                self.stop()
                return
            on_response(results)

        self.bus.add_signal_receiver(
            response_handler, dbus_interface=REQUEST,
            signal_name="Response", path=path)
        options = dict(options)
        options["handle_token"] = self.dbus.String(token)

        def failed(error):
            print(f"Portal {method_name} error: {error}", file=sys.stderr, flush=True)
            self.stop()

        getattr(self.portal, method_name)(
            *args, self.dbus.Dictionary(options, signature="sv"),
            reply_handler=lambda _handle: None, error_handler=failed)

    def start(self):
        options = {"session_handle_token": self.dbus.String("cl" + uuid.uuid4().hex)}
        self.request("CreateSession", (), options, self.on_session_created)
        print("Waiting for user-approved ScreenCast portal session...", flush=True)
        self.loop.run()
        self.close()

    def on_session_created(self, results):
        self.session = str(results["session_handle"])
        self.request("SelectSources", (self.dbus.ObjectPath(self.session),),
                     {"types": self.dbus.UInt32(3),
                      "multiple": self.dbus.Boolean(False)},
                     self.on_sources_selected)

    def on_sources_selected(self, _results):
        self.request("Start", (self.dbus.ObjectPath(self.session), ""),
                     {}, self.on_started)

    def on_started(self, results):
        streams = results.get("streams") or []
        if not streams:
            print("Portal returned no capture streams", file=sys.stderr, flush=True)
            self.stop()
            return
        node_id = int(streams[0][0])
        try:
            fd_object = self.portal.OpenPipeWireRemote(
                self.dbus.ObjectPath(self.session),
                self.dbus.Dictionary({}, signature="sv"))
            self.remote_fd = fd_object.take()
            self.begin_pipeline(self.remote_fd, node_id)
        except Exception as error:
            print(f"Unable to access PipeWire stream: {error}", file=sys.stderr, flush=True)
            self.stop()

    def begin_pipeline(self, fd, node_id):
        frequency = interval_to_fraction(self.seconds)
        description = (
            f"pipewiresrc fd={fd} path={node_id} do-timestamp=true "
            "! queue max-size-buffers=1 leaky=downstream "
            "! videoconvert ! videorate "
            f"! video/x-raw,framerate={frequency} "
            "! pngenc ! appsink name=frame_sink "
            "emit-signals=true max-buffers=1 drop=true sync=false"
        )
        self.pipeline = self.Gst.parse_launch(description)
        sink = self.pipeline.get_by_name("frame_sink")
        sink.connect("new-sample", self.on_sample)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_gst_message)
        self.pipeline.set_state(self.Gst.State.PLAYING)
        print("Portal capture ACTIVE; use Ctrl+C or Stop to end.", flush=True)

    def on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.Gst.FlowReturn.ERROR
        buffer = sample.get_buffer()
        success, mapping = buffer.map(self.Gst.MapFlags.READ)
        if not success:
            return self.Gst.FlowReturn.ERROR
        try:
            atomic_write(self.destination, bytes(mapping.data))
        except OSError as error:
            print(f"Could not save frame: {error}", file=sys.stderr, flush=True)
            self.GLib.idle_add(self.stop)
            return self.Gst.FlowReturn.ERROR
        finally:
            buffer.unmap(mapping)
        return self.Gst.FlowReturn.OK

    def on_gst_message(self, _bus, message):
        if message.type == self.Gst.MessageType.ERROR:
            _error, details = message.parse_error()
            print(f"GStreamer screen pipeline failed: {_error} {details}",
                  file=sys.stderr, flush=True)
            self.stop()
        elif message.type == self.Gst.MessageType.EOS:
            self.stop()

    def stop(self):
        if self.loop.is_running():
            self.loop.quit()
        return False

    def close(self):
        if self.pipeline is not None:
            self.pipeline.set_state(self.Gst.State.NULL)
        if self.session:
            try:
                session = self.dbus.Interface(
                    self.bus.get_object(PORTAL_BUS, self.session), SESSION)
                session.Close()
            except Exception:
                pass
        if self.remote_fd is not None:
            os.close(self.remote_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorized", action="store_true",
                        help="explicit consent to present the system ScreenCast chooser")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=5)
    args = parser.parse_args(argv)
    if not args.authorized:
        parser.error("explicit --authorized is required")
    try:
        interval_to_fraction(args.interval)
    except ValueError as error:
        parser.error(str(error))
    if not args.output.parent.is_dir():
        parser.error("output parent directory does not exist")
    try:
        import dbus
        import dbus.mainloop.glib
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst
    except (ImportError, ValueError) as error:
        print(f"Portal dependencies unavailable: {error}", file=sys.stderr)
        return 2
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    Gst.init(None)
    controller = PortalCapture(
        args.output, args.interval, dbus.SessionBus(), dbus, GLib, Gst)
    signal.signal(signal.SIGINT, lambda *_: GLib.idle_add(controller.stop))
    signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(controller.stop))
    try:
        controller.start()
        return 0
    except Exception as error:
        print(f"ScreenCast failure: {error}", file=sys.stderr)
        controller.close()
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
