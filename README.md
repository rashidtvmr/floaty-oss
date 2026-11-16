# SocketStream (Rust)

Latest endpoint update: base and full completion URLs, model discovery, confirmed test requests, redirect blocking and bounded responses are implemented. A native Codex/ChatGPT device-authorization flow is implemented (no Codex CLI required); a real-account sign-in and independent security review are still outstanding. See `docs/AUTH-AND-ENDPOINTS.md` for the exact verified status.

A permission-based, lightweight screen-text and audio assistant for **coding practice and authorized work**.

**Status:** Rust CLI, native macOS GUI and offline Ollama/Whisper tests pass. Linux
GTK now has a light workspace, translucent reading panel, pinned Q&A history and
seventeen editable **in-app** shortcuts. The native GTK layout and process flows
are tested on Ubuntu GNOME/XWayland and Xvfb; see
[the UI design and verification record](docs/READABLE-ANSWER-UI.md). The private
GNOME capture runtime is active and the user confirmed working sharing exclusion;
automated received-share-pixel verification remains incomplete
([evidence](docs/GNOME-CAPTURE-EXCLUSION.md)). macOS retains its existing AppKit UI
and twelve actions, with installed-bundle Accessibility, panel and `screencapture`
checks. Linux accessibility-tree/portal E2E and native Codex real-account sign-in
remain pending.

## Approach
1. Read accessible text from the selected application via native OS accessibility APIs where authorized.
2. If text is missing, capture the permitted screen/window and run **local OCR** (Apple Vision on Mac, Tesseract on Linux).
3. Deduplicate unchanged questions before asking a text-only AI model.
4. Built-in English Moonshine streaming speech, with whisper.cpp available for offline recordings.
5. User chooses: OpenAI-compatible cloud endpoint with their own API key, local Ollama, or official Codex CLI with their existing authorization.

No vision LLM is required for the OCR path; it works by sending extracted text.

## Try it

- Install Rust stable. macOS OCR uses Apple's built-in Vision framework; no Tesseract needed. Linux OCR uses distro Tesseract plus AT-SPI2/GTK Python bindings.
- `cargo test`
- `cargo run -p socket_stream -- doctor`
- `cargo run -p socket_stream -- analyze-file --file examples/sample-question.txt --provider mock`
- macOS GUI: `./scripts/start-macos.sh` opens the installed `~/Applications/SocketStream.app` (the bundle has the stable TCC identity). Build or refresh it with `./scripts/build-macos-app.sh`; use `./scripts/start-macos.sh --dev` for the raw development GUI. See `docs/MACOS-TESTING.md`.
- Linux GUI: `./scripts/start-linux.sh` (native GTK3; requires `python3-gi`, `gir1.2-gtk-3.0`).
- `cargo run -p socket_stream -- observe --authorized --provider mock` (explicit on-screen session required).
- `cargo run -p socket_stream -- observe --authorized --force-ocr --provider mock` to skip accessibility for canvas-based editors.
- `cargo run -p socket_stream -- a11y --authorized` to inspect current foreground app accessible text (after OS authorization).
- `cargo run -p socket_stream -- record-audio --authorized --device 0 --duration 30 --file practice.wav` for explicit macOS microphone capture; on Linux use a named PulseAudio source instead of `0`. Requires FFmpeg.
- `cargo run -p socket_stream -- transcribe --file practice.wav --model /path/to/whisper-model.bin` requires installed whisper-cli and a local model.

For a cloud OpenAI-compatible endpoint set `SOCKET_STREAM_BASE_URL`, `SOCKET_STREAM_MODEL`, `SOCKET_STREAM_API_KEY` in the environment; see `.env.example`. For a subscribed Codex installation use `--provider codex` after `codex login`. Codex CLI is invoked in read-only mode; this is **not** a generic OpenAI API token and plan limits apply. On the development Mac, the CLI was signed in but returned an **out-of-credits** error during a test; the provider cannot succeed until eligible credits are available.

## Platform support

- Linux: AT-SPI foreground text and Tesseract, user-approved XDG ScreenCast/PipeWire on GNOME/KDE Wayland, grim on compatible wlroots compositors, and ImageMagick import on X11.
- macOS: native Accessibility helper (AXUIElement), screenshot via OS `screencapture`, built-in Vision OCR. Native AppKit UI.
- Linux: GTK3 native shell, AT-SPI2 PyGObject, local Tesseract, and XDG portal helper. The portal has passed offline structural tests but is **not yet verified on a live Linux desktop**.
- Different Wayland compositors and macOS permissions may require interactive authorization. Nothing is guaranteed to work without OS grants.

## Development

`docs/FEATURES.md`, `docs/ARCHITECTURE.md`, `docs/ACCEPTANCE.md`, `tasks/backlog.json`, `AGENTS.md`.

Contributions welcome. Actual lightweight native UI shells are AppKit and GTK3; the excluded Slint prototype is unused.

## What is not complete
- Linux GTK has synthetic native-UI and compositor-input checks; multi-hour use,
  native Wayland layouts outside GNOME and real audio-device UX remain unverified.
- Finite recordings and built-in Moonshine streaming speech are implemented; real audio-device permission tests, speaker diarization and OS accessibility events remain pending.
- Linux XDG ScreenCast/PipeWire source is implemented; the real GNOME/KDE portal session needs Linux E2E testing.
- Native Mac screenshot/Accessibility permissions need to be granted by the user; never bypass them.
- OpenAI-compatible BYOK passed a local HTTP mock. Offline Ollama with `qwen2.5-coder:1.5b` passed a real Two Sum answer test. Real cloud provider credentials were not accessed.
- Codex CLI authentication alone does not guarantee available plan credits.
- Structured accessibility-tree rendering, region OCR of image/graphic nodes, the JSON config file and the global shortcut registry are implemented and unit tested. The macOS tree path has been exercised against real Terminal and Chrome windows from the installed bundle; the Linux desktop tree path has not.
- Linux in-app shortcuts work while the control window is focused; out-of-focus
  registration needs the desktop's own hotkey tool. See [shortcuts](docs/SHORTCUTS.md).
- The native Codex/ChatGPT device flow uses official OpenAI endpoints and stores credentials `0600` in SocketStream's own config directory, but no real account has signed in yet and independent security review is outstanding.
- Linux exclusion depends on the compositor. The private GNOME runtime has a
  user-confirmed working share; generic X11/native Wayland hints alone do not
  establish exclusion. See [capture evidence](docs/GNOME-CAPTURE-EXCLUSION.md).

## Measured limitations and observed behavior

- Native Apple Vision OCR fast mode passed the test-image smoke test, but the resulting source text may misread code punctuation or indentation. Set `SOCKET_STREAM_OCR_ACCURATE=1` to opt in to the slower/more accurate recognizer, which may stall or fail on some macOS versions.
- The Codex CLI is logged in with ChatGPT on the connected Mac, but a live request returned **workspace out of credits**. Subscription authentication does not guarantee an available request allowance.
- Rust/Python regressions and synthetic Moonshine speech-to-answer checks pass. Offline whisper.cpp transcribed synthetic speech correctly. The macOS screen-permission flow, accessibility reading, live panel, panel controls and `screencapture` exclusion have been exercised from the installed bundle; real Linux desktop capture and real macOS system-audio/microphone permission UX remain unverified.
- Launchers: `./scripts/start-macos.sh` and `./scripts/start-linux.sh`. No autorun is configured. The system process retains its normal identity.

## Built-in streaming speech

Choose **Listen · Moonshine** on macOS or **Listen to audio** on Linux. The app
bundles local English speech recognition and streams finalized speech to your
selected AI provider. Microphone/system playback selection, rolling transcripts,
Pause/Resume and Stop are integrated into the answer panel.
See [build instructions and tested platform limits](docs/MOONSHINE.md).

## Linux reading workspace

The floating panel keeps a question paired with its answer while newer questions
arrive. Pin a pair, browse the last 24 questions, or choose **Follow latest**.
History stays in memory for the current session. Answers render headings,
emphasis and code blocks; Copy preserves the underlying Markdown.

**Appearance** controls background opacity (55–100%), answer size and reduced
motion. **Shortcuts** supports editing, key recording and immediate application
on Save. The light control window groups Session, Capture sources, Appearance,
Shortcuts and Tools & diagnostics. See [screenshots and behavior](docs/READABLE-ANSWER-UI.md).

## Offline local inference and audio quickstart

The connected Mac has Ollama, local `qwen2.5-coder:1.5b`, whisper.cpp and `models/ggml-tiny.en.bin` installed. The speech model is excluded from Git. In the native Mac GUI choose **ollama** and click **Start local AI**. For other machines, install these providers and run `./scripts/setup-whisper-model.sh` to download the small English speech model.

```bash
cargo run -p socket_stream -- analyze-image --file tests/fixtures/sample-code.png --provider ollama
cargo run -p socket_stream -- record-system-audio --authorized --duration 15 --file authorized.wav
cargo run -p socket_stream -- audio-practice --authorized --source system --duration 15 --model models/ggml-tiny.en.bin --provider ollama
```

System audio requires a user-approved OS capture permission. Explicitly exported WAV files persist until deleted; temporary `audio-practice` recordings are removed.

## Linux Wayland dependencies

On recent Ubuntu/Debian install `python3-dbus`, `python3-gi`, `gir1.2-atspi-2.0`, `gir1.2-gtk-3.0`, `gir1.2-gstreamer-1.0`, `gstreamer1.0-pipewire`, `gstreamer1.0-plugins-good`, `xdg-desktop-portal`, `tesseract-ocr`, `ffmpeg` and `imagemagick`. The `--portal` flag explicitly selects the XDG ScreenCast chooser for authorized OCR:

```bash
cargo run -p socket_stream -- observe --authorized --portal --force-ocr --provider mock
```

**Linux portal runtime is unverified** until a real GNOME/KDE Wayland GUI test can be completed. macOS Vision OCR fast mode sometimes misreads code punctuation; try `SOCKET_STREAM_OCR_ACCURATE=1` for slower experimental accurate recognition.

## Explicit development agents

```bash
python3 scripts/dev_agent_runner.py --auto-ready --limit 4
# Only when you deliberately want to spend Codex credits and Git is clean:
python3 scripts/dev_agent_runner.py --auto-ready --limit 4 --execute
```

The runner rejects duplicate/colliding task IDs, branch/worktree conflicts, a dirty repository, overlapping task owners, and more than four workers. Codex runs in a workspace-write sandbox, with deterministic gates and **no automatic merges**. The connected Codex workspace has reported out-of-credits, so a live agent run is currently unavailable.

## Portable Linux build (x86-64 and ARM64)

The project can cross-compile Linux musl binaries from macOS using Zig:

    rustup target add x86_64-unknown-linux-musl
    cargo install cargo-zigbuild --locked
    cargo zigbuild --release --locked -p socket_stream --target x86_64-unknown-linux-musl
    ./scripts/build-linux-release.sh x86_64

ARM64 uses aarch64-unknown-linux-musl and the aarch64 package argument.
The standalone artifacts and SHA-256 manifests are in dist/. They bundle the
static Rust CLI, GTK3 Python shell, AT-SPI reader, XDG ScreenCast/PipeWire
helper, Tesseract integration, dependency checker, optional tiny Whisper model
and GPL license. See docs/LINUX-TESTING.md (also bundled as README-LINUX.md).

The working GNOME capture-exclusion method, loader fix, guarded activation,
deployment and recovery are documented in
[GNOME capture exclusion](docs/GNOME-CAPTURE-EXCLUSION.md), with the Lenovo's
live activation record and the user's successful sharing report.

Additional CLI actions:
- analyze-audio --file INPUT.wav --model models/ggml-tiny.en.bin --provider ollama
- audio-devices
- analyze-image --file SCREENSHOT.png --provider ollama

This is a practice-oriented assistant, not a proctoring workaround. Explicit
user authorization and desktop permissions remain required for capture.
Linux GNOME/KDE Wayland integration must be tested on a real Linux desktop;
macOS cross-compilation cannot demonstrate system portal operation.
