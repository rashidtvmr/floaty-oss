#!/usr/bin/env python3
"""Run opted-in GTK checks through a disposable Linux compositor input device.

Run as root with --user naming the desktop owner and that user's DISPLAY,
XAUTHORITY, XDG_RUNTIME_DIR, WAYLAND_DISPLAY and DBUS_SESSION_BUS_ADDRESS.
The GTK suite runs as that user. No service, package or persistent device is
installed. The desktop must be idle: the suite moves its pointer and uses F8.
"""
import argparse
import fcntl
import os
from pathlib import Path
import pwd
import struct
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("tests", nargs="*")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("creating the temporary uinput device requires root")
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0:
        parser.error("the GTK desktop tests must run as a non-root user")
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    created = False
    try:
        # Linux uinput.h: _IOW('U', request, int), UI_DEV_SETUP and UI_ABS_SETUP.
        for event_type in (1, 3):  # EV_KEY, EV_ABS
            fcntl.ioctl(fd, 0x40045564, event_type)
        for key in (0x110, 125, 66, 30, 28):  # BTN_LEFT, Super, F8, A, Enter
            fcntl.ioctl(fd, 0x40045565, key)
        fcntl.ioctl(fd, 0x4004556e, 0)  # INPUT_PROP_POINTER
        for axis in (0, 1):  # ABS_X, ABS_Y
            fcntl.ioctl(fd, 0x40045567, axis)
            fcntl.ioctl(fd, 0x401c5504, struct.pack("H2xiiiiii", axis, 0, 0, 65535, 0, 0, 1))
        name = b"SocketStream synthetic GTK verification"
        fcntl.ioctl(fd, 0x405c5503, struct.pack("HHHH80sI", 3, 0x1234, 0x5678, 1, name, 0))
        fcntl.ioctl(fd, 0x5501)  # UI_DEV_CREATE
        created = True
        subprocess.run(["udevadm", "settle", "--timeout=5"], check=True, timeout=8)
        env = dict(os.environ, HOME=account.pw_dir, USER=args.user, LOGNAME=args.user,
                   SOCKET_STREAM_GTK_POINTER_CHECKS="1", SOCKET_STREAM_GTK_UINPUT_FD=str(fd))
        suite = Path(__file__).with_name("gtk_workspace_checks.py")
        result = subprocess.run(
            [sys.executable, "-W", "ignore::DeprecationWarning", "-W", "error::ResourceWarning",
             str(suite), *args.tests], env=env, cwd=suite.parent.parent,
            user=account.pw_uid, group=account.pw_gid,
            extra_groups=os.getgrouplist(args.user, account.pw_gid), pass_fds=(fd,), timeout=120)
        return result.returncode
    finally:
        if created:
            fcntl.ioctl(fd, 0x5502)  # UI_DEV_DESTROY
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main())
