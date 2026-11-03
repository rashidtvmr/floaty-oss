# ContextLens (Rust)

Latest endpoint update: base and full completion URLs, model discovery, confirmed test requests, redirect blocking and bounded responses are implemented. A native Codex/ChatGPT device-authorization flow is implemented (no Codex CLI required); a real-account sign-in and independent security review are still outstanding. See `docs/AUTH-AND-ENDPOINTS.md` for the exact verified status.

A permission-based, lightweight screen-text and audio assistant for **coding practice and authorized work**.

**Status:** Rust CLI, native macOS GUI and real offline Ollama/Whisper tests pass. Linux GTK and Wayland portal backends are implemented but require Linux desktop E2E validation. A Linux floating overlay with best-effort capture exclusion and a behavioural auto-hide is implemented; exclusion from full-screen capture is never guaranteed. Structured accessibility-tree reading with a token budget, region OCR of image/graphic nodes, a JSON config file and twelve configurable global shortcuts are implemented but need real-desktop E2E validation. A native Codex/ChatGPT device-authorization flow is implemented; a real-account sign-in is still pending. No proctoring-evasion features.

## Approach
1. Read accessible text from the selected application via native OS accessibility APIs where authorized.
2. If text is missing, capture the permitted screen/window and run **local OCR** (Apple Vision on Mac, Tesseract on Linux).
3. Deduplicate unchanged questions before asking a text-only AI model.
4. Optional voice transcription via a locally installed whisper.cpp CLI.
5. User chooses: OpenAI-compatible cloud endpoint with their own API key, local Ollama, or official Codex CLI with their existing authorization.

No vision LLM is required for the OCR path; it works by sending extracted text.

## Try it

- Install Rust stable. macOS OCR uses Apple's built-in Vision framework; no Tesseract needed. Linux OCR uses distro Tesseract plus AT-SPI2/GTK Python bindings.
- `cargo test`
- `cargo run -p context-cli -- doctor`
- `cargo run -p context-cli -- analyze-file --file examples/sample-question.txt --provider mock`
- macOS GUI: `./scripts/start-macos.sh` (native AppKit window, compiled helper binaries).
- Linux GUI: `./scripts/start-linux.sh` (native GTK3; requires `python3-gi`, `gir1.2-gtk-3.0`).
- `cargo run -p context-cli -- observe --authorized --provider mock` (explicit on-screen session required).
- `cargo run -p context-cli -- observe --authorized --force-ocr --provider mock` to skip accessibility for canvas-based editors.
- `cargo run -p context-cli -- a11y --authorized` to inspect current foreground app accessible text (after OS authorization).
- `cargo run -p context-cli -- record-audio --authorized --device 0 --duration 30 --file practice.wav` for explicit macOS microphone capture; on Linux use a named PulseAudio source instead of `0`. Requires FFmpeg.
- `cargo run -p context-cli -- transcribe --file practice.wav --model /path/to/whisper-model.bin` requires installed whisper-cli and a local model.

For a cloud OpenAI-compatible endpoint set `CONTEXT_LENS_BASE_URL`, `CONTEXT_LENS_MODEL`, `CONTEXT_LENS_API_KEY` in the environment; see `.env.example`. For a subscribed Codex installation use `--provider codex` after `codex login`. Codex CLI is invoked in read-only mode; this is **not** a generic OpenAI API token and plan limits apply. On the development Mac, the CLI was signed in but returned an **out-of-credits** error during a test; the provider cannot succeed until eligible credits are available.

## Platform support

- Linux: AT-SPI foreground text and Tesseract, user-approved XDG ScreenCast/PipeWire on GNOME/KDE Wayland, grim on compatible wlroots compositors, and ImageMagick import on X11.
- macOS: native Accessibility helper (AXUIElement), screenshot via OS `screencapture`, built-in Vision OCR. Native AppKit UI.
- Linux: GTK3 native shell, AT-SPI2 PyGObject, local Tesseract, and XDG portal helper. The portal has passed offline structural tests but is **not yet verified on a live Linux desktop**.
- Different Wayland compositors and macOS permissions may require interactive authorization. Nothing is guaranteed to work without OS grants.

## Development

`docs/FEATURES.md`, `docs/ARCHITECTURE.md`, `docs/ACCEPTANCE.md`, `tasks/backlog.json`, `AGENTS.md`.

Contributions welcome. Actual lightweight native UI shells are AppKit and GTK3; the excluded Slint prototype is unused.

## What is not complete
- Native shells exist but are not yet fully interactive-tested on real Linux/GUI sessions.
- Finite macOS ScreenCaptureKit and Linux Pulse system-audio recording are implemented, but real device permission tests remain. Streaming speech, speaker diarization, VAD and OS accessibility events are pending.
- Linux XDG ScreenCast/PipeWire source is implemented; the real GNOME/KDE portal session needs Linux E2E testing.
- Native Mac screenshot/Accessibility permissions need to be granted by the user; never bypass them.
- OpenAI-compatible BYOK passed a local HTTP mock. Offline Ollama with `qwen2.5-coder:1.5b` passed a real Two Sum answer test. Real cloud provider credentials were not accessed.
- Codex CLI authentication alone does not guarantee available plan credits.
- Structured accessibility-tree rendering, region OCR of image/graphic nodes, the JSON config file and the global shortcut registry are implemented and unit tested, but they have not been exercised on a real Linux or macOS desktop.
- In-app shortcuts work while the window is focused; out-of-focus global shortcuts require the desktop's own hotkey tool and are never installed silently.
- The native Codex/ChatGPT device flow uses official OpenAI endpoints and stores credentials `0600` in ContextLens's own config directory, but no real account has signed in yet and independent security review is outstanding.
- The Linux floating overlay implements best-effort capture exclusion (X11/Wayland window hints) plus a behavioural auto-hide while a ScreenCast session is detected. Linux cannot exclude a window from full-screen capture, and no OS permission prompt is bypassed; universal invisibility is not claimed.

## Measured limitations and observed behavior

- Native Apple Vision OCR fast mode passed the test-image smoke test, but the resulting source text may misread code punctuation or indentation. Set `CONTEXT_LENS_OCR_ACCURATE=1` to opt in to the slower/more accurate recognizer, which may stall or fail on some macOS versions.
- The Codex CLI is logged in with ChatGPT on the connected Mac, but a live request returned **workspace out of credits**. Subscription authentication does not guarantee an available request allowance.
- Rust/Python regressions pass. Offline whisper.cpp transcribed synthetic speech correctly. Real Linux capture, Mac screen/audio permission UX and continuous speech streaming have not been exercised.
- Launchers: `./scripts/start-macos.sh` and `./scripts/start-linux.sh`. No autorun is configured. The system process retains its normal identity.

## Offline local inference and audio quickstart

The connected Mac has Ollama, local `qwen2.5-coder:1.5b`, whisper.cpp and `models/ggml-tiny.en.bin` installed. The speech model is excluded from Git. In the native Mac GUI choose **ollama** and click **Start local AI**. For other machines, install these providers and run `./scripts/setup-whisper-model.sh` to download the small English speech model.

```bash
cargo run -p context-cli -- analyze-image --file tests/fixtures/sample-code.png --provider ollama
cargo run -p context-cli -- record-system-audio --authorized --duration 15 --file authorized.wav
cargo run -p context-cli -- audio-practice --authorized --source system --duration 15 --model models/ggml-tiny.en.bin --provider ollama
```

System audio requires a user-approved OS capture permission. Explicitly exported WAV files persist until deleted; temporary `audio-practice` recordings are removed.

## Linux Wayland dependencies

On recent Ubuntu/Debian install `python3-dbus`, `python3-gi`, `gir1.2-atspi-2.0`, `gir1.2-gtk-3.0`, `gir1.2-gstreamer-1.0`, `gstreamer1.0-pipewire`, `gstreamer1.0-plugins-good`, `xdg-desktop-portal`, `tesseract-ocr`, `ffmpeg` and `imagemagick`. The `--portal` flag explicitly selects the XDG ScreenCast chooser for authorized OCR:

```bash
cargo run -p context-cli -- observe --authorized --portal --force-ocr --provider mock
```

**Linux portal runtime is unverified** until a real GNOME/KDE Wayland GUI test can be completed. macOS Vision OCR fast mode sometimes misreads code punctuation; try `CONTEXT_LENS_OCR_ACCURATE=1` for slower experimental accurate recognition.

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
    cargo zigbuild --release --locked -p context-cli --target x86_64-unknown-linux-musl
    ./scripts/build-linux-release.sh x86_64

ARM64 uses aarch64-unknown-linux-musl and the aarch64 package argument.
The standalone artifacts and SHA-256 manifests are in dist/. They bundle the
static Rust CLI, GTK3 Python shell, AT-SPI reader, XDG ScreenCast/PipeWire
helper, Tesseract integration, dependency checker, optional tiny Whisper model
and GPL license. See docs/LINUX-TESTING.md (also bundled as README-LINUX.md).

Additional CLI actions:
- analyze-audio --file INPUT.wav --model models/ggml-tiny.en.bin --provider ollama
- audio-devices
- analyze-image --file SCREENSHOT.png --provider ollama

This is a practice-oriented assistant, not a proctoring workaround. Explicit
user authorization and desktop permissions remain required for capture.
Linux GNOME/KDE Wayland integration must be tested on a real Linux desktop;
macOS cross-compilation cannot demonstrate system portal operation.
