# Ubuntu screen reading and incremental session context

This change targets the reported Ubuntu 26 / Wayland screen-reading failure and
adds incremental accessibility context to `observe-live` and Moonshine
`audio-live --read-screen`. The one-shot `ask` command remains a one-shot request;
it does not share history with a different process or a later session.

## Linux defects addressed

Foreground selection now applies the same GNOME compositor-mirror exclusions as
the explicit window picker. Previously, a GNOME/Mutter decoration mirror could
shadow the application's accessible window or produce a false multiple-active-
window error. Dead applications and windows are ignored without silently
retargeting a user-selected window.

The helper reads text and bounds through explicit AT-SPI GI interfaces, preserves
native bus/object identities across separate helper processes, and rejects
undefined Wayland global coordinates as OCR regions. Missing GI packages,
initialization failures, missing desktop buses and timeouts produce bounded,
actionable diagnostics. A four-second helper deadline cannot be swallowed by a
best-effort per-node read; Rust also has an independent outer timeout.

The launcher, readiness checks and Rust helper prefer the distro's
`/usr/bin/python3`, where Ubuntu installs its GI bindings. Dependency setup now
explicitly includes `at-spi2-core` and the `gdbus` utility. Preflight reads the
accessibility service's `IsEnabled` status and suggests the relevant desktop
setting if disabled; it does not change that setting automatically.

These are defects established from source and regression fixtures. They do not
establish which defect occurred in a particular user's desktop session.

## Tree reconciliation

The schema-1 helper output remains compatible with older readers. Two optional
fields extend it:

- A tree's `window_id` identifies the selected AT-SPI window by its unique bus
  name and object path.
- Each node's `stable_id` identifies that accessible object the same way.

The existing numeric `id` and `parent` remain local to one extraction. The Rust
reconciler maps native keys to session-local numeric IDs, analogous to keyed
reconciliation in a UI renderer. Inserting an earlier sibling does not rename
all later nodes. No React dependency is introduced.

The first acknowledged observation establishes a JSON `snapshot`. Later
observations produce a `patch` with:

| Field | Meaning |
| --- | --- |
| `base_revision` | The screen revision already acknowledged by a successful model turn |
| `revision` | The revision established by this update |
| `added` | New nodes, including their content, parent and sibling position |
| `updated` | Nodes whose content, role, focus, bounds or image status changed |
| `moved` | Existing nodes with a different parent or sibling position, without repeating their unchanged text |
| `removed` | IDs confirmed absent from a complete observation |

An unchanged tree contributes no new screen payload. Navigation chrome and
protected subtrees are filtered before allocating the model's screen budget.
Full-tree modes also discard empty structural branches, retaining meaningful
nodes and their complete ancestry so layout-only nodes cannot exhaust the
budget before readable content. This does not reparent retained nodes.
Focused reading modes retain their configured scope; ordinary tree mode keeps
the filtered question window. The UI still receives readable question text.

A different source/window, missing or ambiguous native identities, malformed
data, truncation or context compaction cannot be treated as an ordinary complete
patch. The reconciler either reports the invalid input or sends an explicit
replacement snapshot. `complete: false` means omitted nodes are unknown, not
confirmed deletions. The next complete observation resynchronizes explicitly.
This conservative path also supports older/macOS helpers without stable keys.

All serialized metadata, escaping and Unicode count toward the payload byte
budget. Truncation preserves valid JSON and character boundaries. OCR has no
native node identity, so it uses bounded `text_snapshot` replacements and
deduplicates identical text; it does not pretend OCR lines have stable AT-SPI
keys.

## Session continuity and failure recovery

Preparing a request does not mutate acknowledged state. The request carries a
staged tree update and conversation turn. Only a successful complete answer
commits them together. Provider failure, cancellation, an empty answer, an
incomplete SSE response, or a stale reply leaves the earlier baseline intact.
Retries therefore include everything the model has not acknowledged.

Each acknowledged baseline has its own identity token. A turn from another
session, an older baseline, or a divergent clone at the same revision is refused.
Pause revokes capture and drops pending work;
Resume retains acknowledged conversation. Speech-reader updates carry capture
generations, so a read started before Pause cannot become current after Resume.
Stop drops the in-memory session. No screen transcript is persisted by this
feature.

The latest user message contains only the current patch, plus the new question.
**OpenAI-compatible chat endpoints are stateless**, so the HTTP request still
includes the earlier snapshot, successful patches and answers as conversation
history. This is not a claim of server-held delta sessions or guaranteed token
caching. Codex/local fallback providers receive a bounded textual replay. A
failed fallback candidate cannot leak a partial answer before a different
provider succeeds.

Speech turn detection is staged with the same request, so a failed or cancelled
question cannot suppress an identical retry or replace clarification context.
Clarifications reference acknowledged history without repeating old screen text
or answers in the latest message. Queued manual questions resume with historical
context while current-screen availability is stated explicitly.

Context is bounded rather than unlimited:

| Limit | Default |
| --- | ---: |
| Retained conversation text | 48,000 UTF-8 bytes |
| Successful turns before a checkpoint | 16 |
| Retained text per assistant answer | 4,000 bytes, explicitly marked if abbreviated |
| Dialogue recap at a checkpoint | Up to 8,000 bytes from the most recent 12 turns |
| Total model text including fixed instructions | 64,000 bytes |

When the conversation reaches its bounds, the next request establishes a fresh
current-screen snapshot with a bounded dialogue recap. Old dependent patches
are not kept after their base snapshot is dropped. Checkpoint preparation is
also transactional: a failed checkpoint cannot erase the previous context.

## Running on Ubuntu

From a terminal in the logged-in graphical desktop session, review/install the
dependencies and build:

```sh
./scripts/linux-install-deps-debian.sh --yes
cargo build --release -p socket_stream
./scripts/start-linux.sh
```

The helper can be diagnosed independently with:

```sh
/usr/bin/python3 scripts/linux-accessibility.py --mode windows
```

To test the actual foreground read through the CLI, focus the intended
application and run:

```sh
cargo run -p socket_stream -- a11y --authorized --mode tree
```

If preflight reports that desktop toolkit accessibility is disabled, enable it
in the graphical settings or explicitly run:

```sh
gsettings set org.gnome.desktop.interface toolkit-accessibility true
```

Restart an application if it still exposes no accessible content. Run capture
as the logged-in desktop user, with that session's D-Bus environment. Apps that
do not expose usable AT-SPI content still require their own accessibility
support or the existing opt-in OCR path. Wayland pixels continue to require the
normal approved portal chooser. Fixed-window consent remains text-only.

## Reproducible validation

Install the development coverage tools once:

```sh
rustup component add llvm-tools-preview
cargo install cargo-llvm-cov --locked
python3 -m pip install coverage
```

Then run:

```sh
bash scripts/verify-screen-coverage.sh origin/main
```

The script runs formatting, Clippy, all Rust tests, Python unit tests and CLI
acceptance tests. CLI subprocesses use the instrumented Rust binary, so the
HTTP session tests contribute actual runtime coverage. It writes raw reports
and `summary.json` to `target/screen-coverage/`.
Instrumented builds use `target/screen-coverage/build`, separate from ordinary
development artifacts. The coverage environment is established before cleaning
workspace artifacts, so cached uninstrumented test executables cannot silently
replace the instrumented tests.

The 95% gate applies separately to the Linux helper, readiness checks, tree
reconciler, conversation module and session coordinator, and to changed
executable runtime lines in aggregate. Inline Rust test code is excluded from
those production metrics. Unmeasured executable files fail the gate. Whole
measured Rust/Python workspace coverage is also reported separately; this change
does not claim 95% coverage for existing unrelated platform, GUI and audio code.

Tests cover insertion, deletion, reorder, reparenting, focus changes, protected
content, malformed and partial trees, Unicode/JSON budgets, a 250-step edit
replay, real loopback HTTP messages, failed requests, cancellation, stale capture
generations, checkpointing, session isolation and incomplete SSE responses.
The full gate also exposed an existing settings-parser portability defect:
excessively nested unknown JSON is now rejected with an explicit 64-container
limit, preserving the existing file and credentials.

The native Wayland fixture and its environment requirements are documented in
[NATIVE-WAYLAND-ACCEPTANCE.md](NATIVE-WAYLAND-ACCEPTANCE.md). This executor rejects
Unix sockets, preventing D-Bus and Weston from starting. That native fixture is
an explicit skip here, and actual Ubuntu/GNOME/browser runtime remains pending.

Primary platform references:

- [Ubuntu 26.04 release summary](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/)
- [Ubuntu accessibility stack](https://ubuntu.com/desktop/docs/en/26.04/explanation/accessibility-stack/)
- [AT-SPI native object identity fields](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/class.Object.html)

## Verification record — 2026-11-28

`bash scripts/verify-screen-coverage.sh 886ab92147542c65a057fb3ba81f40520e763c96`
passed against base
`886ab92147542c65a057fb3ba81f40520e763c96`. The run used Rust 1.100.0 and
Python 3.12.14 in a Debian 13 x86_64 executor, with cargo-llvm-cov 0.9.2 and
coverage.py 7.17.5. This isolated instrumented run supersedes the earlier
coverage record.

Formatting and Clippy passed. The Rust workspace passed 281 tests, Python unit
tests passed 710 with 6 skipped, and acceptance passed 49 with 7 skipped:
1,040 passing tests in total. Shell syntax and `git diff --check` also passed.

| Production scope | Covered executable lines | Line coverage |
| --- | ---: | ---: |
| `scripts/linux-accessibility.py` | 409 / 411 | 99.51% |
| `apps/socket_stream_desktop/ui/source_preflight.py` | 87 / 87 | 100.00% |
| `crates/socket_stream_core/src/a11y_diff.rs` | 288 / 290 | 99.31% |
| `crates/socket_stream_core/src/conversation.rs` | 110 / 110 | 100.00% |
| `apps/socket_stream/src/live_context.rs` | 160 / 162 | 98.77% |
| All changed executable runtime code against main | 1,008 / 1,050 | **96.00%** |
| Hardening changes against the published implementation | 162 / 167 | **97.01%** |

The hardening-only baseline is `5ffa8cb4c615e628d6668befb9d7fd82333d8fc1`.
The complete measured Rust workspace is 71.98% and the
complete measured Python workspace is 49.41%. Those broader
figures include existing unrelated code and are reported separately from the
95% feature/change gate. Inline Rust test bodies do not contribute to these
production percentages. Raw branch data is retained in the generated Python
coverage JSON; the enforced cross-language gate is line coverage.

The checked-in [machine-readable coverage record](SCREEN-READING-COVERAGE.json)
contains denominators, per-file changes, missing lines and scope definitions.
It contains synthetic-test measurements only.

The subsequent [adversarial review](ADVERSARIAL-SCREEN-REVIEW.md) records
reproduced counterexamples, the evaluator/fix cycle, measured content
preservation, and remaining native-validation limits.
