# Floating overlay and screen-capture exclusion

**Ticket:** `STEALTH-OVERLAY-005` (Linux GTK overlay)
**Target:** Linux x86-64 (X11 and Wayland). The native macOS AppKit panel is a
separate implementation documented in section 7 and `docs/MACOS-TESTING.md`;
sections 1–6 and the capability matrix below describe Linux only.
**Status:** visibility, slight opacity, panel identity and compositor-rule setup implemented. Panel identity and opacity are runtime-verified on the target GNOME 50 desktop; niri/Hyprland/KDE rules have automated configuration tests but pending real-share QA. Stock GNOME/X11/sway have no per-window whole-screen exclusion. An opt-in custom Mutter 50.1 source patch is available in `native/mutter-capture-filter`; see its README for measured coverage and activation.

## 1. What this feature is

A small **floating answer panel** that renders the live question and the model answer
produced by `socket_stream observe-live` / `audio-live`. It is a *normal, identifiable*
process with a normal window; it does not disguise itself, does not masquerade as a
system component, and does not bypass any OS permission prompt.

On top of that, it offers **every capture-exclusion mechanism the Linux platform
actually documents**, and honestly reports which ones are available, which are only
configured, and which do not exist.

## 2. Honest capability matrix (this is the important part)

| Session | Mechanism | "Share a window" | "Share entire screen" | Runtime verified? |
|---|---|---|---|---|
| X11 (any WM) | override-redirect + `_NET_WM_STATE_SKIP_TASKBAR/PAGER` + `_NET_WM_WINDOW_TYPE_NOTIFICATION` | Best effort: usually absent from pickers | **Included** | No; no exclusion API |
| X11 + compositor honouring `_NET_WM_STATE_SKIP_CAPTURE` | above + `SKIP_CAPTURE` | Best effort | Best effort, compositor-dependent | No |
| GNOME Shell 50 / mutter 18 (Wayland) | XWayland **dock** layer: stays above fullscreen windows; no per-window capture rule | Excluded only because it is not the shared window | **Included** | Stacking verified on target; exclusion absent |
| Custom Mutter 50.1 (Wayland) | capture-only actor filter, compositor environment opt-in | Filtered window rendering | Filtered offscreen rendering | See `native/mutter-capture-filter/README.md`; deployed browser QA pending |
| KDE Plasma/KWin 6.7+ (Wayland) | registered `excludefromcapture=true`, `excludefromcapturerule=2` in `kwinrulesrc` | Explicit panel-window capture not covered | Output/region capture filter | Unit-tested; real-share QA pending |
| Hyprland 0.53+ (Wayland) | window rule `no_screen_share = true` | Excluded | Excluded (black rectangle) | Unit-tested; real-share QA pending |
| niri (Wayland) | window rule `block-out-from "screen-capture"` | Excluded | Excluded (black rectangle) | Unit-tested; real-share QA pending |
| sway / other wlroots (Wayland) | no documented per-window rule | Best effort | **Included** | No; no rule |
| Any | automatic hiding | **Never** | **Never** | By design |

**Bottom line:** Linux has no public API equivalent to Windows
`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` or macOS `NSWindow.sharingType = .none`.
Stock GNOME Shell/mutter, X11 and sway have no supported per-window rule.
The custom Mutter build adds a capture filter; it is a separate compositor deployment.
On niri, Hyprland and KDE Plasma/KWin 6.7+ the documented mechanism is a **compositor rule the
user installs**; the application writes that rule only after an explicit **Set up exclusion**
action. Until a real share is tested, "configured" must not be reported as "verified".
The panel never auto-hides: it stays visible locally, and on supported compositors the
compositor replaces or omits it in the capture.

**Stacking is a separate problem from capture.** On GNOME Wayland a normal toplevel is
covered by the next focused window, and by any fullscreen window. mutter has no
always-on-top protocol for Wayland clients and does not implement wlr-layer-shell.
SocketStream therefore starts the GTK process with `GDK_BACKEND=x11` on GNOME Wayland
(unless the user already set a backend) and gives the panel the X11 **dock** type. The dock
layer stays above fullscreen windows and mutter does not focus dock windows. KDE, niri,
Hyprland and native X11 sessions keep their existing plans.

### 2.1 What Final Round's Electron `setContentProtection` proves

Final Round 3.0.7 sets Electron `setContentProtection(true)` for its overlay. That is a
macOS/Windows API. It is evidence that the desktop overlay is intentionally excluded on
those platforms; it is **not** evidence of Linux capture exclusion. No Linux counterpart
exists in Electron, GTK3, Qt, X11 or GNOME 50.

### 2.2 Runtime verification on the target desktop

Measured on **Ubuntu 26.04.1, GNOME Shell 50.1, mutter 18, Wayland session,
GTK 3.24.52**, by running the real `floating_overlay.py` module (not a mock) under
`WAYLAND_DEBUG=1` and inspecting the X11 XWayland window with `xwininfo`/`xprop`:

| Check | Wayland (`GDK_BACKEND=wayland`) | X11 (`GDK_BACKEND=x11`) |
|---|---|---|
| Compositor identity | `xdg_toplevel.set_app_id("socket_stream_overlay")` | `WM_CLASS = "socket_stream_overlay", "socket_stream_overlay"` |
| Title | `set_title("SocketStream — Practice Answer")` | `WM_NAME = "SocketStream — Practice Answer"` |
| Opacity | `get_opacity() = 0.94`; toplevel visual is the RGBA visual, so GTK renders it with `cairo_paint_with_alpha` | `get_opacity() = 0.94`; the visual is not RGBA, so GTK uses `gdk_window_set_opacity` (needs a compositing manager) |
| Stacking / taskbar | normal toplevel; compositor decides | `_NET_WM_WINDOW_TYPE_NOTIFICATION`, `_NET_WM_STATE_SKIP_PAGER, SKIP_TASKBAR, ABOVE` |
| Extra | none (GTK Wayland ignores the X11 hints) | `Override Redirect State: yes`, absent from `_NET_CLIENT_LIST`, centred at (700, 355) on 1920×1080 |
| Capture exclusion | **no** — GNOME 50/mutter 18 has no rule | **no** — included in whole-screen capture |

Two implementation details came out of this verification:

* GTK 3.24 sends `set_app_id` from `g_get_prgname()` at **map** time, not at realize
  time, so the panel identity must be active around the first `show_all()`
  (`_show_window()`), not only around `realize()`.
* GTK3 has no `Gtk.Window.set_override_redirect` and no `POPUP` type hint; the working
  X11 mechanism is the introspectable `Gdk.Window.set_override_redirect(True)` before
  the first map. The earlier `POPUP` fallback could never succeed.
* Ubuntu 26.04 ships GTK4, so every GTK3 entry point must call
  `gi.require_version("Gdk", "3.0")` before importing `Gdk`; otherwise the import
  resolves `Gdk` 4.0 and conflicts with `Gtk` 3.0.

The GNOME default plan was then verified separately, while a **fullscreen RustDesk
window** covered the desktop (the real target setup):

| Check | Result on GNOME 50 / mutter 18 |
|---|---|
| Backend chosen before GTK starts | `GDK_BACKEND=x11` (only on GNOME Wayland; explicit user value wins) |
| `WM_CLASS` | `"socket_stream_overlay", "socket_stream_overlay"` |
| `_NET_WM_WINDOW_TYPE` | `_NET_WM_WINDOW_TYPE_DOCK` |
| `_NET_WM_STATE` | `SKIP_PAGER, SKIP_TASKBAR, ABOVE, DEMANDS_ATTENTION, STICKY` |
| Override redirect | `no` (a managed dock, not an unmanaged popup) |
| Placement | centred at (700, 355) on 1920×1080 |
| Above the fullscreen window | **yes** — the panel stayed visible over the fullscreen window and received pointer clicks |
| Opacity | 94%, visibly translucent over the window behind it |
| Capture exclusion | still **no**; the panel is part of the compositor output |

## 3. Frozen module interfaces

### 3.1 `apps/socket_stream_desktop/ui/overlay_guard.py` (pure, stdlib only, no GTK)

```python
SESSION_X11 = "x11"
SESSION_WAYLAND = "wayland"
SESSION_UNKNOWN = "unknown"

class SessionInfo:            # dataclass(frozen=True)
    session_type: str         # SESSION_X11 | SESSION_WAYLAND | SESSION_UNKNOWN
    desktop: str              # lowercased XDG_CURRENT_DESKTOP
    compositor: str           # "gnome" | "kde" | "sway" | "niri" | "hyprland" | "x11" | "unknown"
    has_x11_display: bool
    has_wayland_display: bool

def detect_session(environ=None) -> SessionInfo

class ExclusionPlan:          # dataclass(frozen=True)
    strategy: str             # "x11-override-redirect" | "xwayland-dock" | "wayland-toplevel" | "none"
    guaranteed: bool          # documented compositor rule already configured
    override_redirect: bool
    keep_above: bool          # X11 only; GTK Wayland ignores it
    skip_taskbar: bool        # X11 only; GTK Wayland ignores it
    window_type: str          # e.g. "_NET_WM_WINDOW_TYPE_DOCK" or ""
    atoms: dict               # {"_NET_WM_STATE": ["_NET_WM_STATE_ABOVE", ...]}
    layer: str                # "" | "toplevel" | "dock"
    notes: tuple              # tuple[str, ...]
    def as_dict(self) -> dict

def prefer_x11_backend(session) -> bool   # GNOME Wayland only
def ensure_panel_backend(session=None) -> str   # sets GDK_BACKEND before GTK imports
def backend_is_x11(value) -> bool
def display_is_x11(display_name) -> bool
def build_plan(session, *, compositor_skip_capture=False,
               xwayland_dock=False) -> ExclusionPlan
def capability_report(session, *, compositor_skip_capture=False,
                      xwayland_dock=False) -> dict
    # {"session", "compositor", "guaranteed", "mechanism", "limitations",
    #  "recommended", "capture_rule": {"backend", "supported", "configured",
    #  "runtime_verified", "setup"}}
def should_hide(plan, share_active, user_hidden) -> bool   # only user_hidden matters

class ScreenShareWatcher:
    def __init__(self, probe=None, interval=2.0)
    def poll(self) -> bool
    def status(self) -> dict   # {"active": bool, "checked": bool, "detail": str}
def default_probe() -> bool
```

`default_probe()` is best-effort. It inspects PipeWire (`pw-dump`) for an active video
capture stream and returns `True`/`False`. When no supported probe is installed it raises
`ProbeUnavailable`; `ScreenShareWatcher.poll()` catches that and reports
`status()["checked"] is False` so the UI can say "probe unavailable" instead of pretending
the screen is clean. `SOCKET_STREAM_SCREEN_SHARE=1|0` forces the probe result for QA and tests.
Detection is heuristic and can miss a share (for example a capture path that does not create a
PipeWire video stream); the app reports detection for status only and does not auto-hide.

### 3.2 `apps/socket_stream_desktop/ui/capture_rules.py` (pure, stdlib only)

```python
PANEL_APP_ID = "socket_stream_overlay"
PANEL_WM_CLASS = PANEL_APP_ID

class RuleStatus:             # dataclass(frozen=True)
    backend: str              # "niri" | "hyprland" | "kde" | "gnome" | "x11" | ...
    supported: bool
    configured: bool
    verified: bool            # config validation passed, not runtime capture proof
    detail: str
    guidance: str
    path: str
    can_install: bool
    can_remove: bool

class CaptureExclusion:
    def __init__(self, session=None, *, environ=None, home=None, runner=None, which=None)
    def status(self) -> RuleStatus
    def install(self) -> RuleStatus   # explicit; writes compositor config
    def remove(self) -> RuleStatus
    def note(self) -> str

# CLI
python3 capture_rules.py status|install|remove [--json]
```

Safety properties: no write without `install()`; `<file>.socket_stream.bak` backup; atomic
write that preserves mode; managed markers (`>>> socket_stream capture exclusion (managed) >>>`)
or the `[socket_stream_capture]` KWin group; restore on validator/reload failure; `status()`
never raises. Supported backends only: niri `config.kdl`, Hyprland `hyprland.conf` /
`hyprland.lua`, KWin 6.7+ `kwinrulesrc`. Stock GNOME, X11 and sway return an unsupported status
with guidance instead of writing anything.

### 3.3 `apps/socket_stream_desktop/ui/floating_overlay.py` (GTK3, lazily imported)

```python
class FloatingOverlay:
    def __init__(self, plan=None, session=None, *, stealth=True, compact=False,
                 opacity=0.94, on_stop=None, on_pause=None, on_copy=None,
                 on_minimize=None)
    def show(self); def hide(self); def toggle(self)
    def is_visible(self) -> bool
    def set_question(self, text); def set_answer(self, text); def append_answer(self, text)
    def set_status(self, text); def set_stealth(self, enabled: bool)
    def apply_share_state(self, active: bool)
    def feed_event(self, event: dict) -> bool     # delegates to LiveState
    def run(self)                                 # Gtk.main()
    def destroy(self)

def main(argv=None) -> int
```

The panel calls `GLib.set_prgname("socket_stream_overlay")` and
`Gdk.set_program_class("socket_stream_overlay")` around realize **and** the first
`show_all()`, and sets `WM_CLASS=socket_stream_overlay`, so an exact-match compositor rule
targets the panel rather than every SocketStream window. GTK 3.24 derives
`xdg_toplevel.app_id` from `g_get_prgname()` when the toplevel is created at map time.
The main window keeps its own identity.

When the GTK display is X11 but the session is Wayland, the constructor upgrades a
`wayland-toplevel` plan to the `xwayland-dock` plan (`_NET_WM_WINDOW_TYPE_DOCK`,
undecorated, centred, never override-redirect) and records it in `xwayland_dock`. The
standalone `main()` calls `ensure_panel_backend()` before importing GTK, so the backend is
chosen once per process.

`main()` CLI:

```
python3 floating_overlay.py --stdin
python3 floating_overlay.py --provider ollama [--interval 4] [--allow-ocr] [--portal]
python3 floating_overlay.py --provider ollama --cli /path/to/socket_stream
                            [--stealth | --no-stealth] [--compact] [--opacity 0.94]
```

`--stdin` consumes JSONL events (schema 1) on stdin. `--provider` spawns
`socket_stream observe-live --authorized ...` itself and reads its stdout. The module must
be importable on machines without GTK so tests can run headless.

### 3.4 `socket_stream overlay`

A thin Rust subcommand that locates `apps/socket_stream_desktop/ui/floating_overlay.py`
(or `$SOCKET_STREAM_OVERLAY`) and execs it, forwarding
`--provider`, `--interval`, `--allow-ocr`, `--portal`, `--stealth`, `--no-stealth`,
`--compact`, `--opacity`, `--cli`. It performs no capture itself.

## 4. Visibility behavior

`ScreenShareWatcher` is polled on a GLib timer (default 2 s) for status reporting only.
Focus changes and detected shares do not change panel visibility. Only clicking the
panel's **Close panel** control or window-close control hides it; show shortcuts can
restore it. Full-screen capture may include the visible panel.

Wayland has no client-controlled always-on-top or skip-taskbar hint; GTK3's
`set_keep_above` and `set_skip_taskbar_hint` are no-ops on the Wayland backend. On GNOME
Wayland SocketStream therefore starts with `GDK_BACKEND=x11` and applies the X11 dock plan,
so the panel stays above normal and fullscreen windows without taking focus. Set
`GDK_BACKEND=wayland` to opt out; the panel then behaves like any Wayland toplevel and can
be covered. On KDE, niri and Hyprland the app stays on Wayland so their capture rules keep
matching. A native X11 session uses the existing override-redirect plan.

The GTK panel requests 94% opacity by default (`--opacity` accepts 0.85–1.0). Verified on
the target: on Wayland the toplevel uses the RGBA visual and GTK paints it with
`cairo_paint_with_alpha`, so the panel is genuinely semi-transparent; on X11 GTK calls
`gdk_window_set_opacity`, which requires a compositing manager. Opacity is a visual
presentation setting, not a capture-exclusion mechanism.

## 4.1 Setting up a compositor capture rule

The main Linux window shows a **Capture exclusion** row with **Set up exclusion**,
**Remove rule**, and **Details**. Setup is always explicit and reversible:

* **KDE Plasma/KWin 6.7+ Wayland** — registers `[socket_stream_capture]` in `[General] rules/count`, preserving other rule order, and writes it to `kwinrulesrc` with
  `wmclass=socket_stream_overlay`, `wmclassmatch=1` (Exact), `excludefromcapture=true`,
  `excludefromcapturerule=2` (Force), then asks KWin to reconfigure.
* **Hyprland 0.53+** — writes a managed `windowrule { ... no_screen_share = true }` block
  to `hyprland.conf` (or `hl.window_rule` to `hyprland.lua`), runs
  `Hyprland --verify-config`, then `hyprctl reload` and checks `hyprctl configerrors`.
* **niri** — writes a managed `window-rule { match app-id=... block-out-from "screen-capture" }`
  block to `config.kdl` and validates with `niri validate`.
* **GNOME / X11 / sway** — no rule exists. The row says so and recommends sharing a single
  application window; no file is modified.

A configured rule is not runtime proof. Verify on a real compositor with a real
whole-screen share; `RuleStatus.verified` means only that the configuration passed its
validator.

## 5. Tests

- `tests/test_overlay_guard.py` — session detection, plan selection per compositor,
  capability honesty, configured-rule reporting, explicit-hide visibility rule, watcher
  with injected probes.
- `tests/test_capture_rules.py` — niri/Hyprland/KDE install/remove, idempotence, backups,
  validation rollback, KDE version gating, unsupported GNOME/X11/sway, identity.
- `tests/test_floating_overlay.py` — import without GTK, CLI argument parsing, opacity,
  panel identity during realize and map, X11 override-redirect fallbacks, GTK3/Gdk
  version pinning, event feeding, visibility across share/focus changes.
- `tests/test_overlay_probe.py` — probe has no auto-hide path; configured/runtime status
  stays separated in its report.
- `security-lab/overlay_probe.py` — a human-run GTK probe for real screen-share QA. It
  never hides; it shows the capture-rule status and the remaining limitations.

## 6. Non-goals (Linux ticket)

- No process masquerading, no permission bypass, no hidden capture.
- No claim of universal invisibility.
- No automatic hiding during screen sharing.
- No macOS/Windows work in this Linux ticket; macOS has its own implementation
  and verification document.

## 7. macOS AppKit panel (separate implementation)

macOS uses a native `NSPanel` in `apps/socket_stream_desktop/ui/mac-app.swift`, not
the GTK script. It renders the same schema-1 JSONL state and exposes the same
controls (Compact, Pause/Resume, Copy, Close, Main, Stop).

| Check | Result |
|---|---|
| Window class / level | `NSPanel`, layer 25 (`.statusBar`), `.fullScreenAuxiliary`, `.canJoinAllSpaces`, `.nonactivatingPanel` |
| Focus behavior | `hidesOnDeactivate = false`; focus changes never hide it |
| Minimize main window | Panel stays visible |
| Fullscreen app | Panel stays above a fullscreen Google Chrome window |
| Opacity | `alphaValue = 0.94` (configurable 0.85–1.0) |
| Capture exclusion | `sharingType = .none` with stealth on; verified absent from `screencapture` and present when stealth is toggled off |
| Real remote share | **Not verified**; test RustDesk/Meet/Zoom before claiming it |
| Permissions | Accessibility and Screen Recording granted to the installed bundle through the normal System Settings panes |
| Ad-hoc signing | No Developer ID; permissions can reset after a rebuild |

The AppKit panel is honest about the same limits: `sharingType` is a documented
hint, and some capture paths may still include the window. It never hides itself
during a share. See `docs/MACOS-TESTING.md` for the full build, permission,
panel-control and capture-exclusion procedure.
