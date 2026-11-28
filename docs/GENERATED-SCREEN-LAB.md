# Generated screen-reading lab

For the later HP dependency repair, stopped-Capture correction, and the still
pending ordinary-desktop real-answer test, see
[`HP-RUNTIME-REPAIR.md`](HP-RUNTIME-REPAIR.md). The recorded generated and CLI
checks below keep their original scope.

This lab uses 20 generated HTML cases to exercise native screen extraction,
stable accessibility identities, changed-node updates, and conversational
context. It separates the page, the evaluation oracle, native extraction, and
model evaluation so a pass in one layer cannot silently stand in for another.
These are selected regression cases, not an exhaustive reliability guarantee.

## Files and evaluation boundary

| File | Purpose |
| --- | --- |
| [`screen-reading-lab.html`](../tests/fixtures/screen-reading-lab.html) | Self-contained generated page with Previous, Next, a case selector, and page reset. |
| [`screen-reading-lab-cases.json`](../tests/fixtures/screen-reading-lab-cases.json) | Separate oracle: required/forbidden extraction markers, expected responses, staged actions, and context prerequisites. |
| [`native_browser_screen_lab.py`](../tests/acceptance/native_browser_screen_lab.py) | Opt-in native Chrome, AT-SPI, and production CLI runner. |
| [`native_gtk_panel.py`](../tests/acceptance/native_gtk_panel.py) | Separate generated-answer GTK panel, main-loop heartbeat, and explicit restoration check; no capture or model request. |
| [`native_canvas_ocr.py`](../tests/acceptance/native_canvas_ocr.py) | Optional production file-OCR check using only the generated canvas pixels. |
| [`native_lab_action.py`](../tests/acceptance/native_lab_action.py) | Optional helper for one native AT-SPI Next-button action in the pinned fixture window. |
| [`screen_model_relay.py`](../tests/acceptance/screen_model_relay.py) | Optional two-turn file relay between the native host and a separate key-holding controller. |
| [`openai_live_smoke.py`](../tests/acceptance/openai_live_smoke.py) | Audited fixed-endpoint transport, response validation, and a separate synthetic CLI smoke test. |
| [`HP-NATIVE-ACCEPTANCE.json`](HP-NATIVE-ACCEPTANCE.json) | Public sanitized acceptance summary: environment, hashes, native/model results, usage, and explicit limits. No raw payloads or private identifiers. |

The HTML never loads the oracle. The native runner's HTTP server serves only
the generated HTML at its owned random route; it does not serve the oracle or
the repository. Expected answers and question-marker strings from the oracle
are not injected into DOM, ARIA, JavaScript arguments, screenshots, OCR input,
or model messages. Ownership metadata such as an expected fixture URL, title
prefix, case ID, or selector can enter JavaScript scope checks; those checks do
not supply question text or expected answers to the page.
The page contains the questions and their intended inputs, including hints
and session codes, but no answer-key annotations. The evaluator compares
results against the oracle after extraction or response completion.

The `baseline` and `followup` pair deliberately retains the same question
element and Text node. Only the question's `Text.data` changes. The session
anchor, heading, navigation labels, and surrounding layout remain unchanged.
The runner must measure whether Chrome also preserves native AT-SPI identities;
DOM identity alone is insufficient evidence. The second model request must
contain the first successfully completed assistant response in its history.
Chrome may expose that one Text object through more than one native node, so
the expected patch size must follow the observed accessibility tree.

## The 20 cases

| Group | Case IDs | What the cases distinguish |
| --- | --- | --- |
| Initial snapshot and session context | `baseline`, `followup` | A changed question, an unchanged anchor, and a followup requiring the previous completed answer. |
| Text and pixel availability | `unselectable`, `canvas-accessible`, `canvas-bare` | Real text with selection disabled; a canvas with an accessible alternative; a question available only in generated pixels. |
| Tree structure and budgets | `layout-budget`, `reparent`, `duplicates` | Empty layout before the question; retained cards reordered/reparented; duplicate labels with different parent context. |
| Changing or removed evidence | `hint-change`, `hint-remove` | An unchanged question with an updated hint, then removal of that hint with explicit recall from prior context. |
| Encoding and materialization | `unicode`, `scroll`, `virtualized`, `delayed` | Tamil/emoji/quotes; offscreen but mounted text; absent virtual rows; a question that has not arrived yet. |
| Exclusion and untrusted content | `hidden`, `password`, `untrusted-note` | Hidden/decorative markers, a generated protected field, and a benign instruction embedded in quoted page data. |
| Session and unavailable boundaries | `reset`, `context-unavailable`, `unavailable` | A new topic, missing prior context, and no readable question. |

`layout-budget` creates 352 empty DOM leaves, grouped to avoid a single huge
sibling list. Browsers may prune them before exposing AT-SPI nodes, so record
the observed native count instead of claiming that all 352 were traversed.

The oracle also defines multi-step sequences for changed/removed hints, an
actual assistant-session reset, and cancellation of a delayed page update.
`--cases all` visits every case and its listed in-page steps. It does not by
itself execute every multi-case conversational sequence in `sequences`.

## Open the page manually, offline

Open the HTML file directly in a browser, or use the desktop opener:

```sh
xdg-open /absolute/path/to/floaty-oss/tests/fixtures/screen-reading-lab.html
```

No server, account, network connection, or model is needed for the page. A
`?case=baseline` query selects a starting case when the file viewer supports
it; the in-page selector always provides navigation. Wait for the delayed
case to become ready before evaluating its arrived state. A canvas whose
rendering fails is reported as unavailable and must not count as an OCR pass.

Page reset changes only the fixture DOM. To test a fresh AI context, stop and
clear the actual assistant session or start a new CLI process before following
the oracle's fresh-session sequence. Resetting the page is not a model reset.

## Native Chrome runner

Run as the signed-in, non-root desktop user. Prerequisites are a verified
production `socket_stream` CLI, the Chrome ELF executable, an explicitly
selected Wayland socket owned by that user, `/usr/bin/python3` with GI/AT-SPI,
`dbus-run-session`, and `/usr/libexec/at-spi-bus-launcher`. Optional canvas OCR
also needs a working local Tesseract installation. The runner does not install
packages or change desktop accessibility settings.

Keep the acceptance helpers together when staging a checkout on another host.
Even mock mode imports the shared validator from `screen_model_relay.py`, which
imports `openai_live_smoke.py`; all three files must be present. Importing these
modules does not read a real key or contact an external provider.

From the repository root, replacing the three absolute paths with the actual
verified executable and existing display socket:

```sh
/usr/bin/python3 tests/acceptance/native_browser_screen_lab.py \
  --chrome /absolute/path/to/chrome-elf \
  --cli /absolute/path/to/verified/socket_stream \
  --display-socket /absolute/path/to/owned/wayland-0 \
  --cases core
```

`core` contains `baseline`, `followup`, `unselectable`, `canvas-accessible`,
`canvas-bare`, `unicode`, `hidden`, `password`, and `layout-budget`. Use
`--cases all` for all 20 cases, or a comma-separated selection beginning with
`baseline,followup`. The pair must run first because it measures a single
session's snapshot-to-patch transition.

The runner creates a private runtime directory, D-Bus session, accessibility
bus, Chrome profile, config, and cache. It verifies the accessibility bus's
owner and an empty inventory before starting Chrome. Chrome uses the selected
Wayland display with its sandbox enabled; CDP is connected through inherited
FD3/FD4 pipes belonging to that child process. No existing browser profile or
public debugging port is used.

Startup uses `--no-startup-window` and creates an explicit owned blank target
before navigating to the exact fixture URL. The temporary profile uses
`--password-store=basic` to avoid a real desktop keyring dependency; it contains
no real credentials. The HTTP probe verifies the served fixture hash, and
browser loading must produce an additional response. A blank or browser-internal
page cannot count as a rendered fixture.

After Chrome exposes the generated window, the runner pins its native
bus/object-path identity. Production CLI reads remain scoped to that window
and preserve the existing no-pixel-fallback rule. The default two model turns
use a loopback mock provider; their responses establish transport/history
behavior, not answer correctness. The live model CLI stops after `followup`.
Later cases are extraction checks, so `--cases all` does not send 20 model
requests or establish 20 successful model answers.

### Native changed-node contract

The reconciler keys native accessibility objects. The recorded Chrome run
exposed both a paragraph and its static-text child for the question. Changing
the one DOM Text object changed the paragraph's `text` and the child's `name`,
`text`, and `bounds`. Sending both changed native IDs preserves the model's
observed tree; silently omitting either can leave a stale question alias.

The runner and relay now share a strict question-patch validator:

- The updated IDs must equal every alias in the initial snapshot's one
  connected question group. Duplicate IDs, unrelated nodes, unchanged-node
  resends, new IDs, additions, removals, and moves are rejected for this fixture.
- Parent, sibling position, role, focus, and image flags must stay unchanged.
  Every name/text field carrying the old question must contain the new one.
- Replaying the patch must remove all old-question aliases and retain the
  unchanged anchor. The new payload must omit the old question and anchor.
- Window and revision bindings, exactly two requests, and exact acknowledged
  assistant history remain required.

The delta belongs to the newest screen-update message. Chat Completions is a
stateless API: each complete request still sends the system prompt and bounded
prior user/assistant history, including earlier snapshots or patches needed
for context. Consequently, unchanged screen text can be retransmitted inside
earlier history even when it is absent from the newest patch. Conversation
checkpoints use an explicit bounded snapshot when the replay budget requires
one. A changed-node payload does not mean the entire HTTP request contains
only those changed nodes.

The native runner also has the complete before/after helper captures. It
independently applies the documented Tree-mode scope: exclude chrome and
protected subtrees, clear root labels, and prune empty structural branches.
Only after comparing that entire initial projection with the model snapshot
does it bind native keys to model IDs. It then requires the exact observed
changed-ID set and equality between the replayed model tree and the complete
followup projection, including unchanged nodes. Incomplete observations or
capture disagreement fail this check. The result records
`native_snapshot_replay_verified` separately from model-answer correctness.

This replaces a faulty count assertion, not the production reconciliation
algorithm or fixture. A local counterexample showed why: the old assertion
rejected the correct two-node update but accepted a one-node update that left
the child's old question intact. Local regressions now reject that omission,
extra or duplicate nodes, invalid structure/state/revisions, and mismatches
against the independently captured followup tree. The stronger check passed
the subsequent native Chrome run recorded in the validation section below.

### Optional checks

| Option or helper | Scope and result |
| --- | --- |
| `--scoped-default-selection` | Checks default selection on the private bus containing only the generated browser. It does not test selection among the user's ordinary GNOME applications. |
| `--canvas-ocr` | At `canvas-bare`, exports only the owned 720 × 160 canvas PNG through CDP, writes it privately, and invokes production `socket_stream ocr --file`. Compares normalized extracted question markers against the oracle afterward. |
| `--ocr-tool-dir /absolute/owned/ocr-bin` | Requires `--canvas-ocr`. Selects a current-user-owned directory containing a regular executable `tesseract`; neither may be group/world writable. Revalidated before startup, in the worker, and immediately before OCR. Prepends only the OCR subprocess's copied `PATH`, with an explicit executable-selection check. |
| Native Next helper; planned `--native-next` | `native_lab_action.py` checks the private bus, exact unique pinned window, protected-subtree exclusion, and unique enabled Next button, then attempts one named native AT-SPI Action. The caller must verify the case actually changed. The flag is not wired yet; do not pass it to the current runner or assume the default CDP click is a native-input test. |

Canvas OCR never captures the desktop or calls a portal. It does not inject
oracle text into the pixels, and missing Tesseract or failed rendering is a
non-pass. It checks the file-OCR component only: it does not test automatic
screen capture, fallback selection, portal permission, or a vision model.

An existing trusted user-owned Tesseract runtime can be selected without a
system-package installation. The directory and executable must satisfy the
checks above; relative paths, final symlinks, and unsafe permissions are
refused. The runner does not install the runtime. For the full native matrix
with this optional OCR component, use:

```sh
/usr/bin/python3 tests/acceptance/native_browser_screen_lab.py \
  --chrome /absolute/path/to/chrome-elf \
  --cli /absolute/path/to/verified/socket_stream \
  --display-socket /absolute/path/to/owned/wayland-0 \
  --cases all --scoped-default-selection --canvas-ocr \
  --ocr-tool-dir /absolute/path/to/owned/ocr-bin
```

The override leaves the browser, accessibility bus, global environment, and
library-search variables unchanged. A selected directory becoming invalid
fails the check; it does not silently choose a different Tesseract.

## Local self-tests

These commands exercise local mocks, validation, owned loopback servers,
temporary files, framing, and cleanup. They do not connect to a desktop,
read a real API key, or call OpenAI:

```sh
python3 tests/acceptance/native_browser_screen_lab.py --self-test
python3 tests/acceptance/native_canvas_ocr.py --self-test
python3 tests/acceptance/native_lab_action.py --self-test
python3 tests/acceptance/screen_model_relay.py --self-test
python3 tests/acceptance/openai_live_smoke.py \
  --cli /absolute/path/to/verified/socket_stream --self-test
```

The last command executes the specified real CLI with a synthetic reader and
synthetic upstream. Its pass does not establish native accessibility or live
provider success. Native browser checks are opt-in scripts, not automatically
enabled by ordinary unit-test discovery.

The separate GTK acceptance fixture uses a private headless Weston compositor,
private D-Bus/AT-SPI, and a generated GTK application:

```sh
SOCKET_STREAM_RUN_WAYLAND_A11Y=1 \
SOCKET_STREAM_TEST_CLI=/absolute/path/to/verified/socket_stream \
python3 -m unittest discover -s tests/acceptance \
  -p test_wayland_atspi_runtime.py -v
```

It requires Weston and GTK3 in addition to the accessibility dependencies. It
does not connect to the ordinary GNOME desktop, and its provider is local/mock.
An explicit dependency or socket-restriction skip is not a native pass.

## Optional two-turn OpenAI file relay

This mode is for an explicitly authorized live-provider run of the actual
native HTML-to-CLI requests. The native host receives only a random loopback
bearer token. The real API key exists only in a separate controller process;
“controller” does not mean the operating system's root account.

Create a fresh, owned `0700` relay directory on each host. Run the controller
before the native sequence, using its hidden terminal prompt:

```sh
mkdir -m 700 /absolute/private/controller-relay
python3 tests/acceptance/screen_model_relay.py \
  --relay-dir /absolute/private/controller-relay
```

For automation, `--key-fd FD` accepts an inherited anonymous pipe on Linux,
verified through `/proc`; a regular file is refused. Never put the key in
command arguments, environment variables, relay files, or logs. Other
platforms must use the hidden terminal prompt. The explicit optional
`--https-proxy http://host:port` uses HTTP CONNECT without proxy credentials,
paths, or query strings; it does not inherit proxy settings from the environment.

On the native host, add its own directory to the normal runner command:

```sh
mkdir -m 700 /absolute/private/native-relay
/usr/bin/python3 tests/acceptance/native_browser_screen_lab.py \
  --chrome /absolute/path/to/chrome-elf \
  --cli /absolute/path/to/verified/socket_stream \
  --display-socket /absolute/path/to/owned/wayland-0 \
  --cases core \
  --relay-dir /absolute/private/native-relay
```

The operator supplies the bounded file-transfer mechanism; the relay itself
opens no remote-file endpoint. For each turn, transfer the complete
`turn-N.request.json` from the native directory to the controller directory,
then the corresponding `turn-N.response.json` back. Publish complete files
atomically with mode `0600`, without replacing existing targets. The request
and response are bound to a session ID, turn, request hash, fixture/oracle
hashes, and selected native window. Keep the attempt receipts for review;
deleting them to replay an attempted request is not part of this workflow.

Before forwarding, the controller validates the real CLI's model, system
prompt, message roles, selected fixture, initial snapshot, and the complete
changed question-alias group described above. Known question/anchor markers,
protected-value absence, and exact acknowledged assistant history remain
required. Native Chrome's hierarchy is preserved rather than replaced with a
synthetic tree. Oracle answers are used only to validate completed responses.
They are never appended to the request.

The upstream destination is fixed to
`https://api.openai.com/v1/chat/completions`, with TLS hostname verification,
no redirects, model `gpt-6-luna`, at most 1,024 completion tokens per request,
`store: false`, and usage reporting. There are **at most two upstream POST
attempts and no retries**. An atomic receipt reserves each attempt before
external I/O. A failed attempt stops the sequence rather than quietly trying
another provider or repeating a paid call.

The original completed SSE response is transported as base64 only after
completion, semantic, size, and credential-echo validation. The second request
must reproduce the first acknowledged assistant response exactly. A native
report alone does not prove external inference: review it together with the
separate controller report, request/response bindings, usage, and attempt count.

File waits are bounded at 180 seconds, but the production CLI still has its
independent **90-second HTTP request timeout**. Transfers plus upstream work
must fit that tighter limit; the relay does not extend it. The upstream
transport's configured timeout is 75 seconds. Responses are buffered before
delivery, so this setup makes no streaming-latency or real-time performance
claim.

## Validation record

Status as of 2026-11-28; update this section only from the corresponding saved
native and controller reports, with the tested source and CLI hashes.

| Layer | Recorded status | What is established |
| --- | --- | --- |
| Local helper self-tests | Passed locally; independently reviewed | The browser runner including the explicit OCR directory passes 22 guard groups. The relay's local file/HTTP two-turn exchange passes with two native question aliases; a legitimate single-native-node case also passes. Twenty-two negative patch cases and additional native reconstruction cases reject omissions, unrelated changes, invalid structure, and capture mismatches. These are local test results, not native or real-provider inference. |
| Native GTK extraction fixture | Passed on the validation host | A generated GTK fixture on the existing owned Wayland display, with private D-Bus/AT-SPI, reached the production CLI. Its second request had one updated native node, zero added/moved/removed nodes, exact acknowledged history, and no generated password value. This recorded run is separate from the headless Weston command above and from the answer-panel test. |
| Native GTK answer panel | Passed with the compatible private GI-Cairo runtime | With the same UI source, the compatible GI-Cairo bridge removed the previously observed callback `TypeError` failures. The generated answer buffer matched, the panel mapped, and the controlled background status probe completed while the main loop stayed responsive. The compositor did not expose iconification, so minimized-panel restoration remains unsupported in this run. |
| Installed application recovery | Passed the recorded generated-preview health check | The old UI stopped and the installed application restarted using a private version-matched runtime. The main window and panel were mapped, the panel was visible, the generated preview reported rendered, and callback errors were zero. No capture or provider request was started; no system package was installed for this recovery. |
| Native Chrome extraction matrix | All 20 case IDs and 26 extraction stages passed | Required/forbidden markers passed for the initial and staged cases, including native and production-CLI baseline/followup extraction. The baseline had 249 native nodes. This is extraction coverage of the generated fixture, not 20 AI evaluations or universal browser coverage. |
| Native Chrome mock session | Passed the strict native replay gate | Two mock answers completed with exact acknowledged history and unchanged anchor omission. Both changed native aliases were updated with no additions, moves, or removals. The independently reconstructed whole followup snapshot matched the replayed model tree: `native_snapshot_replay_verified: true`. No real model-answer semantics were tested by these mock responses. |
| Private-bus default selection | Passed | One unpinned read selected the owned generated browser and found the expected markers on the private accessibility bus. Selection among ordinary GNOME applications and global foreground behavior remain outside this check. |
| Native Next action | Helper reviewed; integration/result pending | Local self-test only. No native Chrome button-action pass claimed. |
| Generated-canvas OCR | Passed the production file-OCR component check | The explicit owned Tesseract directory was selected. Production `ocr --file` read the generated 720 × 160 canvas PNG and matched its expected normalized question marker. No oracle text was injected. This one component result does not establish desktop screenshots, portal permissions, automatic fallback, or general OCR accuracy. |
| Real OpenAI with production CLI and synthetic tree helper | Passed; independently audited | Two actual HTTP 200 completions from `gpt-6-luna` produced the exact validated Answer fields `25` and `50; MARIGOLD-42`. The latest second-turn update contained one changed node, omitted the unchanged hint, and retained exact acknowledged history. The report explicitly sets `native_validation: false`. |
| Two live OpenAI turns from native Chrome | Passed the combined native-to-provider-to-CLI flow | The actual native Chrome captures reached `gpt-6-luna` through the production CLI and file relay. Both HTTP 200 answers returned to and completed in the native CLI: `25; MARIGOLD-42`, then `50; MARIGOLD-42`. Exact acknowledged history, both changed native aliases, and full native snapshot replay passed. Native and controller reports plus their bound response files jointly establish this result. |

The full-matrix Chrome evidence is `target/screen-coverage/hp-native-verified.json`.
It records Chrome `150.0.7871.186`, `external_ai: false`, overall
`success: true`, and process exit code zero. All 20 case IDs, 26 extraction
stages, the two-turn strict native replay, private-bus default selection, and
the generated-canvas file-OCR check passed in that run.

The earlier `target/screen-coverage/hp-native-all-first.json` remains unchanged
with `success: false`. Its old exact-one count assertion and missing Tesseract
were recorded failures. The successful record is a new native execution with
the reviewed validator and explicit OCR runtime, not a retroactive relabeling.

| Recorded Chrome artifact | Fingerprint |
| --- | --- |
| Base source commit | `eea9666fe8df8706eea084bd27ea998e8ccc0801` |
| Successful staged source bundle SHA-256, including lab helpers | `a80c3977f40cdfbd927e8286312bdc9e854ebf9b6f4ecfa24bc7857be133dc33` |
| Production CLI SHA-256 | `4abcd3e5bbf68a39dcfb327ed4030e20e345f47fe05bd9fd5a090dc95143a82d` |
| HTML fixture SHA-256 | `2333952a78fece31f376f14e133ea017e20b3d4b1fec308d134f2481aeb7bf8c` |
| Oracle SHA-256 | `3558c56bfa8d5861f60169fe104f17971440437961512ad83de606b9b62500e1` |

The GTK extraction evidence is `target/screen-coverage/hp-gtk-native-success.json`.
The earlier answer-panel observations are in
`target/screen-coverage/hp-gtk-panel-userbus.json` and
`target/screen-coverage/hp-gtk-panel-errors.json`. The latter recorded 14
callback errors. The followup dependency A/B is saved in
`target/screen-coverage/hp-gtk-panel-portable.json`: it reused system GI with
an owned compatible Cairo bridge, did not override system libraries, and
passed with zero callback errors. With a 50 ms target heartbeat interval, the
maximum measured gap was 52.7 ms; 15 ticks occurred during the controlled
750 ms background delay. The answer buffer matched and the panel remained
mapped. These are short-probe measurements, not long-session latency bounds.

`target/screen-coverage/hp-installed-ui-recovery.json` separately records the
installed application's generated-preview recovery as passed, with mapped
main and panel windows, a visible panel, and zero callback errors. Its source
commit is `d8b612cd5e10e7e47a7933db945447026703bd95`, tree
`8d1746249387584c422ea536d11edbb7832c09d0`. The private version-matched runtime
required no system-package installation. The panel test started no screen or
audio capture. The installed recovery record also shows no screen capture or
provider request; its generated preview did not run a model turn.

A later `target/screen-coverage/hp-final-ui-health.json` records 1,143
heartbeat ticks, a state age of 0.23 seconds, an active unit with matching
process state, zero callback errors, and mapped main/panel windows. The
generated preview remained rendered and capture remained inactive. This is
continued UI liveness evidence; actual AI-stream rendering is still a separate
untested path.

The independently reviewed browser runner with the scoped OCR override has SHA-256
`149d872a7fc53b3c3cdeca6b7e49e469469e1bf7007da5d0fe3da280f42a2127`;
the relay validator has SHA-256
`b417dfbde0eb3fdf788263bd3bc49a6ddda852282a76a0aeb60154198e4d3b72`.
Their local tests passed, and the stronger changed-ID/replay gate is now
supported by the separate successful native Chrome report.

The separately audited real-provider evidence is
`target/screen-coverage/openai-live-production-cli-current-route.json`. The
production CLI used the synthetic tree helper through `openai_live_smoke.py`;
this run made two actual POST attempts to the fixed OpenAI endpoint, received
two HTTP 200 responses with `finish_reason: stop`, and performed no retries.
The requested and returned model was `gpt-6-luna`. Both completed answers
passed exact semantic validation: `25`, followed by `50; MARIGOLD-42`. The
second request replayed the first acknowledged response exactly and its newest
screen patch contained one changed synthetic node. Protected text stayed
excluded, and the unchanged hint was absent from that newest patch.

The report records `external_ai: true`, `success: true`, and
`native_validation: false`. It records the selected CLI SHA-256 as
`748312e5f51925c50192ffd1b9f74b3cd0ce83326586d11de60e61add155a939`
and a clean tracked source checkout at commit
`d8b612cd5e10e7e47a7933db945447026703bd95`. The complete second API request
still included bounded prior history; only its latest screen-update message
was a delta. Streaming latency was not tested. This result establishes actual
provider completion and context use with synthetic screen data. The combined
native-host result is recorded separately below.

The native-to-live-provider flow subsequently passed using the actual generated
HTML, native Chrome/AT-SPI, and production CLI. Its paired evidence is:

- `target/screen-coverage/hp-openai-live-native-report.json`: two native cases,
  two completed relay answers in the CLI, exact acknowledged history, and
  verified replay against the complete followup native projection; process
  exit code zero.
- `target/screen-coverage/hp-openai-live-root-report.json`: two completed real
  `gpt-6-luna` responses, two upstream POST attempts, zero retries, and successful
  response validation.

Both bound response envelopes were independently checked as HTTP 200. The
original SSE validated the exact Answer fields below; request/message hashes,
attempt receipts, response bindings, and the acknowledged first response in
the second request were rechecked from all six saved relay files. The native
runner and controller reports provide complementary evidence; neither report
alone establishes the entire flow. The API key remained on the separate
controller, and native model messages were forwarded without replacement by
synthetic tree data.

| Native turn | Validated Answer field | Prompt tokens | Completion tokens | Cached prompt tokens | Total tokens |
| --- | --- | ---: | ---: | ---: | ---: |
| `baseline` | `25; MARIGOLD-42` | 4,421 | 198 | 1,845 | 4,619 |
| `followup` | `50; MARIGOLD-42` | 4,881 | 83 | 4,418 | 4,964 |

The provider reported 9,302 prompt tokens and 281 completion tokens, for
9,583 total tokens across these two native turns. Cached tokens are included
in the prompt counts. Both responses finished with `stop`; the configured
completion cap remained 1,024 tokens per request. These are recorded usage
values, not a latency benchmark or a guarantee about future costs.

Each raw native capture contained 249 nodes; the scoped initial model snapshot
contained 68. The followup patch updated the paragraph and static child only,
with zero additions, removals, or moves. The unchanged anchor stayed out of the
new patch, and the complete replay matched the independently captured native
view. Only `baseline` and `followup` were evaluated with the real provider in
this native run; the 20-case matrix remains a separate extraction test.

The publishable evidence summary is
[`HP-NATIVE-ACCEPTANCE.json`](HP-NATIVE-ACCEPTANCE.json). It includes the Ubuntu
26.04 LTS, Chrome, and optional runtime versions; source, CLI, fixture, and
evidence-artifact hashes; safe per-turn bindings and usage; and the scope of
each check. Usernames, private absolute paths, native/session/allocation IDs,
HTTP request IDs, credentials, and raw requests/responses are omitted.

Remaining boundaries are deliberate: no global GNOME foreground-selection
claim, no desktop screenshot or portal permission test, no full automatic
pixel fallback test, no native physical-input proof, no hardware-audio test,
and no assertion of universal browser accessibility. Case transitions in the
recorded browser run used the owned CDP fixture controls. GTK panel recovery
used generated preview events; rendering an actual AI response stream in that
recovered UI was not part of the completed CLI/provider test. Canvas OCR remains
an explicit generated-PNG component check, with portal and automatic fallback
behavior untested.
Missing or truncated required input must be reported as incomplete; a cautious
unavailable answer does not count as successful extraction of that input.
There is no 100% reliability claim. The full extraction matrix used two mock
requests. The combined native flow used two actual API requests, and a separate
synthetic-tree check used two more. The 20-case extraction matrix is not 20 AI
evaluations.

## Why the separate computer-use package is not installed for this run

The user-owned repository is
[`rashidtvmr/opencode-computer-use`](https://github.com/rashidtvmr/opencode-computer-use).
The inspected package is `@frontendxlab/opencode-computer-use@0.1.5`, using
`open-computer-use@0.3.5`. The published adapter differs from repository main
despite sharing a version number, so package and source evidence must be kept
distinct.

Two inspected paths do not meet this lab's narrow scope unchanged:

1. The adapter's native-child environment allowlist omits `AT_SPI_BUS_ADDRESS`.
   The native launcher fills a missing value from other same-user desktop
   processes. Passing the private session bus alone therefore does not prove
   private accessibility-bus routing through the adapter.
2. Linux state reads and successful action readbacks automatically attempt a
   GDK root-window crop. The inspected native version has no exposed Linux
   capture opt-out. A semantic-only click still reaches this snapshot path;
   relying on Wayland capture failure would not be an appropriate scope check.

Pinned sources:

- [Adapter environment allowlist, commit `df445b4`](https://github.com/rashidtvmr/opencode-computer-use/blob/df445b4798842c95e4897d735b5079f1c3fed160/lib/backend.js#L142).
- [Native environment recovery, v0.3.5 commit `386a260`](https://github.com/iFurySt/open-computer-use/blob/386a260d1ab8b690adbbb27f7471595cf0c2b752/apps/OpenComputerUseLinux/main.go#L548).
- [Native capture, snapshot, and action readback](https://github.com/iFurySt/open-computer-use/blob/386a260d1ab8b690adbbb27f7471595cf0c2b752/apps/OpenComputerUseLinux/runtime.py#L333).
- [Published adapter tarball](https://registry.npmjs.org/@frontendxlab/opencode-computer-use/-/opencode-computer-use-0.1.5.tgz), SHA-256 `699fc3d1dc01023a9c913989130e864622747a336d1d28c5fcc8df96274b8a1e`.
- [Published native tarball](https://registry.npmjs.org/open-computer-use/-/open-computer-use-0.3.5.tgz), SHA-256 `7c16a87c8cd357787b02c0ebd040dd5f93880c1fb51c1901caa2e0c05d063130`.

The lab already has an owned CDP connection for its generated page and a
separate exact-window AT-SPI action helper. No computer-use package, global
daemon, or additional desktop-control endpoint is needed for those checks.
