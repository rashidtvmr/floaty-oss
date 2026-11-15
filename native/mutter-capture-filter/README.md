# Custom Mutter 50.1 capture filter

An opt-in compositor source patch that keeps `socket_stream_overlay` visible on
the local desktop and removes its actor from supported capture renders.
This is a custom **Wayland compositor build**, not a kernel flag or GTK setting.

Targets: upstream GNOME Mutter **50.1**, commit
`8e9bb806da4a6a4fc87aa4a481b228dba6735cbd` (libmutter API 18), and Ubuntu's
**50.1-0ubuntu2.5** source with its downstream patches applied. Other versions
need a port and validation. The patcher checks every source anchor.

## Isolated upstream development

From this repository, with an isolated upstream source checkout:

```sh
git clone --depth 1 --branch 50.1 https://github.com/GNOME/mutter.git "$HOME/mutter-socket_stream"
python3 native/mutter-capture-filter/patch_mutter.py "$HOME/mutter-socket_stream"
python3 native/mutter-capture-filter/patch_mutter.py "$HOME/mutter-socket_stream" --diff
python3 native/mutter-capture-filter/patch_mutter.py "$HOME/mutter-socket_stream" --apply
meson setup "$HOME/mutter-socket_stream_build" "$HOME/mutter-socket_stream" -Dtests=enabled --buildtype=debugoptimized
meson compile -C "$HOME/mutter-socket_stream_build" -j 2
meson test -C "$HOME/mutter-socket_stream_build" --print-errorlogs socket_stream_capture
bash native/mutter-capture-filter/run_headless_qa.sh "$HOME/mutter-socket_stream_build" "$HOME/socket_stream_capture-qa"
```

Install the distribution's Mutter build dependencies first. The patcher is
read-only by default, validates the entire patch before any write, uses atomic
`git apply`, and is idempotent. It refuses incompatible anchors or an existing
modified test file. Preserve the emitted diff for `git apply --reverse` rollback.

## Ubuntu runtime deployment

The desktop runtime must be built from the **installed distribution source
version**, including Ubuntu's patches. An upstream version number alone does not
establish ABI compatibility. The GNOME Shell process must load all four matching
Mutter libraries and typelibs, then inherit `SOCKET_STREAM_CAPTURE_EXCLUDE=1` at
startup. Setting that variable in SocketStream or Chrome has no effect on an
already-running compositor.

The supported layout is a versioned directory under `/opt/socket_stream-mutter`
with a `current` symlink. Runtime staging copies the four Mutter libraries and
four typelibs from a **private DESTDIR install**, the installed GNOME Shell
executable, the launcher/validator/probe/guard, and source provenance. Ubuntu's
`/usr` files stay package-managed. Each private DSO has `DT_RPATH=$ORIGIN`; the
copied Shell has `DT_RUNPATH=$ORIGIN/lib:$ORIGIN/typelib:/usr/lib/gnome-shell`.
Both contracts are checked with `readelf` before dependency resolution. The
private typelib directory also carries the matching library aliases: GI's late
loads must resolve to the same libraries as the native executable.

Example for Ubuntu 50.1-0ubuntu2.5, after installing the distribution's build
dependencies and `patchelf` (enable `deb-src` repositories for `apt-get source`):

```sh
REPO=/absolute/path/to/floaty-oss
WORK=$(mktemp -d "$HOME/socket_stream-mutter.XXXXXXXX")
cd "$WORK"
VERSION=$(dpkg-query -W -f='${source:Version}' libmutter-18-0)
apt-get source "mutter=$VERSION"
SOURCE="$WORK/mutter-50.1"
python3 "$REPO/native/mutter-capture-filter/patch_mutter.py" "$SOURCE" --diff > "$WORK/capture.patch"
python3 "$REPO/native/mutter-capture-filter/patch_mutter.py" "$SOURCE" --apply
meson setup "$WORK/build" "$SOURCE" --prefix=/usr --libdir=lib/x86_64-linux-gnu \
  --buildtype=debugoptimized -Dauto_features=enabled -Degl_device=true \
  -Dremote_desktop=true -Dwayland_eglstream=true -Dtests=enabled
meson compile -C "$WORK/build" -j 2
meson test -C "$WORK/build" --print-errorlogs socket_stream_capture
bash "$REPO/native/mutter-capture-filter/run_headless_qa.sh" "$WORK/build" "$WORK/qa"
meson install -C "$WORK/build" --no-rebuild --destdir "$WORK/install"
sudo python3 "$REPO/native/mutter-capture-filter/stage_runtime.py" \
  --build "$WORK/build" --destdir "$WORK/install" \
  --source-dsc "$WORK/mutter_$VERSION.dsc" --patch "$WORK/capture.patch"
```

Staging verifies the downloaded source archives against the `.dsc`, the source
version and applied capture patch, exported Clutter functions, file hashes, exact
installed package versions, ELF search paths and direct dependency resolution. It
publishes `current` only after validation, preserves existing versions, and
refuses unmanaged pointer collisions. The manifest records `.dsc` and emitted
patch SHA-256 digests and hashes/sizes for every payload file. Static validation
does not establish successful GI/JavaScript startup; activation requires the full
startup probe below.

### Activate for the desktop user

Use SocketStream's **Set up exclusion** action or, from this repository in the
desktop user's session:

```sh
python3 security-lab/overlay_probe.py --exclusion-status
python3 security-lab/overlay_probe.py --setup-exclusion
```

The backend resolves GNOME Shell through the session D-Bus, checks its UID,
derives the service from its cgroup, and checks systemd's `MainPID`. On the tested
Ubuntu desktop the unit is **`org.gnome.Shell@ubuntu.service`**; `@wayland` is not
its instance. The managed drop-in wraps the detected service's original mode
argument and requires `Type=notify` with `NotifyAccess=main`. Setup runs an
isolated full-Shell startup probe, writes and reloads the override, checks its
effective command and lifecycle hooks, then arms its durable one-shot guard.
**Log out and log back in** to load the runtime. Setup does not restart the active
Wayland compositor.

Before each login, `launch-gnome-shell` validates the immutable runtime and exact
versions of `gnome-shell`, `gnome-shell-common`, `libmutter-18-0`, `mutter-common`,
`mutter-common-bin`, and `gir1.2-mutter-18`. Missing or incompatible files cause a
stock GNOME launch. The drop-in also falls back when the launcher disappears.
The launcher validates once per login and consumes the guard before same-PID
`exec`; its isolated Python bootstrap also falls back if the dispatcher is
missing or broken. Only a validated mode reaches stock, and inherited private
loader entries and the capture opt-in are removed before fallback.

`ExecStartPost` promotes `pending` to `ready` only when the invocation, boot,
PID/starttime, private executable, opt-in and all four private mappings agree.
`ExecStopPost` rearms only the same ready invocation with the exact tuple
`SERVICE_RESULT=success`, `EXIT_CODE=exited`, `EXIT_STATUS=0`. A crash, exit 1,
pre-readiness failure, or stale consumed attempt leaves subsequent logins on
stock until **Set up exclusion** passes again. A validation refusal before
consumption may leave the guard armed for a later retry.

Guard records live under `$XDG_CONFIG_HOME/socket_stream/mutter-startup/` (or
`~/.config/socket_stream/mutter-startup/`): a private `0700` directory, `0600`
record and stable sibling lock, bound to the immutable runtime and mode.

The default manifest is `/opt/socket_stream-mutter/current/runtime.json`.
`SOCKET_STREAM_MUTTER_RUNTIME` can select a different **absolute** manifest path;
the backend resolves it to the version directory before configuring activation.
Publishing a newer `current` does not retarget an existing immutable activation;
use setup again and start a fresh login.

Status distinguishes runtime availability, configuration, **runtime active**,
and **restart required**. Active requires the actual Shell's opt-in and all four
mapped libraries, with matching device/inode and manifest hashes. Missing,
mixed, deleted, or incompatible mappings cannot count as active. Pixel-level
`verified` remains false: use the real browser test in `../../security-lab/`.

### Removal and login recovery

As the same desktop user:

```sh
python3 security-lab/overlay_probe.py --remove-exclusion
```

Removal remains available after the runtime disappears or the session mode
changes. It durably revokes the guard locally, without executing runtime helpers,
before removing the managed override and reloading. A failed revocation is
reported, and rollback never restores a removed override whose guard may still
be armed. A fresh login switches a running custom compositor back to stock.

For a TTY recovery, remove the **reported managed file** and reload user services.
For the tested Ubuntu instance with the default XDG config directory:

```sh
rm -- "$HOME/.config/systemd/user/org.gnome.Shell@ubuntu.service.d/90-socket_stream-capture.conf"
systemctl --user daemon-reload
```

The runtime can remain on disk for diagnosis. Package reinstalls do not remove
it; an exact-version mismatch disables it until rebuilt and activated again.

### Startup and lifecycle checks

Run as the desktop user with the user-service manager available:

```sh
ROOT=$(readlink -f /opt/socket_stream-mutter/current)
python3 -I "$ROOT/startup_probe.py" --runtime "$ROOT" --mode ubuntu \
  --output /absolute/new/startup-evidence
python3 native/mutter-capture-filter/run_startup_guard_qa.py \
  --runtime "$ROOT" --output /absolute/new/lifecycle-evidence
```

The production probe uses a bounded disposable user service, private HOME/XDG
directories, and a private D-Bus with no service activation for both bus
addresses. Passing requires authenticated real-Shell readiness, three seconds
of continued liveness, coherent mappings, shutdown code 0, complete process
cleanup and removal of the disposable tree. It does not test physical displays,
the user's extensions, or capture pixels.

The lifecycle harness exercises the production guard with actual systemd
`Type=notify` services, `/proc`, and hook environment tuples. Its C executable
and four read-only mapped files are **synthetic fixtures**, not another
compositor build. Eight cases cover clean exit, pre-ready failure, post-ready
crash, ready exit 1 despite `SuccessExitStatus=1`, three stock-fallback invocations,
and disarm during a live attempt. See `VERIFICATION.md` for the real-device
results and the still-unmeasured normal desktop logout tuple.

## Rendering changes

* Match the exact XWayland `WM_CLASS` / native Wayland app ID
  `socket_stream_overlay`; transient children inherit the exclusion.
* Evaluate the actor predicate before cached window effects are painted; refresh
  cached ancestor effects on every capture paint and when returning to local painting.
* Mark actual screencast consumers as capture, preserving generic offscreen
  snapshots used by local Shell animations. Reset window-group capture clipping
  so hidden windows do not leave holes in the scene behind them. Disable
  persistent occlusion throttling under the compositor opt-in so surfaces behind
  an opaque panel continue receiving frame callbacks and scheduling damage.
* Rerender monitor and virtual-monitor streams rather than copying local
  framebuffer/direct-scanout pixels containing the panel.
* Clear excluded single-window CPU buffers and filter single-window framebuffer
  painting and image flattening.
* Preserve local snapshot pixels while tagging their texture content for omission
  when reused in capture passes.
* Preserve ordinary local actor rendering and window visibility. The environment
  opt-in controls the compositor predicate and rerender changes.

This affects captures routed through the patched compositor. Direct X11 window
reads, applications rendering their own content, hardware/driver capture, and
external cameras are different paths. Browser tab capture normally contains the
tab's own pixels, not desktop windows. No universal all-app guarantee is made.
GNOME Shell screenshot consumers and third-party effects with their own caches
require separate integration. Generic offscreen APIs remain unfiltered because
they also produce visible local animations. The exported
`clutter_stage_capture_view_into()` framebuffer-readback API is also unfiltered;
no ordinary screencast caller was found in the inspected tree. Rerendering and
keeping obscured surfaces active have performance costs that have not been measured.

## Verification

* Full patched Mutter 50.1 build with tests enabled: **passed on Ubuntu 26.04.1
  x86-64**, GCC 15.2.0 / Meson 1.10.1 / PipeWire 1.6.2.
* Python patcher: read-only preflight, idempotence, all-or-nothing failure, exact
  reverse application, incompatible-version rejection: **passed**.
* Native pixel and synthetic PipeWire results are recorded in `VERIFICATION.md`.
* Ubuntu schema-2 runtime `3b4e8e8a98dcb8a3`: **published**; full real-Shell
  startup probe and all eight real-systemd lifecycle cases **passed** on the
  Lenovo. The desktop remains on stock GNOME pending a coordinated login.
* Live GNOME activation, Chrome/Meet portal pixels, hardware DMA-BUF/direct
  scanout, fractional/multi-monitor scaling and KDE live capture: **pending**.

`socket_stream_capture.c` runs in Mutter's existing headless Clutter test harness.
It checks local/capture/local pixels, visibility preservation, cached window
and ancestor effects, same-mode predicate changes, and snapshot-only pixels with
an exclusion-disabled negative control. The external QA launcher uses private
D-Bus, runtime/config directories, PipeWire and a headless compositor. Synthetic
green control and magenta panel windows exercise XWayland and native Wayland
with the filter both off and on; monitor, area and focused-window frames and
pixel counts are retained in the requested new output directory. It has no
hardcoded desktop display, user home, or live-session credentials.

For the source transaction tests:

```sh
SOCKET_STREAM_MUTTER_SOURCE="$HOME/mutter-socket_stream" python3 -m unittest discover -s tests -p test_mutter_patch.py -v
```

## KDE alternative

`apps/socket_stream_desktop/ui/capture_rules.py` supports **KWin 6.7+ on Wayland**.
Use the UI's **Set up exclusion** action (or the probe's `--setup-exclusion`). It
registers the managed rule first in `[General] rules/count` (6.7) or `Order`
(6.8+), preserves other
rules, writes exact `wmclass` matching with `excludefromcapture=true` and force
rule `excludefromcapturerule=2`, and requests D-Bus reconfiguration. Installation
is reversible. KWin 6.6 lacks that persistent rule even though related capture
support exists. The rule applies to KWin output/region capture; explicitly
selecting the panel as a single-window stream is not covered by upstream KWin's
rule. Immutable/extended KConfig syntax is refused before backup or write.
Comma-containing IDs use both KConfig escaping layers. Legacy registries with
retired groups must first be migrated using KWin Window Rules settings; the
installer refuses to activate or delete those groups. Configuration tests pass;
a real KWin capture test is pending.
