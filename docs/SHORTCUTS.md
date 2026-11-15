# SocketStream shortcuts

**Ticket:** `HOTKEYS-008`
**Contract:** `docs/A11Y-TREE-SHORTCUTS.md` section 3
**Status:** implemented; end-to-end verification on real X11/Wayland/macOS hardware is pending.

SocketStream has twelve stable actions exposed through keyboard accelerators,
desktop controls and the CLI (`socket_stream hotkeys`), subject to platform-specific
behavior noted below. The accelerator set is configurable.

## Actions and defaults

| Action | Default | What it does | UI equivalent |
|---|---|---|---|
| `start` | `Ctrl+Alt+S` | Start authorized observation | **Start observation** button |
| `stop` | `Ctrl+Alt+X` | Stop capture and inference | **Stop** button |
| `pause_resume` | `Ctrl+Alt+P` | Pause/resume the live session | **Pause** button in the answer panel |
| `toggle_overlay` | `Ctrl+Alt+O` | Toggle the panel (Linux: show only) | **Show answer panel** button |
| `show_overlay` | `Ctrl+Alt+Shift+O` | Show the answer panel | **Show answer panel** button |
| `hide_overlay` | `Ctrl+Alt+H` | Hide the app from the screen | **Close panel** button in the answer panel |
| `copy_answer` | `Ctrl+Alt+C` | Copy the current answer | **Copy answer** button |
| `compact_toggle` | `Ctrl+Alt+M` | Toggle the compact panel layout | **Compact** toggle |
| `toggle_stealth` | `Ctrl+Alt+K` | Toggle capture-exclusion hints | **Stealth overlay** checkbox |
| `open_settings` | `Ctrl+Alt+Comma` | Open provider settings | **Settings** button |
| `refresh_question` | `Ctrl+Alt+R` | Best-effort status check (see below) | — |
| `quit` | `Ctrl+Alt+Q` | Quit SocketStream | window close |

`hide_overlay` is the explicit hide action on platforms that support it.
`show_overlay` restores the panel.

In the Linux GTK shell, **Minimize app** is available beside the main controls
and **Minimize** is available on the answer panel. It minimizes the main window
without hiding the panel or stopping capture/inference. For the Linux GTK panel,
visibility is click-to-close: focus changes and screen-share detection never hide
it. Only clicking **Close panel** or the panel's window-close control hides it.
Overlay shortcuts never close it (`toggle_overlay` shows it; `hide_overlay`
reminds you to use the Close control). A screen capture may include the panel.

`refresh_question` is honest about what it can do: the observation process
decides when a new question is read, so the shortcut only reports the current
state. It never forces a capture.

## Rebinding

The accelerator for every action lives in the JSON config file. Path
resolution, in order:

1. `$SOCKET_STREAM_CONFIG`
2. `$XDG_CONFIG_HOME/socket_stream/config.json`
3. `~/.config/socket_stream/config.json`
4. `~/.socket_stream.json`

Write a fresh file with the defaults, then edit it:

```sh
socket_stream hotkeys init           # refuses to overwrite an existing file
socket_stream hotkeys init --force   # overwrite it
socket_stream hotkeys list           # show the resolved bindings and platform
socket_stream hotkeys print          # print a registration snippet for your desktop
```

```json
{
  "shortcuts": {
    "enabled": true,
    "bindings": {
      "start": "ctrl+alt+s",
      "hide_overlay": "ctrl+alt+h"
    }
  }
}
```

Rules:

- Only the actions listed above are valid. An unknown action is an error.
- An accelerator is `modifier+...+key`. Modifiers are `ctrl`/`control`,
  `alt`/`opt`/`option`, `shift` and `meta`/`cmd`/`super`/`win`. Exactly one key
  is required.
- Keys: letters, digits, `f1`–`f24`, `space`, `comma`, `period`, `slash`,
  `semicolon`, `apostrophe`, `minus`, `equals`, `tab`, `enter`, `escape`,
  `backspace`.
- Duplicate accelerators are rejected, and an invalid entry is reported rather
  than silently ignored. `socket_stream hotkeys list` shows the resolved table;
  the desktop shells fall back to the defaults if the file cannot be parsed so
  the app always starts usable.
- `shortcuts.enabled = false` turns the accelerators off without deleting the
  bindings. The buttons and menus still work.

## Platform notes

### Linux — X11

In-app accelerators are installed with `Gtk.AccelGroup` on the main window and
work while the SocketStream window is focused.

Out-of-focus (global) shortcuts are **not** installed by the app. X11 has no
standard global-hotkey API, so use your desktop's own tool. `socket_stream hotkeys
print` emits an `sxhkdrc` block that forwards each key to the SocketStream window
with `xdotool`:

```
ctrl + alt + s
    xdotool search --name "SocketStream" windowactivate --sync key --clearmodifiers ctrl+alt+s
```

Save it as `~/.config/sxhkd/sxhkdrc` and run `sxhkd`. `xbindkeys` works the same
way. SocketStream installs nothing by itself.

### Linux — Wayland

Wayland deliberately does not let a client grab global keys. Out-of-focus
shortcuts need the compositor's own settings or the
`org.freedesktop.portal.GlobalShortcuts` portal, when the desktop implements it.
SocketStream does not open that portal on its own; the in-app accelerators work
while the window is focused.

### macOS

The app registers an `NSMenu` **Shortcuts** menu with a key equivalent for every
action, so each one works while SocketStream is focused and is also clickable
without a keyboard. It also installs a single
`NSEvent.addGlobalMonitorForEvents(matching: .keyDown)` monitor (registered once,
guarded against duplicates) so the same keys work while another app is focused.

macOS may require **Accessibility** permission for a global key monitor, and a
global monitor can observe but not consume the event. SocketStream never bypasses
that permission and never installs a login item or a background grab.

## Honesty

- SocketStream **never installs a global hotkey grab silently** and never bypasses
  OS permission. Global shortcuts are best-effort and documented as such.
- Every shortcut is also reachable from the UI without a keyboard (buttons or
  menu items), and every shortcut is listed here.
- The stealth overlay is best-effort: on Linux no public API guarantees
  exclusion from a full-screen capture, and on macOS `NSWindow.sharingType` is
  the documented mechanism but not a universal guarantee.
