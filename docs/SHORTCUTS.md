# SocketStream shortcuts

**Ticket:** `HOTKEYS-008` · **Contract:** [A11Y-TREE-SHORTCUTS.md](A11Y-TREE-SHORTCUTS.md), section 3

Linux GTK implements **nineteen editable in-app actions**. They work while the
control window or answer panel is focused. macOS retains twelve Swift actions. See
[UI verification](READABLE-ANSWER-UI.md) for the exact tested scope.

**Ctrl+Alt+Up** (`scroll_answer_up`) and **Ctrl+Alt+Down** (`scroll_answer_down`)
scroll the Linux answer by a readable page. Both can be rebound in Keyboard
shortcuts. Mouse wheel/trackpad scrolling works over the answer even with cursor
blending on. Out-of-focus use requires user-managed desktop shortcut bindings;
the app installs no global key grab. Native macOS does not yet implement these
two actions.

## Actions and defaults

| Action | Default | Linux behavior | UI equivalent |
|---|---|---|---|
| `start` | `Ctrl+Alt+S` | Start authorized screen observation | **Start observation** |
| `stop` | `Ctrl+Alt+X` | Stop capture and inference | **Stop** |
| `pause_resume` | `Ctrl+Alt+P` | Pause/resume a live session | **Pause / Resume** |
| `toggle_overlay` | `Ctrl+Alt+O` | Show the panel (legacy show-only behavior) | **Open answer panel** |
| `show_overlay` | `Ctrl+Alt+Shift+O` | Show the answer panel | **Open answer panel** |
| `hide_overlay` | `Ctrl+Alt+H` | Remind the user to close the panel explicitly | **Close panel** icon performs the close |
| `copy_answer` | `Ctrl+Alt+C` | Copy the displayed answer, including Markdown | **Copy answer** icon |
| `compact_toggle` | `Ctrl+Alt+M` | Toggle the compact reading layout | **Compact layout** icon |
| `toggle_stealth` | `Ctrl+Alt+K` | Toggle keep-above/taskbar window hints; capture rules are separate | **Capture sources → Stealth overlay** |
| `open_settings` | `Ctrl+Alt+Comma` | Open Appearance | **Appearance** / panel settings icon |
| `refresh_question` | `Ctrl+Alt+R` | Report the current session; does not force capture | **Tools & diagnostics → Session status** |
| `quit` | `Ctrl+Alt+Q` | Stop owned processes and exit | **Quit SocketStream** in the sidebar |
| `listen_audio` | `Ctrl+Alt+A` | Open source/authorization selection for English Moonshine | **Listen to audio** |
| `pin_question` | `Ctrl+Alt+B` | Pin/unpin the displayed question and answer | **Pin question** |
| `previous_question` | `Ctrl+Alt+Left` | Pin the preceding retained pair | Select an earlier history row |
| `next_question` | `Ctrl+Alt+Right` | Pin the next retained pair | Select a later history row |
| `follow_latest` | `Ctrl+Alt+L` | Unpin and follow the newest question | **Follow latest** / **N new · Latest** |

The five Q&A actions at the bottom of the table are Linux-only. On macOS,
`open_settings` keeps its provider-settings behavior. On Linux, configure a
provider through **Session → AI provider → Configure**.

### Visibility and session lifetime

- **Minimize app** minimizes the workspace while the panel and session continue.
- Closing the main workspace keeps the session in the background.
- **Close panel** hides the panel without stopping inference. **Open answer panel**
  restores it. Focus changes and detected screen sharing never hide it.
- Linux overlay shortcuts retain show-only/reminder behavior; they do not close
  the panel. On other platforms, `hide_overlay` can be an explicit hide action.
- **Quit SocketStream** stops owned processes and exits.
- `refresh_question` reports state; the observation process decides when to read
  the next question on its configured interval.

Capture behavior is documented separately in
[GNOME-CAPTURE-EXCLUSION.md](GNOME-CAPTURE-EXCLUSION.md).

## Editing shortcuts

Open **Shortcuts** in the Linux workspace. Type an accelerator or click its pencil
icon to record keys; Escape cancels recording. **Save shortcuts** checks duplicate
bindings, writes the full set and applies it immediately. **Reset to defaults**
changes the form; Save applies that reset. The enable switch also takes effect on
Save. Editing a shortcut field or recording keys suppresses action dispatch,
including Quit.

The shared JSON config path is resolved in this order:

1. `$SOCKET_STREAM_CONFIG`
2. `$XDG_CONFIG_HOME/socket_stream/config.json`
3. `$HOME/.config/socket_stream/config.json`
4. `./.socket_stream.json` when `HOME` is unavailable

```sh
socket_stream hotkeys init           # create defaults; refuse an existing file
socket_stream hotkeys init --force   # reset shortcuts; preserve other settings
socket_stream hotkeys list           # resolved bindings and platform notes
socket_stream hotkeys print          # user-managed desktop registration example
```

```json
{
  "shortcuts": {
    "enabled": true,
    "bindings": {
      "start": "ctrl+shift+1",
      "pin_question": "ctrl+alt+b"
    }
  }
}
```

Rules:

- Only the listed actions are valid. Missing actions load their defaults.
- An accelerator is `modifier+...+key`, with exactly one key. Modifiers are
  `ctrl`/`control`, `alt`/`opt`/`option`, `shift`, and `meta`/`cmd`/`super`/`win`.
- Linux/Rust keys: letters, digits, `f1`–`f24`, `left`, `right`, `up`, `down`,
  `space`, `comma`, `period`, `slash`, `semicolon`, `apostrophe`, `minus`, `equals`,
  `tab`, `enter`, `escape`, `backspace`. Arrow bindings are unsupported on Swift.
- On Linux, portable Meta/Cmd/Super/Win means the physical **Super** key.
- Duplicates and invalid entries are rejected. The CLI reports malformed config;
  desktop shortcut loading falls back to defaults so the app remains usable.
- `shortcuts.enabled = false` disables accelerators, leaving buttons available.

### Shared persistence and appearance

The **Appearance** page previews changes immediately; **Save appearance** keeps
them for the next launch:

```json
{
  "desktop": {
    "appearance": {
      "opacity": 0.9,
      "font_size": 16,
      "reduced_motion": false,
      "cursor_blending": true
    }
  }
}
```

Opacity accepts `0.55–1.0`, text size accepts integers `13–22`, and reduced motion
and cursor blending are booleans. Cursor blending defaults to `true`: the answer
background passes hover/clicks to the window below while controls remain active.
Switch it off for normal panel selection and scrolling. The panel's **Cursor
blending: On/Off** button saves immediately and mirrors the Appearance checkbox;
Appearance previews persist on **Save appearance**. Native Wayland disables this
X11/XWayland feature and accepts input over the full panel, retaining the saved
preference for the next supported session. Capture exclusion is a separate setting.
Missing fields use defaults. Invalid appearance data is reported in
the workspace, which starts with defaults. Provider credentials stay in their
separate provider-settings file.

Python saves and Rust shortcut resets coordinate through `<config>.lock` with
POSIX `flock`, reread the latest object and atomically replace it with a `0600`
file. They preserve unrelated sections and unknown siblings inside `shortcuts`.
Malformed/empty/non-object JSON and symlink/non-regular config or lock files are
refused. Correct the reported JSON error before saving again.

`save_config_file(bindings, enabled=True)` writes a **full shortcut snapshot**:
omitted actions take their defaults and the binding map is replaced. GTK passes
all seventeen entries and the enable toggle. Rust `Config::save()` writes all
typed fields while retaining unknown fields recursively; its binding map is also
a snapshot. `hotkeys init --force` only resets bindings and enables them.

## Platform behavior

### Linux — X11

`Gtk.AccelGroup` is attached to the **main/control window**. The non-activating
answer panel does not register a separate accelerator group. Out-of-focus global
shortcuts are not registered by SocketStream.

For user-managed registration, `hotkeys print` emits an `sxhkdrc` block:

```text
ctrl + alt + s
    xdotool search --limit 1 --name '^SocketStream — [A-Za-z][A-Za-z]*$' windowactivate --sync key --clearmodifiers ctrl+alt+s
```

The anchored pattern selects one-word status titles (`Stopped`, `Active`,
`Stopping`), excluding `SocketStream — Practice Answer`. Merge the block into
your `sxhkdrc` if desired. This forwarding path depends on desktop grabs and XKB
symbol synthesis; it is not an app-owned global binding. SocketStream installs
nothing automatically.

### Linux — Wayland

Out-of-focus shortcuts need compositor settings or the
`org.freedesktop.portal.GlobalShortcuts` portal. SocketStream does not register
with that portal. In-app shortcuts operate with the control window focused;
GNOME uses the app's XWayland window. Native Wayland compositor-specific global
registration remains unimplemented.

### macOS

Swift exposes the original twelve actions through an `NSMenu` **Shortcuts** menu
and one `NSEvent.addGlobalMonitorForEvents(matching: .keyDown)` monitor. The five
Q&A actions and arrow-key bindings are not implemented there. Accessibility
permission may be needed for the global monitor, which observes rather than
consumes events. Physical out-of-focus verification remains platform-specific;
the Linux UI checks do not establish macOS behavior.
