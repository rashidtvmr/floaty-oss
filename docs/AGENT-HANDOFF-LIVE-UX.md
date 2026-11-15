# P0 — Complete the Linux live-practice experience

**Ticket:** `LIVE-EXPERIENCE-004`
**Owner:** next implementation agent (`live-experience`)
**Status:** READY / NOT IMPLEMENTED
**Primary target:** Linux x86-64, GNOME Wayland + X11; keep macOS compatible
**Read first:** `AGENTS.md`, `docs/ACCEPTANCE.md`, `docs/LINUX-TESTING.md`

## Update (accessibility tree, region OCR, shortcuts, device auth)

A later wave extends this work; see `docs/A11Y-TREE-SHORTCUTS.md` for the frozen
contract. It adds structured accessibility-tree reading with a token budget
(`A11Y-TREE-006`), region OCR of image/graphic nodes (`OCR-REGION-007`),
configurable global shortcuts for every feature (`HOTKEYS-008`) and a native
Codex/ChatGPT device-authorization flow (`CODEX-DEVICE-009`). Those changes do not
alter the acceptance criteria below; the A01–A16 table remains the original
contract for the live-practice experience.

## 0. Problem statement and why the current release failed

The existing Linux build opens a large GTK diagnostic window with buttons and a monospaced log.
The CLI has `observe` and `audio-practice`, but there is **no working integrated live-product UX**.
The `observe` command emits human-readable text delimiters (`--- New screen text ---`,
`--- Answer ---`) instead of machine-readable events. Audio is currently finite-duration and
initiated separately. The GTK window does not display a persistent, independently updateable answer.
The model selector defaults to `mock`, which cannot provide real answers. The Linux AT-SPI and
portal runtime have not been validated on a real Wayland desktop.

**Business acceptance is based on a human using the shipped Linux archive, not on unit tests
passing or a CLI example appearing to work.** The next agent should not label this ticket done
until the end-to-end evidence below exists.

## 1. Required user journey (P0)

1. User extracts the Linux archive and launches `./socket_stream` from a normal desktop session.
2. A clean, minimal launcher shows provider status, the selected capture source(s), an explicit
   **Start practice** button, and a clearly visible session state. The current diagnostic log is
   moved to an expandable diagnostics area, not the product's main screen.
3. User selects a **permitted practice window/app** and authorizes text observation. In a browser
   page that exposes the question through AT-SPI, the assistant reads the accessible text **without
   requesting a screen-share permission prompt**.
4. Within the next observation cycle, the assistant recognizes the question, places it in a
   separate resizable **floating practice-answer panel**, requests one answer from the selected
   working provider, and displays the returned response.
5. When the question changes, the panel replaces the question and response. Duplicate/unchanged
   question text causes **no additional provider request**.
6. User can pause and resume, copy the answer, inspect errors, and stop. Stop terminates all
   capture and model subprocesses; reopening the panel cannot restart observation.
7. As an *independent opt-in source*, user may enable microphone or playback-monitor transcription
   during practice. The app processes bounded speech chunks and updates the answer for new,
   complete recognized questions without requiring the user to press Record again for every one.
8. The source (screen / microphone / playback audio), recognition quality, and model name are
   visible. Errors are user-facing, not silently converted to mock answers.

The product must never claim to be functional because a simulated provider echoed input.
For the Linux release gate, repeat with a real locally running Ollama model.

## 2. Required architecture and data contract

### 2.1 Rust observation engine

Keep Rust core, existing Linux AT-SPI adapter, local Tesseract, selected provider and GTK shell.
Split observation from presentation.

- Export **JSON Lines** over a dedicated stdout/pipe channel; each line is one valid JSON object,
  ends in `\\n`, and is flushed immediately. Human diagnostics go to stderr and must exclude
  raw question text, audio content, API keys and tokens.
- Event schema version 1; include `schema`, `type`, `session_id`, `request_id` when applicable,
  `source`, `timestamp`, `sequence`. Supported types:
  `session_started`, `question_detected`, `answer_started`, `answer_delta`,
  `answer_completed`, `session_paused`, `session_resumed`, `error`, `session_stopped`.
- A request ID is immutable; a later question invalidates earlier requests. Discard old
  answer chunks and old worker completions. Prefer cancellation of superseded provider calls.
- Bound input/event line sizes, number of queued events, inference concurrency (one active
  request by default) and stored text. Refuse raw media or secret values in the event log.
- Add an explicit fixture replay mode (e.g. `replay-events --file FILE`), with **no** OS capture
  and **no** cloud calls. Tests must use this, not the participant's real screen.
- Do not break existing `observe` usage before the desktop changes to the event stream are ready.

Example wire events:

```json
{"schema":1,"type":"question_detected","session_id":"demo","request_id":"q1","source":"screen","sequence":2,"timestamp":"2026-11-03T00:00:01Z","text":"Find two numbers summing to a target"}
{"schema":1,"type":"answer_delta","session_id":"demo","request_id":"q1","source":"screen","sequence":3,"timestamp":"2026-11-03T00:00:02Z","text":"Use a hash map."}
{"schema":1,"type":"answer_completed","session_id":"demo","request_id":"q1","source":"screen","sequence":4,"timestamp":"2026-11-03T00:00:03Z"}
```

### 2.2 Accessibility-first reading; opt-in fallback

- Primary: AT-SPI2 foreground accessible text. Use bounds, focus state and existing
  protected-field filters. Do not recursively analyze SocketStream's own floating panel.
- Run OCR **only** if accessibility data is unavailable or too incomplete to interpret
  the question; do not automatically force OCR whenever an answer is needed.
- For GNOME/KDE Wayland, the screenshot fallback requires a user-approved XDG ScreenCast
  chooser; if denied or unavailable, display `OCR unavailable; accessibility-only`, and
  continue the authorized accessibility path. Do not bypass desktop permission controls.
- Show a deterministic source badge (`Accessibility`, `Local OCR`, `Microphone`,
  `Playback audio`) and a visible status. Never send raw screenshots to a cloud model by
  default. Text-only models must work with locally extracted text.
- Handle blank, secure-field-only, noisy and self-window observations without calling AI.
- Preserve code layout and operators, and show an OCR quality warning if confidence is low.
- When native Linux accessibility is unavailable, diagnose what dependency or permission is
  missing; don't silently start a different capture mechanism.

### 2.3 Bounded continuous audio (P1, after P0 screen pipeline is working)

- Separate, user-enabled microphone and playback-audio inputs; explicit input choice and
  prominent `Listening` state. Prefer locally running whisper.cpp.
- Capture and transcribe bounded sliding chunks with overlap, e.g. 3–8 seconds, without
  creating an unbounded backlog. Process complete/stable recognized questions, not each word.
- Use basic VAD/silence detection and stable transcript deduplication. Do not advertise
  diarization until it is implemented and independently evaluated.
- User can stop or revoke permission at any time; stop closes audio handles and removes
  temporary media. Keep the last answer visible but **no new answers after Stop**.
- If mic and screen ask about the same question, prefer one provider request. Distinct
  questions get new request IDs. Surface the source so a tester can audit behavior.

### 2.4 Floating answer panel, not a log window

- Separate, resizable, movable, clearly identifiable GTK top-level window, standard window
  manager behavior, normal process identity. The panel may be placed on top but must not
  bypass capture, share-selectors or desktop permissions.
- Layout: compact status/source row, **current question**, scrollable/selectable **answer**,
  small provider/model status, buttons for pause/resume, copy and Stop.
- Launching practice opens the panel automatically; it must not steal focus from the
  authorized question window after initialization.
- Show interim `Detecting` / `Thinking` states, incremental answer chunks where the
  provider supports streaming, otherwise update on completion. Never display an old response
  under a new question. A provider error must replace Thinking with an actionable message.
- Default to a configured real provider or an explicit provider setup prompt. The `mock`
  provider is exclusively identified as a development/test tool.

### 2.5 Inference/provider requirements

- Ollama loopback, OpenAI-compatible BYOK and the **official authenticated Codex CLI** must
  use the same event pipeline. Prefer local inference for repeatable demo and privacy.
- Verify the provider before starting or report an actionable error. No silent fallback
  to another remote endpoint. Preserve the custom endpoint validation already added.
- Stop/cancel interrupts in-flight requests. If a provider does not support streaming,
  show a pending state and post one atomic completion event.
- Never promise Codex subscription credits can be used as unrestricted API quota.
  In-app OAuth is a separate feature and must not block core Linux practice flows.

## 3. Non-negotiable acceptance criteria

| ID | Requirement | Verifiable pass condition |
| --- | --- | --- |
| A01 | Linux GUI starts | `./socket_stream` opens without Cargo or editing source paths on Ubuntu GNOME Wayland and one X11 desktop |
| A02 | Real answer panel | Separate floating GTK answer panel opens from Start, has question/answer/status/Stop/Copy; logs not the main UX |
| A03 | Screen reader priority | AT-SPI fixture returns readable question, exactly **zero** OCR/portal requests |
| A04 | OCR fallback | Blank AT-SPI triggers local OCR after explicit OS approval; portal denial fails closed |
| A05 | Live updates | Two different questions display two correct mock responses, without restarting the session |
| A06 | Dedup | Unchanged question across 10 observations produces exactly one model call |
| A07 | Stale results | Delayed answer for question 1 never appears after question 2 becomes current |
| A08 | Stop/Pause | Stop terminates active observation, audio and provider children; no post-stop model requests |
| A09 | Audio practice | Three prerecorded synthetic questions replayed as bounded chunks yield ordered distinct question events (no mic required in automated tests) |
| A10 | Live audio | Real mic and playback monitor tested with user permission, stop and temporary-file cleanup confirmed |
| A11 | Provider | Real locally running Ollama answers one imported question and one new observed question; `mock` is clearly labeled |
| A12 | Privacy | No keys, raw audio, screenshots or private question text printed into diagnostics or stored by default |
| A13 | Packaging | Linux x86-64 archive includes native shell, model instructions, helpers and tests; clean extract works |
| A14 | Reliability | 30-minute mock-replay soak: bounded memory/event queues, zero orphan subprocesses, no duplicate calls |
| A15 | Accessibility UX | All core UI controls keyboard-reachable; status messages meaningful, answer text selectable |
| A16 | End-user evidence | Short recorded Linux demo of screen source -> new question -> answer -> second question -> Stop, plus trace showing real model calls |

**Release rule:** All A01–A16 must be demonstrated. Unit tests alone are insufficient.

## 4. Agent execution order and ownership

1. **P0 (Rust/contract):** Define event schema, deterministic JSONL fixture replay, sequential state logic, explicit session lifecycle and tests.
2. **P0 (GTK/UI):** Replace log-centric interface with the floating answer experience and live event consumption, separately testable from GTK through pure Python `live_state.py`.
3. **P0 (Linux capture):** Verify AT-SPI first without portal; test OCR fallback and denial on real Wayland hardware; do not silently substitute screenshot capture.
4. **P0 (real inference):** Wire Ollama/mock/custom provider into the same state flow. Verify no stale results and cancellation.
5. **P1 (audio):** Implement continuous, bounded, opt-in microphone/playback transcription. Verify with synthetic audio; then actual hardware.
6. **P1 (QA/release):** Run smoke, security, concurrency/soak and real Linux E2E; build clean x86-64 release.

Agent should use separate worktrees, max four agents, no overlapping ownership domains.
**Do not change existing `in_review` task states merely to make the queue select work.**
An existing working-tree change should be reviewed and preserved, not overwritten or reset.
Do not modify the acceptance tests to make them green; implement their contract.
Tests may expose previously missing features; fix the implementation instead.

## 5. Machine-readable GUI state interface (contract)

A new import-safe, GTK-independent Python module MUST be placed at:

`apps/socket_stream_desktop/ui/live_state.py`

It must expose:

```python
class LiveState:
    def __init__(self): ...
    def apply(self, event: dict) -> bool: ...  # True only for visible state change
    def snapshot(self) -> dict: ...              # Plain JSON-compatible state
```

Expected snapshot keys: `status`, `session_id`, `request_id`,
`question`, `answer`, `source`, `error`.

Supported statuses: `idle`, `observing`, `thinking`, `ready`, `paused`, `error`,
`stopped`. `apply` validates schema and ignores events from foreign sessions,
old question IDs or after Stop. An event with a repeated question text must not
clear an existing answer. An `answer_delta` appends only to the current request.
On `answer_completed`, move to `ready`. Strip terminal control characters and
bound question/answer lengths. Do not incorporate unknown JSON keys such as `api_key`.

The pure module allows the next agent to test all ordering/cancellation edge
cases without access to a live GNOME desktop.

## 6. Required tests and exact commands

### Automated (run on Mac and Linux)

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
python3 -m unittest discover -s tests -q
# New RED acceptance suite; it MUST become GREEN before task is done:
python3 -m unittest discover -s tests/acceptance -p 'test_*.py' -v
```

The acceptance suite in `tests/acceptance` intentionally fails on today's code:
the new `live_state.py` contract and CLI event replay are not implemented yet.
**Keep it isolated from the baseline suite so unfinished product work does not
create a false CI regression.** Do not mark task done while it is red.

### Linux desktop E2E (must perform manually on the intended test machine)

```bash
./check-dependencies.sh
./socket_stream doctor
./socket_stream
```

- Focus a permitted simple HTML problem whose accessible text can be read by AT-SPI.
  Start one practice session with working Ollama. Verify a second problem automatically
  replaces the first answer.
- With AT-SPI available, verify that **no** screen-share chooser appears.
- Open a local image-only practice problem; explicitly approve the desktop portal
  and validate the OCR fallback. Deny and retry to validate failure behavior.
- Enable mic with a known synthetic speaker recording, then actual mic; confirm
  bounded transcripts, response updates, no lingering audio after Stop.
- Switch focus to SocketStream's own panel; it must not recursively answer itself.
- Collect CPU/RSS and child-process counts before, during and after Stop.
- Repeat with an external mock provider whose responses are deliberately delayed.

Provide user-facing evidence: a brief screen recording, sanitized event trace,
`./socket_stream doctor` output and checked acceptance IDs. Do not collect secrets.

## 7. Important scope exclusions

Follow `AGENTS.md`: ordinary identifiable UI/process, must have features are explicit permission
concealment, evasion of screen capture, proctoring and OS permission systems.
An optional keyboard shortcut to show/hide a **normal** window is fine where the
desktop provides a supported interface. assert a window is universally
invisible in screen recordings.

## 8. Definition of Done

The next agent must deliver: (1) tested source, (2) release-ready Linux x86-64
archive + SHA-256, (3) a screenshot/video of the real flowing UI, (4) a completed
A01–A16 acceptance table with evidence/limitations, (5) tests passing from
a clean checkout, and (6) no hidden reliance on mocked data in the real demo.

**Do not report complete merely because GUI launches or synthetic unit tests pass.**
