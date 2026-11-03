//! `context-cli hotkeys` — inspect, initialise and print the shortcut setup.
//!
//! See `docs/A11Y-TREE-SHORTCUTS.md` section 3.3. This command never installs
//! a global hotkey grab: `print` only emits a registration snippet for the
//! user's own desktop tool.
use context_core::config::Config;
use context_core::shortcuts::{
    DEFAULT_BINDINGS, ShortcutMap, ShortcutPlatform, detect_platform, platform_notes,
};

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
                    other => return Err(format!("unexpected argument for 'hotkeys init': {other}")),
                }
            }
            Ok(Command::Init { force })
        }
        "help" | "--help" | "-h" => Ok(Command::Help),
        other => Err(format!("unknown hotkeys subcommand: {other}")),
    }
}

fn print_usage() {
    println!("ContextLens shortcuts");
    println!("  context-cli hotkeys list          show the resolved bindings and platform");
    println!("  context-cli hotkeys init [--force] write a default config file");
    println!("  context-cli hotkeys print         print a registration snippet for your desktop");
    println!();
    println!("ContextLens never installs a global hotkey grab by itself.");
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
    println!("ContextLens shortcuts — platform: {}", platform_name(platform));
    println!(
        "enabled: {}",
        if config.shortcuts.enabled { "yes" } else { "no" }
    );
    println!();
    for binding in &map.bindings {
        println!("  {:<18} {}", binding.action, binding.accelerator.display());
    }
    println!();
    println!("Platform guidance:");
    // `platform_notes` emits one line per default binding first; only the
    // trailing platform-specific lines are useful next to the resolved table.
    for note in platform_notes(platform).into_iter().skip(DEFAULT_BINDINGS.len()) {
        println!("  {note}");
    }
    Ok(())
}

fn init(force: bool) -> Result<(), String> {
    let path = Config::default_path();
    if path.exists() && !force {
        return Err(format!(
            "{} already exists; re-run with --force to overwrite it",
            path.display()
        ));
    }
    let config = Config::default();
    config.save().map_err(|e| e.to_string())?;
    println!("Wrote default configuration to {}", path.display());
    println!(
        "Shortcuts are enabled by default. Rebind under \"shortcuts\".\"bindings\" \
         (see docs/SHORTCUTS.md)."
    );
    Ok(())
}

/// The platform-native registration snippet. Pure so it can be unit tested.
fn snippet_for(platform: ShortcutPlatform) -> String {
    let mut out = String::new();
    match platform {
        ShortcutPlatform::X11 => {
            out.push_str(
                "# Out-of-focus global shortcuts for ContextLens on X11.\n\
                 # Save as ~/.config/sxhkd/sxhkdrc and run sxhkd. Requires xdotool.\n\
                 # ContextLens installs nothing by itself and grabs no keys.\n\
                 #\n",
            );
            for (action, value) in DEFAULT_BINDINGS {
                let display = context_core::shortcuts::Accelerator::parse(value)
                    .map(|a| a.display())
                    .unwrap_or_else(|_| value.to_owned());
                out.push_str(&format!("# {action}\n"));
                out.push_str(&format!("{}\n", sxhkd_keys(value)));
                out.push_str(&format!(
                    "    xdotool search --name \"ContextLens\" windowactivate --sync key --clearmodifiers {}\n",
                    display.to_ascii_lowercase()
                ));
            }
        }
        ShortcutPlatform::Wayland => {
            out.push_str(
                "# Wayland: out-of-focus global shortcuts are owned by the compositor.\n\
                 # ContextLens installs no grab. Bind these keys in your desktop's own\n\
                 # settings, or use the org.freedesktop.portal.GlobalShortcuts portal when\n\
                 # your desktop implements it. The in-app accelerators below then work\n\
                 # while the ContextLens window is focused:\n#\n",
            );
            push_binding_list(&mut out);
        }
        ShortcutPlatform::MacOs => {
            out.push_str(
                "# macOS: the ContextLens app registers these as NSMenu key equivalents\n\
                 # (active while the app is focused) and as a global key monitor while it\n\
                 # runs. It installs no login item and no background grab.\n#\n",
            );
            push_binding_list(&mut out);
        }
        ShortcutPlatform::Unknown => {
            out.push_str(
                "# Session type not detected; shortcuts work only while the ContextLens\n\
                 # window is focused. No global grab is installed.\n#\n",
            );
            push_binding_list(&mut out);
        }
    }
    out.push('\n');
    out.push_str(
        "ContextLens never installs a global hotkey grab silently and never bypasses OS permission.\n",
    );
    out
}

fn push_binding_list(out: &mut String) {
    for (action, value) in DEFAULT_BINDINGS {
        let display = context_core::shortcuts::Accelerator::parse(value)
            .map(|a| a.display())
            .unwrap_or_else(|_| value.to_owned());
        out.push_str(&format!("#   {action}: {display}\n"));
    }
}

/// Render an accelerator in sxhkd syntax (`ctrl + alt + s`).
fn sxhkd_keys(value: &str) -> String {
    value
        .split('+')
        .map(|part| part.trim().to_ascii_lowercase())
        .collect::<Vec<_>>()
        .join(" + ")
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

        let wayland = snippet_for(ShortcutPlatform::Wayland);
        assert!(wayland.contains("GlobalShortcuts"));

        let macos = snippet_for(ShortcutPlatform::MacOs);
        assert!(macos.contains("NSMenu"));

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
    fn sxhkd_renders_spaced_modifiers() {
        assert_eq!(sxhkd_keys("ctrl+alt+shift+o"), "ctrl + alt + shift + o");
        assert_eq!(sxhkd_keys("ctrl+alt+comma"), "ctrl + alt + comma");
    }
}
