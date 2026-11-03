//! Thin launcher for the Python floating answer panel.
//! This command performs no capture and no inference; it only locates and
//! execs `apps/context-desktop/ui/floating_overlay.py`, forwarding arguments.
use std::path::{Path, PathBuf};
use std::process::Command;

/// Flags that take a value and are forwarded verbatim to the overlay script.
const VALUE_FLAGS: [&str; 6] = [
    "--provider",
    "--interval",
    "--cli",
    "--source",
    "--model",
    "--chunk-seconds",
];

/// Boolean flags forwarded verbatim to the overlay script.
const BOOLEAN_FLAGS: [&str; 5] = [
    "--allow-ocr",
    "--portal",
    "--stealth",
    "--no-stealth",
    "--compact",
];

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

/// `$CONTEXT_LENS_OVERLAY` wins; otherwise the bundled script under the repo root.
fn overlay_script_path() -> PathBuf {
    if let Some(custom) = std::env::var_os("CONTEXT_LENS_OVERLAY") {
        let path = PathBuf::from(custom);
        if !path.as_os_str().is_empty() {
            return path;
        }
    }
    context_core::paths::project_root().join("apps/context-desktop/ui/floating_overlay.py")
}

/// Prefer the distro interpreter when it exists; otherwise fall back to `python3`.
fn python_interpreter() -> String {
    let candidate = context_core::paths::system_python();
    if Path::new(candidate).is_file() {
        candidate.to_owned()
    } else {
        "python3".to_owned()
    }
}

pub fn run(args: &[String]) -> Result<(), String> {
    let script = overlay_script_path();
    if !script.is_file() {
        return Err(format!(
            "floating overlay script not found at {}; expected apps/context-desktop/ui/floating_overlay.py (set CONTEXT_LENS_OVERLAY to override)",
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
                "/usr/local/bin/context-cli",
                "--source",
                "microphone",
                "--model",
                "ggml-model.bin",
                "--chunk-seconds",
                "6",
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
                "/usr/local/bin/context-cli",
                "--source",
                "microphone",
                "--model",
                "ggml-model.bin",
                "--chunk-seconds",
                "6",
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
}
