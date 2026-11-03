# ContextLens architecture

## Goals
Low-overhead Rust desktop assistant for coding practice, accessibility assistance and authorized interviews. The product is not a covert proctoring-bypass tool.

## Capture
- Use frontmost application accessibility text when it is accessible to the logged-in user and authorized by OS permissions.
- macOS: AXUIElement; Linux: AT-SPI2 through PyGObject helper. Both are currently one-shot helpers; automatic observation is via explicit sampling.
- If text is inaccessible/too short: use a consent-gated screenshot and local OCR. macOS: Apple Vision; Linux: Tesseract.
- No screenshots are intentionally retained after sampling. Captures used for user-requested export are explicit.
- Some code editors expose only the focused line; OCR is necessary in these cases.
- GNOME/KDE Wayland use the user-approved XDG ScreenCast/PipeWire backend when OCR is required; wlroots grim and X11 import remain alternatives. Real Linux portal E2E pending.

## Accessibility tree, region OCR and shortcuts
- The AT-SPI2 (Linux) and AX (macOS) helpers accept `--mode tree|text` and print one schema-1 JSON object in `tree` mode: a flat pre-order node list with `id`, `parent`, `role`, `name`, `text`, `focused`, `bounds`, `image` and `protected`, plus `truncated` and `node_count`. `--mode text` keeps the previous flat-text output byte-for-byte.
- `crates/context-core/src/a11y.rs` parses that JSON into an `A11yTree` and renders it under a `TokenBudget` in `focused`, `tree`, `text` or `auto` mode. Rendering is pure and deterministic: structural roles are skipped, protected nodes never render text, duplicate `(role, text)` pairs are emitted once, and the budget caps tokens, nodes, depth and characters and sets `truncated`.
- `A11yTree::image_regions` returns image/graphic nodes worth OCR, largest first and bounded by the budget. This is what makes region OCR resource-efficient: instead of OCR-ing a whole frame, `ocr.rs` crops only those rectangles (`sips`/`magick`/`convert`) and runs Tesseract on them, with a small bounded cache keyed by screenshot path and region. If no crop tool exists it returns an error rather than silently OCR-ing the whole screen. OCR remains opt-in.
- `crates/context-core/src/config.rs` reads a JSON config from `$CONTEXT_LENS_CONFIG`, else `$XDG_CONFIG_HOME/contextlens/config.json`, else `~/.config/contextlens/config.json`. It controls the a11y mode and budget, the OCR policy, the shortcut bindings and the overlay defaults. A missing file means defaults; a malformed file is reported, never silently ignored. CLI flags override the file, which overrides the defaults.
- `crates/context-core/src/shortcuts.rs` holds the twelve stable action names and their accelerators, parses and validates bindings, and reports platform notes. The desktop shells register them with their own toolkits; out-of-focus global grabs require the desktop's own hotkey tool and the app never installs a grab silently. Every shortcut is also reachable from the UI without a keyboard.
- All of this keeps the permission-based model: accessibility is read only for the authorized foreground application, OCR only runs after explicit opt-in, and no capture happens when the accessibility text is sufficient.

## Audio
- Explicit FFmpeg microphone recording is available with a user-chosen source, finite duration and output file; local Whisper CLI can transcribe the resulting WAV. Batch audio-practice composes these steps.
- Finite user-approved macOS ScreenCaptureKit audio and Linux Pulse sink monitor recording are implemented. Local whisper.cpp synthetic speech transcription is verified. Continuous captions, voice activity detection and diarization remain pending.

## Providers
- Mock model always works offline for test-only echo; live local Ollama qwen2.5-coder:1.5b answered a sample algorithm question.
- OpenAI-compatible chat completions use BYOK. Send extracted text, not screenshots. HTTPS is required for nonlocal URLs. Explicit provider fallback never silently uses a second API service.
- Codex provider invokes the user-installed, officially authenticated CLI with read-only sandbox and stdin prompt. Subscription plan restrictions and availability apply; this isn't an API-key substitution.
- No assistant has privilege to execute code extracted from a captured screen. Screen text is untrusted data.

## Execution
- Session controls: Stopped / Active / Paused.
- Explicit authorization before capture.
- Bounded dedup cache; configurable sampling interval (2-3600s), maximum iterations (1-1000).
- Rust CLI is fully wired. macOS AppKit and Linux GTK3 thin native windows launch the Rust core with explicit session consent. GUI runtime testing is incomplete.
- Slint prototype remains excluded from the workspace; the platform-native shells avoid adding WebKitGTK or an Electron runtime.

### Floating overlay and capture exclusion
- `context-cli overlay` execs `apps/context-desktop/ui/floating_overlay.py`, a standalone GTK3 panel that renders the live question and answer. It performs no capture or inference itself; it consumes the same JSONL event stream (schema 1) as the desktop shell.
- `apps/context-desktop/ui/overlay_guard.py` is a pure, GTK-free module that detects the session/compositor and builds an `ExclusionPlan` from the window hints the platform actually offers. `capability_report()` reports honestly what is and is not excluded.
- Capture exclusion is best effort and Linux-only. X11 uses override-redirect plus `_NET_WM_STATE_SKIP_TASKBAR/PAGER` and a notification window type; compositors exposing `_NET_WM_STATE_SKIP_CAPTURE` may honour it. Wayland uses a `wlr-layer-shell` overlay layer, which the portal still lists as a toplevel. niri's user-configured `block-out-from=screencast` is the only compositor-level exclusion.
- No Linux API can exclude a window from full-screen capture (unlike Windows `WDA_EXCLUDEFROMCAPTURE` or macOS `sharingType`). The overlay therefore adds a behavioural guard: `ScreenShareWatcher` polls (default 2 s) for an active desktop ScreenCast session and hides the panel for the duration, restoring it afterwards. A manual hide is remembered.
- The overlay is a normal, identifiable process. It never masquerades as a system component, bypasses an OS permission prompt, or hides capture from the user.

## Privacy
- No background persistence or login-item auto-start.
- No disguised process names.
- Redact user secrets if adding structured diagnostics.
- Mac AX and Linux AT-SPI exclude recognized secure controls and avoid reading the app's own window. Further privacy review and an application allowlist are required before general use.
