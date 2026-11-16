//! Shared application configuration and shortcut bindings.
//!
//! Missing files use defaults; malformed configuration is reported and never
//! silently replaced. Reads and writes:
//!
//! * open with `O_NOFOLLOW`, `O_NONBLOCK` and `O_CLOEXEC` and require a regular
//!   file via `fstat`, so a symlinked (including dangling) or non-regular
//!   `config.json` is refused instead of followed or blocked on;
//! * take an exclusive `flock` on `<config>.lock`, the same convention the
//!   Python desktop shell uses, around the whole read/merge/atomic-replace;
//! * replace the file with a `NamedTempFile` in the same directory (mode 0600),
//!   fsync it, rename, then fsync the directory when the platform allows.
//!
//! File access and locking are Unix-only (Linux/macOS, the app's targets);
//! other platforms receive a clear [`ConfigError::Unsupported`].
use crate::a11y::{ReadMode, TokenBudget};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

#[cfg(unix)]
use std::ffi::CString;
#[cfg(unix)]
use std::os::unix::ffi::OsStrExt;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
#[cfg(unix)]
use std::os::unix::io::{AsRawFd, FromRawFd};

/// Default accelerator strings, mirroring docs/A11Y-TREE-SHORTCUTS.md section 3.2.
/// Kept local so configuration does not depend on the shortcut runtime module.
pub const DEFAULT_BINDINGS: [(&str, &str); 19] = [
    ("start", "ctrl+alt+s"),
    ("stop", "ctrl+alt+x"),
    ("pause_resume", "ctrl+alt+p"),
    ("toggle_overlay", "ctrl+alt+o"),
    ("show_overlay", "ctrl+alt+shift+o"),
    ("hide_overlay", "ctrl+alt+h"),
    ("copy_answer", "ctrl+alt+c"),
    ("compact_toggle", "ctrl+alt+m"),
    ("toggle_stealth", "ctrl+alt+k"),
    ("open_settings", "ctrl+alt+comma"),
    ("refresh_question", "ctrl+alt+r"),
    ("quit", "ctrl+alt+q"),
    ("listen_audio", "ctrl+alt+a"),
    ("pin_question", "ctrl+alt+b"),
    ("previous_question", "ctrl+alt+left"),
    ("next_question", "ctrl+alt+right"),
    ("follow_latest", "ctrl+alt+l"),
    ("scroll_answer_up", "ctrl+alt+up"),
    ("scroll_answer_down", "ctrl+alt+down"),
];

/// Owner-only mode for the config and its lock file.
const CONFIG_MODE: u32 = 0o600;

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("configuration I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("malformed configuration JSON: {0}")]
    Malformed(#[from] serde_json::Error),
    #[error("invalid configuration: {0}")]
    Invalid(String),
    #[error(
        "{0} already exists; re-run with --force to reset shortcuts only \
         (preserving other settings) or remove it manually"
    )]
    Exists(PathBuf),
    #[error("unsupported platform: {0}")]
    Unsupported(String),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct Config {
    pub a11y: A11yConfig,
    pub ocr: OcrConfig,
    pub shortcuts: ShortcutsConfig,
    pub overlay: OverlayConfig,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
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
impl Default for A11yConfig {
    fn default() -> Self {
        Self {
            // "tree" reads the whole foreground window outline. The previous
            // "auto" default rendered only the focused subtree (plus one level
            // of children), which for a browser is the address bar or toolbar
            // and never reaches the page content where the question lives.
            mode: "tree".into(),
            max_tokens: 4000,
            max_nodes: 800,
            max_depth: 20,
            max_chars: 32_000,
            ocr_images: false,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct OcrConfig {
    pub enabled: bool,
    pub max_regions: usize,
    /// Skip regions smaller than this many pixels.
    pub min_region_pixels: i64,
    pub tesseract_psm: String,
}
impl Default for OcrConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            max_regions: 3,
            min_region_pixels: 256,
            tesseract_psm: "6".into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ShortcutsConfig {
    pub enabled: bool,
    /// action name -> accelerator string, e.g. "start": "ctrl+alt+s"
    pub bindings: BTreeMap<String, String>,
}
impl Default for ShortcutsConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            bindings: DEFAULT_BINDINGS
                .iter()
                .map(|(action, accel)| ((*action).into(), (*accel).into()))
                .collect(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct OverlayConfig {
    pub stealth: bool,
    pub compact: bool,
}
impl Default for OverlayConfig {
    fn default() -> Self {
        Self {
            stealth: true,
            compact: false,
        }
    }
}

/// Path of the flock companion file shared with `shortcuts.py`.
fn lock_path_for(path: &Path) -> PathBuf {
    let mut name = path
        .file_name()
        .map(|name| name.to_os_string())
        .unwrap_or_else(|| std::ffi::OsString::from("config.json"));
    name.push(".lock");
    path.parent()
        .map(|parent| parent.join(&name))
        .unwrap_or_else(|| PathBuf::from(name))
}

/// Map a failed `open(2)` to either a refusal or a plain I/O error.
#[cfg(unix)]
fn classify_open_error(path: &Path, error: std::io::Error) -> ConfigError {
    match error.raw_os_error() {
        Some(code) if code == libc::ELOOP => {
            ConfigError::Invalid(format!("refusing to follow symlink: {}", path.display()))
        }
        Some(code) if code == libc::ENOENT => match fs::symlink_metadata(path) {
            // With O_NOFOLLOW a dangling link reports ENOENT on some systems;
            // the link is what must be refused, not treated as "missing".
            Ok(metadata) if metadata.file_type().is_symlink() => ConfigError::Invalid(format!(
                "refusing to follow dangling symlink: {}",
                path.display()
            )),
            _ => ConfigError::Io(error),
        },
        _ => ConfigError::Io(error),
    }
}

/// Open without following links, without blocking on FIFOs, and require a
/// regular file. The regular-file check happens after `open` (via `fstat`) and
/// not before, so a swap between a check and the open cannot be followed.
#[cfg(unix)]
fn open_nofollow(path: &Path, access: libc::c_int, create: bool) -> Result<fs::File, ConfigError> {
    let c_path = CString::new(path.as_os_str().as_bytes()).map_err(|_| {
        ConfigError::Invalid(format!("path contains a NUL byte: {}", path.display()))
    })?;
    let mut flags = access | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC;
    if create {
        flags |= libc::O_CREAT;
    }
    // SAFETY: `c_path` is a NUL-terminated C string that outlives the call.
    let fd = unsafe { libc::open(c_path.as_ptr(), flags, CONFIG_MODE as libc::c_uint) };
    if fd < 0 {
        let error = std::io::Error::last_os_error();
        return Err(classify_open_error(path, error));
    }
    // SAFETY: `open` returned a fresh descriptor that nobody else owns.
    let file = unsafe { fs::File::from_raw_fd(fd) };
    let metadata = file.metadata()?;
    if !metadata.is_file() {
        return Err(ConfigError::Invalid(format!(
            "refusing non-regular file: {}",
            path.display()
        )));
    }
    Ok(file)
}

#[cfg(unix)]
fn open_read(path: &Path) -> Result<fs::File, ConfigError> {
    open_nofollow(path, libc::O_RDONLY, false)
}

#[cfg(unix)]
fn open_lock_file(path: &Path) -> Result<fs::File, ConfigError> {
    open_nofollow(path, libc::O_RDWR, true)
}

#[cfg(not(unix))]
fn open_read(_path: &Path) -> Result<fs::File, ConfigError> {
    Err(ConfigError::Unsupported(
        "configuration reads require Unix (Linux/macOS)".to_owned(),
    ))
}

#[cfg(not(unix))]
fn open_lock_file(_path: &Path) -> Result<fs::File, ConfigError> {
    Err(ConfigError::Unsupported(
        "configuration locking requires Unix (Linux/macOS)".to_owned(),
    ))
}

/// Refuse a symlink or non-regular existing path before replacing it.
fn refuse_unsafe_target(path: &Path) -> Result<(), ConfigError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_symlink() => Err(ConfigError::Invalid(format!(
            "refusing to write through symlink: {}",
            path.display()
        ))),
        Ok(metadata) if !metadata.is_file() => Err(ConfigError::Invalid(format!(
            "refusing to replace non-regular file: {}",
            path.display()
        ))),
        Ok(_) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(ConfigError::Io(error)),
    }
}

/// Read the config text; `Ok(None)` only when the path itself is missing.
fn read_config_text(path: &Path) -> Result<Option<String>, ConfigError> {
    let mut file = match open_read(path) {
        Ok(file) => file,
        Err(ConfigError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(None);
        }
        Err(error) => return Err(error),
    };
    let mut text = String::new();
    file.read_to_string(&mut text)?;
    Ok(Some(text))
}

/// Read the config as a JSON object. Empty content, invalid JSON and a
/// non-object root are errors so no caller can silently replace them with `{}`.
fn read_json_object(path: &Path) -> Result<Option<serde_json::Value>, ConfigError> {
    let Some(text) = read_config_text(path)? else {
        return Ok(None);
    };
    if text.trim().is_empty() {
        return Err(ConfigError::Invalid(format!(
            "config file is empty: {}",
            path.display()
        )));
    }
    let value: serde_json::Value = serde_json::from_str(&text)?;
    if !value.is_object() {
        return Err(ConfigError::Invalid(format!(
            "expected a JSON object at the top level: {}",
            path.display()
        )));
    }
    Ok(Some(value))
}

/// Best-effort directory fsync so a completed rename survives a crash.
fn sync_directory(directory: &Path) {
    if let Ok(file) = fs::File::open(directory) {
        let _ = file.sync_all();
    }
}

/// Serialize and atomically replace `path` with owner-only permissions.
fn write_json_atomic(path: &Path, value: &serde_json::Value) -> Result<(), ConfigError> {
    let mut text = serde_json::to_string_pretty(value)?;
    text.push('\n');
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    fs::create_dir_all(&parent)?;
    let mut temp = tempfile::NamedTempFile::new_in(&parent)?;
    #[cfg(unix)]
    temp.as_file()
        .set_permissions(fs::Permissions::from_mode(CONFIG_MODE))?;
    temp.write_all(text.as_bytes())?;
    temp.as_file().sync_all()?;
    temp.persist(path)
        .map_err(|error| ConfigError::Io(error.error))?;
    sync_directory(&parent);
    Ok(())
}

/// Recursively overlay `overlay` onto `base`, keeping keys that only exist in
/// `base`. Objects merge key by key; every other value replaces the old one.
fn overlay_json(base: &mut serde_json::Value, overlay: &serde_json::Value) {
    match (base, overlay) {
        (serde_json::Value::Object(base_map), serde_json::Value::Object(overlay_map)) => {
            for (key, value) in overlay_map {
                match base_map.get_mut(key) {
                    Some(slot) => overlay_json(slot, value),
                    None => {
                        base_map.insert(key.clone(), value.clone());
                    }
                }
            }
        }
        (slot, value) => *slot = value.clone(),
    }
}

/// Exclusive flock on `<config>.lock`, released and closed on drop.
#[cfg(unix)]
struct ConfigLock {
    file: fs::File,
}

#[cfg(unix)]
impl ConfigLock {
    fn acquire(path: &Path) -> Result<Self, ConfigError> {
        let lock_path = lock_path_for(path);
        if let Some(parent) = lock_path.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent)?;
        }
        let file = open_lock_file(&lock_path)?;
        // Tighten a pre-existing world-readable lock file; the content is empty
        // but the mode should match the config convention.
        let _ = file.set_permissions(fs::Permissions::from_mode(CONFIG_MODE));
        loop {
            // SAFETY: `file` owns a valid descriptor for the lock file.
            let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) };
            if result == 0 {
                break;
            }
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(ConfigError::Io(error));
        }
        Ok(Self { file })
    }
}

#[cfg(unix)]
impl Drop for ConfigLock {
    fn drop(&mut self) {
        // SAFETY: the descriptor is still owned by `self.file`; failures while
        // closing are irrelevant because the `close` that follows releases it.
        unsafe {
            libc::flock(self.file.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

#[cfg(not(unix))]
struct ConfigLock;

#[cfg(not(unix))]
impl ConfigLock {
    fn acquire(_path: &Path) -> Result<Self, ConfigError> {
        Err(ConfigError::Unsupported(
            "configuration locking requires Unix (Linux/macOS)".to_owned(),
        ))
    }
}

impl Config {
    /// `$SOCKET_STREAM_CONFIG`, else `$XDG_CONFIG_HOME/socket_stream/config.json`,
    /// else `$HOME/.config/socket_stream/config.json`.
    /// A missing HOME falls back to `.socket_stream.json` in the working directory.
    pub fn default_path() -> PathBuf {
        if let Some(path) = std::env::var_os("SOCKET_STREAM_CONFIG").filter(|v| !v.is_empty()) {
            return PathBuf::from(path);
        }
        if let Some(path) = std::env::var_os("XDG_CONFIG_HOME").filter(|v| !v.is_empty()) {
            return PathBuf::from(path).join("socket_stream/config.json");
        }
        if let Some(path) = std::env::var_os("HOME").filter(|v| !v.is_empty()) {
            return PathBuf::from(path).join(".config/socket_stream/config.json");
        }
        PathBuf::from(".socket_stream.json")
    }

    /// Load the default config path; a missing file yields defaults.
    pub fn load() -> Result<Self, ConfigError> {
        Self::load_from(&Self::default_path())
    }

    /// Load one file; a missing file yields defaults.
    ///
    /// A symlinked (including dangling) or non-regular path, empty content,
    /// malformed JSON and a non-object root are errors: the file is never
    /// followed or reinterpreted as empty defaults.
    pub fn load_from(path: &Path) -> Result<Self, ConfigError> {
        match read_json_object(path)? {
            Some(value) => Ok(serde_json::from_value(value)?),
            None => Ok(Self::default()),
        }
    }

    /// Reset `shortcuts.enabled` and `shortcuts.bindings` to defaults while
    /// preserving everything else. See [`Self::init_shortcuts`].
    pub fn reset_shortcuts_preserve_others(path: Option<&Path>) -> Result<PathBuf, ConfigError> {
        Self::init_shortcuts(path, true)
    }

    /// Write default shortcuts, optionally refusing to touch an existing file.
    ///
    /// The existence check, read, merge and atomic replace all run while the
    /// shared config flock is held, so a concurrent writer cannot slip a file
    /// in between the check and the write. Unknown top-level keys, other
    /// sections, unknown siblings inside `shortcuts`, and unknown keys inside
    /// known sections are preserved; only `shortcuts.enabled` and
    /// `shortcuts.bindings` are written (the binding map is replaced wholesale,
    /// so an unknown action name in an older map is dropped). Symlinked
    /// (including dangling), non-regular, empty, malformed or non-object config
    /// files are refused without clobbering their bytes.
    pub fn init_shortcuts(path: Option<&Path>, force: bool) -> Result<PathBuf, ConfigError> {
        let path = path
            .map(Path::to_path_buf)
            .unwrap_or_else(Self::default_path);
        refuse_unsafe_target(&path)?;
        let _lock = ConfigLock::acquire(&path)?;
        refuse_unsafe_target(&path)?;
        let existing = read_json_object(&path)?;
        if existing.is_some() && !force {
            return Err(ConfigError::Exists(path));
        }
        let mut root = existing.unwrap_or_else(|| serde_json::json!({}));
        {
            let object = root.as_object_mut().ok_or_else(|| {
                ConfigError::Invalid(format!(
                    "expected a JSON object at the top level: {}",
                    path.display()
                ))
            })?;
            match object.get("shortcuts") {
                Some(serde_json::Value::Object(_)) | None => {}
                Some(_) => {
                    return Err(ConfigError::Invalid(format!(
                        "refusing to reset shortcuts: 'shortcuts' is not a JSON object in {}",
                        path.display()
                    )));
                }
            }
            object
                .entry("shortcuts".to_owned())
                .or_insert_with(|| serde_json::Value::Object(serde_json::Map::new()));
            let shortcuts = match object.get_mut("shortcuts") {
                Some(serde_json::Value::Object(map)) => map,
                _ => {
                    return Err(ConfigError::Invalid(format!(
                        "refusing to reset shortcuts: 'shortcuts' is not a JSON object in {}",
                        path.display()
                    )));
                }
            };
            shortcuts.insert("enabled".to_owned(), serde_json::Value::Bool(true));
            shortcuts.insert("bindings".to_owned(), default_bindings_value());
        }
        write_json_atomic(&path, &root)?;
        Ok(path)
    }

    /// Write this typed configuration to the default path. See [`Self::save_to`].
    pub fn save(&self) -> Result<(), ConfigError> {
        self.save_to(&Self::default_path())
    }

    /// Write this typed configuration to `path` under the shared config lock.
    ///
    /// The JSON is freshly read while the lock is held and the serialized typed
    /// config is recursively overlaid onto it, so unknown top-level keys and
    /// unknown nested keys inside known sections survive another writer's
    /// changes. Known fields are intentionally a full snapshot (every typed
    /// field is written). An existing known section of the wrong type is
    /// refused, not overwritten. The `shortcuts.bindings` map is replaced
    /// wholesale, so unknown action names in an older map are dropped.
    /// Malformed, empty, non-object or wrong-typed existing JSON is reported
    /// and left untouched; a missing file is created.
    pub fn save_to(&self, path: &Path) -> Result<(), ConfigError> {
        refuse_unsafe_target(path)?;
        let _lock = ConfigLock::acquire(path)?;
        refuse_unsafe_target(path)?;
        let mut root = read_json_object(path)?.unwrap_or_else(|| serde_json::json!({}));
        let overlay = serde_json::to_value(self)?;
        for section in ["a11y", "ocr", "shortcuts", "overlay"] {
            if root.get(section).is_some_and(|value| !value.is_object()) {
                return Err(ConfigError::Invalid(format!(
                    "refusing to overwrite non-object section '{section}' in {}",
                    path.display()
                )));
            }
        }
        let bindings = overlay
            .get("shortcuts")
            .and_then(|shortcuts| shortcuts.get("bindings"))
            .cloned();
        overlay_json(&mut root, &overlay);
        if let (Some(shortcuts), Some(bindings)) = (
            root.get_mut("shortcuts")
                .and_then(serde_json::Value::as_object_mut),
            bindings,
        ) {
            // Documented exception: the binding map is a typed snapshot, not a
            // merge, matching the Python `save_config_file` behavior.
            shortcuts.insert("bindings".to_owned(), bindings);
        }
        write_json_atomic(path, &root)
    }

    /// Pretty JSON for this typed configuration.
    ///
    /// Serialization of these plain structs cannot fail; it is asserted rather
    /// than replaced with `{}`, which would silently corrupt a saved config.
    pub fn to_json_pretty(&self) -> String {
        serde_json::to_string_pretty(self)
            .expect("Config contains only maps, strings, numbers and booleans")
    }

    /// Map the configured mode string; anything unknown is `Auto`.
    pub fn a11y_mode(&self) -> ReadMode {
        match self.a11y.mode.trim().to_ascii_lowercase().as_str() {
            "focused" => ReadMode::Focused,
            "tree" => ReadMode::Tree,
            "text" => ReadMode::Text,
            _ => ReadMode::Auto,
        }
    }

    /// Build the accessibility token budget from the a11y section.
    pub fn budget(&self) -> TokenBudget {
        TokenBudget {
            max_tokens: self.a11y.max_tokens,
            max_nodes: self.a11y.max_nodes,
            max_depth: self.a11y.max_depth,
            max_chars: self.a11y.max_chars,
        }
    }
}

/// JSON object holding the 19 default bindings, in the same shape the Python
/// shell writes.
fn default_bindings_value() -> serde_json::Value {
    let mut bindings = serde_json::Map::new();
    for (action, accelerator) in DEFAULT_BINDINGS {
        bindings.insert(
            action.to_owned(),
            serde_json::Value::String(accelerator.to_owned()),
        );
    }
    serde_json::Value::Object(bindings)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Serializes tests that mutate process-wide environment variables.
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn write_json(path: &Path, value: &serde_json::Value) {
        fs::write(path, serde_json::to_vec_pretty(value).unwrap()).unwrap();
    }

    fn read_json(path: &Path) -> serde_json::Value {
        serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
    }

    #[test]
    fn missing_file_uses_defaults() {
        let dir = tempfile::tempdir().unwrap();
        let config = Config::load_from(&dir.path().join("missing.json")).unwrap();
        assert!(config.shortcuts.enabled);
        assert!(!config.ocr.enabled);
        assert_eq!(config.shortcuts.bindings.len(), 19);
    }

    #[test]
    fn malformed_file_is_error() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("bad.json");
        std::fs::write(&path, "{bad").unwrap();
        assert!(matches!(
            Config::load_from(&path),
            Err(ConfigError::Malformed(_))
        ));
    }

    #[test]
    fn empty_file_is_error_not_defaults() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("empty.json");
        std::fs::write(&path, "   \n").unwrap();
        assert!(matches!(
            Config::load_from(&path),
            Err(ConfigError::Invalid(message)) if message.contains("empty")
        ));
    }

    #[test]
    fn config_defaults_round_trip() {
        let raw = Config::default().to_json_pretty();
        assert_ne!(raw.trim(), "{}");
        let parsed: Config = serde_json::from_str(&raw).unwrap();
        assert_eq!(parsed, Config::default());
    }

    #[test]
    fn defaults_match_documented_values() {
        let config = Config::default();
        assert_eq!(config.a11y.mode, "tree");
        assert!(!config.ocr.enabled);
        assert!(config.shortcuts.enabled);
        assert!(config.overlay.stealth);
        assert_eq!(config.a11y.max_tokens, 4000);
        assert_eq!(config.a11y.max_nodes, 800);
        assert_eq!(config.a11y.max_depth, 20);
        assert_eq!(config.a11y.max_chars, 32_000);
        assert_eq!(
            config.shortcuts.bindings.get("start").map(String::as_str),
            Some("ctrl+alt+s")
        );
    }

    #[test]
    fn unknown_mode_maps_to_auto() {
        let mut config = Config::default();
        assert_eq!(config.a11y_mode(), ReadMode::Tree);
        config.a11y.mode = "focused".into();
        assert_eq!(config.a11y_mode(), ReadMode::Focused);
        config.a11y.mode = "tree".into();
        assert_eq!(config.a11y_mode(), ReadMode::Tree);
        config.a11y.mode = "text".into();
        assert_eq!(config.a11y_mode(), ReadMode::Text);
        config.a11y.mode = "nonsense".into();
        assert_eq!(config.a11y_mode(), ReadMode::Auto);
    }

    #[test]
    fn budget_reflects_a11y_section() {
        let mut config = Config::default();
        config.a11y.max_tokens = 10;
        config.a11y.max_nodes = 20;
        config.a11y.max_depth = 3;
        config.a11y.max_chars = 40;
        let budget = config.budget();
        assert_eq!(
            budget,
            TokenBudget {
                max_tokens: 10,
                max_nodes: 20,
                max_depth: 3,
                max_chars: 40,
            }
        );
    }

    #[test]
    fn default_path_honours_override_env() {
        let _guard = ENV_LOCK.lock().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let override_path = dir.path().join("custom.json");
        // SAFETY: single-threaded under ENV_LOCK; no other thread reads this var.
        unsafe { std::env::set_var("SOCKET_STREAM_CONFIG", &override_path) };
        assert_eq!(Config::default_path(), override_path);
        unsafe { std::env::remove_var("SOCKET_STREAM_CONFIG") };
    }

    #[test]
    fn save_then_load_round_trips_through_disk() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested/socket_stream/config.json");

        let mut config = Config::default();
        config.a11y.mode = "tree".into();
        config.ocr.enabled = true;
        config.save_to(&path).unwrap();

        assert!(path.is_file());
        let loaded = Config::load_from(&path).unwrap();
        assert_eq!(loaded, config);
        assert_eq!(loaded.a11y_mode(), ReadMode::Tree);
    }

    #[test]
    fn save_to_preserves_unknown_top_level_and_nested_fields() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.json");
        write_json(
            &path,
            &serde_json::json!({
                "a11y": {"mode": "text", "unknown_a11y": {"keep": 1}},
                "ocr": {"enabled": false, "unknown_ocr": [1, 2, 3]},
                "shortcuts": {
                    "enabled": false,
                    "bindings": {
                        "start": "ctrl+shift+1",
                        "custom_action": "ctrl+alt+z"
                    },
                    "unknown_shortcuts": "keep"
                },
                "overlay": {"stealth": false, "unknown_overlay": {"n": 1}},
                "desktop": {"appearance": {"opacity": 0.7, "font_size": 20, "reduced_motion": true}},
                "provider": {"api_base": "http://127.0.0.1:11434/v1"},
                "unknown_top": {"deep": [true, null]}
            }),
        );

        let mut config = Config::default();
        config.a11y.mode = "tree".into();
        config.ocr.enabled = true;
        config.shortcuts.enabled = true;
        config.save_to(&path).unwrap();

        let root = read_json(&path);
        assert_eq!(root["a11y"]["mode"], "tree");
        assert_eq!(root["a11y"]["unknown_a11y"]["keep"], 1);
        assert_eq!(root["ocr"]["enabled"], true);
        assert_eq!(root["ocr"]["unknown_ocr"][2], 3);
        assert_eq!(root["shortcuts"]["enabled"], true);
        assert_eq!(root["shortcuts"]["unknown_shortcuts"], "keep");
        assert_eq!(root["shortcuts"]["bindings"]["start"], "ctrl+alt+s");
        assert_eq!(root["shortcuts"]["bindings"].as_object().unwrap().len(), 19);
        // Documented: the typed binding map replaces the old mapping wholesale.
        assert!(root["shortcuts"]["bindings"].get("custom_action").is_none());
        assert_eq!(root["overlay"]["stealth"], true);
        assert_eq!(root["overlay"]["unknown_overlay"]["n"], 1);
        assert_eq!(root["desktop"]["appearance"]["opacity"].as_f64(), Some(0.7));
        assert_eq!(root["provider"]["api_base"], "http://127.0.0.1:11434/v1");
        assert_eq!(root["unknown_top"]["deep"][0], true);
        assert_eq!(Config::load_from(&path).unwrap(), config);
    }

    #[test]
    fn save_to_refuses_invalid_known_section_type_without_clobbering() {
        let dir = tempfile::tempdir().unwrap();
        for raw in [
            r#"{"a11y": 5, "keep": 1}"#,
            r#"{"shortcuts": "oops", "keep": 1}"#,
            r#"{"ocr": [], "keep": 1}"#,
            r#"{"overlay": null, "keep": 1}"#,
        ] {
            let path = dir.path().join("config.json");
            fs::write(&path, raw).unwrap();
            assert!(
                Config::default().save_to(&path).is_err(),
                "section type in {raw} must be refused"
            );
            assert_eq!(fs::read_to_string(&path).unwrap(), raw);
        }
    }

    #[test]
    fn save_to_refuses_nonobject_root_without_clobbering() {
        let dir = tempfile::tempdir().unwrap();
        for raw in ["[]", "1", "\"text\"", "null"] {
            let path = dir.path().join("config.json");
            fs::write(&path, raw).unwrap();
            assert!(
                Config::default().save_to(&path).is_err(),
                "root {raw} must be refused"
            );
            assert_eq!(fs::read_to_string(&path).unwrap(), raw);
        }
    }

    #[test]
    fn reset_preserves_others_and_unknown_shortcut_siblings() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.json");
        write_json(
            &path,
            &serde_json::json!({
                "a11y": {"mode": "focused", "unknown_a11y": [1]},
                "ocr": {"enabled": true},
                "shortcuts": {
                    "enabled": false,
                    "bindings": {"start": "ctrl+shift+1", "custom_action": "ctrl+alt+z"},
                    "unknown_shortcuts": {"keep": true}
                },
                "overlay": {"compact": true},
                "desktop": {"appearance": {"opacity": 0.6, "font_size": 21, "reduced_motion": true}},
                "provider": {"model": "local"},
                "unknown_top": "keep"
            }),
        );
        let written = Config::reset_shortcuts_preserve_others(Some(&path)).unwrap();
        assert_eq!(written, path);
        let root = read_json(&path);
        assert_eq!(root["shortcuts"]["enabled"], true);
        let bindings = root["shortcuts"]["bindings"].as_object().unwrap();
        assert_eq!(bindings.len(), 19);
        assert_eq!(bindings["start"], "ctrl+alt+s");
        assert_eq!(bindings["previous_question"], "ctrl+alt+left");
        assert!(bindings.get("custom_action").is_none());
        assert_eq!(root["shortcuts"]["unknown_shortcuts"]["keep"], true);
        assert_eq!(root["a11y"]["mode"], "focused");
        assert_eq!(root["a11y"]["unknown_a11y"][0], 1);
        assert_eq!(root["ocr"]["enabled"], true);
        assert_eq!(root["overlay"]["compact"], true);
        assert_eq!(
            root["desktop"]["appearance"]["font_size"].as_u64(),
            Some(21)
        );
        assert_eq!(root["provider"]["model"], "local");
        assert_eq!(root["unknown_top"], "keep");
    }

    #[test]
    fn reset_refuses_invalid_roots_and_preserves_bytes() {
        let dir = tempfile::tempdir().unwrap();
        for raw in ["[]", "1", "", "{bad", "   "] {
            let path = dir.path().join("config.json");
            fs::write(&path, raw).unwrap();
            assert!(
                Config::reset_shortcuts_preserve_others(Some(&path)).is_err(),
                "root {raw:?} must be refused"
            );
            assert_eq!(fs::read_to_string(&path).unwrap(), raw);
        }
    }

    #[test]
    fn reset_refuses_nonobject_shortcuts_section() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.json");
        let raw = r#"{"shortcuts": "oops", "keep": 1}"#;
        fs::write(&path, raw).unwrap();
        assert!(Config::reset_shortcuts_preserve_others(Some(&path)).is_err());
        assert_eq!(fs::read_to_string(&path).unwrap(), raw);
    }

    #[test]
    fn reset_creates_missing_file_with_defaults() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("new/config.json");
        Config::reset_shortcuts_preserve_others(Some(&path)).unwrap();
        let root = read_json(&path);
        assert_eq!(root["shortcuts"]["enabled"], true);
        assert_eq!(root["shortcuts"]["bindings"].as_object().unwrap().len(), 19);
        assert!(path.with_file_name("config.json.lock").is_file());
    }

    #[test]
    fn init_without_force_refuses_existing_file_and_leaves_it_alone() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.json");
        let raw = r#"{"shortcuts": {"enabled": false}, "keep": 1}"#;
        fs::write(&path, raw).unwrap();
        match Config::init_shortcuts(Some(&path), false) {
            Err(ConfigError::Exists(reported)) => assert_eq!(reported, path),
            other => panic!("expected Exists, got {other:?}"),
        }
        assert_eq!(fs::read_to_string(&path).unwrap(), raw);
        Config::init_shortcuts(Some(&path), true).unwrap();
        assert_eq!(read_json(&path)["shortcuts"]["enabled"], true);
        assert_eq!(read_json(&path)["keep"], 1);
    }

    #[test]
    fn init_without_force_creates_missing_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.json");
        Config::init_shortcuts(Some(&path), false).unwrap();
        assert!(path.is_file());
    }

    #[test]
    fn writes_use_owner_only_mode_and_leave_no_temp_files() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.json");
        Config::default().save_to(&path).unwrap();
        #[cfg(unix)]
        {
            let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
            let lock = path.with_file_name("config.json.lock");
            let lock_mode = fs::metadata(&lock).unwrap().permissions().mode() & 0o777;
            assert_eq!(lock_mode, 0o600);
        }
        let mut names: Vec<String> = fs::read_dir(dir.path())
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        assert_eq!(names, vec!["config.json", "config.json.lock"]);
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_targets_are_refused_for_read_save_and_reset() {
        use std::os::unix::fs::symlink;
        let dir = tempfile::tempdir().unwrap();
        let victim = dir.path().join("victim.json");
        write_json(
            &victim,
            &serde_json::json!({"shortcuts": {"enabled": false}, "keep": 1}),
        );
        let link = dir.path().join("config.json");
        symlink(&victim, &link).unwrap();
        let original = fs::read_to_string(&victim).unwrap();

        assert!(Config::load_from(&link).is_err());
        assert!(Config::default().save_to(&link).is_err());
        assert!(Config::reset_shortcuts_preserve_others(Some(&link)).is_err());
        assert!(Config::init_shortcuts(Some(&link), false).is_err());
        assert_eq!(fs::read_to_string(&victim).unwrap(), original);
        assert!(link.symlink_metadata().unwrap().file_type().is_symlink());
    }

    #[cfg(unix)]
    #[test]
    fn dangling_symlink_is_refused_everywhere() {
        use std::os::unix::fs::symlink;
        let dir = tempfile::tempdir().unwrap();
        let link = dir.path().join("config.json");
        symlink(dir.path().join("missing.json"), &link).unwrap();

        assert!(Config::load_from(&link).is_err());
        assert!(Config::default().save_to(&link).is_err());
        assert!(Config::reset_shortcuts_preserve_others(Some(&link)).is_err());
        assert!(Config::init_shortcuts(Some(&link), true).is_err());
        assert!(link.symlink_metadata().unwrap().file_type().is_symlink());
        assert!(!dir.path().join("missing.json").exists());
    }

    #[cfg(unix)]
    #[test]
    fn symlinked_lock_file_is_refused() {
        use std::os::unix::fs::symlink;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.json");
        let victim = dir.path().join("victim.json");
        write_json(&victim, &serde_json::json!({"keep": 1}));
        symlink(&victim, dir.path().join("config.json.lock")).unwrap();
        assert!(Config::default().save_to(&path).is_err());
        assert_eq!(read_json(&victim), serde_json::json!({"keep": 1}));
    }

    #[test]
    fn nonregular_target_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.json");
        fs::create_dir(&path).unwrap();
        assert!(Config::default().save_to(&path).is_err());
        assert!(Config::reset_shortcuts_preserve_others(Some(&path)).is_err());
        assert!(Config::load_from(&path).is_err());
        assert!(path.is_dir());
    }
}
