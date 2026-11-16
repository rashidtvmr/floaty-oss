//! OS accessibility text extraction is preferred to OCR.
//! User controls when the helper runs; it only reads a foreground app.
use crate::a11y::A11yTree;
use std::path::Path;
use std::process::Command;

#[cfg(unix)]
fn helper_output(
    mut command: Command,
    timeout: std::time::Duration,
) -> Result<std::process::Output, AccessibilityError> {
    use std::io::Read;
    use std::os::fd::AsRawFd;
    use std::os::unix::process::CommandExt;
    use std::process::Stdio;
    const OUTPUT_LIMIT: usize = 1024 * 1024;

    struct OwnedHelper(std::process::Child, bool);
    impl Drop for OwnedHelper {
        fn drop(&mut self) {
            if !self.1 {
                // Only the process group created for this helper is terminated.
                unsafe {
                    libc::kill(-(self.0.id() as i32), libc::SIGKILL);
                }
                let _ = self.0.kill();
                let _ = self.0.wait();
            }
        }
    }
    let mut child = OwnedHelper(
        command
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| AccessibilityError::Backend("cannot start accessibility helper".into()))?,
        false,
    );
    let mut stdout = child.0.stdout.take().expect("piped stdout");
    let mut stderr = child.0.stderr.take().expect("piped stderr");
    for fd in [stdout.as_raw_fd(), stderr.as_raw_fd()] {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
        if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
            return Err(AccessibilityError::Backend(
                "cannot monitor accessibility helper".into(),
            ));
        }
    }
    fn drain(
        reader: &mut impl Read,
        output: &mut Vec<u8>,
        limit: usize,
    ) -> Result<bool, AccessibilityError> {
        let mut buffer = [0; 4096];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => return Ok(true),
                Ok(size) => {
                    if output.len() + size > limit {
                        return Err(AccessibilityError::Backend(
                            "accessibility helper output limit exceeded".into(),
                        ));
                    }
                    output.extend_from_slice(&buffer[..size]);
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => return Ok(false),
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => {
                    return Err(AccessibilityError::Backend(
                        "accessibility helper pipe failed".into(),
                    ));
                }
            }
        }
    }
    let deadline = std::time::Instant::now() + timeout;
    let (mut out, mut err) = (Vec::new(), Vec::new());
    loop {
        let out_done = drain(&mut stdout, &mut out, OUTPUT_LIMIT)?;
        let err_done = drain(&mut stderr, &mut err, 16 * 1024)?;
        // Do not reap the parent while descendants still hold its pipes: its
        // reserved PID keeps the owned process-group identity unambiguous.
        if out_done && err_done {
            let status = child.0.try_wait().map_err(|_| {
                AccessibilityError::Backend("cannot wait for accessibility helper".into())
            })?;
            if let Some(status) = status {
                child.1 = true;
                return Ok(std::process::Output {
                    status,
                    stdout: out,
                    stderr: err,
                });
            }
        }
        if std::time::Instant::now() >= deadline {
            return Err(AccessibilityError::Backend("accessibility read timed out; focus the question window and retry, or enable authorized OCR fallback".into()));
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
}

fn run_helper(command: Command) -> Result<std::process::Output, AccessibilityError> {
    #[cfg(unix)]
    {
        helper_output(
            command,
            std::time::Duration::from_secs(if cfg!(target_os = "macos") { 15 } else { 6 }),
        )
    }
    #[cfg(not(unix))]
    {
        let _ = command;
        Err(AccessibilityError::Backend(
            "unsupported operating system".into(),
        ))
    }
}

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
    let mut command = accessibility_command(helper_root)?;
    command.args(["--mode", "text"]);
    let output = run_helper(command)?;
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
    let output = run_helper(command)?;
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

/// Cheap text-coverage signal, not a claim that a whole question is complete.
/// Accept short questions, code and non-space-delimited languages; reject empty,
/// punctuation-only and isolated-label readings. Structured callers should pass
/// role-free, content-scoped text so UI role names cannot satisfy this check.
/// Capturing screen pixels remains separately consent-gated.
pub fn needs_ocr(text: Option<&str>) -> bool {
    let Some(text) = text.map(str::trim).filter(|text| !text.is_empty()) else {
        return true;
    };
    let letters = text.chars().filter(|ch| ch.is_alphabetic()).count();
    let words = text
        .split_whitespace()
        .filter(|word| word.chars().any(char::is_alphanumeric))
        .count();
    let question = text.contains(['?', '？']) && letters >= 3;
    let code = letters >= 3
        && (text.contains(['{', '}', '=', ';']) || (text.contains('(') && text.contains(')')));
    let prose = letters >= 8 && words >= 3;
    let dense_script = text
        .chars()
        .filter(|ch| ch.is_alphabetic() && !ch.is_ascii())
        .count()
        >= 6;
    !(question || code || prose || dense_script)
}

#[cfg(test)]
mod tests {
    #[cfg(unix)]
    #[test]
    fn helper_timeout_and_output_limits_are_bounded() {
        let mut slow = std::process::Command::new("sh");
        slow.args(["-c", "sleep 10"]);
        let started = std::time::Instant::now();
        let error = super::helper_output(slow, std::time::Duration::from_millis(40)).unwrap_err();
        assert!(error.to_string().contains("timed out"));
        assert!(started.elapsed() < std::time::Duration::from_secs(2));
        let mut noisy = std::process::Command::new("sh");
        noisy.args([
            "-c",
            "while :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done",
        ]);
        let error = super::helper_output(noisy, std::time::Duration::from_secs(3)).unwrap_err();
        assert!(error.to_string().contains("output limit"));
    }
    use super::*;

    #[test]
    fn fallback_only_when_text_missing() {
        assert!(needs_ocr(None));
        assert!(needs_ocr(Some("")));
        assert!(needs_ocr(Some("tiny")));
        assert!(!needs_ocr(Some("a sufficiently long coding question here")));
    }

    #[test]
    fn content_coverage_accepts_short_questions_code_and_dense_scripts() {
        assert!(!needs_ocr(Some("Why?")));
        assert!(!needs_ocr(Some("return x + 1;")));
        assert!(!needs_ocr(Some("print(x)")));
        assert!(!needs_ocr(Some("求数组中最大的元素")));
        assert!(needs_ocr(Some(&". ".repeat(40))));
        assert!(needs_ocr(Some("123456789012345678901234567890")));
        assert!(needs_ocr(Some("Settings")));
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
