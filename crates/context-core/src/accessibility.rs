//! OS accessibility text extraction is preferred to OCR.
//! User controls when the helper runs; it only reads a foreground app.
use std::process::Command;

#[derive(Debug, thiserror::Error)]
pub enum AccessibilityError {
    #[error("permission missing or accessibility backend not available: {0}")]
    Backend(String),
    #[error("accessibility extracted no text")]
    Empty,
    #[error("ContextLens is the foreground application")]
    OwnWindow,
}

pub fn read_foreground_text(helper_root: &std::path::Path) -> Result<String, AccessibilityError> {
    let command_result = if cfg!(target_os = "macos") {
        let binary = helper_root.join("target/native-helpers/context-lens-a11y");
        if binary.is_file() {
            Command::new(binary).output()
        } else {
            let helper = helper_root.join("scripts/macos-accessibility.swift");
            Command::new("swift").arg(helper).output()
        }
    } else if cfg!(target_os = "linux") {
        let helper = helper_root.join("scripts/linux-accessibility.py");
        Command::new(crate::paths::system_python())
            .arg(helper)
            .output()
    } else {
        return Err(AccessibilityError::Backend(
            "unsupported operating system".into(),
        ));
    };
    let output = command_result.map_err(|e| AccessibilityError::Backend(e.to_string()))?;
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
}
