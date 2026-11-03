# Features and status

| Feature | State | Notes |
|---|---|---|
| Rust workspace + tested CLI | Implemented | Tests/build verified on macOS |
| Explicit capture permission | Implemented | Requires --authorized + OS permission |
| Mac accessibility extraction | Implemented helper | Swift compile verified; runtime permission integration not yet tested |
| Linux accessibility extraction | Implemented helper | AT-SPI2 Python GI dependency; not tested on Linux GUI |
| Accessibility first, OCR fallback | Implemented CLI | Structured tree modes (focused/tree/text/auto) with a token budget; sampling rather than OS event subscriptions |
| macOS Vision offline OCR | Tested | Compiled native binary and real fixture test passed |
| Linux Tesseract OCR | Implemented adapter | Linux verification pending |
| Screen deduplication | Implemented | Normalization preserves indentation |
| OpenAI-compatible BYOK | Verified mock HTTP | Real paid cloud credential not tested |
| Codex CLI provider | Blocked by credits | CLI signed in but workspace reported out of credits; native device flow added separately |
| Automated capture and answers | Implemented CLI | Only in explicitly authorized practice mode |
| Audio transcription | Verified local | whisper.cpp correctly transcribed generated speech |
| Consent-gated microphone recording | Implemented CLI | FFmpeg and selected input required; not runtime-tested with microphone |
| Mic/system audio capture | Implemented, E2E pending | Finite FFmpeg and ScreenCaptureKit pathways; real user permission not tested |
| Native macOS window | Smoke-verified | AppKit compiled and stayed alive; interactive capture permission test pending |
| Native Linux window | Implemented shell | GTK3 syntax verified; Linux runtime pending |
| Floating answer panel | Implemented, Linux QA pending | Standalone GTK3 overlay (`context-cli overlay`); live question/answer, compact mode; Linux-only capture-exclusion best effort plus behavioural auto-hide; not guaranteed against full-screen capture |
| Native Slint UI | Deferred | Platform-native thin shells selected instead |
| Multi-model fallback | Verified unit tests | Explicit --fallback-provider option |
| Visual diagram understanding | Pending | Optional only; OCR first |
| Agent task queue | Implemented | Dependency aware, manual claim |
| Autonomous development agents | Verified mocked tests | Explicit launch, max four worktrees, sandbox and CI gates; live Codex unavailable |
| Ollama local inference | Verified live | qwen2.5-coder:1.5b answered algorithm practice via Rust CLI |
| Linux Wayland XDG portal/PipeWire | Implemented, E2E pending | User chooser and atomic latest-frame source, structural tests |
| macOS system audio | Compiled | ScreenCaptureKit AAC to local WAV; live permission test pending |
| Continuous STT, VAD, diarization | Pending | Batch local Whisper currently works |
| Cross-platform installers | Pending | Mac/Ubuntu release QA |
| Best-effort capture exclusion | Implemented best-effort, Linux only | X11/Wayland window hints plus auto-hide while a ScreenCast session is detected; no per-window exclusion from full-screen capture; OS permission prompts are never bypassed |
| Accessibility tree reading | Implemented, Linux/macOS E2E pending | Structured AT-SPI/AX tree with focused/tree/text/auto modes and a token budget; configurable. |
| Region OCR for graphics | Implemented, E2E pending | OCRs only image/graphic nodes found in the accessibility tree; opt-in; bounded cache. |
| Config file | Implemented | ~/.config/contextlens/config.json controls a11y mode, OCR, shortcuts, overlay. |
| Global shortcuts | Implemented, platform E2E pending | 12 configurable actions incl. hide-from-screen; out-of-focus globals need the desktop's own tool. |
| Codex device authorization | Implemented, real-account test pending | Native official device flow, no Codex CLI required; credentials stored 0600; tokens never logged. |
