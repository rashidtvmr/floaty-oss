//! Locate bundled helpers without embedding the build machine's path.
use std::path::{Path, PathBuf};

pub fn project_root() -> PathBuf {
    if let Some(root) = std::env::var_os("SOCKET_STREAM_HOME") {
        let path = PathBuf::from(root);
        if path.is_dir() {
            return path;
        }
    }
    if let Ok(exe) = std::env::current_exe()
        && let Some(parent) = exe.parent()
    {
        for candidate in [parent, parent.parent().unwrap_or(parent)] {
            if has_helpers(candidate) {
                return candidate.to_path_buf();
            }
        }
        // macOS app bundle: Contents/MacOS/<exe> -> Contents/Resources.
        let resources = parent.join("../Resources");
        if has_helpers(&resources) {
            return resources;
        }
    }
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn has_helpers(root: &Path) -> bool {
    root.join("scripts/linux-accessibility.py").is_file()
        || root.join("scripts/macos-accessibility.swift").is_file()
}

/// Linux GI and DBus bindings are typically installed in the distro Python,
/// not necessarily in an activated virtual environment.
pub fn system_python() -> &'static str {
    if cfg!(target_os = "linux") && Path::new("/usr/bin/python3").is_file() {
        "/usr/bin/python3"
    } else {
        "python3"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cargo_checkout_has_helpers() {
        assert!(has_helpers(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../..")
                .as_path()
        ));
    }
}
