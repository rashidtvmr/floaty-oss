//! Shared application configuration and shortcut bindings.
//! Missing files use defaults; malformed configuration is reported.
use crate::a11y::{ReadMode, TokenBudget};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// Default accelerator strings, mirroring docs/A11Y-TREE-SHORTCUTS.md section 3.2.
/// Kept local so configuration does not depend on the shortcut runtime module.
pub const DEFAULT_BINDINGS: [(&str, &str); 12] = [
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
];

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("configuration I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("malformed configuration JSON: {0}")]
    Malformed(#[from] serde_json::Error),
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

impl Config {
    /// `$SOCKET_STREAM_CONFIG`, else `$XDG_CONFIG_HOME/socket_stream/config.json`,
    /// else `~/.config/socket_stream/config.json`, else `~/.socket_stream.json`.
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

    /// Load one file; a missing file yields defaults, malformed JSON is an error.
    pub fn load_from(path: &Path) -> Result<Self, ConfigError> {
        match std::fs::read_to_string(path) {
            Ok(text) => Ok(serde_json::from_str(&text)?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(e.into()),
        }
    }

    /// Write pretty JSON to the default path, creating parent directories.
    pub fn save(&self) -> Result<(), ConfigError> {
        let path = Self::default_path();
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(path, self.to_json_pretty())?;
        Ok(())
    }

    pub fn to_json_pretty(&self) -> String {
        serde_json::to_string_pretty(self).unwrap_or_else(|_| "{}".into())
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

#[cfg(test)]
mod tests {
    use super::*;

    /// Serializes tests that mutate process-wide environment variables.
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    #[test]
    fn missing_file_uses_defaults() {
        let dir = tempfile::tempdir().unwrap();
        let config = Config::load_from(&dir.path().join("missing.json")).unwrap();
        assert!(config.shortcuts.enabled);
        assert!(!config.ocr.enabled);
        assert_eq!(config.shortcuts.bindings.len(), 12);
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
    fn config_defaults_round_trip() {
        let raw = Config::default().to_json_pretty();
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
        let _guard = ENV_LOCK.lock().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested/socket_stream/config.json");
        // SAFETY: single-threaded under ENV_LOCK; no other thread reads this var.
        unsafe { std::env::set_var("SOCKET_STREAM_CONFIG", &path) };

        let mut config = Config::default();
        config.a11y.mode = "tree".into();
        config.ocr.enabled = true;
        config.save().unwrap();

        assert!(path.is_file());
        let loaded = Config::load().unwrap();
        assert_eq!(loaded, config);
        assert_eq!(loaded.a11y_mode(), ReadMode::Tree);

        unsafe { std::env::remove_var("SOCKET_STREAM_CONFIG") };
    }
}
