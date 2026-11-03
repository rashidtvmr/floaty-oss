//! Host capability probing; capture is never initiated here.
use std::process::Command;

pub fn command_exists(name: &str) -> bool {
    Command::new(name).arg("--version").output().is_ok()
}

pub fn display_name() -> &'static str {
    if cfg!(target_os = "macos") {
        "macOS"
    } else if cfg!(target_os = "linux") {
        "Linux"
    } else {
        "Unsupported OS"
    }
}

pub fn capture_capabilities() -> Vec<(&'static str, bool)> {
    let root = crate::paths::project_root();
    vec![
        ("tesseract", command_exists("tesseract")),
        ("ffmpeg", command_exists("ffmpeg")),
        ("whisper-cli", command_exists("whisper-cli")),
        ("codex", command_exists("codex")),
        ("ollama", command_exists("ollama")),
        (
            "macOS Vision OCR",
            cfg!(target_os = "macos")
                && root.join("target/native-helpers/context-lens-ocr").exists(),
        ),
        (
            "macOS system audio",
            cfg!(target_os = "macos")
                && root
                    .join("target/native-helpers/context-lens-system-audio")
                    .exists(),
        ),
        (
            "local English speech model",
            root.join("models/ggml-tiny.en.bin").exists(),
        ),
        ("screencapture (macOS)", cfg!(target_os = "macos")),
        ("grim (Wayland)", command_exists("grim")),
        ("import (X11)", command_exists("import")),
    ]
}
