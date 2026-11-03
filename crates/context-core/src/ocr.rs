//! Offline Tesseract integration. No image bytes are sent to a cloud service.
use std::path::Path;
use std::process::Command;

#[derive(Debug, thiserror::Error)]
pub enum OcrError {
    #[error("OCR input path does not exist")]
    MissingInput,
    #[error("OCR command failed: {0}")]
    Process(String),
    #[error("OCR result was not UTF-8")]
    Utf8,
}

pub fn extract_text(path: &Path) -> Result<String, OcrError> {
    if !path.is_file() {
        return Err(OcrError::MissingInput);
    }
    let output = if cfg!(target_os = "macos") {
        // macOS Vision OCR works offline and requires no Tesseract installation.
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let binary = root.join("target/native-helpers/context-lens-ocr");
        if binary.is_file() {
            Command::new(binary).arg(path).output()
        } else {
            Command::new("swift")
                .arg(root.join("scripts/macos-ocr.swift"))
                .arg(path)
                .output()
        }
    } else {
        let psm = std::env::var("CONTEXT_LENS_TESSERACT_PSM")
            .ok()
            .filter(|value| matches!(value.as_str(), "3" | "6" | "11" | "13"))
            .unwrap_or_else(|| "6".into());
        Command::new("tesseract")
            .arg(path)
            .args(["stdout", "--psm", psm.as_str()])
            .output()
    }
    .map_err(|e| OcrError::Process(e.to_string()))?;
    if !output.status.success() {
        return Err(OcrError::Process(
            String::from_utf8_lossy(&output.stderr)
                .chars()
                .take(300)
                .collect(),
        ));
    }
    String::from_utf8(output.stdout).map_err(|_| OcrError::Utf8)
}

/// Detect simple syntax incompleteness before sending extracted code to a model.
/// Conservative: strings/comments may contain punctuation, so warnings aren't definitive.
pub fn quality_warnings(text: &str) -> Vec<String> {
    let mut warnings = Vec::new();
    if text.trim().is_empty() {
        warnings.push("OCR returned no readable text".to_owned());
        return warnings;
    }
    let pairs = [('{', '}'), ('(', ')'), ('[', ']')];
    for (left, right) in pairs {
        let opens = text.chars().filter(|c| *c == left).count();
        let closes = text.chars().filter(|c| *c == right).count();
        if opens != closes {
            warnings.push(format!(
                "Possible missing or misrecognized bracket: {left} count={opens}, {right} count={closes}"
            ));
        }
    }
    if text.contains('�') {
        warnings.push("OCR contains replacement characters".to_owned());
    }
    warnings
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn detects_unbalanced_brackets() {
        let warnings = quality_warnings("function a() { return 1;");
        assert!(warnings.iter().any(|w| w.contains("bracket")));
        assert!(quality_warnings("function a() { return 1; }").is_empty());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn native_vision_reads_golden_fixture() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/sample-code.png");
        let text = extract_text(&path).expect("native Vision OCR should run on a provided image");
        assert!(text.contains("twoSum"), "unexpected OCR: {text}");
        assert!(text.contains("target"), "unexpected OCR: {text}");
    }

    #[test]
    fn missing_image_cannot_process() {
        assert!(matches!(
            extract_text(Path::new("/does/not/exist.png")),
            Err(OcrError::MissingInput)
        ));
    }
}
