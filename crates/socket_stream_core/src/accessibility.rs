//! OS accessibility text extraction is preferred to OCR.
//! User controls when the helper runs; it only reads a foreground app.
use crate::a11y::A11yTree;
use std::path::Path;
use std::process::Command;

#[derive(Debug, thiserror::Error)]
pub enum AccessibilityError {
    #[error("permission missing or accessibility backend not available: {0}")]
    Backend(String),
    #[error("accessibility extracted no text")]
    Empty,
    #[error("SocketStream is the foreground application")]
    OwnWindow,
}

/// Build the helper invocation for this platform without any arguments.
/// Shared by the text and tree readers so both use the same helper script.
fn accessibility_command(helper_root: &Path) -> Result<Command, AccessibilityError> {
    if cfg!(target_os = "macos") {
        // When launched by the native macOS app, run AX reads through that
        // app's own signed executable. TCC can grant Accessibility to the app
        // while rejecting a separately signed nested helper binary.
        if let Some(executable) = std::env::var_os("SOCKET_STREAM_MACOS_A11Y_EXECUTABLE")
            .filter(|path| Path::new(path).is_file())
        {
            let mut command = Command::new(executable);
            command.arg("--socket_stream_accessibility");
            return Ok(command);
        }
        let binary = helper_root.join("target/native-helpers/socket_stream_a11y");
        if binary.is_file() {
            Ok(Command::new(binary))
        } else {
            let helper = helper_root.join("scripts/macos-accessibility.swift");
            let mut command = Command::new("swift");
            command.arg(helper);
            Ok(command)
        }
    } else if cfg!(target_os = "linux") {
        let helper = helper_root.join("scripts/linux-accessibility.py");
        let mut command = Command::new(crate::paths::system_python());
        command.arg(helper);
        Ok(command)
    } else {
        Err(AccessibilityError::Backend(
            "unsupported operating system".into(),
        ))
    }
}

pub fn read_foreground_text(helper_root: &std::path::Path) -> Result<String, AccessibilityError> {
    let output = accessibility_command(helper_root)?
        .args(["--mode", "text"])
        .output()
        .map_err(|e| AccessibilityError::Backend(e.to_string()))?;
    if !output.status.success() {
        if output.status.code() == Some(4) {
            return Err(AccessibilityError::OwnWindow);
        }
        let reason = String::from_utf8_lossy(&output.stderr)
            .chars()
            .take(250)
            .collect();
        return Err(AccessibilityError::Backend(reason));
    }
    let text = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    if text.is_empty() {
        Err(AccessibilityError::Empty)
    } else {
        Ok(text)
    }
}

/// Read the foreground application as a structured accessibility tree.
/// Runs the same helper as [`read_foreground_text`] with tree-mode arguments.
pub fn read_foreground_tree(
    helper_root: &Path,
    max_nodes: usize,
    max_depth: usize,
    max_chars: usize,
) -> Result<A11yTree, AccessibilityError> {
    let mut command = accessibility_command(helper_root)?;
    command.args([
        "--mode",
        "tree",
        "--max-nodes",
        &max_nodes.to_string(),
        "--max-depth",
        &max_depth.to_string(),
        "--max-chars",
        &max_chars.to_string(),
    ]);
    let output = command
        .output()
        .map_err(|e| AccessibilityError::Backend(e.to_string()))?;
    if !output.status.success() {
        if output.status.code() == Some(4) {
            return Err(AccessibilityError::OwnWindow);
        }
        let reason = String::from_utf8_lossy(&output.stderr)
            .chars()
            .take(250)
            .collect();
        return Err(AccessibilityError::Backend(reason));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    let trimmed = stdout.trim();
    if trimmed.is_empty() {
        return Err(AccessibilityError::Empty);
    }
    A11yTree::parse(trimmed)
        .map_err(|_| AccessibilityError::Backend("invalid accessibility tree JSON".into()))
}

/// Calls accessibility reader first; runs image-based OCR only if needed.
/// Capturing screen pixels is separately consent-gated.
pub fn needs_ocr(text: Option<&str>) -> bool {
    text.is_none_or(|value| value.trim().chars().count() < 24)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fallback_only_when_text_missing() {
        assert!(needs_ocr(None));
        assert!(needs_ocr(Some("")));
        assert!(needs_ocr(Some("tiny")));
        assert!(!needs_ocr(Some("a sufficiently long coding question here")));
    }

    #[cfg(unix)]
    mod fake_helper {
        use super::*;
        use std::os::unix::fs::PermissionsExt;

        const JSON: &str = r#"{"schema":1,"source":"atspi","app":"Test","nodes":[{"id":1,"parent":null,"role":"frame","text":"hello"}]}"#;

        /// Write an executable fake helper at the path this platform uses.
        /// `shell` runs on macOS (native binary path); `python` runs on Linux.
        fn write_fake(root: &Path, shell: &str, python: &str) {
            if cfg!(target_os = "macos") {
                let dir = root.join("target/native-helpers");
                std::fs::create_dir_all(&dir).unwrap();
                let path = dir.join("socket_stream_a11y");
                std::fs::write(&path, format!("#!/bin/sh\n{shell}\n")).unwrap();
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
            } else {
                let dir = root.join("scripts");
                std::fs::create_dir_all(&dir).unwrap();
                let path = dir.join("linux-accessibility.py");
                std::fs::write(&path, format!("#!/usr/bin/env python3\n{python}\n")).unwrap();
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
            }
        }

        fn fake_json(root: &Path) {
            write_fake(
                root,
                &format!("printf '%s' '{JSON}'"),
                &format!("print(r'''{JSON}''')"),
            );
        }

        fn fake_script(root: &Path, shell: &str, python: &str) {
            write_fake(root, shell, python);
        }

        #[test]
        fn reads_and_parses_tree_json() {
            let dir = tempfile::tempdir().unwrap();
            fake_json(dir.path());
            let tree = read_foreground_tree(dir.path(), 50, 4, 1000).unwrap();
            assert_eq!(tree.schema, 1);
            assert_eq!(tree.source, "atspi");
            assert_eq!(tree.nodes.len(), 1);
            assert_eq!(tree.nodes[0].text, "hello");
        }

        #[test]
        fn exit_four_is_own_window() {
            let dir = tempfile::tempdir().unwrap();
            fake_script(dir.path(), "exit 4", "import sys\nsys.exit(4)");
            assert!(matches!(
                read_foreground_tree(dir.path(), 50, 4, 1000),
                Err(AccessibilityError::OwnWindow)
            ));
        }

        #[test]
        fn exit_three_is_reported_as_a_read_failure() {
            let dir = tempfile::tempdir().unwrap();
            fake_script(
                dir.path(),
                "printf 'read failed' >&2\nexit 3",
                "import sys\nsys.stderr.write('read failed')\nsys.exit(3)",
            );
            match read_foreground_tree(dir.path(), 50, 4, 1000) {
                Err(AccessibilityError::Backend(reason)) => assert_eq!(reason, "read failed"),
                other => panic!("expected Backend, got {other:?}"),
            }
        }

        #[test]
        fn non_zero_exit_reports_truncated_stderr() {
            let dir = tempfile::tempdir().unwrap();
            fake_script(
                dir.path(),
                "printf 'boom' >&2\nexit 2",
                "import sys\nsys.stderr.write('boom')\nsys.exit(2)",
            );
            match read_foreground_tree(dir.path(), 50, 4, 1000) {
                Err(AccessibilityError::Backend(reason)) => assert_eq!(reason, "boom"),
                other => panic!("expected Backend, got {other:?}"),
            }
        }

        #[test]
        fn empty_stdout_is_empty_error() {
            let dir = tempfile::tempdir().unwrap();
            fake_script(dir.path(), "exit 0", "import sys\nsys.exit(0)");
            assert!(matches!(
                read_foreground_tree(dir.path(), 50, 4, 1000),
                Err(AccessibilityError::Empty)
            ));
        }

        #[test]
        fn invalid_json_reports_backend() {
            let dir = tempfile::tempdir().unwrap();
            fake_script(dir.path(), "printf '%s' 'not json'", "print('not json')");
            match read_foreground_tree(dir.path(), 50, 4, 1000) {
                Err(AccessibilityError::Backend(reason)) => {
                    assert_eq!(reason, "invalid accessibility tree JSON")
                }
                other => panic!("expected Backend, got {other:?}"),
            }
        }
    }
}
