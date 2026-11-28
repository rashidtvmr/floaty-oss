# Native Wayland accessibility acceptance

`tests/acceptance/test_wayland_atspi_runtime.py` exercises the production Python
AT-SPI helper and the Rust live screen session against a synthetic GTK3 window
running on a private Weston headless compositor. It is opt-in because a normal
headless unit-test environment may not have the native dependencies or permission
to create Unix sockets.

## Run

Install the normal Linux dependencies from the reviewed installer, plus Weston.
On Debian/Ubuntu the fixture needs `weston`, `python3-gi`, `gir1.2-gtk-3.0`,
`gir1.2-atspi-2.0`, `at-spi2-core`, and `dbus-daemon`. Then run:

```sh
cargo build -p socket_stream
SOCKET_STREAM_RUN_WAYLAND_A11Y=1 python3 -m unittest discover \
  -s tests/acceptance -p test_wayland_atspi_runtime.py -v
```

`SOCKET_STREAM_TEST_CLI` can select a different freshly built CLI. For a
task-local extraction of Debian 13 x86_64 Weston 14 and AT-SPI packages,
`SOCKET_STREAM_WAYLAND_RUNTIME=/absolute/path/to/package-prefix` selects its
`usr/bin`, shared libraries and GI typelibs. Other architectures or Weston major
versions should use their normal installed dependencies.

## Isolation and assertions

The test creates a private `XDG_RUNTIME_DIR`, private D-Bus session, memory-only
GSettings backend and a headless Wayland display. It removes inherited X11 and
accessibility-bus addresses. Only the synthetic GTK fixture registers on this
accessibility bus. Its synthetic provider listens only on loopback, records the
complete HTTP requests, and returns fixed fixture answers; no external AI
receives anything. Assertions use those complete requests, rather than the
bounded request preview shown in the application's UI.
The compositor and fixture processes are terminated and their temporary files
removed when the test finishes.

The test checks:

- GTK is using its native Wayland backend.
- The actual AT-SPI helper finds the explicitly selected fixture window.
- Native window and node keys survive separate helper processes.
- A protected password value is absent from the extracted tree.
- The first successful model request establishes a snapshot.
- Editing one GTK label sends a patch containing exactly one changed node;
  the unchanged label is absent from that patch.
- Previous successful conversation turns remain in session context.
- Explicit selected-window mode remains accessibility-only even when OCR and
  portal options are requested.
- Closing the selected fixture produces an unavailable-target error instead of
  reading another window.

This fixture does not validate GNOME compositor behavior, a particular Ubuntu
release, browser-specific accessibility bridges, graphical permission prompts,
physical display hardware or a user's existing session. Those need separate
testing on the intended desktop.

## Validation status for this change

The source, Python regressions and native GI interface declarations were checked.
Debian 13 Weston 14.0.2 and libweston were extracted locally and their shared
library dependencies resolved. The executor rejects `AF_UNIX` socket creation
with `EPERM`, which prevents both private D-Bus and Weston from starting. The
native acceptance test therefore reports an explicit skip here; its end-to-end
assertions have **not** been verified in this executor.

Ubuntu documents AT-SPI as a D-Bus accessibility framework and its default 26.04
GNOME session as Wayland-only:

- [Ubuntu accessibility stack](https://ubuntu.com/desktop/docs/en/26.04/explanation/accessibility-stack/)
- [Ubuntu 26.04 LTS summary](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/)
