# GNOME capture exclusion: implementation and deployment record

This document explains how SocketStream keeps its answer panel in the local
desktop scene while omitting it from supported Mutter screencast renders. It
also records the loader failure, recovery, and guarded deployment that made the
custom compositor start successfully on the Lenovo.

**Live result, 2026-11-15:** the installed application configured the runtime,
a normal logout/sign-in loaded the private GNOME Shell and all four matching
Mutter libraries, and the startup guard reached `ready`. The user then reported
**“its working finally”**. Machine-readable activation evidence is retained in
[`live-desktop-20261115`](../native/mutter-capture-filter/evidence/live-desktop-20261115/README.md).
That confirmation is recorded separately from automated pixel evidence: native
and headless capture tests passed; this live run did not retain a new encoded
received-frame comparison. The application's `verified` field therefore remains
`false`.

## 1. Exact deployed versions

| Component | Recorded value |
| --- | --- |
| Repository | [`rashidtvmr/floaty-oss`](https://github.com/rashidtvmr/floaty-oss) |
| Application source commit | `4c4eba9fe7769ea2e808438abf70bf53b02f90b1` |
| Host | `rashid-lenovo`, Ubuntu 26.04.1, x86_64, GNOME Wayland |
| GNOME Shell package | `50.1-0ubuntu1.5` |
| Mutter source/package | `50.1-0ubuntu2.5`, including Ubuntu's downstream patches |
| Private runtime | `/opt/socket_stream-mutter/50.1-0ubuntu2.5.socket-stream.3b4e8e8a98dcb8a3` |
| Installed application | `/opt/socket_stream` |
| Actual Shell service | `org.gnome.Shell@ubuntu.service` |
| Live Shell PID at verification | `125880` |
| Live invocation | `cd7151d47e314851b5539adf64b8d42a` |
| Release archive SHA-256 | `6cd60e591d70d376a4d2d8220adb2f422c533ffd3007d68012c3d2c3b35fc14e` |

The runtime suffix identifies a published runtime; it is not the application's
Git commit. Updating the Python/Rust application and updating the compositor are
separate operations.

## 2. Why the compositor is the implementation point

For the target whole-monitor sharing path, the data flow is:

```text
SocketStream GTK window / other application surfaces
                         |
                GNOME Shell + Mutter
                         |
              Clutter scene and window actors
                 /                       \
        ordinary local paint       capture-marked paint
                 |                       |
       local display output       filtered capture target
                                         |
                                      PipeWire
                                         |
                        portal-authorized Chrome capture
                                         |
                          browser video / WebRTC receiver
```

At the scene-rendering boundary, Mutter still knows which actor belongs to which
window. A completed framebuffer or encoded video no longer carries that window
identity. The implementation makes the decision before the capture image is
flattened.

Window stacking and capture exclusion solve different requirements:

- The GTK panel uses XWayland on GNOME, with the exact `WM_CLASS`
  `socket_stream_overlay` and the managed `_NET_WM_WINDOW_TYPE_DOCK` hint. This
  gives it the intended local stacking behavior above fullscreen applications.
- Skip-taskbar/pager, opacity and focus hints control desktop presentation.
- The custom Mutter predicate determines whether that actor is painted into a
  supported capture target.

The panel remains mapped. Focus changes or detection of a ScreenCast session do
not hide it. The bounded OSS/REA investigation found no surveyed project that
provided this GNOME/Mutter whole-screen mechanism; see the
[`source-verified survey`](research/linux-screen-exclusion-2026-11-15.md) for
pinned projects, evidence types, and platform-specific content-protection limits.

## 3. Capture-only rendering

The source transformation is
[`patch_mutter.py`](../native/mutter-capture-filter/patch_mutter.py). It checks
every expected source anchor before applying the generated diff. The upstream
reference is Mutter 50.1, commit
`8e9bb806da4a6a4fc87aa4a481b228dba6735cbd`; the desktop build uses the exact Ubuntu
source instead of assuming an upstream version number establishes compatibility.

### 3.1 Explicit paint context

The patch adds:

```c
CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE = 1 << 3
```

It also adds an instance-private callback and setter:

```c
void clutter_actor_set_capture_filter (
    ClutterActor *actor,
    gboolean (*filter) (ClutterActor *actor));
```

The callback is stored in `ClutterActorPrivate`, avoiding a new virtual method in
`ClutterActorClass`. At the beginning of actor painting, before cached effects or
paint nodes can contribute pixels, the logic is equivalent to:

```text
if this is a capture paint and the actor's filter says exclude:
    omit this actor from this paint
else:
    continue ordinary painting
```

Ordinary local painting does not carry the capture flag. Generic offscreen
rendering is also kept distinct because Shell uses it for visible local
transitions and animations.

### 3.2 Window identity and transient children

Each `MetaWindowActor` receives a predicate. It first requires the compositor's
environment variable to be exactly:

```text
SOCKET_STREAM_CAPTURE_EXCLUDE=1
```

It then walks the window and its transient-parent chain, bounded to 64 steps,
looking for `meta_window_get_wm_class(window) == "socket_stream_overlay"`. Mutter
exposes the native Wayland app ID through this identity accessor too. This
allows related transient windows to inherit the exclusion without matching
titles or process names.

The variable belongs to the GNOME Shell process at startup. Setting it in
SocketStream or Chrome cannot change the already-running compositor. Likewise,
the GTK **Stealth overlay** checkbox changes window hints; it is not a switch
for this compositor-wide opt-in and identity predicate.

### 3.3 Capture entry points and framebuffer shortcuts

The patch marks the actual monitor, area, virtual-monitor and relevant
single-window capture paints. Two important paths need additional handling:

1. **Monitor/virtual-monitor framebuffer copies.** A copy of the display buffer
   already contains the locally visible panel. Under the opt-in, the capture
   path rerenders the scene into its capture target instead of using that copy
   shortcut. Virtual streams preserve their requested cursor mode.
2. **Direct single-window capture.** A CPU/SHM buffer path can bypass normal actor
   painting, so an explicitly excluded window's output buffer is cleared.
   Framebuffer/DMA-BUF window painting receives the capture flag, and image
   flattening does not take the unfiltered direct-texture shortcut for an
   excluded actor.

This rerouting concerns capture. It is not a claim that all local display direct
scanout or every hardware capture mechanism has been disabled.

### 3.4 Cache correctness

Omitting an actor once is insufficient when another object holds its pixels:

- **Window effects:** evaluate the filter before the actor's cached effect is
  painted.
- **Ancestor offscreen effects:** track whether the cache belongs to capture or
  local painting. Refresh when switching contexts and on every capture paint.
  The latter also handles changed window identity/ancestry between consecutive
  capture paints without an intervening local paint.
- **Snapshot textures:** carry an `exclude_from_capture` property on
  `ClutterTextureContent`. A snapshot can remain visible locally when another
  actor reuses it, yet be omitted when that content is painted for capture.
  Stage snapshots made under the opt-in are marked conservatively; window-image
  snapshots use the window predicate.

The native regression includes a snapshot-only case: it hides the original
actor, checks the reused snapshot's local/capture behavior, then disables
snapshot exclusion as a negative control. That prevents a test from accidentally
passing by observing only the original live actor.

### 3.5 Reconstructing the scene behind the panel

An opaque local panel normally lets the compositor skip obscured content. If
capture simply omitted the panel but reused local occlusion state, the shared
image could contain a hole or a frozen background.

The patch therefore:

- clears the window group's capture redraw clipping under the opt-in;
- removes persistent unobscured-region culling for the normal and top window
  groups while the opt-in is enabled, so covered surfaces keep receiving frame
  callbacks and scheduling damage;
- rerenders the capture scene with the excluded actor absent.

The expected monitor/area result is the live scene behind the panel. This costs
additional rendering and keeps otherwise-obscured surfaces active. GPU time,
power consumption and multi-monitor overhead have not been benchmarked.

## 4. The login failure and the loader fix

The initial desktop activation failed during GNOME Shell's JavaScript/GObject
Introspection startup, reporting duplicate registration of `ClutterActor` /
`ClutterAnimatable`. The desktop was recovered to stock GNOME and the failure was
reproduced in an isolated full Shell.

Static version checks and `ldd` had passed. Those checks exercised direct native
dependency resolution, but did not execute the later GI/JavaScript imports.
Successful direct resolution was therefore too weak a startup gate.

The working runtime controls both native and GI lookup:

```text
versioned runtime/
  runtime.json                 # schema-2 manifest
  gnome-shell                  # copy of the installed Shell executable
  launch-gnome-shell
  validate_runtime.py
  startup_probe.py
  startup_guard.py
  lib/                         # four matching private Mutter DSOs
  typelib/                     # matching typelibs and library aliases
  ...source provenance...
```

The required ELF search paths are:

| Payload | Required dynamic search path |
| --- | --- |
| Each private Mutter DSO | `DT_RPATH=$ORIGIN` |
| Copied GNOME Shell executable | `DT_RUNPATH=$ORIGIN/lib:$ORIGIN/typelib:/usr/lib/gnome-shell` |

Mutter's normal installation RPATH can take precedence over `LD_LIBRARY_PATH`.
Merely prepending a private library directory was insufficient to establish a
coherent family. The private DSO RPATHs, copied Shell RUNPATH, matching typelibs,
and aliases make late GI loads resolve within the same runtime layout. The
validator checks these ELF contracts before dependency resolution.

The published runtime contains all four families together: `libmutter`,
`libmutter-clutter`, `libmutter-cogl`, and `libmutter-mtk`. A live status check
requires the expected mapped files by device/inode as well as validated manifest
hashes. One private library, a private path string, or a successful `ldd` command
is not sufficient evidence.

The package-managed files in `/usr` remain available for stock fallback. The
deployment changes private copies under `/opt/socket_stream-mutter`.

## 5. Building and publishing the runtime

[`stage_runtime.py`](../native/mutter-capture-filter/stage_runtime.py) stages a
private `DESTDIR` install built from the installed distribution source. The
complete tested build commands are in the
[`native README`](../native/mutter-capture-filter/README.md#ubuntu-runtime-deployment).
The sequence is:

1. Obtain the exact installed Mutter source version with its Ubuntu patches.
2. Generate and retain `capture.patch`; validate/apply it to the isolated source.
3. Build with tests enabled and run native plus headless capture checks.
4. Install into a private `DESTDIR` with `meson install --no-rebuild`.
5. Stage the DSOs, typelibs, copied Shell, launch helpers and provenance.
6. Validate source-archive digests against the `.dsc`, package pins, required
   exports, payload hashes/sizes, ownership, layout and loader resolution.
7. Publish an immutable version directory, then update the managed `current`
   pointer only after validation succeeds.

Recorded provenance:

```text
34db0d1aa96e0ee6c3c5bd906b14fb9ed243e0ed3ff3c01f16c0952f5907dad7  source.dsc
e4daf6e0d5b02499b8373aece210db9db9f3ef2c7d1b765abbd02fe8b922cdd3  capture.patch
```

The default discovery path is
`/opt/socket_stream-mutter/current/runtime.json`. Setup resolves that pointer to
an immutable version directory and embeds that directory in activation. Changing
`current` later does not silently retarget an existing activation.

Package pins cover `gnome-shell`, `gnome-shell-common`, `libmutter-18-0`,
`mutter-common`, `mutter-common-bin`, and `gir1.2-mutter-18`. A distribution update
that breaks those pins causes stock fallback until a matching runtime is built
and explicitly configured.

## 6. A real startup probe before activation

[`startup_probe.py`](../native/mutter-capture-filter/startup_probe.py) runs the
actual copied GNOME Shell, including GI/JavaScript initialization, inside a
disposable user service. It uses:

- disposable HOME and XDG runtime/config/cache/data/state directories;
- a private D-Bus with no service activation, used for both bus addresses;
- an authenticated readiness socket that accepts `READY=1` only from the
  expected Shell process's kernel credentials;
- bounded process ownership and shutdown/cleanup checks;
- `MemoryMax=1G`, `TasksMax=512`, `RuntimeMaxSec=50s`, and
  `KillMode=control-group` on the outer service.

A pass requires the private executable, all four coherent mappings, real
readiness, at least three seconds of continued liveness, shutdown code 0, and
complete cleanup of processes and disposable storage. A D-Bus daemon's readiness
message cannot substitute for the Shell's readiness.

The retained production probe passed with **3.021 seconds** of stable liveness.
It checks startup in isolation; physical displays, user extensions and captured
pixels are checked separately. Running the heavy probe directly inside the
Nomad runner's 128 MB allocation had failed, so it runs in the separately sized
user service.

## 7. One-shot startup guard and same-PID execution

The startup gate is intentionally durable. An in-memory flag would disappear
precisely when a crashing compositor needs to be prevented from retrying.

### State transitions

```text
explicit setup + successful probe + verified effective unit
                         |
                       armed
                         |
                 validate and consume
                         |
                      pending ---- failure / stale attempt ----> blocked
                         |
       main-process READY + exact identity + four mappings
                         |
                       ready ----- crash / exit 1 -------------> blocked
                         |
       same invocation + (success, exited, 0)
                         |
                       armed

explicit removal / disarm -------------------------------------> blocked
```

State is bound to the immutable runtime and Shell mode. Consumed records also
bind the systemd invocation ID, boot ID, PID, and `/proc` start time. PID reuse
alone cannot satisfy that identity. The private state directory is `0700`, the
record is `0600`, and a stable sibling lock coordinates durable updates.

### Why the PID must stay the same

The launcher validates once per login, consumes the armed record before launch,
then uses `exec` to replace itself with the private Shell. This retains the PID
that systemd identifies as `MainPID`.

The service must use `Type=notify` and `NotifyAccess=main`. Once Shell sends
readiness, `ExecStartPost` verifies the matching invocation, executable,
environment opt-in, and all four mappings before promoting `pending` to `ready`.
A wrapper that forked the real Shell could break this readiness/identity contract.

### Stock fallback

The launcher and the drop-in's bootstrap handle missing or invalid runtime
components. The isolated Python `-I` / `runpy` bootstrap also catches a missing or
broken dispatcher and execs stock Shell in the same PID. Internal guard flags
and malformed modes are never forwarded to stock. Private loader/typelib entries
and the capture opt-in are removed for fallback.

A consumed failed attempt stays blocked. A later stock invocation exiting 0
does not rearm the failed custom invocation. Explicit setup, including a fresh
successful startup probe, is required. A validation refusal before consumption
can leave an existing armed record available for a later valid attempt.

### Clean exit is stricter than systemd's `Result=success`

The real Ubuntu unit has `SuccessExitStatus=1`. The real-systemd harness showed
that a ready process exiting 1 can produce `(success, exited, 1)`. The guard
therefore rearms only for the exact tuple `(success, exited, 0)` from the same
ready invocation. Disarming while that invocation is alive also prevents its
eventual clean exit from rearming it.

In the live transition, the **stock** desktop being replaced exited with
`(success, exited, 0)`. The custom desktop was subsequently left running; a
normal logout/rearm of that custom invocation is not part of the retained live
evidence.

## 8. Application integration and transaction ordering

The GTK UI delegates to
[`capture_rules.py`](../apps/socket_stream_desktop/ui/capture_rules.py) and
[`mutter_capture.py`](../apps/socket_stream_desktop/ui/mutter_capture.py).
Setup runs on a worker thread so the full startup probe does not block GTK.

The backend discovers the live Shell through session D-Bus, checks its UID,
derives the actual service from its cgroup, and cross-checks systemd `MainPID`.
On this machine the instance is `@ubuntu`, not an assumed `@wayland` instance.

The managed activation is:

```text
~/.config/systemd/user/org.gnome.Shell@ubuntu.service.d/90-socket_stream-capture.conf
```

Setup validates the runtime and service, passes the probe, prepares/revokes guard
state as necessary, atomically writes the managed override, reloads systemd,
verifies the effective command and lifecycle hooks, then arms the guard. The
override pins the immutable runtime and original validated Shell mode. The app
does not restart the user's desktop.

Removal first performs local, locked, durable guard revocation, then removes the
managed override and reloads. It remains available when the runtime helpers are
missing. Transaction recovery reports every failed revocation and does not
restore an already-removed override whose guard might still be armed. Directory
ownership, permissions, symlink handling and expected file contents are checked
before replacing managed configuration.

### Status vocabulary

| Field | Meaning |
| --- | --- |
| `deployed` | A compatible private runtime is available on disk. |
| `configured` | Managed activation is present for the intended Shell service. |
| `config_validated` | Runtime/configuration compatibility and effective unit checks pass. |
| `startup_guard` | Persistent startup state, such as `armed`, `ready`, or `blocked`. |
| `runtime_active` | The actual running Shell has the validated runtime mappings and opt-in. |
| `restart_required` | The running compositor and intended activation need a new login to agree. |
| `verified` | Capture verification is separate; runtime activation does not set this true. |

Before logout, the installed app reported `configured=true`, `armed`,
`runtime_active=false`, `restart_required=true`. After login it reported
`config_validated=true`, `ready`, `runtime_active=true`, `restart_required=false`.

## 9. Installed application, speech bundle and launchers

The release was built from commit `4c4eba9`, uploaded, checksum-verified, and
extracted into a new staging directory. Eleven packaged UI/reference-page source
files were compared with the commit. The CLI and Moonshine executable were
verified as Linux ELF files.

The bundled **Moonshine 0.1.6 English TINY_STREAMING** helper passed `--check` as
the desktop user in a separately sized service: it emitted `ready`, exited 0,
and peaked at **166.2 MB**. The bundle was reused; the compositor fix did not
replace the speech implementation or model.

The old application directory was retained at:

```text
/opt/socket_stream-releases/a74adaa-installed-20261115-215444
```

The new payload was moved into `/opt/socket_stream` with restoration of the old
directory if the publication rename failed. User configuration stayed outside
the installation. The pre-install snapshot of the inspected configuration
directories was empty; no claim of migrating existing provider keys is made.

New menu/Desktop launchers execute the absolute
`/opt/socket_stream/socket_stream` path. This matters because the portable
launcher derives its root from its own path and exports the matching application
home and CLI. Legacy ContextLens shortcuts were preserved.

Saved provider settings, including an API key when saved, live in
`~/.config/socket_stream/provider-settings.json` with mode `0600`. They are not
part of the `/opt` directory swap. Diagnostics retain only configuration
path/hash metadata. The live app's checked overrides pointed to the installed
home and CLI, with no development-root redirect or preview-demo override.

## 10. Reproduce activation and inspect the live result

Run application/setup commands as the desktop user with the live user bus and
XDG runtime directory available:

```sh
cat /opt/socket_stream/SOURCE-COMMIT.txt
/opt/socket_stream/socket_stream doctor
python3 /opt/socket_stream/security-lab/overlay_probe.py --exclusion-status
python3 /opt/socket_stream/security-lab/overlay_probe.py --setup-exclusion
```

The UI's **Set up exclusion** invokes the same backend. After successful setup,
perform a normal **Log Out → sign in**. Then inspect:

```sh
python3 /opt/socket_stream/apps/socket_stream_desktop/ui/capture_rules.py status --json

# This is the recorded Ubuntu instance; use the backend-discovered instance
# when deploying on a different GNOME session mode.
UNIT=org.gnome.Shell@ubuntu.service
systemctl --user show "$UNIT" -p MainPID -p Type -p NotifyAccess -p DropInPaths
PID=$(systemctl --user show "$UNIT" -p MainPID --value)
readlink "/proc/$PID/exe"
awk '/\/libmutter/ {print $6}' "/proc/$PID/maps" | sort -u
```

Expected for this deployment: the copied `gnome-shell` executable and all four
Mutter mappings come from the same immutable runtime. Use the installed backend
for the stronger hash/inode, opt-in and guard checks rather than treating this
human-readable path listing as equivalent validation.

### The exact browser reference test

Serve the installed release on loopback:

```sh
python3 -m http.server 18765 --bind 127.0.0.1 --directory /opt/socket_stream
```

Open **http://127.0.0.1:18765/security-lab/screen-share-test.html**, start the
labelled probe with `python3 /opt/socket_stream/security-lab/overlay_probe.py`,
and choose the entire monitor in Chrome's normal source chooser.

For a same-stream positive control on this compositor, use a clearly identified
QA window with a nonmatching class, capture its visible marker, then restore its
`socket_stream_overlay` class without hiding the window. On XWayland,
`xdotool set_window --class CLASS WINDOW_ID` can change that QA window's class.
Confirm the selected XID belongs to the labelled probe. Changing the compositor
opt-in through a logout would create a new stream and would not satisfy the
reference page's same-stream comparison.

Keep the panel visible locally, capture the protected frame, move the panel, and
repeat over an animated background. Retain the positive control, monitor
selection, frame dimensions, and timestamps. The page labels a missing marker a
**candidate**, not a universal guarantee. Independent received-video/recording
evidence should additionally establish that encoded frames remain live and that
the scene behind the panel continues updating.

## 11. Verification layers and recorded outcome

| Layer | Result and scope |
| --- | --- |
| Rust gates | Formatting, Clippy with warnings denied, and 144 tests passed before this documentation update. Rust source was unchanged by the startup work. |
| Full Python suite | 500 tests passed with six skips on macOS. |
| Linux capture/startup suites | 343 tests passed with four source-fixture skips. |
| Native compositor checks | Four executables passed, including capture/cache/snapshot behavior. |
| Synthetic capture matrix | Twelve XWayland/native-Wayland, opt-in off/on, monitor/area/window cases passed. Disabled controls contained 54,000 magenta pixels; enabled captures contained none. |
| Animated background | Sixty frames per backend passed, with changing background pixels and no magenta panel pixels. |
| Isolated real-Shell startup | Passed actual readiness, 3.021 s stable liveness, four mappings, exit 0 and cleanup. |
| Real-systemd guard lifecycle | Eight cases passed, including pre-ready failure, SIGSEGV, ready exit 1, stock fallback and live disarm. |
| Installed GTK application | Real packaged Replay demo and Set up exclusion actions exercised through AT-SPI; actual answer panel present. |
| Normal desktop transition | Stock invocation exited 0; private Shell PID 125880 reached active/running with a matching ready guard. |
| User confirmation | User reported the live result working and requested this technical record. |
| Automated live receiver | Chrome 155 opened the reference page, then closed before the scripted receiver attached. No new encoded frame comparison was retained. |
| Automated drag attempt | Two XTest attempts observed no movement; this run does not independently establish drag behavior. |

See [`VERIFICATION.md`](../native/mutter-capture-filter/VERIFICATION.md) and the
linked evidence directories for the underlying records and independent-review
dispositions. The application remains accurate about the difference between
runtime activation and separately observed capture behavior.

## 12. Removal, recovery and updates

For normal removal, use **Remove rule** or:

```sh
python3 /opt/socket_stream/security-lab/overlay_probe.py --remove-exclusion
```

This revokes startup state and removes the managed activation. A fresh login
starts stock Shell; the current compositor process keeps its existing runtime
until it exits. The versioned runtime and application rollback copy can remain
on disk.

If a graphical login cannot be reached, use a TTY and the same user's session
configuration. The backend is preferred because it also revokes the guard. If
the application itself is unavailable, move the specific managed override out
of the service drop-in directory and run `systemctl --user daemon-reload` before
the next login. Do not restore an old override or manually arm a record without
passing the current setup/probe checks.

For an update, rebuild against the newly installed distribution package set,
publish a new immutable runtime, run setup to pin it, and perform a new login.
Retain the previous runtime and installation until the new deployment is
validated. An application-only update can reuse an unchanged validated runtime.

## 13. Coverage and engineering boundaries

The mechanism applies to capture routes through the patched Mutter renderer.
Direct X11 window reads, a program rendering its own content, hardware/driver
capture, and external cameras are separate paths. Browser tab capture normally
uses the tab's own content rather than the desktop scene.

Generic Shell screenshot consumers, `clutter_stage_capture_view_into()`, and
third-party effects with independent caches require separate integration.
Physical multi-monitor/fractional-scaling behavior, real Shell transitions,
hardware capture variants, performance, and a subsequent clean logout of the
live custom desktop remain outside this run's retained evidence. Other Mutter
versions require a port and validation; KDE/niri/Hyprland use their own supported
configuration mechanisms.

The app keeps its normal permission-based accessibility-first capture workflow,
explicit OCR fallback, bundled English speech model, provider configuration,
and identifiable process/window. No kernel module or post-encoding pixel repair
is part of this implementation.
