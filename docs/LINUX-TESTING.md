# SocketStream Linux release: installation and validation

This archive includes a statically linked Linux Rust CLI, a GTK3 desktop shell,
accessible-text reading via AT-SPI2, Tesseract OCR, and the XDG ScreenCast/PipeWire
capture helper. It is a practice assistant for authorized screen/audio capture,
not an exam-proctoring workaround. The package does not conceal itself or bypass
operating-system permissions.

## Extract and check

Unpack the archive using:

    tar -xzf socket_stream_linux-x86_64-*.tar.gz
    cd socket_stream_linux-x86_64-*
    ./check-dependencies.sh

On Ubuntu or Debian, review and deliberately run the optional installer:

    ./install-dependencies-debian.sh --yes
    ./check-dependencies.sh

The installer uses apt and sudo. Fedora/Arch users should install the equivalent
GTK3, AT-SPI2, DBus, GStreamer/PipeWire, Tesseract, ffmpeg, and PulseAudio tools.

## Start with a no-cloud smoke test

    file bin/socket_stream
    ./socket_stream doctor
    ./socket_stream analyze-file --file examples/sample-question.txt --provider mock
    ./socket_stream ocr --file tests/fixtures/sample-code.png

The file must report a Linux x86-64 ELF executable for the x86_64 package.
The mock model does not send data to any API provider.

### Measured install record (2026-11-15)

`socket_stream_linux-x86_64-20261115.tar.gz` (SHA-256
`2ea895eb15fea7f5284aea66478dcaf9a18c2e1d6c9ffceb5389ace6c3ecba77`) was
installed over a Nomad raw_exec runner on Ubuntu 26.04.1 x86_64 (glibc 2.43,
Python 3.14.4) at `/opt/socket_stream`. Verified on that host: remote checksum
match, `check-dependencies.sh` all-OK, statically linked x86-64 ELF
`bin/socket_stream`, `doctor` reporting tesseract/ffmpeg and the bundled English
speech model available, mock `analyze-file`, sample OCR, Moonshine runtime
`--check` reporting `ready`, GTK 3.24 import via python3-gi 3.56.2, and the
`capture_rules`/`overlay_guard` unit suites (35 + 30 tests) passing. A headless
runner cannot exercise the desktop GUI, real capture permissions, or a live
screencast; those remain manual on-desktop steps.

## Remote Linux checks using HashiCorp Nomad

Two small, explicit Nomad jobs in `scripts/` let an operator test the Linux
capture-exclusion rules without changing the host compositor:

1. `nomad job run scripts/nomad-linux-desktop-diagnostic.nomad.hcl` runs a
   read-only host session inspection on the Linux client. Get its allocation ID
   with `nomad job status socket_stream_linux-desktop-diagnostic` and inspect
   `nomad alloc logs ALLOC_ID session`.
2. `nomad job run -detach scripts/nomad-linux-test-runner.nomad.hcl` creates a
   short-lived allocation. Get its ID with
   `nomad job status socket_stream_linux-test-runner`, then from the project root
   transfer just the standard-library capture modules and unit tests:

   ```sh
   tar -cf - apps/socket_stream_desktop/ui/capture_rules.py \
     apps/socket_stream_desktop/ui/overlay_guard.py \
     tests/test_capture_rules.py tests/test_overlay_guard.py | \
     nomad alloc exec -i -task runner ALLOC_ID /bin/bash -lc \
     'mkdir -p /tmp/socket_stream_nomad-check; tar -xf - -C /tmp/socket_stream_nomad-check; cd /tmp/socket_stream_nomad-check; python3 -m unittest discover -s tests -p test_capture_rules.py -q && python3 -m unittest discover -s tests -p test_overlay_guard.py -q'
   ```

3. Explicitly clean up with
   `nomad job stop -purge socket_stream_linux-test-runner` and
   `nomad job stop -purge socket_stream_linux-desktop-diagnostic`.

These jobs use `raw_exec`, so run them only on your own trusted Nomad cluster.
They do not install software, alter GNOME, or request screen-capture permissions.
Passing unit tests proves that configuration rules and unsupported fallbacks work;
it **does not prove** a window was hidden from a real screencast. In status output,
`config_validated` refers only to a compositor syntax check; `verified` is false
unless runtime capture evidence has been established separately. GNOME Wayland
does not expose a supported per-window full-desktop capture exclusion API.

## Desktop GUI

**Live practice UI:** Choose a real provider (the GUI defaults to Ollama), then
click **Start observation**. A separate labeled GTK answer panel opens, and
foreground accessibility text is evaluated first. The normal workflow does
not prompt for a screen share when AT-SPI provides readable question text.
The selected provider is checked before capture begins; mock is explicitly
identified as preview-only. The panel shows the current question, answer,
source, provider, and status. Use **Pause/Resume** to suspend observation and
discard in-flight answers; **Stop** ends the authorized observation process.
Diagnostic output is under the
collapsed **Advanced tools and diagnostics** section.

For an offline, no-inference UI test, open Advanced tools and click
**Replay demo**. This loads a bundled synthetic JSONL fixture containing two
questions and deliberate stale-answer events; it does not read the screen,
open a portal, listen to audio, or call AI.

Check **Enable local OCR fallback** only when the accessible question text is
unavailable. On GNOME/KDE Wayland this will invoke the normal, user-approved
desktop ScreenCast source chooser as needed; rejection fails closed. There is
no invisible or permission-bypassing screenshot path. If the accessibility
backend itself is missing (for example AT-SPI2 or the macOS Accessibility
permission), the panel reports that diagnosis and does **not** silently switch
to pixels; OCR still requires the explicit opt-in above.

    ./socket_stream

Select the mock provider first, then use Analyze text and Analyze image.
Controls include Start observation, Stop, provider settings, doctor, audio devices,
audio practice, and local AI start/stop. Provider credentials are held in process
memory only and are never displayed by the app.

For a no-fee local model, install Ollama from its official Linux distribution
and pull the selected model once:

    ollama pull qwen2.5-coder:1.5b

If Ollama is already a system service, there is no need to start a duplicate.
Select Ollama in the app and analyze an imported text file to test inference.

## Floating overlay

The standalone floating answer panel can run without the main GTK window:

    ./socket_stream overlay --provider ollama

It execs `apps/socket_stream_desktop/ui/floating_overlay.py`, which consumes the same
JSONL event stream as the desktop shell and performs no capture of its own. Add
`--allow-ocr` and `--portal` to permit the user-approved ScreenCast fallback, and
`--compact` for the small panel and `--opacity 0.94` for slight transparency (range
0.85–1.0). `--stealth` (the default) enables best-effort
capture-exclusion hints; `--no-stealth` disables those hints. Neither option
changes panel visibility.

In the desktop GUI the **Stealth overlay** checkbox controls the same setting. A
status line reports whether a screen share is detected; the panel remains visible.
A **Capture exclusion** row shows the compositor rule state and offers
**Set up exclusion**, **Remove rule**, and **Details**.

Best-effort exclusion is Linux-only and cannot guarantee invisibility:

- X11 sets override-redirect, skip-taskbar/pager and a notification window type;
  some compositors also honour `_NET_WM_STATE_SKIP_CAPTURE`.
- GNOME Wayland has no always-on-top protocol for Wayland clients and no
  layer-shell. The app therefore starts GTK with `GDK_BACKEND=x11` and gives the
  panel the X11 **dock** type, which mutter keeps above fullscreen windows without
  focusing it. Verified on the target over a fullscreen window. Set
  `GDK_BACKEND=wayland` to opt out; the panel is then a normal toplevel and can be
  covered. Stock GNOME Shell 50 / mutter 18 and sway have no per-window
  capture-exclusion rule. GNOME's custom Mutter runtime is a separate deployment
  and must be loaded by a fresh desktop login before it can filter a share.
- KDE, niri and Hyprland stay on Wayland so their capture rules keep applying.
  niri, Hyprland 0.53+ and KDE Plasma/KWin 6.7+ document a per-window rule. Use
  **Set up exclusion** (or `python3 capture_rules.py install`) to write it; this
  makes an explicit backup of the compositor file and is reversible. The panel
  stays visible locally and the compositor omits/blacks it in captures.
- Focus changes and detected ScreenCast sessions do not hide the panel. It may be
  present in a full-screen capture when no rule applies.

**Capture coverage depends on the compositor and capture path.** Stock X11,
GNOME and sway whole-screen capture includes the visible panel. A configured
rule or deployed runtime is not proof: verify the browser's captured video.

Check the current state without changing anything:

    python3 apps/socket_stream_desktop/ui/capture_rules.py status
    python3 apps/socket_stream_desktop/ui/capture_rules.py status --json

The supported writers are:

| Compositor | File | Documented setting |
|---|---|---|
| niri | `~/.config/niri/config.kdl` | `block-out-from "screen-capture"` |
| Hyprland 0.53+ | `~/.config/hypr/hyprland.conf` (or `.lua`) | `no_screen_share = true` |
| KDE Plasma/KWin 6.7+ Wayland | `~/.config/kwinrulesrc` | `[General] rules/count` registration, `excludefromcapture=true`, `excludefromcapturerule=2` |
| GNOME Wayland with validated custom Mutter runtime | Detected GNOME Shell user service's `90-socket_stream-capture.conf` drop-in | Immutable guarded runtime launcher; active after full desktop logout/login |

For GNOME, the custom Mutter 50.1 implementation and isolated synthetic capture
checks are documented in [`native/mutter-capture-filter/README.md`](../native/mutter-capture-filter/README.md).
KWin 6.6 does not have the persistent exclusion rule; the version check uses
`kwin_wayland --version`, not the version of Plasma Shell. Validate each deployed
browser/recorder with entire-screen, ordinary-window, panel-window (if offered),
and browser-tab sharing. Tab capture generally renders the browser's tab content
and does not include desktop windows. Never infer one source's result from another.
Upstream KWin's rule filters output/region streams but does not cover explicitly
selecting the panel as a window stream. The custom Mutter patch filters those
window streams; generic GNOME Shell screenshots require separate integration.

GNOME without a validated compatible custom runtime, X11, sway and unknown
compositors return "unsupported". Existing managed GNOME activation can still be
removed if its runtime is missing or the session type changes.

### GNOME activation status

The **Capture exclusion** row distinguishes:

- **Deployed:** runtime files, hashes, ownership and package pins validate.
- **Configured:** the detected Shell service's managed override and effective
  command match the selected immutable runtime.
- **Restart required:** the current Shell has not loaded that configuration, or
  is still running a runtime whose activation was removed.
- **Runtime active:** the live Shell process has the compositor opt-in and all
  four matching custom Mutter library mappings.
- **Verified:** separate received-pixel evidence from an actual share; neither
  configuration nor process activation sets this automatically.

On the tested Ubuntu desktop the service is `org.gnome.Shell@ubuntu.service`.
**Set up exclusion** first requires a successful isolated full-Shell startup
probe, then reloads and verifies its configuration before arming a durable
one-shot guard. A full **Log Out → sign in** loads the compositor. Closing
SocketStream or locking/unlocking the screen does not restart GNOME. The guarded
same-PID launcher falls back to stock when the runtime is missing, modified,
incompatible, or its startup attempt is no longer armed. After a consumed
failure, use setup again; another login alone does not rearm it.

The schema-2 runtime `50.1-0ubuntu2.5.socket-stream.3b4e8e8a98dcb8a3` passed the
production startup probe on the Lenovo (3.021 seconds of stable readiness, four
private mappings, clean exit 0 and cleanup). All eight disposable real-systemd
lifecycle cases passed, including exit 1 treated as successful by systemd but
refused by the guard. The desktop is still on stock GNOME pending a coordinated
login; the normal desktop logout tuple and live Chrome exclusion remain
unverified. Full evidence and the recovered earlier login failure are documented
in [`VERIFICATION.md`](../native/mutter-capture-filter/VERIFICATION.md).

Verify on a real Linux desktop with the interactive probe:

    python3 security-lab/overlay_probe.py
    python3 security-lab/overlay_probe.py --exclusion-status
    python3 security-lab/overlay_probe.py --setup-exclusion

Start a screen share (for example a browser tab or the desktop portal recorder),
confirm the marker and the real panel remain visible across focus changes, and
compare the recording with the probe. The probe never hides. Record the
compositor, session type, whether a rule was configured, and whether the window
actually disappeared from the capture. "Configured" and "runtime verified" are
separate results.

## Authorized screen-reading workflow

Open a coding practice question in a permitted browser or editor window.
For a manual CLI smoke test:

    ./socket_stream observe --authorized --interval 5 --max-captures 12 --provider mock

Accessibility text is attempted first. If unavailable, on GNOME/KDE Wayland
the desktop portal asks you to approve a screen/window. No bypass is attempted.
To force the whole consented portal -> frame -> local OCR -> model pipeline:

    ./socket_stream observe --authorized --portal --force-ocr --interval 5 --max-captures 6 --provider mock

Press Ctrl+C or use the GUI Stop button to shut down the current capture session.
On X11 local screenshots use ImageMagick import. On supporting wlroots desktops
grim may be used instead.

The GNOME/KDE portal needs an actual on-machine Wayland test: this archive was
cross-compiled on macOS, and the portal was verified through unit tests rather
than a real Linux screen selection.

## Structured accessibility tree

The AT-SPI2 helper can print a structured tree instead of flat text. Inspect the
current foreground application (after OS authorization):

    ./socket_stream a11y --authorized --mode tree
    ./socket_stream a11y --authorized --mode text

`--mode tree` prints one schema-1 JSON object with a flat node list (role, text,
focused, bounds, image, protected). `--mode text` keeps the previous flat output.
Observation accepts the same modes plus a token budget and region OCR:

    ./socket_stream observe-live --authorized --a11y-mode tree --max-tokens 4000 --provider mock
    ./socket_stream observe-live --authorized --ocr-images --allow-ocr --provider mock

`--a11y-mode auto|focused|tree|text` selects the render mode, `--max-tokens N`
sets the budget, and `--ocr-images` / `--no-ocr-images` toggle OCR of image and
graphic nodes. Flags override the config file; the config file overrides the
defaults. Accessibility is always tried first: when the tree text is sufficient
there is no capture and no OCR. Region OCR crops only the image/graphic nodes
found in the tree, so it never OCRs the whole screen.

## Config file

A JSON config file controls the a11y mode, OCR policy, shortcuts and overlay
defaults. It is read from `$SOCKET_STREAM_CONFIG`, else
`$XDG_CONFIG_HOME/socket_stream/config.json`, else `~/.config/socket_stream/config.json`.
A missing file uses defaults; a malformed file is reported, not ignored. Keys:

    a11y.mode            auto | focused | tree | text (default tree)
    a11y.max_tokens      token budget for the rendered tree (default 4000)
    a11y.max_nodes       maximum nodes rendered (default 800)
    a11y.max_depth       maximum tree depth (default 20)
    a11y.max_chars       maximum characters rendered (default 32000)
    a11y.ocr_images      OCR image/graphic nodes (default false)
    ocr.enabled          OCR opt-in (default false)
    ocr.max_regions      maximum regions OCRed per frame
    ocr.min_region_pixels  skip regions smaller than this
    ocr.tesseract_psm    Tesseract page segmentation mode
    shortcuts.enabled    enable the shortcut registry
    shortcuts.bindings   action -> accelerator map
    overlay.stealth      best-effort capture exclusion default
    overlay.compact      compact panel default

## Global shortcuts

Twelve configurable actions (start, stop, pause_resume, toggle_overlay,
show_overlay, hide_overlay, copy_answer, compact_toggle, toggle_stealth,
open_settings, refresh_question, quit) are managed from the CLI:

    ./socket_stream hotkeys list
    ./socket_stream hotkeys init
    ./socket_stream hotkeys print

`hotkeys list` prints the resolved bindings and the detected platform.
`hotkeys init` writes a default config file and refuses to overwrite one without
`--force`. `hotkeys print` prints the platform-native registration snippet (an
`sxhkdrc` block for X11, a note about `org.freedesktop.portal.GlobalShortcuts`
for Wayland, and the in-app equivalents for macOS). It never installs anything.
In-app shortcuts work while the window is focused; out-of-focus global grabs
require the desktop's own hotkey tool and are never installed silently.

## Offline mock provider and soak harness

For repeatable, no-cloud testing:

    python3 scripts/mock_provider_server.py
    python3 scripts/soak_replay.py --dry-run
    python3 scripts/soak_replay.py --minutes 30

The mock provider serves an OpenAI-compatible endpoint locally for provider and
event-pipeline tests. The soak harness replays synthetic events for a bounded
duration (`--minutes 180` for a three-hour run) to check memory and subprocess
cleanup. Neither reads the
screen, opens a portal, listens to audio, or calls a real API.

## Still needs a real Linux desktop

The accessibility-tree helper, region OCR, config file and shortcut registry are
implemented and covered by unit tests, but the following were not exercised on
real hardware from this cross-compiled archive: AT-SPI2 tree output on a live
GNOME/KDE session, region cropping with the distro ImageMagick, in-app GTK
accelerators, and out-of-focus global shortcuts under X11 and Wayland. Verify
these on the intended test machine and record the session type and compositor.

The floating panel itself **was** verified on Ubuntu 26.04.1 / GNOME Shell 50.1 /
mutter 18 / GTK 3.24.52 by running the real module:

- Wayland: the toplevel advertised `app_id=socket_stream_overlay`, stayed mapped,
  reported opacity 0.94, and used the RGBA visual (GTK paints it with alpha).
- X11 (XWayland): `WM_CLASS=socket_stream_overlay`, type
  `_NET_WM_WINDOW_TYPE_NOTIFICATION`, `SKIP_TASKBAR`/`SKIP_PAGER`/`ABOVE`, and
  `Override Redirect State: yes` (unmanaged, centred, not in `_NET_CLIENT_LIST`).
- GNOME default (`GDK_BACKEND=x11`, dock plan): `WM_CLASS=socket_stream_overlay`,
  type `_NET_WM_WINDOW_TYPE_DOCK`, `SKIP_TASKBAR`/`SKIP_PAGER`/`ABOVE`, no
  override-redirect, centred at (700, 355) on 1920×1080, and **visually above a
  fullscreen window** in a real screenshot while staying translucent.

These observations concern the stock compositor. Custom Mutter deployment and
live-capture results are tracked separately in
[`../native/mutter-capture-filter/VERIFICATION.md`](../native/mutter-capture-filter/VERIFICATION.md).

The niri, Hyprland and KDE capture rules are unit-tested and their files are
validated with `niri validate`, `Hyprland --verify-config` or KWin's rule parser
where available, but they have **not** been run on those compositors. A real
whole-screen share is required to confirm that the panel is excluded. The
available Ubuntu 26.04 target uses GNOME Shell 50.1 / mutter 18, whose stock build
has no per-window exclusion API. The custom runtime adds capture-render filtering
for that pinned distribution build.

## Microphone and system-audio practice

The bundle includes a small English Whisper model when built with one.
Install or build official whisper.cpp and put whisper-cli in PATH. Then run:

    ./socket_stream audio-devices
    ./socket_stream audio-practice --authorized --source microphone --device default --duration 10 --model models/ggml-tiny.en.bin --provider mock

For recording playback use --source system; it selects the default PulseAudio/
PipeWire sink monitor. The practice workflow deletes temporary recordings.
Explicitly exported recordings stay until deleted by the user.

## Bring your own AI endpoint

To use a permitted OpenAI-compatible service, set SOCKET_STREAM_BASE_URL,
SOCKET_STREAM_MODEL, and SOCKET_STREAM_API_KEY before running the app.
Only send questions to services you are authorized to use. A Codex CLI provider
is also available subject to a genuine logged-in session and remaining credits.

## Report failures

Run:

    ./check-dependencies.sh
    ./socket_stream doctor
    echo "$XDG_CURRENT_DESKTOP $XDG_SESSION_TYPE"

Share the distribution/version, X11 or Wayland compositor, and error output.
Never share your API keys, private recordings or confidential screen content.
