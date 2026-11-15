//! Global shortcut model for SocketStream.
//!
//! See `docs/A11Y-TREE-SHORTCUTS.md` section 3.2 for the frozen interface.
//!
//! Honest scope: SocketStream never installs a global hotkey grab by itself and
//! never bypasses OS permission. In-app accelerators work while a SocketStream
//! window is focused. Out-of-focus global shortcuts need the desktop's own
//! hotkey tool (X11: `sxhkd`/`xbindkeys`, Wayland: the
//! `org.freedesktop.portal.GlobalShortcuts` portal).

use crate::config::ShortcutsConfig;

/// Stable action names, in the order used by the docs, the config file, the
/// CLI and the desktop shells.
pub const ACTIONS: [&str; 12] = [
    "start",
    "stop",
    "pause_resume",
    "toggle_overlay",
    "show_overlay",
    "hide_overlay",
    "copy_answer",
    "compact_toggle",
    "toggle_stealth",
    "open_settings",
    "refresh_question",
    "quit",
];

/// Default accelerator for each action, in [`ACTIONS`] order.
///
/// Also written to a fresh config file by `socket_stream hotkeys init`.
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

/// Everything that can go wrong while parsing or validating shortcuts.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ShortcutError {
    #[error("accelerator is empty")]
    Empty,
    #[error("accelerator contains an empty part (check for a stray '+')")]
    EmptyPart,
    #[error("duplicate modifier '{0}'")]
    DuplicateModifier(String),
    #[error("unknown modifier '{0}'")]
    UnknownModifier(String),
    #[error("unknown key '{0}'")]
    UnknownKey(String),
    #[error("accelerator needs exactly one non-modifier key")]
    MissingKey,
    #[error("accelerator has more than one key (found '{0}')")]
    MultipleKeys(String),
    #[error("unknown action '{0}'")]
    UnknownAction(String),
    #[error("duplicate accelerator '{0}'")]
    DuplicateAccelerator(String),
}

/// A parsed keyboard accelerator: modifiers plus exactly one key.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Accelerator {
    pub ctrl: bool,
    pub alt: bool,
    pub shift: bool,
    pub meta: bool,
    /// Lowercase key name, e.g. `"s"`, `"f1"`, `"space"`, `"comma"`.
    pub key: String,
}

impl Accelerator {
    /// Parse an accelerator such as `ctrl+alt+shift+o`.
    ///
    /// Case-insensitive. Accepts `ctrl`/`control`, `alt`/`opt`/`option`,
    /// `shift`, `meta`/`cmd`/`super`/`win`. Exactly one non-modifier key is
    /// required.
    pub fn parse(value: &str) -> Result<Self, ShortcutError> {
        if value.trim().is_empty() {
            return Err(ShortcutError::Empty);
        }
        let mut accel = Accelerator {
            ctrl: false,
            alt: false,
            shift: false,
            meta: false,
            key: String::new(),
        };
        for raw in value.split('+') {
            let part = raw.trim().to_ascii_lowercase();
            if part.is_empty() {
                return Err(ShortcutError::EmptyPart);
            }
            match part.as_str() {
                "ctrl" | "control" => set_modifier(&mut accel.ctrl, "ctrl")?,
                "alt" | "opt" | "option" => set_modifier(&mut accel.alt, "alt")?,
                "shift" => set_modifier(&mut accel.shift, "shift")?,
                "meta" | "cmd" | "super" | "win" => set_modifier(&mut accel.meta, "meta")?,
                key => {
                    if !is_valid_key(key) {
                        // Distinguish a mistyped modifier word from a mistyped
                        // key so the error message points at the right thing.
                        if !has_key_shape(key) && looks_like_modifier_word(key) {
                            return Err(ShortcutError::UnknownModifier(key.to_owned()));
                        }
                        return Err(ShortcutError::UnknownKey(key.to_owned()));
                    }
                    if !accel.key.is_empty() {
                        return Err(ShortcutError::MultipleKeys(key.to_owned()));
                    }
                    accel.key = key.to_owned();
                }
            }
        }
        if accel.key.is_empty() {
            return Err(ShortcutError::MissingKey);
        }
        Ok(accel)
    }

    /// Canonical display form, e.g. `Ctrl+Alt+Shift+O`.
    pub fn display(&self) -> String {
        let mut parts: Vec<&str> = Vec::with_capacity(5);
        if self.ctrl {
            parts.push("Ctrl");
        }
        if self.alt {
            parts.push("Alt");
        }
        if self.shift {
            parts.push("Shift");
        }
        if self.meta {
            parts.push("Meta");
        }
        let key = display_key(&self.key);
        parts.push(&key);
        parts.join("+")
    }
}

fn set_modifier(flag: &mut bool, name: &str) -> Result<(), ShortcutError> {
    if *flag {
        return Err(ShortcutError::DuplicateModifier(name.to_owned()));
    }
    *flag = true;
    Ok(())
}

/// Whether a lowercased token is one of the accepted key names.
fn is_valid_key(key: &str) -> bool {
    if key.chars().count() == 1 {
        let ch = key.chars().next().unwrap_or('\0');
        return ch.is_ascii_lowercase() || ch.is_ascii_digit();
    }
    match key {
        "space" | "comma" | "period" | "slash" | "semicolon" | "apostrophe" | "minus"
        | "equals" | "tab" | "enter" | "escape" | "backspace" => true,
        _ => match key.strip_prefix('f') {
            Some(digits) if !digits.is_empty() => digits
                .parse::<u32>()
                .map(|n| (1..=24).contains(&n))
                .unwrap_or(false),
            _ => false,
        },
    }
}

/// Whether a token has the *shape* of a key name, even when out of range
/// (e.g. `f25`), so it is reported as an unknown key rather than a modifier.
fn has_key_shape(token: &str) -> bool {
    if token.chars().count() == 1 {
        return true;
    }
    if matches!(
        token,
        "space"
            | "comma"
            | "period"
            | "slash"
            | "semicolon"
            | "apostrophe"
            | "minus"
            | "equals"
            | "tab"
            | "enter"
            | "escape"
            | "backspace"
    ) {
        return true;
    }
    match token.strip_prefix('f') {
        Some(digits) => !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit()),
        None => false,
    }
}

/// A multi-letter alphabetic token that is not a key shape is most likely a
/// mistyped modifier (e.g. `hyper`).
fn looks_like_modifier_word(token: &str) -> bool {
    token.chars().count() >= 2 && token.chars().all(|c| c.is_ascii_alphabetic())
}

/// Human-readable key name for [`Accelerator::display`].
fn display_key(key: &str) -> String {
    if key.chars().count() == 1 {
        return key.to_ascii_uppercase();
    }
    if let Some(digits) = key.strip_prefix('f')
        && !digits.is_empty()
        && digits.chars().all(|c| c.is_ascii_digit())
    {
        return key.to_ascii_uppercase();
    }
    let mut chars = key.chars();
    match chars.next() {
        Some(first) => {
            let mut out = first.to_uppercase().collect::<String>();
            out.push_str(chars.as_str());
            out
        }
        None => String::new(),
    }
}

/// One action bound to one accelerator.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    pub action: String,
    pub accelerator: Accelerator,
}

/// The resolved action -> accelerator table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShortcutMap {
    pub bindings: Vec<Binding>,
}

impl ShortcutMap {
    /// Start from [`ShortcutMap::defaults`], then override with every entry in
    /// `config.bindings`.
    ///
    /// An invalid entry (unparseable accelerator or unknown action) is an
    /// error; it is never silently ignored. `config.enabled` is not consulted
    /// here — shells decide whether to install the shortcuts at all.
    pub fn from_config(config: &ShortcutsConfig) -> Result<Self, ShortcutError> {
        let mut map = Self::defaults();
        for (action, value) in &config.bindings {
            let accelerator = Accelerator::parse(value)?;
            match map
                .bindings
                .iter_mut()
                .find(|binding| &binding.action == action)
            {
                Some(binding) => binding.accelerator = accelerator,
                None => return Err(ShortcutError::UnknownAction(action.clone())),
            }
        }
        map.validate()?;
        Ok(map)
    }

    /// The twelve default bindings from the docs.
    pub fn defaults() -> Self {
        let bindings = DEFAULT_BINDINGS
            .iter()
            .map(|(action, value)| Binding {
                action: (*action).to_owned(),
                accelerator: Accelerator::parse(value).expect("default accelerator must be valid"),
            })
            .collect();
        ShortcutMap { bindings }
    }

    /// The action bound to `accel`, if any.
    pub fn action_for(&self, accel: &Accelerator) -> Option<&str> {
        self.bindings
            .iter()
            .find(|binding| &binding.accelerator == accel)
            .map(|binding| binding.action.as_str())
    }

    /// The accelerator bound to `action`, if any.
    pub fn accelerator_for(&self, action: &str) -> Option<&Accelerator> {
        self.bindings
            .iter()
            .find(|binding| binding.action == action)
            .map(|binding| &binding.accelerator)
    }

    /// Reject unknown action names and duplicate accelerators.
    pub fn validate(&self) -> Result<(), ShortcutError> {
        let mut seen: Vec<String> = Vec::with_capacity(self.bindings.len());
        for binding in &self.bindings {
            if !ACTIONS.contains(&binding.action.as_str()) {
                return Err(ShortcutError::UnknownAction(binding.action.clone()));
            }
            let display = binding.accelerator.display();
            if seen.contains(&display) {
                return Err(ShortcutError::DuplicateAccelerator(display));
            }
            seen.push(display);
        }
        Ok(())
    }
}

/// Where the app is running, which decides how global shortcuts are installed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShortcutPlatform {
    X11,
    Wayland,
    MacOs,
    Unknown,
}

/// Detect the current platform from the process environment.
pub fn detect_platform() -> ShortcutPlatform {
    platform_from(&|name| std::env::var(name).ok(), cfg!(target_os = "macos"))
}

/// Pure platform detection so it can be unit tested with a fake environment.
///
/// `is_macos` is supplied by the caller (production uses
/// `cfg!(target_os = "macos")`) so the Linux branches stay testable on macOS.
fn platform_from(lookup: &dyn Fn(&str) -> Option<String>, is_macos: bool) -> ShortcutPlatform {
    if is_macos {
        return ShortcutPlatform::MacOs;
    }
    let non_empty = |name: &str| lookup(name).is_some_and(|value| !value.trim().is_empty());
    let session = lookup("XDG_SESSION_TYPE").map(|value| value.trim().to_ascii_lowercase());
    if non_empty("WAYLAND_DISPLAY") || session.as_deref() == Some("wayland") {
        return ShortcutPlatform::Wayland;
    }
    if non_empty("DISPLAY") {
        return ShortcutPlatform::X11;
    }
    ShortcutPlatform::Unknown
}

/// Human instructions for `platform`: one line per default binding, then the
/// platform-specific honesty note.
pub fn platform_notes(platform: ShortcutPlatform) -> Vec<String> {
    let mut notes: Vec<String> = DEFAULT_BINDINGS
        .iter()
        .map(|(action, value)| {
            let display = Accelerator::parse(value)
                .map(|accel| accel.display())
                .unwrap_or_else(|_| (*value).to_owned());
            format!("{action}: {display}")
        })
        .collect();
    match platform {
        ShortcutPlatform::X11 => notes.push(
            "Out-of-focus global shortcuts need your desktop's own hotkey tool \
             (sxhkd or xbindkeys); SocketStream installs nothing by itself."
                .to_owned(),
        ),
        ShortcutPlatform::Wayland => notes.push(
            "Out-of-focus global shortcuts need the org.freedesktop.portal.GlobalShortcuts \
             portal; SocketStream installs nothing by itself."
                .to_owned(),
        ),
        ShortcutPlatform::MacOs => notes.push(
            "In-app menu key equivalents work while SocketStream is focused; a global \
             monitor covers the rest while the app is running."
                .to_owned(),
        ),
        ShortcutPlatform::Unknown => notes.push(
            "Session type not detected; shortcuts work only while the SocketStream window \
             is focused."
                .to_owned(),
        ),
    }
    notes.push(
        "SocketStream never installs a global hotkey grab silently and never bypasses \
         OS permission."
            .to_owned(),
    );
    notes
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_have_twelve_bindings() {
        let map = ShortcutMap::defaults();
        assert_eq!(map.bindings.len(), 12);
        assert_eq!(map.bindings.len(), ACTIONS.len());
        map.validate().expect("defaults must validate");
        for action in ACTIONS {
            assert!(
                map.accelerator_for(action).is_some(),
                "missing default for {action}"
            );
        }
    }

    #[test]
    fn parse_and_display_round_trip() {
        for (_, value) in DEFAULT_BINDINGS {
            let accel = Accelerator::parse(value).expect("default parses");
            let reparsed = Accelerator::parse(&accel.display()).expect("display reparses");
            assert_eq!(accel, reparsed, "round trip failed for {value}");
        }
    }

    #[test]
    fn parse_is_case_insensitive_and_accepts_aliases() {
        let a = Accelerator::parse("CTRL+Alt+SHIFT+O").unwrap();
        assert!(a.ctrl && a.alt && a.shift && !a.meta);
        assert_eq!(a.key, "o");
        assert_eq!(a.display(), "Ctrl+Alt+Shift+O");

        for alias in ["ctrl+s", "control+s"] {
            assert!(Accelerator::parse(alias).unwrap().ctrl);
        }
        for alias in ["alt+s", "opt+s", "option+s"] {
            assert!(Accelerator::parse(alias).unwrap().alt);
        }
        for alias in ["meta+s", "cmd+s", "super+s", "win+s"] {
            assert!(Accelerator::parse(alias).unwrap().meta);
        }
    }

    #[test]
    fn parse_accepts_every_documented_key() {
        for key in [
            "a",
            "z",
            "0",
            "9",
            "f1",
            "f12",
            "f24",
            "space",
            "comma",
            "period",
            "slash",
            "semicolon",
            "apostrophe",
            "minus",
            "equals",
            "tab",
            "enter",
            "escape",
            "backspace",
        ] {
            let accel = Accelerator::parse(&format!("ctrl+{key}"))
                .unwrap_or_else(|e| panic!("{key} should parse: {e}"));
            assert_eq!(accel.key, key);
        }
    }

    #[test]
    fn display_of_named_and_function_keys() {
        assert_eq!(
            Accelerator::parse("ctrl+alt+comma").unwrap().display(),
            "Ctrl+Alt+Comma"
        );
        assert_eq!(Accelerator::parse("ctrl+f1").unwrap().display(), "Ctrl+F1");
        assert_eq!(
            Accelerator::parse("ctrl+alt+space").unwrap().display(),
            "Ctrl+Alt+Space"
        );
        assert_eq!(
            Accelerator::parse("meta+shift+1").unwrap().display(),
            "Shift+Meta+1"
        );
    }

    #[test]
    fn parse_rejects_bad_input() {
        assert_eq!(Accelerator::parse(""), Err(ShortcutError::Empty));
        assert_eq!(Accelerator::parse("   "), Err(ShortcutError::Empty));
        assert_eq!(Accelerator::parse("ctrl++s"), Err(ShortcutError::EmptyPart));
        assert_eq!(
            Accelerator::parse("ctrl+ctrl+s"),
            Err(ShortcutError::DuplicateModifier("ctrl".into()))
        );
        assert_eq!(
            Accelerator::parse("hyper+s"),
            Err(ShortcutError::UnknownModifier("hyper".into()))
        );
        assert_eq!(Accelerator::parse("ctrl+"), Err(ShortcutError::EmptyPart));
        assert_eq!(Accelerator::parse("ctrl"), Err(ShortcutError::MissingKey));
        assert_eq!(
            Accelerator::parse("ctrl+shift"),
            Err(ShortcutError::MissingKey)
        );
        assert_eq!(
            Accelerator::parse("ctrl+s+x"),
            Err(ShortcutError::MultipleKeys("x".into()))
        );
        assert_eq!(
            Accelerator::parse("ctrl+f25"),
            Err(ShortcutError::UnknownKey("f25".into()))
        );
        assert_eq!(
            Accelerator::parse("ctrl+f0"),
            Err(ShortcutError::UnknownKey("f0".into()))
        );
        assert_eq!(
            Accelerator::parse("ctrl+bogus"),
            Err(ShortcutError::UnknownModifier("bogus".into()))
        );
        assert_eq!(
            Accelerator::parse("ctrl+!"),
            Err(ShortcutError::UnknownKey("!".into()))
        );
    }

    #[test]
    fn action_and_accelerator_lookups() {
        let map = ShortcutMap::defaults();
        let accel = Accelerator::parse("ctrl+alt+s").unwrap();
        assert_eq!(map.action_for(&accel), Some("start"));
        assert_eq!(
            map.action_for(&Accelerator::parse("ctrl+alt+z").unwrap()),
            None
        );
        assert_eq!(map.accelerator_for("quit").unwrap().display(), "Ctrl+Alt+Q");
        assert!(map.accelerator_for("nope").is_none());
    }

    #[test]
    fn from_config_overrides_defaults() {
        let mut config = ShortcutsConfig::default();
        config
            .bindings
            .insert("start".to_owned(), "ctrl+shift+1".to_owned());
        config
            .bindings
            .insert("quit".to_owned(), "meta+q".to_owned());

        let map = ShortcutMap::from_config(&config).expect("valid overrides");
        assert_eq!(map.bindings.len(), 12);
        assert_eq!(
            map.accelerator_for("start").unwrap().display(),
            "Ctrl+Shift+1"
        );
        assert_eq!(map.accelerator_for("quit").unwrap().display(), "Meta+Q");
        // Untouched actions keep their defaults.
        assert_eq!(map.accelerator_for("stop").unwrap().display(), "Ctrl+Alt+X");
    }

    #[test]
    fn from_config_rejects_invalid_entries() {
        let mut config = ShortcutsConfig::default();
        config
            .bindings
            .insert("start".to_owned(), "ctrl+f25".to_owned());
        assert!(matches!(
            ShortcutMap::from_config(&config),
            Err(ShortcutError::UnknownKey(_))
        ));

        let mut config = ShortcutsConfig::default();
        config
            .bindings
            .insert("not_an_action".to_owned(), "ctrl+alt+s".to_owned());
        assert_eq!(
            ShortcutMap::from_config(&config),
            Err(ShortcutError::UnknownAction("not_an_action".into()))
        );
    }

    #[test]
    fn from_config_rejects_collisions() {
        let mut config = ShortcutsConfig::default();
        config
            .bindings
            .insert("start".to_owned(), "ctrl+alt+x".to_owned());
        assert_eq!(
            ShortcutMap::from_config(&config),
            Err(ShortcutError::DuplicateAccelerator("Ctrl+Alt+X".into()))
        );
    }

    #[test]
    fn validate_rejects_unknown_action_and_duplicates() {
        let unknown = ShortcutMap {
            bindings: vec![Binding {
                action: "mystery".to_owned(),
                accelerator: Accelerator::parse("ctrl+alt+s").unwrap(),
            }],
        };
        assert_eq!(
            unknown.validate(),
            Err(ShortcutError::UnknownAction("mystery".into()))
        );

        let duplicate = ShortcutMap {
            bindings: vec![
                Binding {
                    action: "start".to_owned(),
                    accelerator: Accelerator::parse("ctrl+alt+s").unwrap(),
                },
                Binding {
                    action: "stop".to_owned(),
                    accelerator: Accelerator::parse("ctrl+alt+s").unwrap(),
                },
            ],
        };
        assert_eq!(
            duplicate.validate(),
            Err(ShortcutError::DuplicateAccelerator("Ctrl+Alt+S".into()))
        );
    }

    #[test]
    fn platform_detection_from_fake_env() {
        let env = |pairs: &[(&str, &str)]| {
            let owned: Vec<(String, String)> = pairs
                .iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
                .collect();
            move |name: &str| {
                owned
                    .iter()
                    .find(|(key, _)| key == name)
                    .map(|(_, value)| value.clone())
            }
        };

        assert_eq!(
            platform_from(&env(&[("WAYLAND_DISPLAY", "wayland-0")]), false),
            ShortcutPlatform::Wayland
        );
        assert_eq!(
            platform_from(&env(&[("XDG_SESSION_TYPE", "wayland")]), false),
            ShortcutPlatform::Wayland
        );
        assert_eq!(
            platform_from(&env(&[("XDG_SESSION_TYPE", "Wayland")]), false),
            ShortcutPlatform::Wayland
        );
        assert_eq!(
            platform_from(&env(&[("DISPLAY", ":0")]), false),
            ShortcutPlatform::X11
        );
        assert_eq!(platform_from(&env(&[]), false), ShortcutPlatform::Unknown);
        assert_eq!(
            platform_from(
                &env(&[("DISPLAY", ":0"), ("WAYLAND_DISPLAY", "wayland-1")]),
                false
            ),
            ShortcutPlatform::Wayland,
            "Wayland wins when both are set"
        );
        assert_eq!(
            platform_from(&env(&[("DISPLAY", ":0")]), true),
            ShortcutPlatform::MacOs
        );
    }

    #[test]
    fn platform_notes_are_non_empty_for_every_platform() {
        for platform in [
            ShortcutPlatform::X11,
            ShortcutPlatform::Wayland,
            ShortcutPlatform::MacOs,
            ShortcutPlatform::Unknown,
        ] {
            let notes = platform_notes(platform);
            assert!(!notes.is_empty());
            assert!(
                notes.len() > DEFAULT_BINDINGS.len(),
                "expected one line per binding plus a note"
            );
            assert!(notes.iter().all(|line| !line.trim().is_empty()));
        }
        let x11 = platform_notes(ShortcutPlatform::X11).join("\n");
        assert!(x11.contains("sxhkd") || x11.contains("xbindkeys"));
        let wayland = platform_notes(ShortcutPlatform::Wayland).join("\n");
        assert!(wayland.contains("GlobalShortcuts"));
    }
}
