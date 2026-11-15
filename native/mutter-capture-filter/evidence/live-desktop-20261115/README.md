# Installed release and live desktop activation — 2026-11-15

Target: `rashid-lenovo`, Ubuntu GNOME Wayland, desktop UID 1000.

The installed application is commit
`4c4eba9fe7769ea2e808438abf70bf53b02f90b1`. The live compositor uses
`50.1-0ubuntu2.5.socket-stream.3b4e8e8a98dcb8a3`.

| File | What it establishes |
| --- | --- |
| `release-validation-4c4eba9.json` | Eleven packaged UI/reference-page source files match the release commit; CLI is ELF; bundled Moonshine is Linux x86_64 English TINY_STREAMING. |
| `installed-4c4eba9.json` | Installation path, retained previous installation, unchanged user-configuration snapshot, and successful packaged Moonshine `--check` (ready, exit 0). |
| `installed-ui-setup.json` | Actions dispatched through the installed GTK application's accessibility tree: expand advanced tools, Replay demo, Set up exclusion. The app reported activation installed and showed its actual answer panel. |
| `pre-login-status.json` | Validated configuration, guard `armed`, current stock Shell PID 11136, and `restart_required=true`. |
| `desktop-shell-lifecycle.jsonl` | Read-only systemd D-Bus observations of the actual `org.gnome.Shell@ubuntu.service`: stock-session exit `(success, exited, 0)`, followed by the new invocation reaching `active/running`. No lifecycle hook was added for this observer. |
| `active-login-status.json` | Actual copied Shell PID 125880, four private library mappings, opt-in `1`, matching `ready` guard, and installed backend reporting `runtime_active=true`, `restart_required=false`. |
| `installed-ui-drag.json`, `installed-ui-drag-paced.json` | Two automated XTest input attempts observed no panel movement. They do not establish working drag behavior for this run. Earlier drag implementation/testing is separate. |

The user subsequently reported: **“its working finally”**, then requested the
method be committed and documented. This is a user-reported live result; it is
not a saved automated received-frame comparison.

## Evidence boundaries

- The exit tuple here was observed for the **stock** session being replaced.
  The newly active custom desktop was left running; its subsequent normal logout
  and automatic rearming were not exercised in this run. The separate eight-case
  systemd harness covers guard rearming semantics.
- `service_exit_tuple` is decoded from systemd's `Result`, `ExecMainCode` and
  `ExecMainStatus` during `stop-post`. The lifecycle harness's tuples come directly
  from its `ExecStopPost` environment; these are distinct observations.
- The installed reference page was opened in Chrome 155. The browser closed
  before the scripted receiver attached, so this directory contains **no new
  encoded received-frame pixel result**. The backend correctly retains
  `verified=false` despite successful live activation.
- The Replay demo fixture deliberately contains a synthetic provider-error event.
  Its displayed error label is fixture content, not a failed real provider call.
- No provider credentials, personal screen images, or audio recordings are
  included. User configuration was inspected by path/hash metadata only.
