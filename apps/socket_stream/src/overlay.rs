//! Thin launcher for the Python floating answer panel.
//! This command performs no capture and no inference; it only locates and
//! execs `apps/socket_stream_desktop/ui/floating_overlay.py`, forwarding arguments.
use std::path::{Path, PathBuf};
use std::process::Command;

/// Flags that take a value and are forwarded verbatim to the overlay script.
const VALUE_FLAGS: [&str; 7] = [
    "--provider",
    "--interval",
    "--cli",
    "--source",
    "--model",
    "--chunk-seconds",
    "--opacity",
];

/// Boolean flags forwarded verbatim to the overlay script.
const BOOLEAN_FLAGS: [&str; 5] = [
    "--allow-ocr",
    "--portal",
    "--stealth",
    "--no-stealth",
    "--compact",
];

/// Value flags the macOS panel-only app understands.
const MACOS_VALUE_FLAGS: [&str; 4] = ["--provider", "--interval", "--opacity", "--cli"];

/// Boolean flags the macOS panel-only app understands.
const MACOS_BOOLEAN_FLAGS: [&str; 4] = ["--allow-ocr", "--stealth", "--no-stealth", "--compact"];

/// Default provider for real answers, matching the desktop GUI default.
const DEFAULT_PROVIDER: &str = "ollama";

/// Forward only the flags the overlay understands, defaulting the provider.
/// Unknown flags and bare values are dropped so nothing is invented.
pub fn overlay_arguments(args: &[String]) -> Vec<String> {
    let mut forwarded = Vec::new();
    if !args.iter().any(|arg| arg == "--provider") {
        forwarded.push("--provider".to_owned());
        forwarded.push(DEFAULT_PROVIDER.to_owned());
    }
    let mut index = 0;
    while index < args.len() {
        let arg = args[index].as_str();
        if VALUE_FLAGS.contains(&arg) {
            forwarded.push(arg.to_owned());
            if let Some(value) = args.get(index + 1) {
                forwarded.push(value.clone());
                index += 1;
            }
        } else if BOOLEAN_FLAGS.contains(&arg) {
            forwarded.push(arg.to_owned());
        }
        index += 1;
    }
    forwarded
}

/// Forward only the flags the macOS panel understands. The AppKit panel has
/// no GTK portal or audio-source flags, so those are not invented.
pub fn macos_overlay_arguments(args: &[String]) -> Vec<String> {
    let mut forwarded = vec!["--panel-only".to_owned()];
    if !args.iter().any(|arg| arg == "--provider") {
        forwarded.push("--provider".to_owned());
        forwarded.push(DEFAULT_PROVIDER.to_owned());
    }
    let mut index = 0;
    while index < args.len() {
        let arg = args[index].as_str();
        if MACOS_VALUE_FLAGS.contains(&arg) {
            forwarded.push(arg.to_owned());
            if let Some(value) = args.get(index + 1) {
                forwarded.push(value.clone());
                index += 1;
            }
        } else if MACOS_BOOLEAN_FLAGS.contains(&arg) {
            forwarded.push(arg.to_owned());
        }
        index += 1;
    }
    forwarded
}

/// `$SOCKET_STREAM_OVERLAY` wins; otherwise the bundled script under the repo root.
fn overlay_script_path() -> PathBuf {
    if let Some(custom) = std::env::var_os("SOCKET_STREAM_OVERLAY") {
        let path = PathBuf::from(custom);
        if !path.as_os_str().is_empty() {
            return path;
        }
    }
    socket_stream_core::paths::project_root()
        .join("apps/socket_stream_desktop/ui/floating_overlay.py")
}

/// The AppKit GUI: bundle sibling, dist bundle, or the development helper.
fn macos_gui_path() -> Result<PathBuf, String> {
    if let Some(custom) = std::env::var_os("SOCKET_STREAM_GUI") {
        let path = PathBuf::from(custom);
        if path.is_file() {
            return Ok(path);
        }
    }
    if let Ok(exe) = std::env::current_exe()
        && let Some(parent) = exe.parent()
    {
        // CLI installed next to the app executable inside Contents/MacOS.
        let sibling = parent.join("SocketStream");
        if sibling.is_file() {
            return Ok(sibling);
        }
    }
    let root = socket_stream_core::paths::project_root();
    for candidate in [
        root.join("dist/SocketStream.app/Contents/MacOS/SocketStream"),
        root.join("target/native-helpers/socket_stream_gui"),
    ] {
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    Err("macOS panel not found; run scripts/build-macos-app.sh (or set SOCKET_STREAM_GUI)".into())
}

/// Prefer the distro interpreter when it exists; otherwise fall back to `python3`.
fn python_interpreter() -> String {
    let candidate = socket_stream_core::paths::system_python();
    if Path::new(candidate).is_file() {
        candidate.to_owned()
    } else {
        "python3".to_owned()
    }
}

pub fn run(args: &[String]) -> Result<(), String> {
    if cfg!(target_os = "macos") {
        let gui = macos_gui_path()?;
        let status = Command::new(gui)
            .args(macos_overlay_arguments(args))
            .status()
            .map_err(|error| format!("cannot launch the macOS answer panel: {error}"))?;
        return match status.code() {
            Some(0) => Ok(()),
            Some(code) => Err(format!("macOS answer panel exited with exit code {code}")),
            None => Err("macOS answer panel was terminated by a signal".to_owned()),
        };
    }
    let script = overlay_script_path();
    if !script.is_file() {
        return Err(format!(
            "floating overlay script not found at {}; expected apps/socket_stream_desktop/ui/floating_overlay.py (set SOCKET_STREAM_OVERLAY to override)",
            script.display()
        ));
    }
    let status = Command::new(python_interpreter())
        .arg(&script)
        .args(overlay_arguments(args))
        .status()
        .map_err(|error| format!("cannot launch floating overlay: {error}"))?;
    match status.code() {
        Some(0) => Ok(()),
        Some(code) => Err(format!("floating overlay exited with exit code {code}")),
        None => Err("floating overlay was terminated by a signal".to_owned()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    #[test]
    fn provider_defaults_to_ollama_when_absent() {
        assert_eq!(
            overlay_arguments(&args(&[])),
            args(&["--provider", "ollama"])
        );
        assert_eq!(
            overlay_arguments(&args(&["--compact"])),
            args(&["--provider", "ollama", "--compact"])
        );
    }

    #[test]
    fn explicit_provider_is_forwarded_unchanged() {
        assert_eq!(
            overlay_arguments(&args(&["--provider", "codex"])),
            args(&["--provider", "codex"])
        );
    }

    #[test]
    fn known_flags_and_values_are_forwarded() {
        assert_eq!(
            overlay_arguments(&args(&[
                "--provider",
                "ollama",
                "--interval",
                "4",
                "--allow-ocr",
                "--portal",
                "--stealth",
                "--no-stealth",
                "--compact",
                "--cli",
                "/usr/local/bin/socket_stream",
                "--source",
                "microphone",
                "--model",
                "ggml-model.bin",
                "--chunk-seconds",
                "6",
                "--opacity",
                "0.9",
            ])),
            args(&[
                "--provider",
                "ollama",
                "--interval",
                "4",
                "--allow-ocr",
                "--portal",
                "--stealth",
                "--no-stealth",
                "--compact",
                "--cli",
                "/usr/local/bin/socket_stream",
                "--source",
                "microphone",
                "--model",
                "ggml-model.bin",
                "--chunk-seconds",
                "6",
                "--opacity",
                "0.9",
            ])
        );
    }

    #[test]
    fn unknown_flags_and_bare_values_are_not_forwarded() {
        assert_eq!(
            overlay_arguments(&args(&[
                "--authorized",
                "--bogus",
                "value",
                "--provider",
                "ollama",
            ])),
            args(&["--provider", "ollama"])
        );
    }

    #[test]
    fn macos_panel_mode_defaults_provider_and_drops_linux_only_flags() {
        assert_eq!(
            macos_overlay_arguments(&args(&["--portal", "--cli", "/tmp/socket_stream"])),
            args(&[
                "--panel-only",
                "--provider",
                "ollama",
                "--cli",
                "/tmp/socket_stream"
            ])
        );
    }

    #[test]
    fn macos_panel_mode_forwards_supported_flags() {
        assert_eq!(
            macos_overlay_arguments(&args(&[
                "--provider",
                "mock",
                "--interval",
                "6",
                "--allow-ocr",
                "--no-stealth",
                "--compact",
                "--opacity",
                "0.9",
            ])),
            args(&[
                "--panel-only",
                "--provider",
                "mock",
                "--interval",
                "6",
                "--allow-ocr",
                "--no-stealth",
                "--compact",
                "--opacity",
                "0.9",
            ])
        );
    }
}
