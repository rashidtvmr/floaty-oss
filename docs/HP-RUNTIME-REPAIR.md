# HP runtime repair and real answer-panel acceptance

## Latest status

The November 28, 2026 repair successfully enabled GNOME toolkit accessibility,
installed usable PulseAudio client utilities in the owned application runtime,
and built and checked the bundled Moonshine speech engine on HP. FFmpeg was
already installed and its Pulse input was available. The existing PipeWire
services were active and were not replaced or restarted.

The requested public-question → native screen read → real model → visible GTK
answer test **has not passed**. A zero-provider inspection of the public
[Two Sum practice page](https://leetcode.com/problems/two-sum/description/)
was dispatched and its task exited unsuccessfully. HP then missed its remote
connection heartbeat at 08:43:32 UTC (14:13:32 India time). The inspection's
stdout/stderr could not be retrieved after the disconnect, so its failure
must not be attributed to a specific browser, accessibility, or UI cause.
There were **zero new upstream API requests** in this followup.

The earlier generated-page and real-provider CLI results remain recorded in
[`HP-NATIVE-ACCEPTANCE.json`](HP-NATIVE-ACCEPTANCE.json). They used a private
accessibility bus. The earlier GTK check rendered generated preview events.
Neither proves that a real answer reached the ordinary desktop's answer panel.

## Observed repairs

| Component | Observed result on HP |
| --- | --- |
| GNOME desktop accessibility | `toolkit-accessibility` changed from `false` to `true`; `org.a11y.Status.IsEnabled` returned true. |
| FFmpeg | Existing Ubuntu package `7:8.0.1-3ubuntu2`; Pulse input support verified. |
| Pulse client utilities | `pulseaudio-utils` and `libpulsedsp`, version `1:17.0+dfsg1-2ubuntu4`, downloaded with APT and extracted into the owned private runtime. |
| Audio connection | Installed `pactl` connected to the existing PipeWire Pulse-compatible server; the default playback monitor was present. No audio was recorded. |
| Bundled speech engine | `moonshine-voice==0.1.6`, English `TINY_STREAMING`, built on Python 3.14.4; the complete frozen worker emitted a ready event from `--check`. |
| GTK Cairo bridge | The previously installed private UI interpreter passed `gi.require_foreign("cairo")`. This does not claim that the global `python3-gi-cairo` package was installed. |
| OCR | The previously installed private Tesseract executable remained resolvable by the application. The prior generated-file OCR check is separate from live portal capture. |

System-wide APT installation required interactive authentication. The private
installation used the logged-in user's permissions; it did not bypass that
requirement, modify global Python packages, or change audio daemons.

Before the disconnect, the running Floaty process was confirmed to have the
private runtime's `bin` directory on PATH, the source checkout as
`SOCKET_STREAM_HOME`, and the installed persistent CLI. The new speech bundle
is at the exact source-tree path used by both GTK preflight and the CLI.
The complete updated dependency-check script still needs to be run on HP.

## Source corrections

Stopped **Capture** previously confirmed a foreground read while Floaty's own
dialog could retain focus. Source inspection showed that this could reach the
reader's own-window exclusion. The correction opens the existing window
chooser when no target has been selected, requires an explicit selection, and
then confirms the named target. The selected native identity is checked again
after the dialog returns. Cancellation, Stop, another Start, or stale chooser
results cannot initiate a read.

This stopped Capture path reads the selected window's accessibility text and
does not enable screenshot or OCR fallback. Live-session Capture retains its
existing control path. Automatic foreground selection and live canvas/portal
capture require their own native verification.

The typed-question field now says **Ask a question…**. Its tooltip explains
that stopped Send sends text only, while a live session can use its existing
context. This corrects the previous wording without silently capturing a
screen when the user sends text.

The installer now includes source-build venv support and an explicit
`--desktop=gnome` or `--desktop=kde` portal-backend option. The checker tests
usable capabilities with bounded, nonrecording probes. An optional
`--ui-python=/absolute/path/to/python3` checks the actual GTK runtime separately
from the system Python used by AT-SPI and portal helpers.

These source changes have local tests and independent adversarial review.
They were not deployed to the disconnected HP node during this followup.

Fresh verification passed 772 Python unit cases and 49 acceptance cases, with
13 skips. The 281 previously passing Rust cases are unchanged. Changed
production executable-line coverage is **95.71% (1,226/1,281)** against main
`886ab921`; Python coverage was refreshed and the unchanged Rust coverage was
reused. This is not whole-repository coverage: the measured workspace totals
are 52.83% for Python and 71.98% for Rust. Shell setup changes have functional
and syntax tests rather than line instrumentation.

## Actual GTK acceptance harness

`tests/acceptance/native_gtk_screen_e2e.py` opens the public practice page in a
new sandboxed Chrome profile on the real desktop accessibility bus. Ordinary
Chrome accessibility is the default; an explicit forced-accessibility run is
reported separately. The harness uses the production window chooser, Start
button, consent dialog, screen-only preflight, CLI process, event reader, and
answer renderer.

A pass requires the actual question markers in the CLI screen event, exactly
one completed provider answer, and a mapped GTK answer panel whose rendered
buffer matches the completed response. There is no injected answer or event.
The existing rendering timer must populate the buffer itself. The harness
selects a known owned window, so it does not claim to test automatic foreground
handoff.

`tests/acceptance/public_practice_relay.py` binds the actual CLI messages to a
fresh native tree and relays at most one request to the fixed OpenAI endpoint
using `gpt-6-luna`. The completion cap is 2,048 tokens. The API key is accepted
only by the separate controller through a hidden terminal or anonymous file
descriptor; the desktop receives a random loopback token. The original response
is retained privately for independent algorithm review. Structural response
checks alone do not establish that a solution is correct.

The relay uses one exclusive attempt reservation and does not retry an attempted
request. Controller and desktop reports must both pass. Responses are buffered,
so this test does not benchmark live network streaming latency.

## Remaining native checks

1. Reconnect the HP node and retrieve the failed inspection's logs before
   choosing a remediation or repeating that inspection.
2. Deploy the reviewed source and update the owned manual launcher's source
   hashes together; stop only the previously verified Floaty process when
   replacing it.
3. Run the updated dependency checker with the private UI Python and runtime
   PATH, then exercise the actual nonrecording audio preflight. Verify the
   normal app's active provider/model, key-present boolean, and selected
   sources without logging credentials; isolated acceptance preferences do
   not establish that the user's normal provider is configured.
4. Run the public-page GTK harness through the real provider, inspect the
   returned algorithm, and verify the actual visible answer buffer.

The machine-readable latest repair summary is
[`HP-RUNTIME-REPAIR.json`](HP-RUNTIME-REPAIR.json). A successful dependency probe,
synthetic test, or earlier CLI completion is not a replacement for that final
native GUI check.
