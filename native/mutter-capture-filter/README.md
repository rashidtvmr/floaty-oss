# Custom Mutter 50.1 capture filter

An opt-in compositor source patch that keeps `socket_stream_overlay` visible on
the local desktop and removes its actor from supported capture renders.
This is a custom **Wayland compositor build**, not a kernel flag or GTK setting.

Target: upstream GNOME Mutter **50.1**, commit
`8e9bb806da4a6a4fc87aa4a481b228dba6735cbd` (libmutter API 18).
Ubuntu's downstream patches may need a separate port; other Mutter versions are
rejected. The running GNOME session is not modified by the patcher or QA tools.

## Build and activate

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

For the actual GNOME desktop, the GNOME Shell process must load **these patched
Mutter/Clutter libraries** and inherit `SOCKET_STREAM_CAPTURE_EXCLUDE=1` when it
starts. Setting this variable in SocketStream or Chrome cannot change an already
running compositor. Package the patched libraries using the distribution's
Mutter packaging and start a fresh matching GNOME Wayland session. Desktop
package replacement and session activation are separate deployment steps and
are not automated here. Unset the variable and start a fresh session to disable
the filter, or restore the distribution packages to remove it.

The app continues to report stock GNOME's limitation: an environment variable in
the client is not proof that the compositor has this patch. Only capture QA on
the deployed session can establish that.

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
* Deployed GNOME Shell, Chrome/Meet portal sharing, hardware DMA-BUF/direct
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
