# Accessibility tree reading, region OCR, global shortcuts and native Codex device flow

**Tickets:** `A11Y-TREE-006`, `OCR-REGION-007`, `HOTKEYS-008`, `CODEX-DEVICE-009`
**Target:** Linux x86-64 (X11 + Wayland) primary; macOS kept working. Windows out of scope.
**Status:** original interfaces implemented; section 3 extended for the Linux
reading workspace (2026-11-16). Platform verification is tracked in
[ACCEPTANCE.md](ACCEPTANCE.md).

This document is the single contract every implementation agent builds against. Do not
change a signature here without changing every consumer in the same commit.

---

## 1. Accessibility tree reading (efficient, structured, configurable)

### 1.1 Why

Today `scripts/linux-accessibility.py` and `scripts/macos-accessibility.swift` flatten the
focused application into a plain text blob. That wastes tokens (repeated labels, decorative
chrome), loses structure (roles, focus, bounds) and cannot tell us where an image or canvas
node is, so OCR can never be targeted.

### 1.2 Helper output contract (helper -> Rust)

Both helpers accept `--mode tree|text` (default `tree`) and `--max-nodes N`, `--max-depth N`,
`--max-chars N`. In `tree` mode each helper prints **one JSON object** on stdout:

```json
{
  "schema": 1,
  "source": "atspi",
  "app": "Firefox",
  "pid": 4321,
  "truncated": false,
  "node_count": 42,
  "nodes": [
    {
      "id": 3,
      "parent": 1,
      "role": "paragraph",
      "name": "",
      "text": "Find two numbers that sum to a target",
      "focused": true,
      "bounds": [120, 240, 700, 32],
      "image": false,
      "protected": false
    }
  ]
}
```

Rules:

- `schema` is `1`. `source` is `atspi` (Linux) or `ax` (macOS).
- `nodes` is a flat, pre-order list. `parent` is the `id` of the parent node or `null` for the
  application root. `id` is a stable integer within one response only.
- `text` is the node's accessible text (may be empty). `name` is the accessible name/label.
- `focused` marks the focused node. At most the focused node and its ancestors are `true`.
- `bounds` is `[x, y, width, height]` in screen pixels, or `null` when unavailable.
- `image` is `true` for image, canvas, graphic, icon, chart or drawing roles.
- `protected` is `true` for password/secure fields. Protected nodes MUST NOT carry `text`.
- Output is bounded by `--max-nodes`, `--max-depth`, `--max-chars`; `truncated` is `true` when
  any bound was hit.
- Exit codes are unchanged: `0` ok, `2` backend unavailable, `3` read failure, `4` own window.

`--mode text` keeps the existing plain-text behaviour byte-for-byte so nothing that already
depends on it breaks.

### 1.3 Rust contract — `crates/socket_stream_core/src/a11y.rs` (new)

```rust
pub const A11Y_SCHEMA: u32 = 1;

#[derive(Debug, Clone, PartialEq, Eq, serde::Deserialize)]
pub struct A11yNode {
    pub id: u64,
    pub parent: Option<u64>,
    pub role: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub focused: bool,
    #[serde(default)]
    pub bounds: Option<[i64; 4]>,
    #[serde(default)]
    pub image: bool,
    #[serde(default)]
    pub protected: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Deserialize)]
pub struct A11yTree {
    pub schema: u32,
    pub source: String,
    #[serde(default)]
    pub app: String,
    #[serde(default)]
    pub pid: Option<u32>,
    #[serde(default)]
    pub truncated: bool,
    #[serde(default)]
    pub nodes: Vec<A11yNode>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadMode {
    /// Focused subtree only; cheapest.
    Focused,
    /// Whole tree as an indented outline. The default: a browser's focused
    /// subtree is the address bar or toolbar, so `Focused`/`Auto` never reach
    /// the page content where a question lives.
    Tree,
    /// Flat text of text-bearing nodes (legacy behaviour).
    Text,
    /// Focused subtree if one exists, otherwise the whole tree.
    Auto,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TokenBudget {
    pub max_tokens: usize,
    pub max_nodes: usize,
    pub max_depth: usize,
    pub max_chars: usize,
}

impl Default for TokenBudget { /* max_tokens 4000, max_nodes 800, max_depth 20, max_chars 32_000 */ }

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct A11yReading {
    /// Token-budgeted rendering handed to the model.
    pub text: String,
    /// Image/graphic nodes worth OCR, largest first, bounded by the budget.
    pub image_regions: Vec<ImageRegion>,
    /// The role of the focused node, when known.
    pub focused_role: Option<String>,
    pub truncated: bool,
    pub source: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ImageRegion {
    pub x: i64,
    pub y: i64,
    pub width: i64,
    pub height: i64,
}

impl ImageRegion {
    pub fn area(&self) -> i64;
    pub fn is_worth_ocr(&self, min_pixels: i64) -> bool;
}

impl A11yTree {
    /// Parse the helper's JSON. Returns Err on schema mismatch or malformed JSON.
    pub fn parse(json: &str) -> Result<Self, A11yError>;
    /// Render under a budget. Pure, deterministic, no I/O.
    pub fn render(&self, mode: ReadMode, budget: TokenBudget) -> A11yReading;
    /// Image/graphic nodes worth OCR, largest first.
    pub fn image_regions(&self, budget: TokenBudget) -> Vec<ImageRegion>;
    /// Flat text of text-bearing nodes (legacy path).
    pub fn flat_text(&self, budget: TokenBudget) -> String;
}

pub fn estimate_tokens(text: &str) -> usize; // ceil(chars / 4)
```

Rendering rules (must be unit tested):

- `Focused`: include the focused node and its ancestors, plus the focused node's direct
  children one level deep. If nothing is focused, fall back to `Tree`.
- `Tree`: indented outline, two spaces per depth level, `role` then `text` (or `name` when
  `text` is empty), skipping purely structural roles (`panel`, `filler`, `separator`,
  `scroll pane`, `unknown`) unless they carry text.
- `Text`: `flat_text` — the legacy newline-joined deduplicated text.
- Protected nodes are always skipped, and their `text` is never rendered.
- Stop adding nodes once `max_tokens`, `max_nodes`, `max_chars` or `max_depth` is reached and
  set `truncated = true`.
- Duplicate identical `(role, text)` pairs are emitted at most once (cheap dedup, bounded set).

### 1.4 Config — `crates/socket_stream_core/src/config.rs` (new)

A JSON config file, no new dependencies (`serde_json` already present).

- Path resolution: `$SOCKET_STREAM_CONFIG`, else `$XDG_CONFIG_HOME/socket_stream/config.json`,
  else `~/.config/socket_stream/config.json`, else `~/.socket_stream.json`.
- A missing file yields `Config::default()`. A malformed file yields
  `Err(ConfigError::Malformed)` and callers MUST report it, never silently ignore it.

```rust
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct Config {
    pub a11y: A11yConfig,
    pub ocr: OcrConfig,
    pub shortcuts: ShortcutsConfig,
    pub overlay: OverlayConfig,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct A11yConfig {
    /// "auto" | "focused" | "tree" | "text"
    pub mode: String,
    pub max_tokens: usize,
    pub max_nodes: usize,
    pub max_depth: usize,
    pub max_chars: usize,
    /// OCR image/graphic nodes found in the tree.
    pub ocr_images: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct OcrConfig {
    pub enabled: bool,
    pub max_regions: usize,
    /// Skip regions smaller than this many pixels.
    pub min_region_pixels: i64,
    pub tesseract_psm: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct ShortcutsConfig {
    pub enabled: bool,
    /// action name -> accelerator string, e.g. "start": "ctrl+alt+s"
    pub bindings: std::collections::BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct OverlayConfig {
    pub stealth: bool,
    pub compact: bool,
}

impl Config {
    pub fn default_path() -> std::path::PathBuf;
    pub fn load() -> Result<Self, ConfigError>;
    pub fn load_from(path: &std::path::Path) -> Result<Self, ConfigError>;
    pub fn save(&self) -> Result<(), ConfigError>;
    pub fn to_json_pretty(&self) -> String;
    pub fn a11y_mode(&self) -> ReadMode; // maps the string, unknown -> Auto
    pub fn budget(&self) -> TokenBudget;
}
```

`Config::default()` uses `mode = "tree"`, `ocr.enabled = false` (explicit opt-in, matching the
existing OCR policy), `shortcuts.enabled = true` and the default bindings from section 3.

### 1.5 Region OCR — `crates/socket_stream_core/src/ocr.rs` (extend)

```rust
/// OCR a sub-rectangle of a screenshot. Crops with the platform tool, then runs
/// the existing extract_text. Never uploads pixels.
pub fn extract_region_text(
    screenshot: &Path,
    region: ImageRegion,
    psm: &str,
) -> Result<String, OcrError>;

/// OCR several regions from one screenshot, bounded and deduplicated.
pub fn extract_regions_text(
    screenshot: &Path,
    regions: &[ImageRegion],
    max_regions: usize,
    min_pixels: i64,
    psm: &str,
) -> Result<String, OcrError>;
```

- Cropping uses `sips`/`magick`/`convert` on macOS and `convert`/`magick` on Linux, whichever
  is present; if none is present return `OcrError::Process("no image crop tool")` — do not
  silently OCR the whole screen instead.
- `extract_regions_text` skips regions below `min_pixels`, caps at `max_regions`, and joins
  the results with `\n`. It returns an empty string (not an error) when nothing qualifies.
- Results are cached by `(screenshot path, region)` in a small bounded map so repeated
  observations of the same frame do not re-run Tesseract.

### 1.6 Capture region helper — `crates/socket_stream_core/src/capture.rs` (extend)

```rust
/// Crop `source` to `region` and write it to `destination`.
pub fn crop_to(source: &Path, region: ImageRegion, destination: &Path) -> Result<(), CaptureError>;
```

Same tool detection as `extract_region_text`; returns `CaptureError::Unsupported` when no crop
tool exists.

---

## 2. Observation wiring (`apps/socket_stream/src/live_observe.rs`)

- New flags: `--a11y-mode auto|focused|tree|text`, `--max-tokens N`, `--ocr-images`,
  `--no-ocr-images`, `--config PATH`. Flags override the config file; the config file overrides
  the defaults.
- Accessibility first, unchanged: AT-SPI/AX is read with `--mode tree`, parsed into an
  `A11yTree`, and rendered under the budget. The rendered text is what reaches the model.
- OCR policy:
  1. If the accessible text is sufficient (`!needs_ocr`) use it and stop. Zero capture.
  2. Else if `ocr_images` is on and the tree has image/graphic regions, capture **one** frame
     and OCR only those regions (`extract_regions_text`). This is the resource-efficient path.
  3. Else fall back to the existing whole-frame OCR path, still gated on `--allow-ocr`.
- The event `source` badge stays one of `accessibility`, `ocr`, `microphone`, `playback`.
  Region OCR reports `ocr`.
- Never capture when the accessibility tree is sufficient. Never OCR when the user has not
  allowed it.

---

## 3. Global shortcuts (`HOTKEYS-008`)

### 3.1 Actions (stable names, used in config and CLI)

`start`, `stop`, `pause_resume`, `toggle_overlay`, `show_overlay`, `hide_overlay`,
`copy_answer`, `compact_toggle`, `toggle_stealth`, `open_settings`, `refresh_question`, `quit`,
`listen_audio`, `pin_question`, `previous_question`, `next_question`, `follow_latest`,
`scroll_answer_up`, `scroll_answer_down`.

The shared Rust/Python contract has nineteen actions; macOS Swift implements
the original twelve. The seven Q&A/scroll actions and arrow-key bindings are Linux-only.

`hide_overlay` is the "hide the app from screen" action. In the Linux GTK shell,
the shortcut is intentionally show-only/reminder behavior: closing requires
clicking the panel's Close control.
Focus and screen-share state never automatically hide or restore it.

### 3.2 Rust contract — `crates/socket_stream_core/src/shortcuts.rs` (new)

```rust
pub const ACTIONS: [&str; 19] = [ ... ]; // the names above, in order

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Accelerator {
    pub ctrl: bool,
    pub alt: bool,
    pub shift: bool,
    pub meta: bool,
    pub key: String, // lowercase, e.g. "s", "f1", "space"
}

impl Accelerator {
    pub fn parse(value: &str) -> Result<Self, ShortcutError>;
    pub fn display(&self) -> String; // "Ctrl+Alt+S"
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    pub action: String,
    pub accelerator: Accelerator,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShortcutMap {
    pub bindings: Vec<Binding>,
}

impl ShortcutMap {
    pub fn from_config(config: &ShortcutsConfig) -> Result<Self, ShortcutError>;
    pub fn defaults() -> Self;
    pub fn action_for(&self, accel: &Accelerator) -> Option<&str>;
    pub fn accelerator_for(&self, action: &str) -> Option<&Accelerator>;
    /// Reject duplicate accelerators and unknown actions.
    pub fn validate(&self) -> Result<(), ShortcutError>;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShortcutPlatform { X11, Wayland, MacOs, Unknown }

pub fn detect_platform() -> ShortcutPlatform;
/// Human instructions for the current platform, one line per binding.
pub fn platform_notes(platform: ShortcutPlatform) -> Vec<String>;
```

Default bindings (also written to a fresh config file):

| Action | Default |
|---|---|
| start | `ctrl+alt+s` |
| stop | `ctrl+alt+x` |
| pause_resume | `ctrl+alt+p` |
| toggle_overlay | `ctrl+alt+o` |
| show_overlay | `ctrl+alt+shift+o` |
| hide_overlay | `ctrl+alt+h` |
| copy_answer | `ctrl+alt+c` |
| compact_toggle | `ctrl+alt+m` |
| toggle_stealth | `ctrl+alt+k` |
| open_settings | `ctrl+alt+comma` |
| refresh_question | `ctrl+alt+r` |
| quit | `ctrl+alt+q` |
| listen_audio | `ctrl+alt+a` |
| pin_question | `ctrl+alt+b` |
| previous_question | `ctrl+alt+left` |
| next_question | `ctrl+alt+right` |
| follow_latest | `ctrl+alt+l` |
| scroll_answer_up | `ctrl+alt+up` |
| scroll_answer_down | `ctrl+alt+down` |

### 3.3 CLI — `apps/socket_stream/src/hotkeys.rs` (new)

`socket_stream hotkeys` subcommands:

- `hotkeys list` — print the resolved bindings and the platform.
- `hotkeys init [--force]` — write default shortcuts, refusing an existing file
  without `--force`. Force resets only `shortcuts.enabled` and `shortcuts.bindings`,
  preserving other settings and unknown siblings. Writes share a POSIX lock and
  atomic replace with the GTK editor; malformed/non-object files are refused.
- `hotkeys print` — print the platform-native registration snippet (an `sxhkdrc` block for X11,
  a note about `org.freedesktop.portal.GlobalShortcuts` for Wayland, and the in-app equivalents
  for macOS). It never installs anything by itself.

### 3.4 Desktop integration

- `apps/socket_stream_desktop/ui/shortcuts.py` (new, pure stdlib): parse accelerators, hold the
  action -> callback map, and dispatch a key event. GTK-agnostic so it is unit testable.
- `linux-gtk.py`: register nineteen GTK accelerators (`Gtk.AccelGroup`) on the
  control window and answer panel and provide a Shortcuts page with recording, validation,
  enable/disable, Save and Reset. Save applies immediately; Reset edits the form
  until saved. `open_settings` opens Appearance. `hide_overlay` retains its
  reminder-only behavior; the panel's Close control calls `hide()` explicitly.
  Global registration needs the desktop's own tool; the app installs no grab.
- `mac-app.swift`: register `NSMenu` key equivalents for its twelve actions plus
  a `NSEvent.addGlobalMonitorForEvents` path and a Shortcuts menu.
- Every shortcut is also reachable from the UI without a keyboard, and every shortcut is listed
  in `docs/SHORTCUTS.md`.

---

## 4. Native Codex/ChatGPT device flow (`CODEX-DEVICE-009`)

The existing `codex_auth.rs` drives the official Codex CLI's app-server. That requires the CLI
to be installed. This ticket adds a **native device-authorization flow** so sign-in works
without it, modelled on the reference implementation at
`/Users/mymac/Projects/opencode-rk-recovery-20261005/crates/providers/src/openai_device_auth.rs`.

### 4.1 `crates/socket_stream_core/src/openai_device_auth.rs` (new)

```rust
pub const CLIENT_ID: &str = "app_EMoamEEZ73f0CkXaXp7hrann";
pub const ISSUER: &str = "https://auth.openai.com";
pub const DEVICE_LOGIN_URL: &str = "https://auth.openai.com/codex/device";
pub const MAX_USER_CODE_BYTES: usize = 256;
pub const MAX_TOKEN_BYTES: usize = 64 * 1024;
pub const MAX_RESPONSE_BYTES: usize = 256 * 1024;
pub const MAX_POLLS: usize = 240;
pub const REQUEST_TIMEOUT: std::time::Duration; // 20s
pub const POLLING_SAFETY_MARGIN: std::time::Duration; // 3s

#[derive(Debug, thiserror::Error)]
pub enum DeviceAuthError {
    #[error("device authorization request failed")] Request,
    #[error("device authorization was denied or expired")] Denied,
    #[error("device authorization timed out")] Timeout,
    #[error("token response was malformed or too large")] Malformed,
    #[error("authentication is unavailable in this build")] Unavailable,
}

/// No Debug: the user code must not enter diagnostics.
pub struct DeviceAuthorization { pub user_code: String, pub verification_url: String,
                                 pub device_code: String, pub interval_seconds: u64,
                                 pub expires_in_seconds: u64 }

/// No Debug, no Serialize of the raw token: credentials must not be logged.
pub struct OpenAiCredential { pub access_token: String, pub refresh_token: String,
                              pub expires_at_ms: u64, pub account_id: Option<String> }

impl OpenAiCredential { pub fn is_expired(&self, now_ms: u64) -> bool; }

pub async fn begin_device_authorization(client: &reqwest::Client)
    -> Result<DeviceAuthorization, DeviceAuthError>;
pub async fn poll_device_authorization(client: &reqwest::Client, auth: &DeviceAuthorization)
    -> Result<OpenAiCredential, DeviceAuthError>;
pub async fn refresh_credential(client: &reqwest::Client, refresh_token: &str)
    -> Result<OpenAiCredential, DeviceAuthError>;
```

Rules:

- Redirects disabled, 20 s timeout, response body capped at `MAX_RESPONSE_BYTES`, polling capped
  at `MAX_POLLS` with `POLLING_SAFETY_MARGIN` added to the server interval.
- Only HTTPS `auth.openai.com` endpoints. No configurable production endpoint.
- `Debug` for both structs must redact the code and tokens.
- A `for_local_test` endpoint override is allowed **only** under `#[cfg(test)]` or behind an
  explicit `--device-auth-test-url` used exclusively by tests against a local mock server.

### 4.2 Credential store — `crates/socket_stream_core/src/auth_store.rs` (new)

- Stores the credential as JSON at `$XDG_CONFIG_HOME/socket_stream/credentials.json` (macOS:
  `~/Library/Application Support/socket_stream/`), created with mode `0600` on Unix.
- Never reads the official Codex CLI's files. Never logs a token. `Debug` redacts.
- `load()`, `save(credential)`, `clear()`.

### 4.3 CLI — extend `apps/socket_stream/src/auth_commands.rs`

- `codex-login --method device|browser [--native]` — `--native` uses the device flow above
  instead of the Codex CLI. Prints the verification URL and user code, polls, stores the
  credential, and prints a redacted confirmation.
- `codex-status --native` — reports signed-in/expired from the local store without the CLI.
- `codex-logout --native --confirm` — clears the local store only.
- Existing CLI-backed behaviour is unchanged when `--native` is absent.

### 4.4 Provider use

When a native credential exists and is not expired, `Provider::Codex` may use it for inference
against the documented Codex backend endpoint. If it is expired, refresh once; if refresh
fails, report an actionable error and fall back to the CLI path only if the user selected it.
Never claim subscription quota is unlimited API quota.

---

## 5. Honesty rules (apply to every ticket here)

- No process masquerading, no permission bypass, no hidden capture.
- No claim of universal invisibility from screen capture.
- OCR is opt-in; accessibility is always tried first.
- No tokens, user codes, raw audio, screenshots or private question text in logs or diagnostics.
- Anything not verified on real hardware must be labelled "implemented, E2E pending" in docs.

## 6. Tests

- Rust: `a11y.rs`, `config.rs`, `ocr.rs` (region helpers), `capture.rs` (crop), `shortcuts.rs`,
  `openai_device_auth.rs` (against a local mock server), `auth_store.rs`.
- Python: `tests/test_a11y_helpers.py` (helper JSON contract, fixture driven),
  `tests/test_shortcuts.py` (accelerator parsing, dispatch, defaults).
- Existing suites must keep passing: `cargo test --workspace`,
  `python3 -m unittest discover -s tests -q`,
  `python3 -m unittest discover -s tests/acceptance -p 'test_*.py'`.
