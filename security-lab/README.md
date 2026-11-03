# Capture boundary lab — QA runbook

This directory is a **manual security-QA harness**, not a product feature.
It helps a human verify, on a real Linux desktop, how the ContextLens floating
overlay behaves with respect to an explicitly approved screen share. Nothing
here starts automatically, reads the screen, or uploads anything.

The frozen interface and honest capability matrix live in
[`../docs/STEALTH-OVERLAY.md`](../docs/STEALTH-OVERLAY.md) (sections 2 and 3.1).
Read that first.

## What is in here

| File | Purpose |
|---|---|
| `serve.py` / `web/` | Local, loopback-only browser lab. Uses `getDisplayMedia` to show exactly what an approved browser capture includes. |
| `probe_overlay.py` | Clearly visible control marker (no stealth). |
| `overlay_probe.py` | Applies the real `ExclusionPlan` from `overlay_guard` and runs the behavioural guard. |
| `run.sh` | Launcher: `./run.sh` serves the browser lab; `./run.sh probe` runs `overlay_probe.py`. |

## Prerequisites

- Linux desktop (X11 or Wayland).
- Python 3.9+.
- GTK3 bindings: `python3-gi` + `gir1.2-gtk-3.0` (Debian/Ubuntu) or
  `python3-gobject` + `gtk3` (Fedora/Arch).
- The frozen `overlay_guard` module at
  `apps/context-desktop/ui/overlay_guard.py`. `overlay_probe.py` prints a clear
  message and exits if it is missing — that is expected until the overlay
  implementation lands.

## How to run

Browser capture lab (default):

```sh
./run.sh                 # http://127.0.0.1:8765
./run.sh --port 9000     # any serve.py flag still works
```

Stealth overlay probe:

```sh
python3 overlay_probe.py               # guard enabled (the real behaviour)
python3 overlay_probe.py --no-stealth  # A/B control: guard disabled
```

`./run.sh probe [--no-stealth]` is an equivalent shortcut.

The visible control marker (no stealth at all) is still available:

```sh
python3 probe_overlay.py
```

## Step-by-step procedure

Run one trial per share mode. Keep the browser lab open on the same desktop.

1. Start the browser lab: `./run.sh`, then open `http://127.0.0.1:8765`.
2. Start the overlay probe on the same desktop: `python3 overlay_probe.py`.
   Read the **Capability report** panel and note the session, compositor and
   whether exclusion is reported as guaranteed (it will say **NO** on stock
   Linux X11/Wayland).
3. In the browser lab, click **Start screen-share test** and choose a source:
   - **Tab** share,
   - **Application window** share, then pick the probe window,
   - **Entire monitor** share.
4. Compare the two views:
   - the probe window as you see it locally, and
   - the browser's live **captured preview**.
5. Record the observation in the browser lab form (test case, seen locally,
   in capture, notes) and click **Record observation**. Use the table below as
   the offline template if you prefer paper.
6. Repeat for each share mode, and repeat once with `--no-stealth` as the A/B
   control. With the guard enabled, the probe should report
   *"Screen share: detected — hiding"* and hide itself while the share is
   active; with `--no-stealth` it should stay visible and appear in the capture.
7. Leave the share (click **Stop**). The probe should report
   *"Screen share: none detected"* and restore the marker.

## Interpreting results

- **Marker visible locally = yes, in capture = yes** → the capture surface
  includes the window; window hints did not exclude it (expected for
  full-screen capture and for most portal window pickers).
- **Marker visible locally = yes, in capture = no** → the selected capture
  surface does not include this window. This is a statement about *that*
  capture surface only.
- **Guard enabled, marker disappears while sharing** → the behavioural guard is
  working. This is the only mechanism that reliably keeps the panel out of a
  full-screen capture.

## What a passing result does NOT prove

Be precise when you write this up. A "not in capture" result does **not** mean
the overlay is invisible in general.

- **Linux X11 cannot be excluded from a full-screen `XGetImage` capture.** Any
  process with an X11 connection and permission to read the root window sees
  every mapped window, including override-redirect ones. Window hints
  (`_NET_WM_STATE_SKIP_TASKBAR`, `_NET_WM_WINDOW_TYPE_NOTIFICATION`,
  override-redirect, and even `_NET_WM_STATE_SKIP_CAPTURE` where a compositor
  exposes it) are **best effort** and compositor-dependent.
- **Wayland portal screencast captures whole outputs.** The `wlr-layer-shell`
  overlay layer does not remove the surface from an output capture. The portal
  lists toplevels to the picker; a compositor rule (for example niri's
  `block-out-from=screencast`) is required to exclude it, and only after the
  user adds that rule.
- **A missing marker only proves the scope of the selected capture surface.**
  It says nothing about native clients, other capture tools, or a different
  share mode.
- **The only reliable mitigation is detection-based auto-hide.** The guard is a
  behavioural mechanism, not a protocol guarantee: it depends on the share
  being detectable (`ScreenShareWatcher` reports `checked: false` when it
  cannot tell, and then nothing is hidden).

Do not claim universal invisibility anywhere, in code comments or in reports.

## Results table template

Copy this into your report and fill one row per trial.

| Session | Compositor | Share mode | Marker visible locally | Marker in capture | Mechanism used | Notes |
|---|---|---|---|---|---|---|
| x11 | x11 | entire screen | yes | yes | hints only | expected: X11 full-screen capture sees all windows |
| x11 | x11 | window | yes | no | override-redirect + skip-taskbar | picker did not list the window |
| wayland | gnome | entire screen | yes | yes | layer-shell (n/a here) | portal captures whole output |
| wayland | gnome | window | yes | no | notification type | toplevel not offered for this source |
| wayland | niri | entire screen | yes | no | compositor block-out rule | only after user adds the rule |
| any | any | any | yes → hidden | no | behavioural auto-hide | guard detected the share and hid the marker |

## Hard rules for this lab

- No evasion of OS permission prompts.
- No process masquerading.
- No hidden capture.
- No third-party dependencies.
- No network access.
- Honest labelling everywhere: this is a QA marker, not an answer panel.
