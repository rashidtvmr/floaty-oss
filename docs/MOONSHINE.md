# Built-in streaming speech

**Listen · Moonshine** on macOS, or **Listen to audio** on Linux, starts the
bundled English speech engine after you choose microphone or system playback.
Recognized text appears in the floating panel. Finalized phrases are debounced
for 900 ms and sent to the selected AI provider; its answer streams into the panel.

Linux now offers an explicit **Speaker/source role** in the listening dialog.
Microphone defaults to **My answers** (automatic replies suppressed); playback
defaults to **Other person's questions**. Choose **Solo practice** for the old
answer-every-phrase behavior, or **Mixed/unknown** for manual Send only. These are
source assignments, not automatic speaker identification. Explicit clarifications
can refine an existing question; see [turn and cache details](AI-PROMPT-CACHING-AND-TURNS.md).

Speech recognition runs locally with **Moonshine Voice 0.1.6, English
TINY_STREAMING**. Release bundles include Python, native inference libraries,
and the model. There is no runtime model download or separate Whisper setup.
An external AI provider still receives finalized text when selected. Use a local
Ollama provider for local answer generation too.

## Controls and CLI

Pause destroys the capture and speech workers and cancels the active answer.
Resume creates a fresh audio stream. Stop ends the session. Neither PCM nor
transcripts are saved by this listening path; its JSONL output is intended for
the UI (redirecting it to a file would save the transcript).

```sh
cargo run -p socket_stream -- audio-live --authorized --source microphone --provider ollama
cargo run -p socket_stream -- audio-live --authorized --source system --provider ollama
```

The CLI accepts `pause`, `resume`, and `stop` on stdin, and handles Ctrl-C/SIGTERM.
Closing stdin ends listening. A closed/full UI output pipe also ends listening
rather than blocking shutdown. Terminal stdout retains its normal flags so it
cannot make shared terminal input nonblocking. Both desktop readers bound their
main-loop handoff to 32 callbacks and terminate their owned CLI on saturation.
The Swift reader consumes only the bytes currently available (POSIX readiness
read), so short events are displayed immediately instead of waiting for a full
buffer; a panel self-test regression covers this. Linux accepts
`--device SOURCE_NAME`;
`audio-devices` lists PulseAudio sources. Default system audio uses the default
playback sink's `.monitor` source. macOS uses the default microphone selected
in System Settings.

Legacy segmented Whisper remains available with
`--stt whisper --model models/ggml-tiny.en.bin`. Its segment behavior is separate
from Moonshine's continuous streaming.

## Build

```sh
# Python 3.10+; macOS example:
python3.12 scripts/build-moonshine.py
sh scripts/build-macos-app.sh --no-install
```

The build downloads dependencies/model weights and freezes the worker under
`target/native-helpers/moonshine/socket_stream_moonshine/`. `--check` on that
directory's executable initializes the model without opening an audio device.
`PLATFORM.json` describes the build; model digests are in `_internal/models/SHA256.json`.

Build the Linux runtime on the target architecture, then package with:

```sh
SOCKET_STREAM_MOONSHINE_DIR=/path/to/linux/socket_stream_moonshine \
  sh scripts/build-linux-release.sh x86_64
```

The packager rejects runtime metadata for a different OS or architecture.
Linux additionally needs FFmpeg and PulseAudio/PipeWire (`pactl` for default
system playback). The speech runtime is glibc-based even when the Rust CLI is
built with musl.

## Support and measured verification

- **macOS:** pinned Moonshine wheel requires Apple Silicon and macOS 15+.
  Build/model initialization and synthetic speech-to-answer tests ran on macOS
  27 arm64. Older supported OS releases still need runtime verification.
- **Linux:** runtime built and initialized on Ubuntu 26.04.1 x86_64, Python
  3.14.4, glibc 2.43. This artifact has not been validated on older distributions;
  build on the oldest supported distribution for a wider release baseline.
- **Other platforms:** SocketStream streaming capture is not implemented.
- Worker tests cover PCM conversion and native transcript-history rotation at
  phrase boundaries after 60 s, with a 120 s hard limit. Rust bounds event queues,
  transcript history, pending prompts and streamed answers.
- Synthetic E2E uses real bundled Moonshine with generated PCM and a mock AI
  provider. It checks partial/final text, answers, authorization rejection and
  worker cleanup. It opens no microphones or system playback devices.
- Real microphone/system-audio permissions, device switching, hardware latency,
  long-session accuracy and speaker separation remain unverified. This feature
  does not identify speakers.

### Packaging check (2026-11-15)

- An isolated macOS debug `.app` at
  `target/moonshine-package-irlrcup0/SocketStream.app` passed strict recursive
  code-signature verification, its panel self-test, and bundled model
  initialization from `/`. It is ad-hoc signed, not notarized, and was **not
  installed** over the user's existing app.
  The same six synthetic E2E cases also passed using the CLI and speech runtime
  copied into this app bundle (with synthetic capture, not hardware sensors).
- `dist/socket_stream_linux-x86_64-20261115.tar.gz` was rebuilt with the bundled
  Linux speech runtime (about 160 MB). SHA-256:
  `2ea895eb15fea7f5284aea66478dcaf9a18c2e1d6c9ffceb5389ace6c3ecba77`.
  On 2026-11-15 the archive was installed on Ubuntu 26.04.1 x86_64 (glibc 2.43,
  Python 3.14.4) at `/opt/socket_stream`: the remote checksum matched,
  `check-dependencies.sh` passed, the CLI smoke tests (`doctor`, mock
  `analyze-file`, sample OCR) ran, and the bundled runtime reported `ready` via
  `--check`. The glibc baseline caveat above still applies; the install host was
  headless, so live desktop-session use was not exercised.
- Rust formatting/Clippy and all 144 workspace tests passed. Python discovery
  ran 217 tests successfully with four optional upstream-source tests skipped;
  the five-test Mutter source suite passed separately with its source fixture.
  Six synthetic Moonshine E2E cases and the Swift panel self-test passed.

Independent audio review found GUI-owner EOF, blocking stdout, and repeated
questions after Resume defects. Fixes terminate on control EOF, use a
nonblocking bounded OS output pipe (failure stops capture), and deduplicate UI
questions by request ID instead of text. Follow-up review found shared-terminal
flags, GUI main-loop handoff bounds, and E2E wait bounds also needed correction.
Terminal output is now exempt from flag changes, GTK/Swift handoffs have 32-slot
limits with fail-closed termination, and E2E waits have an absolute deadline and
retention cap. A further follow-up found the Swift buffered read delayed small
events until 16 KiB accumulated; the reader now performs a POSIX readiness read
and the panel self-test regresses short-event delivery. A final narrow review
confirmed the fix (balanced semaphore accounting, EOF/error handling, no
descriptor-lifetime defect) with synthetic pipes only; hardware permission and
lifecycle acceptance is not complete.

Run the targeted checks after building the CLI and runtime:

```sh
python3 -m unittest discover -s tests -p test_moonshine_worker.py -v
python3 -m unittest discover -s tests/acceptance -p test_moonshine_e2e.py -v
target/native-helpers/socket_stream_gui --self-test
```
