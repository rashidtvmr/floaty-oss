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

## Themes

Open panel settings (gear) or workspace **Appearance**. Choose **Light**, **Dark**,
or **System preference**, then **Save appearance**. The workspace, provider/audio
dialogs and answer panel preview together. System preference follows GTK theme
notifications. Font size, opacity and reduced-motion preferences remain separate.

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
