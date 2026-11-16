# Answer-panel sessions and saved providers (Linux)

The answer panel's **Session & provider** section can run the normal workflow
without reopening the workspace. It collapses in compact mode to preserve the
answer reading area; expand it to change settings or start again.

- **Start fresh** uses the same source consent and readiness checks as Start in
  the workspace. Select Playback/Microphone and/or Screen first. After Stop has
  finished, it starts a new session; cancelling consent retains the old answers.
- **Stop** ends the current operation. Provider/model/source controls are locked
  while an operation is running or starting. **Next question** cancels the current
  answer without stopping capture. **Pause answer** freezes presentation only.
- Choose a saved provider, fetch its models with the refresh button, choose or
  type a model ID, then **Use model**. This saves the preferred model to that
  profile. An edited model must be explicitly applied with Use model before
  Start fresh; cancelling capture consent never saves a model change. Blank Codex
  model uses the official CLI default.
- **Providers…** opens the profile manager. **New profile** starts a clean draft
  with no copied API key. **Duplicate** deliberately copies the selected draft
  under a new name. **Rename** preserves credentials/model; **Delete selected**
  removes only the confirmed profile. **Save profile** keeps the manager open to
  add another; **Save & use profile** closes it. Invalid settings stay in the
  dialog without mutating the active provider. **Use & test** saves/applies the
  profile, then asks permission for one fixed inference test (no screen/audio).
  Replacing another existing named
  profile requires confirmation.
- Unsaved, memory-only settings are labeled as such and cannot overwrite a saved
  profile through Use model. Credentials are never passed on the command line or
  logged. Saved keys remain plaintext in the user-only settings file (0600).
- Fallback remains a separately enabled saved profile. It may receive question
  text and incur charges; it is never enabled automatically by model discovery.
- **Audio…** offers inventory-only device discovery: playback monitor choices for
  system audio, input choices for microphones. Choose the monitor used by your
  call/video; the default monitor is resolved at Start. Stop/Start after changing
  speakers/headphones or their routing. No audio is recorded by the device list.
- **Window…** selects an explicit AT-SPI question window. Fixed-window mode is
  accessibility-only and never silently retargets or falls back to screenshots.

## Manual sending, context and stream compatibility

**Send recognized** sends the latest finalized speech or readable screen text
through the active session. It does not start capture or silently retry a failed
request. It is disabled while stopped, paused or answering. For audio with no
automatic answers, select Mixed/unknown speakers in Audio options.

Live sessions retain up to three successfully completed question/answer pairs
(2,000 question characters and 4,000 answer characters per turn) in memory for
follow-ups, alongside current screen context when screen reading is selected.
Failed/cancelled partial answers are not retained. Start fresh creates a new
backend session and clears this context; it is never written to logs or disk.

In Providers, **Buffered compatibility** sends a single `stream:false` request
up front. The panel waits for the whole answer rather than token deltas. It does
not retry or replay an empty stream and does not enable provider fallback. Use
this setting for a gateway whose streaming responses are intermittent.

## Themes

Open panel settings (gear) or workspace **Appearance**. Choose **Light**, **Dark**,
or **System preference**, then **Save appearance**. The workspace, provider/audio
dialogs and answer panel preview together. System preference follows GTK theme
notifications. Font size, opacity and reduced-motion preferences remain separate.

## One desktop instance

The normal GTK launcher now holds a private per-user instance lock. Opening it
again exits the duplicate launcher and, when PID-safe notification is available,
restores the existing workspace. It never starts capture or a second live backend
as a consequence of opening the launcher. Explicit CLI workflows are unaffected.
Close all older app instances before first running this update: builds predating
this guard do not hold its lock.

## Verified scope and limits

Ubuntu GTK tests cover fresh restart, rejected consent, locked live controls,
profile/model restoration, two-profile saves, validation without runtime mutation,
new drafts without inherited credentials, discarded stale catalog responses,
compact reading space, theme preview and persistence. Model discovery succeeded
against the user's configured compatible provider (69 model IDs, no inference).

Codex login and model discovery still do not establish inference access. An
isolated official CLI default-model test independently retried the connection and
timed out; no application change can establish account entitlement from that.
Physical headphone/Bluetooth switching and native macOS equivalents of these
new Linux controls remain unverified.

For staged Linux verification, set `SOCKET_STREAM_GTK_NATIVE_E2E=1` when running
`tests/gtk_workspace_checks.py` under Xvfb. This additionally exercises the real
CLI, synthetic ASR final events, GTK manual Send, a loopback buffered provider,
contextual follow-up, Stop/fresh-session reset, and isolated duplicate launching.
It never records hardware audio or invokes a remote model. Real FFmpeg/Moonshine
internal-playback tests are separate and use a private Pulse null-sink monitor.
