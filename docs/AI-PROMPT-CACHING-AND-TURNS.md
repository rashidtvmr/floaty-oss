# Prompt caching, answer structure and speech turns

## Unified Start and runtime diagnostics

The answer reading surface now accepts mouse-wheel/trackpad input even with
cursor blending enabled; only the surrounding background passes through.
**Ctrl+Alt+Up / Ctrl+Alt+Down** scroll the answer by a page in the Linux control
window or focused answer panel. These are configurable under Keyboard shortcuts.
Out-of-focus use still requires user-managed desktop shortcut registration.

Combined audio+screen sessions now detect screen questions independently of
speech, using the same plausibility, stability and deduplication rules as
screen-only mode. Previously automatic reads only supplied speech context.
Identical screens are answered once, in-flight answers are not interrupted, and
unreadable updates clear stale screen text/image context. OCR remains opt-in;
unavailable accessibility does not silently enable screenshot capture.

The Session page now has an explicit **Use Codex** button; Configure also offers
**Use Codex for answers when I Apply**. Fetching models alone does not change the
active provider. Stop before switching a running session's provider.

One small line in the answer panel shows the current stage/elapsed time, latest
heard text and a bounded AI-input preview. Speech events report finalization and
the actual request-gate outcome (including My answers suppression); `ai_input`
reports the composed user prompt, including supplied screen/clarification context,
immediately before dispatch. This is a text preview, not a full serialized request
or a view of attached image pixels/system instructions. Content stays in memory
and the live UI event pipe; it is not added to diagnostic logs. Pinned answers do
not pin this live status line. No capture permissions or role defaults changed.

The Linux Session page has **Listen to audio** and **Read the screen** checkboxes
above one **Start** button. No selection disables Start. Audio options configures
the device/source and explicit speaker role without opening capture. Combined
sources use a single `audio-live --read-screen` process. Start opens the panel;
consent refusal opens no sensors, and Stop invalidates pending readiness callbacks.

After confirmation, bounded readiness probes check only the selected sources:
PulseAudio/PipeWire inputs, bundled speech worker, AT-SPI imports and accessibility
bus connectivity. These do not record audio, read foreground text or grant OS
permissions. A later OS ScreenCast chooser must still be approved; OS permission
revocation and actual portal denial/cancellation remain runtime cases, not proven
by a device list. Probes have capped output and timeouts. Independent read-only
review confirmed the no-capture/consent boundaries and bounded probe cleanup;
real chooser-denial acceptance remains incomplete.

At the top of the answer, stage/elapsed status distinguishes reading/listening,
dispatch/waiting for AI output, receiving tokens, completion, pause and stop.
**Details** displays stage, source, reported HTTP status/error category and token
counts, not raw prompts or credentials. HTTP 401/403, 429 and 5xx are classified
separately when reported. Unknown causes stay unknown. A timer is cleaned up when
the panel is destroyed. No automatic paid retries are introduced.

**Configure → Codex model (this session)** offers a dropdown, manual model ID and
**Fetch Codex models** using the official App Server initialize/model-list
protocol, including bounded pagination. It never reads Codex's credential file or
runs inference. This session's selection is passed through `SOCKET_STREAM_CODEX_MODEL`
to the existing validated CLI `--model`; cloud and Ollama selections are separate.
Unsupported/unauthenticated discovery reports a sanitized error and permits manual
entry. The selection is intentionally not persisted by the existing settings
writer. A real local Codex model-list probe succeeded with eight models; that
establishes discovery, not model entitlement, quota or successful inference.

## Prompt design and evidence

All HTTP chat requests (streaming, nonstreaming and screenshot-assisted), and the
Codex stdin payload, start with the same versioned instructions embedded from
`crates/socket_stream_core/prompts/practice-system-v2.md`. Questions, screenshots,
previous-answer context and transcriptions come later. No dates, request IDs,
speaker guesses or changing screen text are interpolated into the shared prefix.

The prompt routes comparisons to a direct distinction, a short pipe table and
when-to-use guidance; algorithms to approach, runnable code, complexity and edge
cases; debugging to fault/cause/fix/regression; concepts to definition/example/
caveat; design to requirements/components/trade-offs; and behavioral questions to
STAR without inventing personal achievements. Clarifications refine the previous
question rather than generating an unrelated answer. It retains the existing
`Question:` / `Answer:` contract and untrusted-screen/audio boundaries.

The TypeScript comparison example is grounded in the official Handbook: both
forms describe object shapes; aliases also name other types, including unions;
interfaces support declaration merging. It avoids blanket performance claims.

Research consulted on 2026-11-16:

- [OpenAI prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering):
  explicit instruction roles, task structure, examples, code-managed prompts and
  regression/evaluation cases. These guide design, not a guarantee of correctness.
- [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching):
  exact-prefix matching, routing keys, model-dependent minimum lengths, retention
  and cache-write costs, and usage reporting. Newer Responses API breakpoint
  controls are **not** silently sent to this Chat Completions integration.
- [DeepSeek context caching](https://api-docs.deepseek.com/guides/kv_cache):
  automatic best-effort caching, persisted-prefix units, hit/miss token counts.
- [Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching):
  Messages API `cache_control`, minimum lengths, explicit breakpoints and TTLs.
  The app has no native Claude Messages adapter, so these options are not injected
  into arbitrary OpenAI-compatible gateways.
- [TypeScript Handbook](https://www.typescriptlang.org/docs/handbook/2/everyday-types.html#differences-between-type-aliases-and-interfaces).

## What caching actually means here

The client cannot force or prove a remote KV-cache hit. First requests, short
prefixes, expired/evicted caches, changed models and gateway routing can miss.
Caching reuses input processing, not a previous final answer. It can still incur
input/cache-write charges; no blanket cost or latency improvement is claimed.

| Backend | Client behavior | Cache guarantee |
| --- | --- | --- |
| Native `https://api.openai.com` | Stable prefix and versioned `prompt_cache_key`; streaming usage requested | Provider/model-dependent, not guaranteed |
| Native `https://api.deepseek.com` | Stable prefix; automatic server caching; streaming usage requested | Best effort, not guaranteed |
| Other compatible endpoints (including Vyce), Ollama | Stable prefix, no unverified vendor fields | Unknown; depends on gateway/server |
| Codex CLI | Same static instruction prefix on stdin | CLI/provider-managed; counts unavailable here |
| Mock | No inference | No token cache |

There is no automatic prewarming call, padding solely to reach a token threshold,
extended-retention opt-in, local prompt/transcript cache, or fallback API request
introduced by this work. The app's existing in-memory history is unchanged.
Your configured endpoint and credentials are not changed.

For provider responses containing usage, the panel shows actual input, cached
input and output counts. OpenAI `usage.prompt_tokens_details.cached_tokens` and
DeepSeek `usage.prompt_cache_hit_tokens` are recognized, including a genuine zero.
Missing counts show **not reported**, never an invented hit or savings percentage.
SSE usage-only chunks and ordinary JSON replies are supported. A gateway that
does not forward usage cannot provide these metrics. Streaming usage is requested
only on the two known native hosts to avoid breaking strict compatible endpoints.

## Speaker roles and generation suppression

This is **not diarization or automatic voice identity recognition**. The bundled
Moonshine worker transcribes one chosen source. It does not separate two voices
in a mixed recording, and it does not simultaneously capture microphone and
playback. The application never assigns identity from transcript wording.

Linux's listening dialog offers:

- **My answers:** automatic generation suppressed, even for quoted or rhetorical
  questions. An explicit clarification referring to an existing question can
  refine it (e.g. “So you mean the input needs to be sorted?”).
- **Other person's questions:** conservative English question/request detection;
  statements and bare repeats do not automatically replace the answer.
- **Mixed/unknown:** transcript only; use manual Send for a question.
- **Solo practice:** the legacy behavior, finalized speech becomes a request.

The dialog defaults microphone to **My answers** and system playback to **Other
person's questions**. This is a source convention you can override, not proof
that the physical source contains only that speaker. Choose mixed/manual-only if
both voices appear in the same source. Echo, quoted questions and ASR errors can
still affect interviewer question detection. Non-English turn classification and
speaker diarization remain unsupported. Typed Send and Capture are deliberate
requests regardless of the selected speech role.

CLI (Moonshine only; legacy Whisper rejects this flag):

```sh
./socket_stream audio-live --authorized --source system \
  --speaker-role interviewer --provider openai-compatible
./socket_stream audio-live --authorized --source microphone \
  --speaker-role interviewee --provider openai-compatible
```

The CLI default remains `practice` for compatibility. During a running CLI session,
`speaker interviewer|interviewee|unknown|practice` changes the assigned role and
discards pending old-role speech; it does not switch the capture device. The GTK
dialog selects a role at startup; it does not expose live role switching. The
macOS shell has not gained a role selector in this change; CLI flags work there.

A clarification receives the bounded previous question and latest successful
answer as untrusted reference material. A new unrelated request resets that
context. A failed answer is not recorded as a successful reference. The speech
gate runs **before** `question_detected`, `answer_started` or any model call, so
your own suppressed turn cannot clear the visible answer. Pause clears pending
speech; Stop discards session state. Bare repeated ASR finals remain deduplicated;
manual Send always permits an intentional repeated question to refine its answer.

## Table presentation

GTK renders pipe tables as aligned, non-wrapping monospace columns with a bold
header and horizontal scrolling, not unaligned raw Markdown pipes. Long cells
wrap within a bounded column width. Escaped pipes, inline-code pipes and CJK
display width are handled. Fenced code is never parsed as a table, and incomplete
streamed rows fall back to plain text until complete. Copy preserves the original
Markdown; content is inert text with no HTML, links or script execution.

## Verification scope

Unit and local HTTP fixtures check a byte-identical prefix across request paths,
safe vendor-field selection, OpenAI/DeepSeek usage and unknown/zero counts, SSE
usage-only frames, table rendering, and request-scoped/pinned usage display.
Real-CLI synthetic speech tests exercise suppression, explicit clarification,
unknown/manual-only mode and role changes without opening any audio device.

These tests do not demonstrate a real provider cache hit, guarantee generated
answer quality, measure hardware speech accuracy, or prove reliable identity
separation in mixed audio. Run a model-specific answer-quality evaluation and
inspect real reported usage before claiming those outcomes. The last installed
release's saved provider returned HTTP 503; this change cannot fix an upstream
service outage.

### Recorded checks — 2026-11-16

- Rust formatting and Clippy passed; 55 CLI + 136 core unit tests passed.
- Python headless discovery: 609 tests OK (6 skipped).
- Acceptance: 29 tests OK (6 skipped), including three real-CLI synthetic speech
  cases and manual-vision JSON usage forwarding.
- On Linux GTK 3.24.52 under a separate Xvfb display: 15 checks OK (3 physical
  input checks intentionally skipped). New table/usage rendering and role-dialog
  defaults passed; compact layout retained at least 100 px of answer space.
- The new Linux-musl CLI passed all three synthetic speech-turn cases and all
  three manual-vision/usage cases on Linux. No microphones or actual playback
  devices were opened, and the user's live desktop was not driven by these checks.
- This change has not received a separate independent review or a live-provider
  answer-quality/cache-hit evaluation. No capture authorization, credentials,
  authentication files, or production config-writing helper was changed.
