# Linux floating overlay and screen-capture exclusion

**Ticket:** `STEALTH-OVERLAY-005`
**Target:** Linux x86-64 (X11 and Wayland). macOS/Windows are out of scope for this feature.
**Status:** interface frozen; implementation in progress.

## 1. What this feature is

A small, always-on-top **floating answer panel** that renders the live question and the
model answer produced by `context-cli observe-live` / `audio-live`. It is a *normal,
identifiable* process with a normal window; it does not disguise itself, does not
masquerade as a system component, and does not bypass any OS permission prompt.

On top of that, it applies **every capture-exclusion mechanism the Linux platform
actually offers**, and honestly reports which ones are available and which are not.

## 2. Honest capability matrix (this is the important part)

| Session | Mechanism | Excluded from portal/browser "share a window"? | Excluded from "share entire screen"? | Guaranteed? |
|---|---|---|---|---|
| X11 (any WM) | override-redirect + `_NET_WM_STATE_SKIP_TASKBAR/PAGER` + `_NET_WM_WINDOW_TYPE_NOTIFICATION` | Best effort: window is usually not listed by pickers | **No** | No |
| X11 + compositor exposing `_NET_WM_STATE_SKIP_CAPTURE` | above + `SKIP_CAPTURE` | Best effort | Best effort, compositor-dependent | No |
| Wayland GNOME/KDE/wlroots | `wlr-layer-shell` overlay layer, no keyboard interactivity | No (portal lists toplevels) | **No** | No |
| Wayland + niri (user-configured `block-out-from=screencast`) | compositor rule | Yes | Yes | Only after the user adds the rule |
| Any | behavioural auto-hide while a ScreenCast session is detected | Yes (the panel is not on screen during the capture) | Yes | Yes, while detection works |

**Bottom line:** on Linux there is no public API equivalent to Windows
`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` or macOS `NSWindow.sharingType = .none`.
Any claim of universal invisibility would be false. The overlay therefore combines
best-effort window hints with a **behavioural guard**: it detects an active desktop
ScreenCast/screen-share session and hides itself for the duration, then restores.

## 3. Frozen module interfaces

### 3.1 `apps/context-desktop/ui/overlay_guard.py` (pure, stdlib only, no GTK)

```python
SESSION_X11 = "x11"
SESSION_WAYLAND = "wayland"
SESSION_UNKNOWN = "unknown"

class SessionInfo:            # dataclass(frozen=True)
    session_type: str         # SESSION_X11 | SESSION_WAYLAND | SESSION_UNKNOWN
    desktop: str              # lowercased XDG_CURRENT_DESKTOP
    compositor: str           # "gnome" | "kde" | "sway" | "niri" | "x11" | "unknown"
    has_x11_display: bool
    has_wayland_display: bool

def detect_session(environ=None) -> SessionInfo

class ExclusionPlan:          # dataclass(frozen=True)
    strategy: str             # "x11-override-redirect" | "wayland-layer-shell" | "none"
    guaranteed: bool
    override_redirect: bool
    keep_above: bool
    skip_taskbar: bool
    window_type: str          # e.g. "_NET_WM_WINDOW_TYPE_NOTIFICATION" or ""
    atoms: dict               # {"_NET_WM_STATE": ["_NET_WM_STATE_ABOVE", ...]}
    layer: str                # "" | "overlay" | "top"
    notes: tuple              # tuple[str, ...]
    def as_dict(self) -> dict

def build_plan(session, *, compositor_skip_capture=False) -> ExclusionPlan
def capability_report(session) -> dict
    # {"session": str, "compositor": str, "guaranteed": bool, "mechanism": str,
    #  "limitations": list[str], "recommended": str}
def should_hide(plan, share_active, user_hidden) -> bool

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
the screen is clean. `CONTEXT_LENS_SCREEN_SHARE=1|0` forces the probe result for QA and tests.
Detection is heuristic and can miss a share (for example a capture path that does not create a
PipeWire video stream), which is why the guard is described as best effort.

### 3.2 `apps/context-desktop/ui/floating_overlay.py` (GTK3, lazily imported)

```python
class FloatingOverlay:
    def __init__(self, plan=None, session=None, *, stealth=True,
                 on_stop=None, on_pause=None, on_copy=None)
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

`main()` CLI:

```
python3 floating_overlay.py --stdin
python3 floating_overlay.py --provider ollama [--interval 4] [--allow-ocr] [--portal]
python3 floating_overlay.py --provider ollama --cli /path/to/context-cli
                            [--stealth | --no-stealth] [--compact]
```

`--stdin` consumes JSONL events (schema 1) on stdin. `--provider` spawns
`context-cli observe-live --authorized ...` itself and reads its stdout. The module must
be importable on machines without GTK so tests can run headless.

### 3.3 `context-cli overlay`

A thin Rust subcommand that locates `apps/context-desktop/ui/floating_overlay.py`
(or `$CONTEXT_LENS_OVERLAY`) and execs it, forwarding
`--provider`, `--interval`, `--allow-ocr`, `--portal`, `--stealth`, `--no-stealth`,
`--compact`, `--cli`. It performs no capture itself.

## 4. Behavioural guard

`ScreenShareWatcher` is polled on a GLib timer (default 2 s). When a share becomes
active and stealth is enabled, the overlay hides; when it ends, the overlay returns to
its previous visibility. A manual "hide" by the user is remembered and never overridden.
The guard is the only mechanism that can honestly claim to keep the panel out of a
full-screen capture, and it is documented as detection-based, not protocol-based.
`--stealth` is on by default; `--no-stealth` disables both the window hints and the guard.

## 5. Tests

- `tests/test_overlay_guard.py` — session detection, plan selection per compositor,
  capability honesty, `should_hide` truth table, watcher with injected probes.
- `tests/test_floating_overlay.py` — import without GTK, CLI argument parsing,
  event feeding delegating to `LiveState`.
- `security-lab/overlay_probe.py` — a human-run GTK probe for real screen-share QA.

## 6. Non-goals

- No process masquerading, no permission bypass, no hidden capture.
- No claim of universal invisibility.
- No macOS/Windows work in this ticket.
