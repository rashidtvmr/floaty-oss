# Extraction and OCR baseline

## Reproduce

```sh
cargo run --release -p socket_stream -- benchmark-extraction \
  --iterations 500 --with-ocr --ocr-iterations 3
```

In the Linux archive, use `./socket_stream benchmark-extraction` with the same
options. Omit `--with-ocr` to run only the embedded synthetic accessibility cases.
No live screen/audio capture or inference is performed. Output contains metrics
rather than source text. Iterations are bounded to 1–10,000 and OCR runs to 1–10.

The six fixtures in `tests/fixtures/extraction/cases.json` cover code/examples/
constraints, an image question behind long navigation, mixed text and a diagram,
multiple-choice options, short CJK text and protected descendants. Every run
checks required text verbatim in both model and display context, excluded text,
image-region counts and text-coverage decisions; a mismatch fails the command.

## Measured macOS arm64 baseline — 2026-11-16

Optimized build, 500 parse/render runs per case:

| Synthetic case | Tree → question token estimate | Reduction | Question parse/render p50 / p95 |
|---|---:|---:|---:|
| Code, examples, constraints | 87 → 65 | 25.3% | 0.0058 / 0.0061 ms |
| Image with navigation | 35 → 4 | 88.6% | 0.0015 / 0.0016 ms |
| Mixed text/diagram | 36 → 36 | 0% | 0.0028 / 0.0029 ms |
| Multiple choice | 53 → 43 | 18.9% | 0.0040 / 0.0042 ms |
| Short CJK | 4 → 4 | 0% | 0.0008 / 0.0009 ms |
| Protected descendants / short code | 20 → 7 | 65.0% | 0.0020 / 0.0021 ms |

All six fidelity checks passed. Image-only context retains structural role labels
but has zero readable characters, so the coverage check correctly requests OCR
when authorized. Mixed content retains the image region despite sufficient text.
Token estimates use `ceil(Unicode characters / 4)` and do not predict a provider's
tokenization or billing.

The existing Apple Vision helper read the bundled `sample-code.png` three times:
first call **642.3 ms**, p50 **346.4 ms**, p95 **642.3 ms**. Both required code
identifiers (`twoSum`, `target`) survived each run; the final output contained
98 characters and one syntax-quality warning. This checks identifier retention,
not character/word accuracy. Each call starts a helper process.

These small-fixture timings exclude OS accessibility traversal, screenshot
acquisition, region cropping, model/network latency and answer accuracy. They
provide a reproducible starting point; they do not establish end-to-end desktop
latency or persistent-engine performance. Measure those stages separately before
changing observation scheduling or the OCR backend.

## Optional browser-reader research

Native AT-SPI2/AX stays the default. The optional Agent Browser integration is
specified but not bundled in this release. Its documented scoped command is
`agent-browser --cdp PORT snapshot -c -s SELECTOR --json`, returning
`{success,data:{snapshot,refs}}`. `-i` omits noninteractive question content and
is unsuitable as the default extraction mode.

The [commands documentation](https://agent-browser.dev/commands) advertises
snapshot deltas; capability/version checks are needed before relying on them.
The inspected [snapshot renderer](https://github.com/vercel-labs/agent-browser/blob/main/cli/src/native/snapshot.rs)
also appends form values and temporarily annotates DOM elements. Its
[daemon](https://github.com/vercel-labs/agent-browser/blob/main/cli/src/native/daemon.rs)
starts a streaming server, and [dialog handling](https://agent-browser.dev/commands#dialogs)
defaults to automatically accepting some dialogs. These behaviors need explicit
configuration and regression checks for a read-only integration.

The adapter must attach only to a user-authorized existing debug connection,
scope the selected content, omit protected values, bound output and clean up its
own connection. Browser navigation, clicking, typing and submission belong to a
separate interactive permission with action/time bounds and Stop cancellation.
No general browser automation is enabled by the screen-reading grant.

References inspected on 2026-11-16: [snapshots](https://agent-browser.dev/snapshots),
[CDP mode](https://agent-browser.dev/cdp-mode),
[security/configuration](https://agent-browser.dev/security).
