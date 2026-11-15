# Capture boundary lab — QA runbook

This directory is a **manual security-QA harness**, not a product feature.
It helps a human verify, on a real Linux desktop, how the SocketStream floating
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
| `overlay_probe.py` | Applies the real `ExclusionPlan` from `overlay_guard` and reports share status; the marker stays visible. |
| `screen-share-test.html` | Entire-monitor preview, marker comparison and explicit local evidence exports. |
| `demo_answer_panel.py` | Actual answer-panel class with synthetic Q&A and a color marker. |
| `run.sh` | Launcher: `./run.sh` serves the browser lab; `./run.sh probe` runs `overlay_probe.py`. |

## Prerequisites

- Linux desktop (X11 or Wayland).
- Python 3.9+.
- GTK3 bindings: `python3-gi` + `gir1.2-gtk-3.0` (Debian/Ubuntu) or
  `python3-gobject` + `gtk3` (Fedora/Arch).
- The frozen `overlay_guard` module at
  `apps/socket_stream_desktop/ui/overlay_guard.py`. `overlay_probe.py` prints a clear
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
python3 overlay_probe.py               # apply hints; marker never hides
python3 overlay_probe.py --no-stealth  # A/B control: no hints
python3 overlay_probe.py --exclusion-status   # rule status, then exit
python3 overlay_probe.py --setup-exclusion    # install the managed rule
python3 overlay_probe.py --remove-exclusion   # remove the managed rule
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
   Read the **Capability report** panel and note the session, compositor, the
   capture-rule state (supported/configured/config-validated) and that runtime
   verification is always reported separately. A rule is only "configured" if
   you explicitly ran `--setup-exclusion` or used the app's Set up exclusion.
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
   control for window hints. The probe stays visible in both modes; note whether
   the selected capture surface includes it.
7. Leave the share (click **Stop**). The probe should report
   *"Screen share: none detected"*; no visibility change is expected.

## Interpreting results

- **Marker visible locally = yes, in capture = yes** → the capture surface
  includes the window; window hints did not exclude it (expected for
  full-screen capture and for most portal window pickers).
- **Marker visible locally = yes, in capture = no** → the selected capture
  surface does not include this window. This is a statement about *that*
  capture surface only.
- The app no longer auto-hides the panel during a share. A visible marker in
  full-screen capture is expected unless a compositor exclusion rule applies.

## What a passing result does NOT prove

Be precise when you write this up. A "not in capture" result does **not** mean
the overlay is invisible in general.

- **Linux X11 cannot be excluded from a full-screen `XGetImage` capture.** Any
  process with an X11 connection and permission to read the root window sees
  every mapped window, including override-redirect ones. Window hints
  (`_NET_WM_STATE_SKIP_TASKBAR`, `_NET_WM_WINDOW_TYPE_NOTIFICATION`,
  override-redirect, and even `_NET_WM_STATE_SKIP_CAPTURE` where a compositor
  exposes it) are **best effort** and compositor-dependent.
- **Wayland portal screencast captures whole outputs.** GTK3 creates a normal
  toplevel, not a layer-shell surface, and the portal lists toplevels to the
  picker. A documented compositor rule is required to exclude it: niri
  `block-out-from "screen-capture"`, Hyprland 0.53+ `no_screen_share = true`, or
  KDE Plasma/KWin 6.7+ `excludefromcapture`. Stock GNOME Shell 50/mutter 18, X11 and sway
  have no such rule.
- **A missing marker only proves the scope of the selected capture surface.**
  It says nothing about native clients, other capture tools, or a different
  share mode.
- **A configured rule is not runtime proof.** `config_validated` records a
  configuration check. GNOME additionally reports runtime deployment, live
  activation and whether a fresh login is needed. `verified` stays false without
  separate capture evidence. Confirm exclusion in the browser's video before
  recording a "no" in the capture column.

The custom GNOME runtime is documented in
[`../native/mutter-capture-filter/README.md`](../native/mutter-capture-filter/README.md).
Its activation takes a full desktop logout/login, which ends an existing share.
Do not label frames from different sessions a same-stream A/B comparison. For an
already active filter, use a distinct, ordinary control window to establish that
the chosen monitor and live capture work, then compare with the locally visible
answer panel and moving content underneath it. `--no-stealth` only disables
client-side hints; it does not disable an installed compositor identity rule.

Do not claim universal invisibility anywhere, in code comments or in reports.

## Results table template

Copy this into your report and fill one row per trial.

| Session | Compositor | Share mode | Marker visible locally | Marker in capture | Mechanism used | Notes |
|---|---|---|---|---|---|---|
| x11 | x11 | entire screen | yes | yes | hints only | expected: X11 full-screen capture sees all windows |
| x11 | x11 | window | yes | no | override-redirect + skip-taskbar | picker did not list the window |
| wayland | gnome | entire screen | yes | yes | toplevel hints only | GNOME 50 has no per-window rule |
| wayland | gnome | window | yes | no | toplevel not selected | only because it is not the shared window |
| wayland | niri | entire screen | yes | no | compositor block-out rule | after `--setup-exclusion` and a real share |
| wayland | hyprland | entire screen | yes | no | `no_screen_share` rule | after `--setup-exclusion` and a real share |
| wayland | kde | entire screen | yes | no | `excludefromcapture` rule | KWin 6.7+, after `--setup-exclusion` and a real share |
| any | any | any | yes | yes | panel remains visible | no auto-hide on any share state |

## Hard rules for this lab

- No evasion of OS permission prompts.
- No process masquerading.
- No hidden capture.
- No third-party dependencies.
- No network access.
- Honest labelling: distinguish the standalone marker from tests using the actual
  installed answer-panel class, and retain the tested runtime/app versions.
