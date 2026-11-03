# Acceptance gates

Before marking any task done:

1. All code paths reachable by users must compile on their intended platform.
2. cargo fmt --all -- --check
3. cargo clippy --workspace --all-targets -- -D warnings
4. cargo test --workspace
5. Regression test exercises modified code, not old/stale code.
6. No nonconsensual monitoring, system-process masquerading or security bypass.
7. No secrets or unredacted screen/audio data written to logs.
8. Platform-specific functionality must be tested on a real device with permissions.
9. Performance and OCR accuracy require measured baselines, not assumptions.
10. Independent review must record any incompleteness explicitly.

Verified: Rust core and CLI compile, Mac native helpers build, synthetic Vision OCR, local Ollama answer and synthetic Whisper transcription pass, Rust and Python tests/format/Clippy pass. Outstanding: Linux GTK/AT-SPI/Wayland portal runtime, Mac screen/audio permission test, interactive GUI QA, multi-hour soak, real-time STT/VAD/diarization, signed installers, accessibility-tree rendering and region OCR on a real Linux and macOS desktop, in-app and out-of-focus global shortcuts on X11/Wayland/macOS, and native Codex device sign-in with a real account plus independent security review.
