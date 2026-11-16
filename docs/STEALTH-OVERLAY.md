# Floating overlay and screen-capture exclusion

**Ticket:** `STEALTH-OVERLAY-005` (Linux GTK overlay)
**Target:** Linux x86-64 (X11 and Wayland). The native macOS AppKit panel is a
separate implementation documented in section 7 and `docs/MACOS-TESTING.md`;
sections 1–6 and the capability matrix below describe Linux only.
**Status:** visibility, slight opacity, panel identity and compositor-rule setup implemented. Panel identity and opacity are runtime-verified on the target GNOME 50 desktop; niri/Hyprland/KDE rules have automated configuration tests but pending real-share QA. Stock GNOME/X11/sway have no per-window whole-screen exclusion. The opt-in Ubuntu Mutter `50.1-0ubuntu2.5` runtime passes native capture tests, the full-Shell startup probe and eight real-systemd lifecycle cases. The earlier duplicate-GType login failure was fixed and the private runtime is now **active on the Lenovo desktop**. The user confirmed the live sharing result works; automated live received-frame verification remains incomplete. See §2.4 and [the detailed technical record](GNOME-CAPTURE-EXCLUSION.md).

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
| Custom Mutter 50.1 (Wayland) | capture-only actor filter, compositor environment opt-in | Filtered window rendering | Filtered offscreen rendering | **Active** — native tests pass and user confirms live sharing works (§2.4); automated live receiver pixels pending |
| KDE Plasma/KWin 6.7+ (Wayland) | registered `excludefromcapture=true`, `excludefromcapturerule=2` in `kwinrulesrc` | Explicit panel-window capture not covered | Output/region capture filter | Unit-tested; real-share QA pending |
| Hyprland 0.53+ (Wayland) | window rule `no_screen_share = true` | Black rectangle | Black rectangle (position still leaks) | Unit-tested; real-share QA pending |
| niri (Wayland) | window rule `block-out-from "screen-capture"` (`kind="hide"` on releases that have it) | Black rectangle by default | Black rectangle by default; hidden only with `kind="hide"` | Unit-tested; real-share QA pending |
| sway / other wlroots (Wayland) | no documented per-window rule | Best effort | **Included** | No; no rule |
| Any | automatic hiding | **Never** | **Never** | By design |

**Bottom line:** Linux has no cross-compositor public API equivalent to Windows
`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`. macOS exposes
`NSWindow.sharingType = .none`, but its coverage depends on the capture API and OS version.
Stock GNOME Shell/mutter, X11 and sway have no supported per-window rule.
The custom Mutter build adds a capture filter; it is a separate compositor deployment.
On niri, Hyprland and KDE Plasma/KWin 6.7+ the documented mechanism is a **compositor rule the
user installs**; the application writes that rule only after an explicit **Set up exclusion**
action. Until a real share is tested, "configured" must not be reported as "verified".
The panel never auto-hides: it stays visible locally, and on supported compositors the
compositor replaces or omits it in the capture.

Three qualifications on that table:

* **Black rectangle is not exclusion.** Hyprland and pre-`kind="hide"` niri replace the
  panel's pixels but still reveal that a window exists and where it is. Do not call
  that "invisible". niri's own docs add that third-party screenshot tools are not
  blocked.
* **KWin has three thresholds, not one.** 6.6 added the `excludeFromCapture` property
  for output screencasts; 6.7 added the persistent rule and screenshot-plugin coverage;
  *window* streams only respect exclusion from the still-open 6.9 merge request.
  Sharing the whole screen is covered; explicitly selecting the panel as a window
  stream is not yet.
* **`_NET_WM_STATE_SKIP_CAPTURE` is not an EWMH standard atom.** No compositor in
  scope (Mutter, KWin, Hyprland, niri) documents honouring it, so that row is an
  unverified best-effort hint and must not drift into a promise.

**Stacking is a separate problem from capture.** On GNOME Wayland a normal toplevel is
covered by the next focused window, and by any fullscreen window. mutter has no
always-on-top protocol for Wayland clients and does not implement wlr-layer-shell.
SocketStream therefore starts the GTK process with `GDK_BACKEND=x11` on GNOME Wayland
(unless the user already set a backend) and gives the panel the X11 **dock** type. The dock
layer stays above fullscreen windows and mutter does not focus dock windows. KDE, niri,
Hyprland and native X11 sessions keep their existing plans.

### 2.1 What Final Round's Electron `setContentProtection` proves

The earlier inspection of Final Round 3.0.7 found
`setContentProtection(true)` on its overlay. That Electron call requests protection
on macOS/Windows; the call alone does not establish what a particular recorder
receives. **On Linux the call is a silent no-op**: Electron's
`NativeWindowViews::SetContentProtection` body is compiled out unless the platform is
Windows, and `IsContentProtected()` returns `false` with the comment
"Not implemented on Linux". Electron's own API reference documents the method as
macOS/Windows only. Windows additionally applies the change at the **next desktop
composition**, not on return.

Linux coverage therefore requires the compositor-specific mechanisms in the table
above; no application-level flag reaches the GNOME screen-cast path.

### 2.2 Historical panel verification on the target desktop

Measured on **Ubuntu 26.04.1, GNOME Shell 50.1, mutter 18, Wayland session,
GTK 3.24.52**, by running the real `floating_overlay.py` module (not a mock) under
`WAYLAND_DEBUG=1` and inspecting the X11 XWayland window with `xwininfo`/`xprop`.
These 2026-11-15 measurements precede the reading-workspace redesign and retain
its original 94% whole-window opacity and geometry. The current GTK panel uses
90% background-only alpha; see section 4 and [READABLE-ANSWER-UI.md](READABLE-ANSWER-UI.md).

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

### 2.3 What surveyed open-source assistants actually do (bounded)

A 2026-11-15 pass read 11 public repositories at pinned commits plus one shipped
artifact (Interview Coder v1.0.33) and the upstream Electron/`tao` code those
projects rely on. Evidence, hashes, licences and every limitation are in
[`research/linux-screen-exclusion-2026-11-15.md`](research/linux-screen-exclusion-2026-11-15.md).
The survey is **not** an enumeration of the ecosystem.

| Family | Examples read | Linux result |
|---|---|---|
| Electron overlays | Interview Coder, cheating-daddy, Vysper, OpenCluely, invisible-notes | `setContentProtection(true)` is a **silent no-op**; three report nothing, two disclose |
| Tauri overlay | GhostAssist | `tao::set_content_protection` is **unsupported on Linux** (compile-time removed); the app still reports `stealth_active: true` — false success |
| Win32-native | ScreenPrompt | Real `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`; Windows-only, nothing transfers |
| macOS-only | Ghostbar | `sharingType = .none`; no Linux path |
| Honest negative | stream-shield | Detection only, overlay deliberately visible |
| Masquerade | Fuck Leetcode | No exclusion at all; process/window renaming — the opposite of this project's policy |

Bounded findings, stated no more strongly than the evidence:

* **No surveyed OSS project implements GNOME/Mutter whole-screen exclusion.** The
  gap is structural: there is no protocol (Wayland `wayland-protocols` private-window
  issue is open), so only the compositor can remove a window from the stream.
* **A `setContentProtection` call in Linux code is not evidence of protection.**
  Neither is a "stealth" flag in a UI: GhostAssist is the cautionary case.
* **No surveyed mechanism was adopted here.** The panel keeps its stable identity
  because identity is what an exact-match compositor rule or filter matches; runtime
  renaming would break both.
* macOS parity is not a strength to promise: `sharingType` is a legacy-path hint and
  community reports say modern capture APIs can bypass it.

### 2.4 Deployment status of the custom Mutter runtime

The runtime is a **separate compositor deployment**. Precise state after the
installed-app setup and successful login on 2026-11-15:

| Item | State |
|---|---|
| Ubuntu `50.1-0ubuntu2.5` runtime staging | **Published** as immutable `3b4e8e8a98dcb8a3` — schema-2 manifest, copied Shell, exact ELF/GI paths, file hashes and package pins validate |
| Native/headless Mutter tests | **Passed** — build, pixel, animated, snapshot and transactional-patch tests |
| Earlier login failure | Duplicate `ClutterActor`/`ClutterAnimatable` registration reproduced in isolation; private copied-Shell RUNPATH and DSO RPATH resolved it in full GI/JS startup |
| Full-Shell production startup probe | **Passed** — actual Shell READY, 3.021 s liveness, four coherent private mappings, exit 0 and complete cleanup |
| Durable one-shot guard | **8/8 real-systemd lifecycle cases passed**; same-PID consume, identity-bound readiness, clean-exit-only rearm and stock fallback after failure |
| Desktop state | **Private runtime active**, Shell PID 125880, four matching libraries, opt-in and matching `ready` guard; installed app commit `4c4eba9` |
| Actual normal desktop logout | **Stock session measured** as `(success, exited, 0)` before successful private login; custom-desktop logout/rearm not yet exercised |
| Live sharing result | **User-confirmed working**; browser closed before scripted receiver attached, so no new automated encoded-frame comparison was retained |

Consequences for every status surface: `deployed` means only "files and pins
validate". It does not mean the compositor loaded them, and it never means a share
was filtered. `runtime_active` is the field that reflects the live
compositor, and it is now true. `restart_required` is false; `verified` remains
false because activation does not establish received pixels. The status also exposes the startup guard;
a latched failure asks for setup again rather than repeatedly requesting login.
`config_validated` requires compatible, effective configuration, not deployment
alone. Evidence is in
[`native/mutter-capture-filter/VERIFICATION.md`](../native/mutter-capture-filter/VERIFICATION.md).

The intended end state is unchanged and worth stating precisely: the panel keeps
rendering on the local desktop, and **only capture renders** omit it. The patch is a
capture-only predicate; it never hides the window from the user and never alters
local painting. That property is what the native tests check. The live activation
and user report are retained in the
[live evidence index](../native/mutter-capture-filter/evidence/live-desktop-20261115/README.md);
automated received-frame inspection remains outstanding.

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
               xwayland_dock=False, runtime_active=False) -> ExclusionPlan
def capability_report(session, *, compositor_skip_capture=False,
                      xwayland_dock=False, capture_rule_supported=None,
                      runtime_deployed=False, runtime_active=False,
                      restart_required=False) -> dict
    # {"session", "compositor", "guaranteed", "mechanism", "limitations",
    #  "recommended", "capture_rule": {"backend", "supported", "configured",
    #  "deployed", "runtime_active", "restart_required", "runtime_verified", "setup"}}
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
    verified: bool            # always false without separate real-capture evidence
    detail: str
    guidance: str
    path: str
    can_install: bool
    can_remove: bool
    config_validated: bool    # configuration syntax/reload succeeded
    deployed: bool            # custom GNOME runtime passes manifest validation
    runtime_active: bool      # live GNOME opt-in and all four matching mappings
    restart_required: bool    # fresh desktop login needed for activation/removal

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
never raises. Supported configuration backends: niri `config.kdl`, Hyprland
`hyprland.conf` / `hyprland.lua`, KWin 6.7+ `kwinrulesrc`, and a managed GNOME
Shell user-service drop-in when a compatible custom Mutter runtime is deployed.
GNOME activation pins the resolved immutable runtime directory and takes effect
at the next full desktop login. This succeeded on the target desktop (§2.4).
The backend checks the live compositor's own
environment and library mappings; an environment variable in the app is not
activation. See [`../native/mutter-capture-filter/README.md`](../native/mutter-capture-filter/README.md).
Stock GNOME without a compatible runtime, X11 and sway report unsupported;
removal of an existing managed GNOME override remains available.

### 3.3 `apps/socket_stream_desktop/ui/floating_overlay.py` (GTK3, lazily imported)

```python
class FloatingOverlay:
    def __init__(self, plan=None, session=None, *, stealth=True, compact=False,
                 opacity=0.90, on_stop=None, on_pause=None, on_copy=None,
                 on_minimize=None, on_settings=None, state=None,
                 on_selection_changed=None, font_size=16, reduced_motion=False)
    def show(self); def hide(self); def toggle(self)
    def is_visible(self) -> bool
    def set_question(self, text); def set_answer(self, text); def append_answer(self, text)
    def set_status(self, text); def set_stealth(self, enabled: bool)
    def apply_share_state(self, active: bool)
    def feed_event(self, event: dict) -> bool     # delegates to LiveState
    def render_state(self, provider=None)
    def pin_question(self, request_id=None); def toggle_pin(self)
    def follow_latest(self); def step_question(self, offset)
    def apply_appearance(self, *, opacity=None, font_size=None, reduced_motion=None)
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
                            [--stealth | --no-stealth] [--compact] [--opacity 0.90]
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

The current GTK panel uses **90% background opacity** by default (`--opacity`
accepts 0.55–1.0). Scoped RGBA CSS paints a separate command capsule and answer
card; text stays opaque at rest. GTK uses an RGBA visual and a compositing manager
for actual transparency; surfaces fall back to opaque when compositing is absent.
The desktop **Appearance** page controls background alpha, text size and reduced
motion. Map fades take 140 ms; stack/revealer transitions take 160 ms. These are
disabled by reduced motion or GTK's animation preference. Native Wayland compact
resize remains compositor-dependent. Opacity does not establish capture exclusion.

## 4.1 Setting up a compositor capture rule

The main Linux window shows a **Capture exclusion** row with **Set up exclusion**,
**Remove rule**, and **Details**. Setup is always explicit and reversible:

* **KDE Plasma/KWin 6.7+ Wayland** — registers `[socket_stream_capture]` in `[General] rules/count`, preserving other rule order, and writes it to `kwinrulesrc` with
  `wmclass=socket_stream_overlay`, `wmclassmatch=1` (Exact), `excludefromcapture=true`,
  `excludefromcapturerule=2` (Force), then asks KWin to reconfigure. Upstream
  coverage is version-split: 6.6 added the property for output screencasts, 6.7
  added this persistent rule and screenshot coverage, and *window* streams only
  respect exclusion from the still-open 6.9 change. Sharing the whole screen is the
  covered case; selecting the panel as a window stream is not.
* **Hyprland 0.53+** — writes a managed `windowrule { ... no_screen_share = true }` block
  to `hyprland.conf` (or `hl.window_rule` to `hyprland.lua`), runs
  `Hyprland --verify-config`, then `hyprctl reload` and checks `hyprctl configerrors`.
* **niri** — writes a managed `window-rule { match app-id=... block-out-from "screen-capture" }`
  block to `config.kdl` and validates with `niri validate`. Releases with
  `kind="hide"` would hide the surface instead of drawing a rectangle; without it
  the panel's position still leaks, and niri's docs do not block third-party
  screenshot tools.
* **GNOME Wayland with a validated custom runtime** — writes a managed systemd user
  drop-in for the detected GNOME Shell unit (on the tested Ubuntu desktop,
  `org.gnome.Shell@ubuntu.service`) that wraps the guarded runtime launcher, then
  reloads user-service configuration. It does **not** restart the live compositor:
  a full log-out/log-in is required. The full-Shell probe now gates setup after the
  first live attempt exposed a late-GI loader failure; the corrected runtime
  subsequently activated successfully (§2.4).
* **GNOME without a validated runtime, X11, sway** — no rule exists. The row says so
  and recommends sharing a single application window; no file is modified. Removing
  an existing managed GNOME override stays available.

A configured rule is not runtime proof. Verify on a real compositor with a real
whole-screen share. `config_validated` records that the written configuration passed
its own validator; `verified` stays false until a real capture is inspected, and
`runtime_active` is the only field that reflects the live compositor.

## 5. Tests

- `tests/test_overlay_guard.py` — session detection, plan selection per compositor,
  capability honesty, configured-rule reporting, explicit-hide visibility rule, watcher
  with injected probes.
- `tests/test_capture_rules.py` — niri/Hyprland/KDE install/remove, idempotence, backups,
  validation rollback, KDE version gating, GNOME runtime gating, X11/sway unsupported, identity.
- `tests/test_mutter_capture.py`, `tests/test_mutter_runtime.py` — runtime manifest and
  live-compositor status separation; they do not prove a login succeeds.
- `tests/test_floating_overlay.py` — import without GTK, CLI argument parsing, opacity,
  panel identity during realize and map, X11 override-redirect fallbacks, GTK3/Gdk
  version pinning, event feeding, visibility across share/focus changes.
- `tests/gtk_workspace_checks.py` — real GTK rendering, pin/history, settings,
  lifecycle and pointer/keyboard checks; see [READABLE-ANSWER-UI.md](READABLE-ANSWER-UI.md).
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
