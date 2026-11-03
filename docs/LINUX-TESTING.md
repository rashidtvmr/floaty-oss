# ContextLens Linux release: installation and validation

This archive includes a statically linked Linux Rust CLI, a GTK3 desktop shell,
accessible-text reading via AT-SPI2, Tesseract OCR, and the XDG ScreenCast/PipeWire
capture helper. It is a practice assistant for authorized screen/audio capture,
not an exam-proctoring workaround. The package does not conceal itself or bypass
operating-system permissions.

## Extract and check

Unpack the archive using:

    tar -xzf contextlens-linux-x86_64-*.tar.gz
    cd contextlens-linux-x86_64-*
    ./check-dependencies.sh

On Ubuntu or Debian, review and deliberately run the optional installer:

    ./install-dependencies-debian.sh --yes
    ./check-dependencies.sh

The installer uses apt and sudo. Fedora/Arch users should install the equivalent
GTK3, AT-SPI2, DBus, GStreamer/PipeWire, Tesseract, ffmpeg, and PulseAudio tools.

## Start with a no-cloud smoke test

    file bin/context-cli
    ./contextlens doctor
    ./contextlens analyze-file --file examples/sample-question.txt --provider mock
    ./contextlens ocr --file tests/fixtures/sample-code.png

The file must report a Linux x86-64 ELF executable for the x86_64 package.
The mock model does not send data to any API provider.

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

    ./contextlens

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

    ./contextlens overlay --provider ollama

It execs `apps/context-desktop/ui/floating_overlay.py`, which consumes the same
JSONL event stream as the desktop shell and performs no capture of its own. Add
`--allow-ocr` and `--portal` to permit the user-approved ScreenCast fallback, and
`--compact` for the small panel. `--stealth` (the default) enables best-effort
capture exclusion; `--no-stealth` disables the window hints and the guard.

In the desktop GUI the **Stealth overlay** checkbox toggles the same behaviour. A
status line reports whether a screen share is currently detected and whether the
panel is hidden because of it.

Best-effort exclusion is Linux-only and cannot guarantee invisibility:

- X11 sets override-redirect, skip-taskbar/pager and a notification window type;
  some compositors also honour `_NET_WM_STATE_SKIP_CAPTURE`.
- Wayland uses a `wlr-layer-shell` overlay layer, which the portal still lists as
  a toplevel; niri users can add `block-out-from=screencast` themselves.
- While a desktop ScreenCast session is detected, the behavioural guard hides the
  panel and restores it afterwards.

**Linux has no per-window exclusion from full-screen capture.** A full-screen
capture still records the panel unless the guard has hidden it or a compositor
rule excludes it. No OS permission prompt is bypassed.

Verify on a real Linux desktop with the interactive probe:

    python3 security-lab/overlay_probe.py

Start a screen share (for example a browser tab or the desktop portal recorder),
confirm the panel hides and returns, and compare the recording with the probe
marker. Record the compositor and session type.

## Authorized screen-reading workflow

Open a coding practice question in a permitted browser or editor window.
For a manual CLI smoke test:

    ./contextlens observe --authorized --interval 5 --max-captures 12 --provider mock

Accessibility text is attempted first. If unavailable, on GNOME/KDE Wayland
the desktop portal asks you to approve a screen/window. No bypass is attempted.
To force the whole consented portal -> frame -> local OCR -> model pipeline:

    ./contextlens observe --authorized --portal --force-ocr --interval 5 --max-captures 6 --provider mock

Press Ctrl+C or use the GUI Stop button to shut down the current capture session.
On X11 local screenshots use ImageMagick import. On supporting wlroots desktops
grim may be used instead.

The GNOME/KDE portal needs an actual on-machine Wayland test: this archive was
cross-compiled on macOS, and the portal was verified through unit tests rather
than a real Linux screen selection.

## Structured accessibility tree

The AT-SPI2 helper can print a structured tree instead of flat text. Inspect the
current foreground application (after OS authorization):

    ./contextlens a11y --authorized --mode tree
    ./contextlens a11y --authorized --mode text

`--mode tree` prints one schema-1 JSON object with a flat node list (role, text,
focused, bounds, image, protected). `--mode text` keeps the previous flat output.
Observation accepts the same modes plus a token budget and region OCR:

    ./contextlens observe --authorized --a11y-mode tree --max-tokens 1200 --provider mock
    ./contextlens observe --authorized --ocr-images --allow-ocr --provider mock

`--a11y-mode auto|focused|tree|text` selects the render mode, `--max-tokens N`
sets the budget, and `--ocr-images` / `--no-ocr-images` toggle OCR of image and
graphic nodes. Flags override the config file; the config file overrides the
defaults. Accessibility is always tried first: when the tree text is sufficient
there is no capture and no OCR. Region OCR crops only the image/graphic nodes
found in the tree, so it never OCRs the whole screen.

## Config file

A JSON config file controls the a11y mode, OCR policy, shortcuts and overlay
defaults. It is read from `$CONTEXT_LENS_CONFIG`, else
`$XDG_CONFIG_HOME/contextlens/config.json`, else `~/.config/contextlens/config.json`.
A missing file uses defaults; a malformed file is reported, not ignored. Keys:

    a11y.mode            auto | focused | tree | text
    a11y.max_tokens      token budget for the rendered tree
    a11y.max_nodes       maximum nodes rendered
    a11y.max_depth       maximum tree depth
    a11y.max_chars       maximum characters rendered
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

    ./contextlens hotkeys list
    ./contextlens hotkeys init
    ./contextlens hotkeys print

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
    python3 scripts/soak_replay.py --iterations 2000

The mock provider serves an OpenAI-compatible endpoint locally for provider and
event-pipeline tests. The soak harness replays synthetic events for a bounded
duration to check memory, queues and orphan subprocesses. Neither reads the
screen, opens a portal, listens to audio, or calls a real API.

## Still needs a real Linux desktop

The accessibility-tree helper, region OCR, config file and shortcut registry are
implemented and covered by unit tests, but the following were not exercised on
real hardware from this cross-compiled archive: AT-SPI2 tree output on a live
GNOME/KDE session, region cropping with the distro ImageMagick, in-app GTK
accelerators, and out-of-focus global shortcuts under X11 and Wayland. Verify
these on the intended test machine and record the session type and compositor.

## Microphone and system-audio practice

The bundle includes a small English Whisper model when built with one.
Install or build official whisper.cpp and put whisper-cli in PATH. Then run:

    ./contextlens audio-devices
    ./contextlens audio-practice --authorized --source microphone --device default --duration 10 --model models/ggml-tiny.en.bin --provider mock

For recording playback use --source system; it selects the default PulseAudio/
PipeWire sink monitor. The practice workflow deletes temporary recordings.
Explicitly exported recordings stay until deleted by the user.

## Bring your own AI endpoint

To use a permitted OpenAI-compatible service, set CONTEXT_LENS_BASE_URL,
CONTEXT_LENS_MODEL, and CONTEXT_LENS_API_KEY before running the app.
Only send questions to services you are authorized to use. A Codex CLI provider
is also available subject to a genuine logged-in session and remaining credits.

## Report failures

Run:

    ./check-dependencies.sh
    ./contextlens doctor
    echo "$XDG_CURRENT_DESKTOP $XDG_SESSION_TYPE"

Share the distribution/version, X11 or Wayland compositor, and error output.
Never share your API keys, private recordings or confidential screen content.
