# Features and status

| Feature | State | Notes |
|---|---|---|
| Rust workspace + tested CLI | Implemented | Tests/build verified on macOS |
| Explicit capture permission | Implemented | Requires --authorized + OS permission |
| Mac accessibility extraction | Tested with the installed app | Swift AX helper roots at the focused/main window, skips the global menu bar, excludes secure fields, and was verified against Terminal and Google Chrome |
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
| Native macOS window | Implemented; installed and E2E-tested | AppKit main window plus floating panel; real Accessibility and Screen Recording grants; mock observation, panel controls and capture-exclusion checks verified from `~/Applications/SocketStream.app` |
| Native Linux window | Implemented shell | GTK3 syntax verified; Linux runtime pending |
| Floating answer panel (Linux) | Implemented; identity/opacity verified on GNOME 50 | Standalone GTK3 overlay (`socket_stream overlay`); live question/answer, compact mode, 94% opacity, app-id/WM_CLASS `socket_stream_overlay`; stays visible across focus changes and screen shares; no automatic hiding |
| Floating answer panel (macOS) | Implemented; installed and E2E-tested | Native `NSPanel` at `.statusBar`, `.fullScreenAuxiliary`, `.nonactivatingPanel`, 94% opacity, `sharingType = .none`; live JSONL state, Compact/Pause/Copy/Close/Main/Stop, stays above fullscreen apps and minimized main window; `socket_stream overlay` launches `--panel-only` |
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
| Capture exclusion | Implemented: hints + rule setup (Linux), experimental custom Mutter patch; `sharingType` verified against `screencapture` (macOS); real remote-share QA pending | macOS: `NSWindow.sharingType = .none` hides the panel from `screencapture`; a real RustDesk/Meet/Zoom share is not yet tested. Linux: X11 hints; niri, Hyprland 0.53+ and KDE Plasma/KWin 6.7+ Wayland managed rules via `capture_rules.py` (explicit install/remove, backup, validation). Stock GNOME 50/mutter 18, X11 and sway have no per-window rule. `native/mutter-capture-filter` implements an opt-in custom Mutter 50.1 capture filter; see its README for deployment and measured coverage. Panel never auto-hides |
| Accessibility tree reading | Implemented; macOS E2E verified, Linux E2E pending | Structured AT-SPI/AX tree with focused/tree/text/auto modes and a token budget; configurable. |
| Region OCR for graphics | Implemented, E2E pending | OCRs only image/graphic nodes found in the accessibility tree; opt-in; bounded cache. |
| Config file | Implemented | ~/.config/socket_stream/config.json controls a11y mode, OCR, shortcuts, overlay. |
| Global shortcuts | Implemented, platform E2E pending | 12 configurable actions; overlay show/toggle never closes the panel; out-of-focus globals need the desktop's own tool. |
| Codex device authorization | Implemented, real-account test pending | Native official device flow, no Codex CLI required; credentials stored 0600; tokens never logged. |
