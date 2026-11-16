# Features and status

| Feature | State | Notes |
|---|---|---|
| Rust workspace + tested CLI | Implemented | Tests/build verified on macOS |
| Explicit capture permission | Implemented | Requires --authorized + OS permission |
| Mac accessibility extraction | Tested with the installed app | Swift AX helper roots at the focused/main window, skips the global menu bar, excludes secure fields, and was verified against Terminal and Google Chrome |
| Linux accessibility extraction | Implemented helper | AT-SPI2 Python GI dependency; not tested on Linux GUI |
| Accessibility first, OCR fallback | Implemented CLI | Structured tree modes (focused/tree/text/auto) with a token budget; sampling rather than OS event subscriptions |
| macOS Vision offline OCR | Tested | Compiled native binary and real fixture test passed |
| Linux Tesseract OCR | Synthetic image verified on Ubuntu | Sample fixture passed in the installed release; real portal/region-capture E2E remains pending |
| Screen deduplication | Implemented | Normalization preserves indentation |
| OpenAI-compatible BYOK | Verified mock HTTP | Real paid cloud credential not tested |
| Codex CLI provider | Blocked by credits | CLI signed in but workspace reported out of credits; native device flow added separately |
| Automated capture and answers | Implemented CLI | Only in explicitly authorized practice mode |
| Audio transcription | Verified local | whisper.cpp correctly transcribed generated speech |
| Consent-gated microphone recording | Implemented CLI | FFmpeg and selected input required; not runtime-tested with microphone |
| Mic/system audio capture | Implemented, E2E pending | Finite FFmpeg and ScreenCaptureKit pathways; real user permission not tested |
| Native macOS window | Implemented; installed and E2E-tested | AppKit main window plus floating panel; real Accessibility and Screen Recording grants; mock observation, panel controls and capture-exclusion checks verified from `~/Applications/SocketStream.app` |
| Native Linux window | Implemented; GTK runtime exercised | Light five-page workspace; real GTK checks on Ubuntu GNOME/XWayland and Xvfb; see [UI verification](READABLE-ANSWER-UI.md) |
| Floating answer panel (Linux) | Implemented; GTK/compositor input exercised | Separate command capsule and translucent answer card; Markdown, pinned Q&A, 24-pair session history, compact mode, configurable 90% background alpha; app-id/WM_CLASS `socket_stream_overlay`; stays visible across focus changes and screen shares |
| Floating answer panel (macOS) | Implemented; installed and E2E-tested | Native `NSPanel` at `.statusBar`, `.fullScreenAuxiliary`, `.nonactivatingPanel`, 94% opacity, `sharingType = .none`; live JSONL state, Compact/Pause/Copy/Close/Main/Stop, stays above fullscreen apps and minimized main window; `socket_stream overlay` launches `--panel-only` |
| Native Slint UI | Deferred | Platform-native thin shells selected instead |
| Multi-model fallback | Verified unit tests | Explicit --fallback-provider option |
| Visual diagram understanding | Pending | Optional only; OCR first |
| Agent task queue | Implemented | Dependency aware, manual claim |
| Autonomous development agents | Verified mocked tests | Explicit launch, max four worktrees, sandbox and CI gates; live Codex unavailable |
| Ollama local inference | Verified live | qwen2.5-coder:1.5b answered algorithm practice via Rust CLI |
| Linux Wayland XDG portal/PipeWire | Implemented, E2E pending | User chooser and atomic latest-frame source, structural tests |
| macOS system audio | Compiled | ScreenCaptureKit AAC to local WAV; live permission test pending |
| Continuous STT | Implemented; synthetic streaming verified | Bundled English Moonshine initializes on macOS arm64 and Ubuntu x86_64; PCM-to-answer, Pause/Resume and Stop tests pass; real device permission/latency checks pending |
| Speaker diarization | Pending | Moonshine transcript is source-labeled; no multi-speaker identity claims |
| Cross-platform installers | Pending | Mac/Ubuntu release QA |
| Capture exclusion | Private GNOME runtime active; user-confirmed sharing result | Native/static/animated tests, full-Shell probe and eight lifecycle cases pass. Automated live received-share-pixel verification remains incomplete. Other Linux compositor rules remain unit-tested; macOS `sharingType = .none` was verified against `screencapture`, not every recorder. See [capture evidence](GNOME-CAPTURE-EXCLUSION.md) |
| Accessibility tree reading | Implemented; macOS E2E verified, Linux E2E pending | Structured AT-SPI/AX tree with focused/tree/text/auto modes and a token budget; configurable. |
| Region OCR for graphics | Implemented, E2E pending | OCRs only image/graphic nodes found in the accessibility tree; opt-in; bounded cache. |
| Config file | Implemented; shared persistence tested | Accessibility/OCR, shortcuts and GTK appearance; Python/Rust share a POSIX lock, no-follow regular-file reads and atomic 0600 writes that preserve unrelated fields |
| Configurable shortcuts | Seventeen Linux in-app actions; twelve on macOS | GTK editor supports recording, validation and immediate Save; control-window focus required on Linux; out-of-focus registration is user-managed. See [SHORTCUTS.md](SHORTCUTS.md) |
| Codex device authorization | Implemented, real-account test pending | Native official device flow, no Codex CLI required; credentials stored 0600; tokens never logged. |
