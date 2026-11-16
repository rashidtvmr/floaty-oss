# SocketStream architecture

## Goals
Low-overhead Rust desktop assistant for coding practice, accessibility assistance and authorized interviews. The product is not a covert proctoring-bypass tool.

## Capture
- Use frontmost application accessibility text when it is accessible to the logged-in user and authorized by OS permissions.
- macOS: AXUIElement; Linux: AT-SPI2 through PyGObject helper. Both are currently one-shot helpers; automatic observation is via explicit sampling. The macOS helper roots at the focused/main window (not the application element, whose first child is the global menu bar), skips `AXMenuBar`, excludes secure fields, and records focus so `auto` rendering can pick the real content element.
- Text coverage recognizes short questions, code and dense-script text. When
  useful text is unavailable, an explicit OCR opt-in permits an approved
  screenshot and local OCR (macOS Apple Vision; Linux Tesseract). Enabled eligible
  image regions are handled before the readable-text early return.
- Captures are session-temporary. The audio/manual-ask vision path can retain the
  current approved frame for a configured compatible provider; `--no-vision`
  disables attachment. A later accessibility-only read clears the previous-frame
  reference. User-requested file exports are explicit.
- Some code editors expose only the focused line; OCR is necessary in these cases.
- GNOME/KDE Wayland use the user-approved XDG ScreenCast/PipeWire backend when OCR is required; wlroots grim and X11 import remain alternatives. Real Linux portal E2E pending.

## Accessibility tree, region OCR and shortcuts
- The AT-SPI2 (Linux) and AX (macOS) helpers accept `--mode tree|text` and print one schema-1 JSON object in `tree` mode: a flat pre-order node list with `id`, `parent`, `role`, `name`, `text`, `focused`, `bounds`, `image` and `protected`, plus `truncated` and `node_count`. `--mode text` keeps the previous flat-text output byte-for-byte.
- `crates/socket_stream_core/src/a11y.rs` parses that JSON into an `A11yTree` and renders it under a `TokenBudget` in `focused`, `tree`, `text` or `auto` mode. Rendering is pure and deterministic: structural roles are skipped, protected nodes never render text, duplicate `(role, text)` pairs are emitted once, and the budget caps tokens, nodes, depth and characters and sets `truncated`. The default mode is `tree` and the default budget is depth 20 / 800 nodes / 32k chars / 4000 tokens: a browser nests its page content (`AXWebArea`) about eight levels below the window and the question several levels below that, so `focused`/`auto` (and the old depth-8 budget) returned only the browser chrome, never the page.
- The live/manual/audio reader uses `render_question()` before spending the
  model budget: explicitly marked navigation, toolbar, menu, banner and status
  subtrees are removed, as are protected descendants. Window titles are metadata.
  Remaining content keeps document order and code whitespace; unknown layouts
  are preserved. The role-free display is used to assess text coverage.
- `A11yTree::image_regions` returns image/graphic nodes worth OCR, largest first and bounded by the budget. This is what makes region OCR resource-efficient: instead of OCR-ing a whole frame, `ocr.rs` crops only those rectangles (`sips`/`magick`/`convert`) and runs Tesseract on them, with a small bounded cache keyed by screenshot path and region. If no crop tool exists it returns an error rather than silently OCR-ing the whole screen. OCR remains opt-in.
- `crates/socket_stream_core/src/config.rs` reads a JSON config from `$SOCKET_STREAM_CONFIG`, else `$XDG_CONFIG_HOME/socket_stream/config.json`, else `~/.config/socket_stream/config.json`. It controls the a11y mode and budget, the OCR policy, the shortcut bindings and the overlay defaults. A missing file means defaults; a malformed file is reported, never silently ignored. CLI flags override the file, which overrides the defaults.
- `crates/socket_stream_core/src/shortcuts.rs` holds seventeen stable action names and their accelerators (Linux set includes five Q&A actions; macOS currently implements 12), parses and validates bindings, and reports platform notes. The desktop shells register them with their own toolkits; out-of-focus global grabs require the desktop's own hotkey tool and the app never installs a grab silently. Every shortcut is also reachable from the UI without a keyboard.
- Linux shortcut/appearance saves and Rust shortcut resets share a POSIX `flock`
  on `<config>.lock`, no-follow regular-file reads and atomic `0600` replacement.
  They merge against the current JSON under the lock, retaining unrelated and
  unknown fields. Binding maps are full snapshots. Malformed/non-object JSON and
  symlink/non-regular targets are refused. Appearance lives in `desktop.appearance`;
  the legacy `overlay` section remains separate. See [SHORTCUTS.md](SHORTCUTS.md).
- Accessibility is read for the authorized foreground application. OCR is
  explicitly enabled; image-region OCR can augment otherwise sufficient text.
  Region failures preserve usable accessible content. Whole-frame fallback needs
  the separate general OCR opt-in, and force-OCR retains whole-frame semantics.
- `desktop.appearance.cursor_blending` defaults to `true`. GTK uses coalesced GLib
  input-shape updates on X11/XWayland; off restores the full input region. The
  toolbar saves immediately and mirrors Appearance. Native Wayland disables the
  unsupported control while preserving its stored preference.

### Adoption status of the accessibility-first recommendations

The current release implements question-scoped context, coverage-based OCR
decisions, image-region precedence, stale vision-frame invalidation and a
reproducible synthetic extraction/OCR benchmark. Native AT-SPI2 and AX remain the
default. See [EXTRACTION-BENCHMARK.md](EXTRACTION-BENCHMARK.md) for measured scope.

The next changes need their own implementation and runtime evidence:

- Event-driven observation: AT-SPI event subscriptions / AXObserver, debounced
  stable-context reads, cancellation and a periodic resynchronization fallback.
- Persistent OCR workers and content-based region caching: the current backend
  starts one process per OCR call; the small cache uses path/rectangle plus file
  size, modification time and segmentation mode.
- Optional Agent Browser reader: explicit existing CDP connection and selected
  content scope, disabled by default. A bundled adapter is pending; version
  capabilities, protected form values, daemon cleanup and automatic browser
  side effects require fixture and real-browser checks first.
- Interactive computer actions: a separate explicit grant, action/time bounds,
  cancellation on Stop and review before externally visible actions. Screen
  reading permission does not enable browser clicks, typing or submission.

## Audio
- Explicit FFmpeg microphone recording is available with a user-chosen source, finite duration and output file; local Whisper CLI can transcribe the resulting WAV. Batch audio-practice composes these steps.
- Finite user-approved macOS ScreenCaptureKit audio and Linux Pulse sink monitor recording are implemented. Local whisper.cpp synthetic speech transcription is verified. Continuous captions, voice activity detection and diarization remain pending.

## Providers
- All HTTP chat paths and Codex stdin share a byte-stable, versioned system
  prefix. Per-question material is appended afterwards. Native OpenAI gets a
  stable cache-routing key; native OpenAI/DeepSeek streaming requests include
  usage. Compatible gateways receive no unverified vendor fields. Actual reported
  cached-input counts are shown in GTK; missing metrics remain unknown, not zero.
  Provider cache hits and cost savings cannot be guaranteed by the client. See
  [prompt/cache/turn scope](AI-PROMPT-CACHING-AND-TURNS.md).
- Mock model always works offline for test-only echo; live local Ollama qwen2.5-coder:1.5b answered a sample algorithm question.
- OpenAI-compatible chat completions use BYOK. Text is the default; authorized
  audio/manual-ask vision requests can attach the current screenshot as a data
  URL. HTTPS is required for nonlocal URLs. Explicit provider fallback never
  silently uses a second API service.
- Codex provider invokes the user-installed, officially authenticated CLI with read-only sandbox and stdin prompt. Subscription plan restrictions and availability apply; this isn't an API-key substitution.
- No assistant has privilege to execute code extracted from a captured screen. Screen text is untrusted data.
- Shared system instructions keep the `Question:`/`Answer:` envelope and request
  complete runnable coding solutions, assumptions, approach, complexity and edge
  cases. The reading UI strips that envelope and renders inert Markdown spans;
  it never executes model output or loads embedded resources.

## Execution
- Session controls: Stopped / Active / Paused.
- Explicit authorization before capture.
- Bounded dedup cache; configurable sampling interval (2-3600s), maximum iterations (1-1000).
- Rust CLI is fully wired. macOS AppKit and Linux GTK3 thin native windows launch the Rust core with explicit session consent. The installed macOS bundle has been driven end-to-end with the mock provider, real OS permissions, the floating panel, and capture-exclusion checks; Linux GUI runtime testing is still incomplete.
- Slint prototype remains excluded from the workspace; the platform-native shells avoid adding WebKitGTK or an Electron runtime.

### Linux GTK workspace

- `linux-gtk.py` owns process control and exposes `create_window()` for real GTK
  integration checks. `workspace_ui.py` builds the light Session, Capture sources,
  Appearance, Shortcuts and Tools pages. `ui_theme.py` supplies scoped GTK CSS.
- `live_state.py` separates the live-engine `snapshot()` from `display_snapshot()`
  and its bounded 24-pair, in-memory history. Pinning selects a presentation pair
  without redirecting tokens. Eviction preserves the pinned pair; a new process
  receives a fresh state instance. Pause/completion/error close the request gate.
- The controller and `FloatingOverlay` share that instance. A generation/process
  check rejects stale main-loop callbacks. Stop closes the instance immediately,
  including queued startup; process stdout/stdin are closed after the reader ends.
- `answer_format.py` parses a small inert Markdown subset. Paragraphs wrap, fenced
  code preserves indentation and horizontal access, and streaming updates preserve
  a reader's scroll position. Copy returns the selected answer's raw Markdown.
- Comparison pipe tables render with aligned monospace columns and a header;
  provider-reported token/cache counts share the answer metadata row. The
  listening dialog assigns a source role (not voice identity). The Moonshine
  turn gate suppresses interviewee answers before model dispatch and retains
  bounded question/answer context for explicit clarification. Mixed-source
  diarization and simultaneous microphone/playback capture are not implemented.
- `Gtk.AccelGroup` registers all seventeen actions on the control window only.
  The shortcut editor suppresses dispatch during recording/entry edits, validates
  duplicates and applies changes on Save. Appearance previews are immediate and
  become persistent on Save. See [READABLE-ANSWER-UI.md](READABLE-ANSWER-UI.md).

### macOS AppKit shell

- `apps/socket_stream_desktop/ui/mac-app.swift` is the native shell. It builds the main window, starts `socket_stream observe-live` with `--authorized` only after the consent alert, consumes schema-1 JSONL on stdout, and mirrors the state machine in `apps/socket_stream_desktop/ui/live_state.py` (`LiveState` + `--self-test`).
- The floating panel is an `NSPanel` with `.statusBar` level, `.fullScreenAuxiliary`, `.canJoinAllSpaces`, `.nonactivatingPanel`, `hidesOnDeactivate = false`, `alphaValue = 0.94`, and `sharingType = .none` for stealth. It never hides on focus changes or share detection; only Close/Hide changes visibility. `canBecomeKey`/`canBecomeMain` return `false`, so no panel click activates SocketStream (`.nonactivatingPanel` alone still activates on text selection) and the practice window keeps focus.
- `scripts/build-macos-helpers.sh` compiles the AX, Vision OCR, system-audio, and GUI executables. `scripts/build-macos-app.sh` assembles `SocketStream.app`, with the GUI and `socket_stream` in `Contents/MacOS` and the helpers in `Contents/Resources/target/native-helpers`. It signs with a stable local identity (`scripts/create-macos-signing-cert.sh`) when one exists, signs the `Contents/Resources` helpers explicitly because `codesign --deep` skips them, and falls back to ad-hoc with a warning. `crates/socket_stream_core/src/paths.rs` and `ocr.rs` resolve those bundle paths before falling back to the repository layout.
- Permissions are requested through the documented `AXIsProcessTrustedWithOptions` and `CGRequestScreenCaptureAccess` APIs, never by editing TCC. The installed bundle has its own permission identity. TCC stores a code-signing requirement per grant; ad-hoc signing pins it to a cdhash that changes on every rebuild, so the grant silently stops matching. The stable local identity keeps the requirement certificate-based and survives rebuilds. `scripts/reset-macos-permissions.sh` and the app's **Repair permissions** action clear stale rows.
- `socket_stream overlay` launches the AppKit app with `--panel-only` on macOS and forwards only the panel flags it supports. The GTK overlay remains the Linux implementation.
- `scripts/build-macos-app.sh`, `scripts/start-macos.sh`, and `docs/MACOS-TESTING.md` document build, permission, panel, and capture-exclusion verification. The real remote-share test remains pending; `screencapture` exclusion is verified.

### Floating overlay and capture exclusion
- `socket_stream overlay` execs `apps/socket_stream_desktop/ui/floating_overlay.py`, a standalone GTK3 panel that renders the live question and answer. It performs no capture or inference itself; it consumes the same JSONL event stream (schema 1) as the desktop shell.
- `apps/socket_stream_desktop/ui/overlay_guard.py` is a pure, GTK-free module that detects the session/compositor and builds an `ExclusionPlan` from the hints the platform actually offers. `capability_report()` reports separately whether a compositor rule is supported, whether one is configured, and that runtime verification is still pending.
- Capture exclusion is Linux-only and compositor-specific. X11 uses `Gdk.Window.set_override_redirect` (verified `Override Redirect State: yes` on GTK 3.24.52) plus `_NET_WM_STATE_SKIP_TASKBAR/PAGER` and a notification window type; compositors exposing `_NET_WM_STATE_SKIP_CAPTURE` may honour it. On GNOME Wayland the app starts GTK with `GDK_BACKEND=x11` and uses the X11 dock type so the panel stays above fullscreen windows; KDE, niri and Hyprland stay on Wayland for their capture rules. GNOME Shell 50/mutter 18, X11 and sway have no per-window capture-exclusion rule.
- `apps/socket_stream_desktop/ui/capture_rules.py` writes the documented per-window rules for niri (`block-out-from "screen-capture"`), Hyprland 0.53+ (`no_screen_share = true`) and KDE Plasma/KWin 6.7+ Wayland (`excludefromcapture`), only after an explicit setup action. It backs up the original file, validates/rolls back, and removes its managed block and KWin registry entry. The panel advertises `app_id`/`WM_CLASS` `socket_stream_overlay` (runtime-verified on GNOME 50 / GTK 3.24.52) so an exact-match rule targets the panel, not every SocketStream window.
- `native/mutter-capture-filter` adds an opt-in capture-only actor predicate and filtered offscreen rendering to a custom Mutter 50.1 build. It requires compositor deployment and `SOCKET_STREAM_CAPTURE_EXCLUDE=1` in that compositor's environment; app environment flags cannot activate it in stock GNOME. Its README tracks runtime coverage.
- Linux panel background opacity defaults to 90% (55–100% configurable); text is
  opaque at rest. Scoped RGBA CSS paints a command capsule and answer card with a
  transparent gap. A compositor is required for transparency; otherwise surfaces
  become opaque. Map fades use 140 ms; stack/revealer transitions use 160 ms.
  Reduced motion and GTK's animation preference disable these transitions.
- No Linux API can guarantee exclusion from full-screen capture (unlike Windows `WDA_EXCLUDEFROMCAPTURE` or macOS `sharingType`). `ScreenShareWatcher` polls (default 2 s) for status, but the panel stays visible across focus changes and detected shares. It may appear in a full-screen capture unless a documented compositor rule is configured and real-share tested; only an explicit user hide/close changes its visibility.
- The overlay is a normal, identifiable process. It never masquerades as a system component, bypasses an OS permission prompt, or hides capture from the user.

## Privacy
- No background persistence or login-item auto-start.
- No disguised process names.
- Redact user secrets if adding structured diagnostics.
- Mac AX and Linux AT-SPI exclude recognized secure controls and avoid reading the app's own window. Further privacy review and an application allowlist are required before general use.
