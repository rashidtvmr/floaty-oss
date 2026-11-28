# Adversarial review of Linux screen reading and session context

Review date: 2026-11-28 (Asia/Kolkata). Reviewed published baseline:
`5ffa8cb4c615e628d6668befb9d7fd82333d8fc1`.

## Review method

Separate adversarial reviewers examined Linux/AT-SPI selection, tree
reconciliation, and provider/session behavior. The integration owner reproduced
failures through the actual CLI and loopback HTTP requests. Reviewers then
cross-reviewed the fixes, looking for counterexamples rather than relying on
line coverage. Newly found defects received regression tests and another
evaluation pass.

All test screen/audio content is synthetic. Provider tests use loopback HTTP or
the mock provider. No user's desktop, microphone, credentials, or external AI
inference was used for these regressions.

## Findings and corrections

| Failure reproduced | Correction | Evidence |
| --- | --- | --- |
| A readable question after hundreds of empty layout containers appeared in the UI but was missing from the model snapshot. | Before full-tree serialization, discard empty structural branches while retaining meaningful, focused, image and actionable nodes and every required ancestor. Preserve native keys, parents and order; explicit focused projections keep their existing scope. | `test_tree_projection_e2e.py` checks the actual HTTP snapshot and subsequent additions/removals. |
| Excluding GNOME compositor mirrors removed the only ACTIVE evidence and allowed a stale background FOCUSED widget to become the target. | Preserve active-mirror evidence and refuse ambiguous foreground fallback. Independently active application windows and explicitly selected windows remain supported. | Linux helper tests prove the background text interface is never called. |
| An active assistant window, or an active window whose native role lookup failed, could lose selection to stale background state. | Check ownership and unavailable active-window evidence before focus tie-breaking. Own-window refusal retains its distinct result so it cannot become screenshot fallback. | PID, application-name, title, missing-role, active-frame/application and explicit-target regressions. |
| Failed role/name lookups became empty authoritative metadata; a failed role lookup also weakened password classification. | Omit the unavailable subtree, mark the read partial, and avoid reading its sensitive name/text/children. Reuse validated root metadata. | Failure/recovery tests preserve stable keys and parent links and check that unknown-role protected content is never accessed. |
| An SSE `[DONE]` marker could override a failed finish reason or an in-band error, committing partial output. | Reject incomplete, filtered, tool-call and unsupported completion outcomes before acknowledgement. Reject empty/whitespace-only output consistently. | Local HTTP tests exercise streaming and non-streaming responses and failed fallback candidates. |
| Malformed streaming field types were silently treated as missing fields. | Validate the known event structure using typed fields while supporting absent/null metadata and additive provider fields. | Wrong choices/delta/content/finish-reason shapes after a valid partial chunk cannot be acknowledged. A separate nine-case JSON-envelope table exercises both public response APIs and proves malformed/empty non-streaming replies publish no successful answer. |
| Divergent cloned sessions at equal numeric revision could cross-commit updates prepared against different histories. | Rotate an immutable baseline identity on every successful commit/reset. Clones share an identity only while they share a baseline. | Reconciler, conversation and live-context regressions reject foreign, stale, duplicate and divergent-branch commits. |
| JSON escaping expanded replay history beyond the local provider limit while raw history still fit. Repeating the request remained stuck. | Check serialized replay size when deciding to checkpoint, then stage a new snapshot and bounded dialogue recap transactionally. | Quote/backslash/control-character/Unicode cases checkpoint before provider rejection; ordinary short turns retain the normal 16-turn behavior. |
| The speech turn gate remembered a failed/cancelled question before the conversation acknowledged it. Identical spoken retries were suppressed and later clarifications could refer to the failed topic. | Stage the gate with the request and commit it only after model context succeeds. Clear failed/cancelled transcript deduplication state. Preserve any newer user-selected speaker role. | Real CLI tests cover failure, cancellation, Pause, identical retry and role changes during a held response. |
| A question entered while audio mode was paused remained queued after Resume. | Dispatch the queued question on Resume with acknowledged history and an explicit unavailable current-screen update until a new capture is accepted. | HTTP history assertions prove dispatch and prevent a pre-pause capture from being presented as current. |
| Clarifications repeated the previous question and answer in addition to conversation history. Large Unicode requests could terminate the session. | Send the latest clarification with a history reference, and report recoverable preparation errors without ending the live session. | Long Unicode, failed-topic, and invalid-capture recovery tests inspect complete provider requests. |
| Coverage setup cleaned before establishing the instrumentation environment, allowing cached uninstrumented unit tests to pass without contributing coverage. | Isolate coverage builds, establish `show-env` first, then clean and rebuild. Point acceptance fixtures at that exact CLI. | Full gate rerun from the isolated target; raw profiles and reports are retained under `target/screen-coverage`. |

The second evaluator pass found the malformed streaming-field cases and the
own-window error-precedence issue after the initial fixes. Those findings were
fed back into implementation and regression coverage before final verification.
An additional coverage check against the already-published implementation
identified untested malformed non-streaming envelopes. Their regression table
was added before the final isolated verification run; no production change was
needed for those cases.

## Measured content improvement

The adversarial fixture contains 352 nodes: a root, five groups of empty panels,
and the actual question. It fits the native helper's node, depth and per-parent
child limits.

| Same synthetic fixture | Published baseline | Hardened implementation |
| --- | ---: | ---: |
| Nodes sent in the initial snapshot | 305 | 2 |
| Complete snapshot | No | Yes |
| Actual question present | No | Yes |
| JSON size using the same harness serialization | 18,412 bytes | 365 bytes |

This is a measured regression fixture, not a claim about average production
latency, token use, or compression. The optimization preserves the question
instead of increasing the payload budget. A later meaningful descendant brings
back its required ancestors and reconciles against stable node identities.

## Verification and reproduction

The checked-in [coverage record](SCREEN-READING-COVERAGE.json) reports exact
production-line denominators, missing lines, full-module percentages, changed
runtime coverage, and whole-workspace figures separately. Inline Rust test
bodies do not count toward production coverage. The enforced gate remains 95%
for the five feature modules and aggregate changed executable runtime lines.

The final isolated run passed formatting, Clippy, shell syntax and diff checks.
Its test results were:

| Suite | Passed | Skipped |
| --- | ---: | ---: |
| Rust workspace | 281 | 0 |
| Python unit tests | 710 | 6 |
| CLI acceptance | 49 | 7 |
| Total | **1,040** | **13** |

Production line coverage is **96.00% (1,008 / 1,050)** for the complete change
against main, and **97.01% (162 / 167)** for this hardening pass against the
published baseline. The five separately gated feature modules range from
98.77% to 100%. Whole measured workspace coverage remains separate: Rust
71.98%, Python 49.41%. These are line-coverage figures, not reliability rates.

From a development environment with the documented Rust/Python coverage tools:

```sh
bash scripts/verify-screen-coverage.sh 886ab92147542c65a057fb3ba81f40520e763c96
```

To additionally evaluate only the hardening changes using that same collected
coverage:

```sh
python3 scripts/check_screen_coverage.py \
  --base-ref 5ffa8cb4c615e628d6668befb9d7fd82333d8fc1 \
  --output target/screen-coverage/hardening-summary.json
```

The new permanent integration regressions are:

- [Tree projection and model-input fidelity](../tests/acceptance/test_tree_projection_e2e.py)
- [Session failure, retry, resume and Unicode behavior](../tests/acceptance/test_session_hardening_e2e.py)
- [Cancellation and speaker-role lifecycle](../tests/acceptance/test_session_lifecycle_e2e.py)

## Limits of the evidence

The isolated native Wayland fixture was attempted again during this review. The
executor still rejects AF_UNIX socket creation with `EPERM`, preventing private
D-Bus and Weston startup. Its native assertions therefore remain **unexecuted**.
The [native acceptance procedure](NATIVE-WAYLAND-ACCEPTANCE.md) must run on a
compatible host; actual Ubuntu 26/GNOME/browser behavior and hardware are not
established by these synthetic tests.

Application accessibility bridges, native permission behavior, provider behavior
and model reasoning can still fail outside the tested cases. Complete line
coverage would not prove universal correctness. This review does not claim a
100% reliability guarantee.

Generic compatible chat endpoints still replay bounded acknowledged history.
Only the latest screen update is incremental. This implementation does not
claim provider-held delta sessions or guaranteed model-side reconstruction of
every partial tree.

## Platform references

Native AT-SPI distinguishes ACTIVE, FOCUSED and DEFUNCT; its state enumeration
does not define PROTECTED. The native role-name API has an explicit failure
channel. These details informed foreground refusal and protected-role handling:

- [GNOME AT-SPI state definitions](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/enum.StateType.html)
- [GNOME accessible role-name API](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/method.Accessible.get_role_name.html)

No blanket non-SHOWING filter was introduced. Reading scope remains explicit:
whole-window tree reading can include content outside the current viewport.
