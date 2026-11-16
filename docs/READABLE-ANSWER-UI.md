# Linux reading workspace — design and verification

## Delivered behavior

The Linux control window is a light workspace with five pages: **Session**,
**Capture sources**, **Appearance**, **Shortcuts**, and **Tools & diagnostics**.
The floating window pairs a dark command capsule with a separate translucent
answer card. The blue, right-aligned question stays above its answer.

- **Pin question** holds that Q&A pair as newer speech/questions arrive. Incoming
  tokens still belong to the active request. **N new · Latest** returns to the
  current pair; history rows and previous/next shortcuts select retained pairs.
- Up to 24 pairs stay in memory, preserving a pinned pair during eviction.
  Restarting capture creates a fresh session/history. Stop retains the visible
  answer and its completion/partial status while stopping the live session.
- Paragraphs, headings, emphasis, lists and fenced code are rendered locally.
  Code keeps its indentation and horizontal scrolling; Copy keeps Markdown.
  The `Question:`/`Answer:` provider envelope is displayed once, without repeating
  the captured accessibility tree. Model content is never executed.
- Answers follow the bottom during streaming only if the reader is already at
  the bottom. Reading higher in a long answer preserves that scroll position.
- Background opacity defaults to **90%**, configurable **55–100%**. Text stays
  opaque at rest. Text size defaults to **16 px**, adjustable **13–22 px**.
  Preferences preview immediately and persist on Save.
- **Cursor blending: On/Off** is available in the panel toolbar and Appearance.
  On passes background hover/clicks through on X11/XWayland; off restores full
  panel selection and scrolling. Close, Pin, the prompt/Send controls and resize
  handle remain input-active. The toolbar saves only this setting immediately;
  other Appearance previews still need Save. `desktop.appearance.cursor_blending`
  defaults to `true`. Native Wayland
  disables the unsupported toggle and uses normal full-panel input.
- **Capture screen** reads once; the prompt field and **Send** accept a custom
  question, using the current screen context when available. Listening can read
  the screen in parallel after the combined screen/audio authorization.
- Clicking the prompt explicitly permits keyboard focus; Enter sends once and
  clears the field. Clicking selectable text permits selection when blending is
  off. Leaving the panel restores nonactivating behavior; showing it does not
  automatically focus it.
- The standard panel is **720 × 620**; compact is **560 × 456**. Drag the
  SocketStream portion of the capsule to move it; drag the lower-right handle to
  resize it. Native Wayland placement/resize requests depend on its compositor.
- Map fades are **140 ms**, stack/revealer transitions **160 ms**, and button
  transitions **120 ms**. Reduced motion and GTK's animation preference disable
  these effects. There is no perpetual typing animation.
- All seventeen Linux shortcuts can be typed or recorded in-app. Their scope is
  the focused **control window**. Save applies immediately; Reset edits the form
  until saved. See [SHORTCUTS.md](SHORTCUTS.md) for defaults and persistence.

Closing the workspace keeps the session running in the background. Closing the
panel hides just the panel. **Quit SocketStream** stops owned processes and exits.

## Public reference and interpretation

The redesign follows public Cluely screenshots inspected on 2026-11-15:

| Source | Evidence used |
|---|---|
| [Changelog](https://docs.cluely.com/changelog) — `usescreen.png`, `newdesktopdesigns.png` | Floating capsule/card separation, 8 px gap, dark `#17171c` surfaces, pale-blue question, left-aligned answer |
| Changelog — `NewDashboardv3.png` | Light `#fafafa` workspace, `#f4f4f5` panels, restrained borders and blue accents |
| [Live Insights](https://docs.cluely.com/feature/liveinsights) | Question/answer presentation and compact session controls |
| [Settings](https://docs.cluely.com/feature/settings) | Editable shortcut affordance and settings grouping |
| [Public site](https://cluely.com/) | Supporting palette/motion references |

The parent directly inspected the three principal screenshots, alongside measured
reference notes. Public images do not establish Cluely's exact desktop animation
timings, font, outer radius or backdrop blur. The timings above are SocketStream
choices. This GTK implementation uses real background alpha, without claiming a
pixel-identical Cluely app or compositor-provided backdrop blur. SocketStream keeps
its own name, controls, providers and process identity.

## Verification scope

Testing uses synthetic questions and a synthetic JSONL child process with isolated
temporary preferences. It covers the actual GTK widgets, not just mocked UI calls.
The verified host is Ubuntu 26.04.1 x86_64, GNOME 50, GTK **3.24.52**; the panel
uses the existing XWayland dock path.

`tests/gtk_workspace_checks.py` covers:

1. CSS/layout, all workspace pages and compact reading space.
2. Shared pin/history state, new-question counts, transcript and copied Markdown.
3. Long-answer scroll retention and horizontal code access.
4. Appearance preview, Save, reduced motion and GTK animation preference.
5. Shortcut recording, conflicts, Save, Reset and enabled state.
6. Pause/Resume, Stop, new-process identity and pipe cleanup.
7. Stop-before-startup and natural replay completion.
8. Capture-setup completion after window exit, using a synthetic worker.
9. Real pointer drag/resize and `WM_CLASS=socket_stream_overlay`.
10. Physical-keycode Super+F8 dispatch to the focused control window.
11. Cursor-blending controls, input-region transitions and saved preference.
12. Physical prompt click, typing and Enter with default-ON blending.

The pointer check measures **(600, 230) → (664, 266)** and
**720 × 620 → 800 × 680**. XTest supplies input under Xvfb. For GNOME Wayland,
`tests/run_gtk_input_checks.py` creates a temporary, explicitly named uinput
device, runs GTK as the desktop owner and destroys the device in `finally`.
No input service or permanent device is installed. XTest clicks were not delivered
to GTK on this GNOME session; xdotool's symbolic Super+F8 synthesis also added an
unwanted Alt modifier, so the key test sends actual keycodes.

The compositor-input check found a genuine drag issue after the capsule redesign:
module-level `Gtk.grab_add/grab_remove` were unavailable. Using the widget methods
restored motion/release delivery outside the small drag handle. Hide and destroy
release active drag/resize grabs.

### Reproduce

```sh
SOCKET_STREAM_GTK_POINTER_CHECKS=1 xvfb-run -a -s '-screen 0 1920x1080x24' \
  python3 -W ignore::DeprecationWarning -W error::ResourceWarning \
  tests/gtk_workspace_checks.py
```

For GNOME compositor input, run `tests/run_gtk_input_checks.py --user <desktop-user>`
as root with that session's `DISPLAY`, `XAUTHORITY`, `WAYLAND_DISPLAY`,
`XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`. The desktop should be idle during
these opt-in pointer/key checks. `SOCKET_STREAM_GTK_ARTIFACT_DIR` retains PNG
drawings of only the synthetic test windows.

Headless regressions additionally cover request/Stop gates, pinned-pair eviction,
Markdown parsing, defaults parity, config refusal/atomicity, concurrent Python
writers and Python-flock/Rust-CLI coordination. The shared prompt is checked in
real local HTTP fixtures for streaming and ordinary chat-completions requests.
The streaming fixture now consumes the complete POST before closing, preventing
TCP resets with the longer system prompt.

## Independent review and remaining limits

The UI/state/prompt pass and shared persistence changes received separate read-only
reviews. Persistence approval includes independent no-follow, malformed-file,
mode, preservation and locking probes. Follow-up review approved shared
HOME-absent path handling and strict rejection of explicit-null appearance sections.
The late capture-setup callback has an exit guard and a real GTK regression.

This verification does not establish native Wayland behavior outside GNOME,
out-of-focus global shortcuts, macOS support for the five new actions, real audio
device latency/permissions or multi-hour soak behavior. The bundled English
Moonshine engine and existing provider configuration remain in use.

Capture exclusion retains `socket_stream_overlay` identity and the existing
private Mutter runtime. Its prior successful activation and user-confirmed sharing
result are recorded in [GNOME-CAPTURE-EXCLUSION.md](GNOME-CAPTURE-EXCLUSION.md).
Synthetic widget drawings, runtime activation and user confirmation are separate
from automated received-share-pixel evidence, which remains incomplete.

## Verification record (2026-11-16)

- Rust: 50 CLI and 134 core tests passed; formatting and Clippy passed.
- Local Python unit suite: 604 tests OK (6 skipped).
- Acceptance suite: 26 OK (6 skipped) with `CARGO_TARGET_DIR` set to the current
  build; leaving it unset lets the suite pick up a stale worktree binary and
  produce six false failures, so the variable is part of the documented command.
- Shortcut defaults parity: 2/2 (Python ↔ Rust ↔ Swift).
- GNOME Wayland desktop GTK input suite: **13/13** passed with the saved cursor
  toggle. Physical prompt typing and Enter dispatched exactly one `aa` question.
  Independent XShape readback found **7 rectangles ON / 1 rectangle OFF**, with
  the answer background excluded initially and after toggles, and the controls
  included. The test uses production scheduling, without directly calling the
  private shape-application method.
  The key test obtains a real X server timestamp, requests focus on the
  mapped control window and verifies focus before physical uinput Super+F8.
  Earlier drag-then-key failures used stale XQueryPointer coordinates to establish
  focus; those coordinates are not authoritative over native Wayland surfaces.
  The role of Xwayland warp emulation or a compositor guard in those failures
  remains unproved. Experimental explicit-cursor hooks were removed.
- The input-shape failure was an invalid Pycairo API call: `Region` has `union`,
  not `union_rectangle`, on the tested Cairo 1.27.0. The exception had been
  swallowed. The corrected call uses `union(RectangleInt(...))` and stores the
  region through GTK's widget API so layout/CSD updates retain it. Earlier
  scheduling/asynchrony explanations did not establish the cause.
- The manual vision regression failed against the old CLI's text-only request
  and passed after reading PNG bytes before stopping/dropping the owning reader.
  Three real-CLI/local-HTTP cases check byte-identical PNG attachment,
  `--no-vision`, and temporary-frame cleanup. Capture/OCR helpers are synthetic;
  these cases do not measure the desktop capture pipeline.
- Independent read-only review found no release-blocking defect in the single-key
  persistence, focus/input shape, extraction/OCR or frame-lifetime changes.
  Cursor-only saves intentionally preserve existing appearance values without
  numerically normalizing unrelated fields. Packaging and the bounded restoration
  of the temporary GDM auto-login setting received separate read-only review.
