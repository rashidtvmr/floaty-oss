"""Explicitly enabled desktop input for the synthetic GTK integration checks.

XTest is suitable for Xvfb/X11. On GNOME Wayland, the root-only test launcher
can pass a temporary uinput device; it is destroyed when the suite finishes.
Only the test's pointer, left button, Super/F8 and prompt typing keys are generated.
"""
import ctypes
import ctypes.util
import os
import struct


class DesktopInput:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.fd = os.environ.get("SOCKET_STREAM_GTK_UINPUT_FD")
        self.display = None
        if self.fd is not None:
            self.fd = int(self.fd)
            self.backend = "uinput → compositor"
            return
        self.backend = "XTest"
        self.x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
        self.xtst = ctypes.CDLL(ctypes.util.find_library("Xtst"))
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x11.XFlush.argtypes = [ctypes.c_void_p]
        self.x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        self.xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                                  ctypes.c_int, ctypes.c_ulong]
        self.xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("XTest needs an X11 display")

    def _events(self, events):
        payload = b"".join(struct.pack("llHHi", 0, 0, kind, code, value)
                           for kind, code, value in (*events, (0, 0, 0)))
        if os.write(self.fd, payload) != len(payload):
            raise RuntimeError("short write to the synthetic input device")

    def move(self, x, y):
        if self.fd is not None:
            self._events(((3, 0, round(x * 65535 / (self.width - 1))),
                          (3, 1, round(y * 65535 / (self.height - 1)))))
        else:
            self.xtst.XTestFakeMotionEvent(self.display, -1, x, y, 0)
            self.x11.XFlush(self.display)

    def button(self, pressed):
        if self.fd is not None:
            self._events(((1, 0x110, int(pressed)),))
        else:
            self.xtst.XTestFakeButtonEvent(self.display, 1, int(pressed), 0)
            self.x11.XFlush(self.display)

    def key(self, name, pressed):
        if self.fd is not None:
            self._events(((1, {"super": 125, "f8": 66, "a": 30, "enter": 28}[name], int(pressed)),))
        else:
            # Send the hardware keycode directly. xdotool's symbol synthesis
            # can add Alt when resolving F8 in the server's XKB keymap.
            code = self.x11.XKeysymToKeycode(self.display, {
                "super": 0xffeb, "f8": 0xffc5, "a": 0x61, "enter": 0xff0d}[name])
            if not code:
                raise RuntimeError(f"key {name} is absent from the test keymap")
            self.xtst.XTestFakeKeyEvent(self.display, code, int(pressed), 0)
            self.x11.XFlush(self.display)

    def close(self):
        self.button(False)
        self.key("f8", False)
        self.key("super", False)
        self.key("a", False)
        self.key("enter", False)
        if self.display is not None:
            self.x11.XCloseDisplay(self.display)
            self.display = None
