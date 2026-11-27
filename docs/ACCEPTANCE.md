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

Linux packaging update (2026-11-03): the x86_64 Linux-musl CLI archive was cross-built on macOS and its SHA-256 verified. The packaged ELF cannot be executed on this macOS host; packaged provider E2E and desktop runtime checks remain pending on Linux. The source-tree Python acceptance suite passes locally.

Streaming/compositor update (2026-11-15): built-in Moonshine English streaming
speech passes synthetic PCM-to-answer tests, including Pause/Resume, Stop during
initialization, authorization rejection, GUI control EOF, and full-output-pipe
shutdown, plus shared-terminal input semantics. GTK and Swift reader-to-main-loop
handoffs are bounded to 32 pending callbacks and Swift uses a readiness read so
short events are not delayed. Both UI state machines cover repeated requests after Resume and bounded
transcript updates. The runtime builds/initializes on macOS arm64 and Ubuntu
26.04.1 x86_64; real audio-device permission/latency tests remain pending. See
`MOONSHINE.md`. Custom Mutter passes headless native/static/animated capture
tests; KDE configuration tests pass. The installed GTK app subsequently configured
the private GNOME runtime, and a normal logout/sign-in activated it with all four
matching mappings, opt-in and a ready guard. The user confirmed the live sharing
result works. Automated live received-frame verification, custom-desktop normal
logout/rearm and KDE sharing remain incomplete. See
`GNOME-CAPTURE-EXCLUSION.md` and `../native/mutter-capture-filter/VERIFICATION.md`.

Workspace/input update (2026-11-16): Rust formatting/Clippy, 50 CLI and 134 core
tests passed. The macOS Python suite passed 604 tests (6 skipped), and acceptance
passed 26 (6 skipped). On Ubuntu GNOME/XWayland, all 13 real GTK checks passed,
including physical prompt typing/Enter, drag/resize, shortcuts and production-path
XShape readback (7 regions ON, 1 OFF). Six embedded extraction fixtures passed
fidelity checks; measured OCR scope is in `EXTRACTION-BENCHMARK.md`. The manual
vision regression proves PNG attachment with synthetic capture and a local HTTP
provider. These results do not establish hardware-audio latency, real Linux
portal-to-model capture, native Wayland input shaping or long-session reliability.

Fixed-window/playback update (2026-11-16): explicit AT-SPI unique-bus/object-path
selection reads only a user-selected window, never silently retargets after close,
and disables all pixel/region-OCR/portal/vision fallback. Existing stale-background
focus protection is retained. Independent review approved the capture boundary.
Ubuntu real-desktop GTK fixture -> AT-SPI question -> completed mock answer and
closed-target refusal passed, including forced OCR/portal options. Ubuntu passed
8 synthetic speech/screen acceptance checks and 25 GTK checks (3 physical-input
checks skipped). Rust fmt/Clippy and 197 tests passed; Python passed 646 tests
(6 skipped); macOS acceptance passed 37 (7 skipped). A private Pulse null-sink
test with real FFmpeg/Moonshine produced 5 transcript updates, a playback-source
binary-search question and a completed mock answer without microphone/physical
speakers/remote AI; default output was unchanged and fixtures removed. Hardware
headphone/Bluetooth switching, arbitrary app routing, inaccessible application
trees and native Wayland-only application behavior remain unverified.

Panel-session/profile UX update (2026-11-16): Rust fmt/Clippy and 197 tests pass;
Python passes 655 tests (6 skipped), and source acceptance passes 37 (7 skipped).
Ubuntu GTK passes 35 checks (3 physical-input checks skipped), including restart
from the answer panel after Stop, declined consent retaining history, per-profile
model persistence, multiple saves without closing the manager, New clearing draft
keys, rename retaining unsaved edits, validation failures preserving runtime/disk,
late catalog rejection, both themes and compact answer reading space.
An independent UI/capture review approved the explicit Use model gate: Start with
an unapplied model neither saves settings nor reaches consent/preflight/capture.
Independent storage/catalog review approved the credential-preserving mutations,
empty-book handling, bounded model discovery and inventory-only audio listing;
27 scoped synthetic tests passed without using user credentials or services.
Actual saved compatible-provider discovery returned 69 model IDs without inference.
The same saved compatible profile passed a fixed-prompt inference test in 3.9s,
without fallback or screen/audio content. Real audio
inventory exposed 5 playback monitors and 3 microphone inputs without recording.
The official Codex default-model CLI independently retried transport and timed out;
login/catalog success remains explicitly insufficient proof of inference access.
See `PANEL-SESSION-PROFILES.md` for workflow and remaining hardware/platform limits.


Ubuntu accessibility/session update (2026-11-27): foreground selection now
excludes GNOME/Mutter mirrors consistently, native AT-SPI identities survive
reads, explicit GI calls and bounded deadlines handle backend failures, and
launcher/preflight use distro Python. Keyed snapshot/patch context is connected
to observe-live and screen-assisted Moonshine, with successful-only commits,
bounded dialogue checkpoints, focused-scope preservation and generation-tagged
pause/resume reads. Rust fmt/Clippy and 259 Rust tests pass. Python unit tests
ran 697 cases (691 passed, 6 skipped), and acceptance ran 45 (38 passed, 7
skipped). Changed production runtime line coverage is 95.67%; the five feature
modules each exceed 98%. Coverage excludes inline Rust test bodies and reports
whole-workspace figures separately. See INCREMENTAL-SCREEN-CONTEXT.md and
SCREEN-READING-COVERAGE.json for exact scope and denominators.

A private native Weston/AT-SPI fixture was added but explicitly skipped here:
the executor rejects AF_UNIX sockets with EPERM, so D-Bus and Wayland cannot
start. Actual Ubuntu 26/GNOME/browser behavior, graphical portal prompts and
hardware remain unverified in this change. Native GI identity/interface
declarations were checked without reading a desktop. Independent reviews
approved the capture boundaries, generation fence, transactional context and
the explicit settings-depth preservation fix.
