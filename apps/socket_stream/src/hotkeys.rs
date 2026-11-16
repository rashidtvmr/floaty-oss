//! `socket_stream hotkeys` — inspect, initialise and print the shortcut setup.
//!
//! See `docs/A11Y-TREE-SHORTCUTS.md` section 3.3. This command never installs
//! a global hotkey grab: `print` only emits a registration snippet for the
//! user's own desktop tool.
use socket_stream_core::config::Config;
use socket_stream_core::shortcuts::{
    DEFAULT_BINDINGS, ShortcutMap, ShortcutPlatform, detect_platform, platform_notes,
};

/// The main control window's title, anchored so the answer panel
/// (`SocketStream — Practice Answer`) is never matched. Linux GTK sets exactly
/// `SocketStream — Stopped`, `SocketStream — Active` or `SocketStream —
/// Stopping`, so the status is a single alphabetic word. The pattern avoids
/// `|`/`+` so it is valid whether xdotool compiles it as a basic or an extended
/// POSIX regex.
const X11_CONTROL_TITLE_PATTERN: &str = "^SocketStream — [A-Za-z][A-Za-z]*$";

/// Entry point wired by `main.rs` as the `hotkeys` dispatch arm.
///
/// `args` is the full argument list (including the leading `hotkeys`), matching
/// how the other subcommands receive theirs.
pub fn run(args: &[String]) -> Result<(), String> {
    let rest = match args.first().map(String::as_str) {
        Some("hotkeys") => &args[1..],
        _ => args,
    };
    match parse_command(rest)? {
        Command::Help => {
            print_usage();
            Ok(())
        }
        Command::List => list(),
        Command::Init { force } => init(force),
        Command::Print => {
            print!("{}", snippet_for(detect_platform()));
            Ok(())
        }
    }
}

/// A parsed `hotkeys` invocation. Kept separate from `run` so it is unit
/// testable without touching the filesystem.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Command {
    Help,
    List,
    Init { force: bool },
    Print,
}

fn parse_command(args: &[String]) -> Result<Command, String> {
    let Some(first) = args.first().map(String::as_str) else {
        return Ok(Command::Help);
    };
    match first {
        "list" => {
            if let Some(extra) = args.get(1) {
                return Err(format!("unexpected argument for 'hotkeys list': {extra}"));
            }
            Ok(Command::List)
        }
        "print" => {
            if let Some(extra) = args.get(1) {
                return Err(format!("unexpected argument for 'hotkeys print': {extra}"));
            }
            Ok(Command::Print)
        }
        "init" => {
            let mut force = false;
            for extra in &args[1..] {
                match extra.as_str() {
                    "--force" => force = true,
                    other => {
                        return Err(format!("unexpected argument for 'hotkeys init': {other}"));
                    }
                }
            }
            Ok(Command::Init { force })
        }
        "help" | "--help" | "-h" => Ok(Command::Help),
        other => Err(format!("unknown hotkeys subcommand: {other}")),
    }
}

fn print_usage() {
    println!("SocketStream shortcuts");
    println!("  socket_stream hotkeys list          show the resolved bindings and platform");
    println!("  socket_stream hotkeys init [--force] write a default config file");
    println!("  socket_stream hotkeys print         print a registration snippet for your desktop");
    println!();
    println!("SocketStream never installs a global hotkey grab by itself.");
}

fn platform_name(platform: ShortcutPlatform) -> &'static str {
    match platform {
        ShortcutPlatform::X11 => "X11",
        ShortcutPlatform::Wayland => "Wayland",
        ShortcutPlatform::MacOs => "macOS",
        ShortcutPlatform::Unknown => "unknown session",
    }
}

fn list() -> Result<(), String> {
    let config = Config::load().map_err(|e| e.to_string())?;
    let map = ShortcutMap::from_config(&config.shortcuts).map_err(|e| e.to_string())?;
    let platform = detect_platform();
    println!(
        "SocketStream shortcuts — platform: {}",
        platform_name(platform)
    );
    println!(
        "enabled: {}",
        if config.shortcuts.enabled {
            "yes"
        } else {
            "no"
        }
    );
    println!();
    for binding in &map.bindings {
        println!("  {:<18} {}", binding.action, binding.accelerator.display());
    }
    println!();
    println!("Platform guidance:");
    // `platform_notes` emits one line per default binding first; only the
    // trailing platform-specific lines are useful next to the resolved table.
    for note in platform_notes(platform)
        .into_iter()
        .skip(DEFAULT_BINDINGS.len())
    {
        println!("  {note}");
    }
    Ok(())
}

fn init(force: bool) -> Result<(), String> {
    // The existence check happens inside the same locked update, so two
    // concurrent `hotkeys init` runs cannot both believe the file is absent.
    let written = Config::init_shortcuts(None, force).map_err(|e| e.to_string())?;
    println!("Reset shortcuts to defaults in {}", written.display());
    println!(
        "Shortcuts are enabled by default. Rebind under \"shortcuts\".\"bindings\" \
         (see docs/SHORTCUTS.md). Other settings (appearance, a11y, ocr, overlay, unknown keys) are preserved."
    );
    Ok(())
}

/// The platform-native registration snippet. Pure so it can be unit tested.
fn snippet_for(platform: ShortcutPlatform) -> String {
    const LINUX_ONLY: &[&str] = &[
        "listen_audio",
        "pin_question",
        "previous_question",
        "next_question",
        "follow_latest",
        "scroll_answer_up",
        "scroll_answer_down",
    ];
    let mut out = String::new();
    match platform {
        ShortcutPlatform::X11 => {
            out.push_str(
                "# Out-of-focus global shortcuts for SocketStream on X11 (19 actions).\n\
                 # User-managed snippet (not installed or managed automatically by the app).\n\
                 # Save as ~/.config/sxhkd/sxhkdrc and run sxhkd. Requires xdotool.\n\
                 # Targets the main control window only, identified by its status title\n\
                 # (\"SocketStream — Stopped\"/\"Active\"/\"Stopping\"). The answer panel is\n\
                 # titled \"SocketStream — Practice Answer\" and is deliberately not matched.\n\
                 # SocketStream installs nothing by itself and grabs no keys.\n\
                 #\n",
            );
            for (action, value) in DEFAULT_BINDINGS {
                let display = socket_stream_core::shortcuts::Accelerator::parse(value)
                    .map(|a| a.display())
                    .unwrap_or_else(|_| value.to_owned());
                out.push_str(&format!("# {action}: {display}\n"));
                out.push_str(&format!("{}\n", sxhkd_keys(value)));
                out.push_str(&format!(
                    "    xdotool search --limit 1 --name '{X11_CONTROL_TITLE_PATTERN}' \
                     windowactivate --sync key --clearmodifiers {}\n",
                    xdotool_keys(value)
                ));
            }
        }
        ShortcutPlatform::Wayland => {
            out.push_str(
                "# Wayland: out-of-focus global shortcuts are owned by the compositor.\n\
                 # SocketStream installs no grab. Bind these keys in your desktop's own\n\
                 # settings, or use the org.freedesktop.portal.GlobalShortcuts portal when\n\
                 # your desktop implements it. The 19 in-app accelerators below then work\n\
                 # while the SocketStream control window or answer panel is focused:\n#\n",
            );
            push_binding_list(&mut out);
        }
        ShortcutPlatform::MacOs => {
            out.push_str(
                "# macOS: the SocketStream app registers these as NSMenu key equivalents\n\
                 # (active while the app is focused) and as a global key monitor while it\n\
                 # runs. It installs no login item and no background grab.\n\
                 # macOS currently wires 12 of the 19 actions. The seven Linux-only Q&A\n\
                 # actions are not implemented on macOS yet: listen_audio, pin_question,\n\
                 # previous_question, next_question, follow_latest, scroll_answer_up,\n\
                 # scroll_answer_down. Arrow keysyms\n\
                 # (Left/Right) and the aliases for them exist in the shared Rust/Python\n\
                 # model but are unsupported on the Swift side, so do not rebind shared\n\
                 # actions to arrows expecting them to work on macOS.\n#\n",
            );
            for (action, value) in DEFAULT_BINDINGS {
                let display = socket_stream_core::shortcuts::Accelerator::parse(value)
                    .map(|a| a.display())
                    .unwrap_or_else(|_| value.to_owned());
                if LINUX_ONLY.contains(&action) {
                    out.push_str(&format!(
                        "#   {action}: {display}  (Linux-only, not wired on macOS yet)\n"
                    ));
                } else {
                    out.push_str(&format!("#   {action}: {display}\n"));
                }
            }
        }
        ShortcutPlatform::Unknown => {
            out.push_str(
                "# Session type not detected; shortcuts work only while the SocketStream\n\
                 # main/control window is focused. No global grab is installed.\n#\n",
            );
            push_binding_list(&mut out);
        }
    }
    out.push('\n');
    out.push_str(
        "# SocketStream never installs a global hotkey grab silently and never bypasses OS permission.\n",
    );
    out
}

fn push_binding_list(out: &mut String) {
    for (action, value) in DEFAULT_BINDINGS {
        let display = socket_stream_core::shortcuts::Accelerator::parse(value)
            .map(|a| a.display())
            .unwrap_or_else(|_| value.to_owned());
        out.push_str(&format!("#   {action}: {display}\n"));
    }
}

/// Map a shared accelerator key name to its X keysym spelling.
///
/// sxhkd and xdotool both resolve keysyms through `XStringToKeysym`, which is
/// case-sensitive: `Left` is a keysym, `left` is not, and `enter` must be
/// `Return`. Letters, digits and the lowercase punctuation keysyms (`comma`,
/// `period`, `space`, ...) are already correct.
fn keysym(key: &str) -> String {
    match key {
        "left" => "Left".to_owned(),
        "right" => "Right".to_owned(),
        "up" => "Up".to_owned(),
        "down" => "Down".to_owned(),
        "enter" => "Return".to_owned(),
        "escape" => "Escape".to_owned(),
        "tab" => "Tab".to_owned(),
        "backspace" => "BackSpace".to_owned(),
        "equals" => "equal".to_owned(),
        other => match other.strip_prefix('f') {
            Some(digits) if !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit()) => {
                format!("F{digits}")
            }
            _ => other.to_owned(),
        },
    }
}

/// Render an accelerator in sxhkd syntax (`ctrl + alt + Left`).
fn sxhkd_keys(value: &str) -> String {
    value
        .split('+')
        .map(|part| {
            let part = part.trim().to_ascii_lowercase();
            match part.as_str() {
                "ctrl" | "alt" | "shift" | "meta" => part,
                _ => keysym(&part),
            }
        })
        .collect::<Vec<_>>()
        .join(" + ")
}

/// Render an accelerator for `xdotool key` (`ctrl+alt+Left`).
fn xdotool_keys(value: &str) -> String {
    value
        .split('+')
        .map(|part| {
            let part = part.trim().to_ascii_lowercase();
            match part.as_str() {
                "ctrl" | "alt" | "shift" | "meta" => part,
                _ => keysym(&part),
            }
        })
        .collect::<Vec<_>>()
        .join("+")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_owned()).collect()
    }

    #[test]
    fn parses_subcommands_with_or_without_leading_name() {
        assert_eq!(parse_command(&args(&["list"])), Ok(Command::List));
        assert_eq!(parse_command(&args(&["print"])), Ok(Command::Print));
        assert_eq!(
            parse_command(&args(&["init"])),
            Ok(Command::Init { force: false })
        );
        assert_eq!(
            parse_command(&args(&["init", "--force"])),
            Ok(Command::Init { force: true })
        );
        assert_eq!(parse_command(&[]), Ok(Command::Help));
        assert_eq!(parse_command(&args(&["--help"])), Ok(Command::Help));
    }

    #[test]
    fn run_accepts_the_leading_command_name() {
        // `run` must tolerate both ["hotkeys", "list"] and ["list"].
        let with_name = args(&["hotkeys", "print"]);
        let rest = match with_name.first().map(String::as_str) {
            Some("hotkeys") => &with_name[1..],
            _ => &with_name[..],
        };
        assert_eq!(parse_command(rest), Ok(Command::Print));
    }

    #[test]
    fn rejects_unknown_subcommands_and_flags() {
        assert!(parse_command(&args(&["bogus"])).is_err());
        assert!(parse_command(&args(&["list", "extra"])).is_err());
        assert!(parse_command(&args(&["init", "--wat"])).is_err());
        assert!(parse_command(&args(&["print", "--force"])).is_err());
    }

    #[test]
    fn snippets_are_platform_specific() {
        let x11 = snippet_for(ShortcutPlatform::X11);
        assert!(x11.contains("sxhkd"));
        assert!(x11.contains("ctrl + alt + s"));
        assert!(x11.contains("xdotool"));
        assert!(x11.contains("19 actions"));

        let wayland = snippet_for(ShortcutPlatform::Wayland);
        assert!(wayland.contains("GlobalShortcuts"));

        let macos = snippet_for(ShortcutPlatform::MacOs);
        assert!(macos.contains("NSMenu"));
        assert!(macos.contains("12 of the 19"));
        assert!(macos.contains("Linux-only"));

        for platform in [
            ShortcutPlatform::X11,
            ShortcutPlatform::Wayland,
            ShortcutPlatform::MacOs,
            ShortcutPlatform::Unknown,
        ] {
            let snippet = snippet_for(platform);
            assert!(snippet.contains("never installs a global hotkey grab"));
            assert!(snippet.contains("start: Ctrl+Alt+S"));
        }
    }

    #[test]
    fn x11_snippet_targets_control_title_not_the_answer_panel() {
        let x11 = snippet_for(ShortcutPlatform::X11);
        assert!(x11.contains(X11_CONTROL_TITLE_PATTERN));
        assert!(x11.contains("--name '^SocketStream — [A-Za-z][A-Za-z]*$'"));
        // The old broad match also found the answer panel.
        assert!(!x11.contains("--name \"SocketStream\""));
        assert!(!x11.contains("--name 'SocketStream'"));
        assert!(x11.contains("Practice Answer"));
    }

    #[test]
    fn x11_control_pattern_excludes_panel_title_and_matches_status_titles() {
        // The pattern is an anchored dash + single alphabetic word; assert the
        // literal shapes rather than compile a regex (std has none).
        assert!(X11_CONTROL_TITLE_PATTERN.starts_with("^SocketStream — "));
        assert!(X11_CONTROL_TITLE_PATTERN.ends_with('$'));
        assert!(X11_CONTROL_TITLE_PATTERN.contains("[A-Za-z]"));
        for status in ["Stopped", "Active", "Stopping"] {
            let title = format!("SocketStream — {status}");
            assert!(
                title
                    .strip_prefix("SocketStream — ")
                    .unwrap()
                    .chars()
                    .all(|c| c.is_ascii_alphabetic())
            );
        }
        assert!(
            "SocketStream — Practice Answer"
                .strip_prefix("SocketStream — ")
                .unwrap()
                .contains(' ')
        );
    }

    #[test]
    fn x11_snippet_uses_x_keysyms_for_named_keys() {
        let x11 = snippet_for(ShortcutPlatform::X11);
        assert!(x11.contains("ctrl + alt + Left"));
        assert!(x11.contains("ctrl + alt + Right"));
        assert!(x11.contains("key --clearmodifiers ctrl+alt+Left"));
        assert!(x11.contains("key --clearmodifiers ctrl+alt+Right"));
        assert!(!x11.contains("ctrl+alt+left"));
        assert!(!x11.contains("ctrl + alt + left"));
    }

    #[test]
    fn every_platform_snippet_is_valid_sxhkdrc_shaped() {
        for platform in [
            ShortcutPlatform::X11,
            ShortcutPlatform::Wayland,
            ShortcutPlatform::MacOs,
            ShortcutPlatform::Unknown,
        ] {
            let snippet = snippet_for(platform);
            for line in snippet.lines() {
                if line.trim().is_empty() {
                    continue;
                }
                if line.starts_with('#') || line.starts_with(' ') {
                    continue;
                }
                // Only sxhkd key-binding lines may be unindented and uncommented.
                assert!(
                    line.contains(" + "),
                    "unexpected non-comment sxhkdrc line for {platform:?}: {line:?}"
                );
            }
            let last = snippet
                .lines()
                .rfind(|line| !line.trim().is_empty())
                .expect("snippet has content");
            assert!(
                last.starts_with('#'),
                "final honesty line must be a comment: {last:?}"
            );
        }
    }

    #[test]
    fn sxhkd_renders_spaced_modifiers() {
        assert_eq!(sxhkd_keys("ctrl+alt+shift+o"), "ctrl + alt + shift + o");
        assert_eq!(sxhkd_keys("ctrl+alt+comma"), "ctrl + alt + comma");
    }

    #[test]
    fn sxhkd_and_xdotool_map_named_keysyms() {
        assert_eq!(sxhkd_keys("ctrl+alt+left"), "ctrl + alt + Left");
        assert_eq!(sxhkd_keys("ctrl+alt+right"), "ctrl + alt + Right");
        assert_eq!(sxhkd_keys("ctrl+alt+up"), "ctrl + alt + Up");
        assert_eq!(sxhkd_keys("ctrl+alt+down"), "ctrl + alt + Down");
        assert_eq!(sxhkd_keys("ctrl+f12"), "ctrl + F12");
        assert_eq!(sxhkd_keys("ctrl+alt+enter"), "ctrl + alt + Return");
        assert_eq!(sxhkd_keys("ctrl+alt+equals"), "ctrl + alt + equal");
        assert_eq!(sxhkd_keys("ctrl+alt+backspace"), "ctrl + alt + BackSpace");
        assert_eq!(xdotool_keys("ctrl+alt+left"), "ctrl+alt+Left");
        assert_eq!(xdotool_keys("ctrl+alt+shift+o"), "ctrl+alt+shift+o");
    }
}
