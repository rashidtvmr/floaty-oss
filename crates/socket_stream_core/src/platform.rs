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
    capability_groups()
        .into_iter()
        .flat_map(|(_, entries)| entries)
        .collect()
}

/// Capabilities grouped by relevance, so `doctor` can label what actually
/// matters on the running platform. Missing entries in later groups are
/// expected and are not problems.
pub fn capability_groups() -> Vec<(&'static str, Vec<(&'static str, bool)>)> {
    let root = crate::paths::project_root();
    let core = vec![
        ("tesseract (OCR)", command_exists("tesseract")),
        ("ffmpeg (audio capture)", command_exists("ffmpeg")),
        (
            "local English speech model",
            root.join("models/ggml-tiny.en.bin").exists(),
        ),
        (
            "macOS Vision OCR",
            cfg!(target_os = "macos")
                && root
                    .join("target/native-helpers/socket_stream_ocr")
                    .exists(),
        ),
        (
            "macOS system audio",
            cfg!(target_os = "macos")
                && root
                    .join("target/native-helpers/socket_stream_system_audio")
                    .exists(),
        ),
    ];
    let providers = vec![
        ("codex", command_exists("codex")),
        ("ollama", command_exists("ollama")),
    ];
    let optional = vec![
        (
            "whisper-cli (legacy STT fallback)",
            command_exists("whisper-cli"),
        ),
        ("screencapture (macOS only)", cfg!(target_os = "macos")),
        ("grim (Wayland screenshot)", command_exists("grim")),
        ("import (X11 screenshot)", command_exists("import")),
    ];
    vec![
        ("core capture (this platform)", core),
        ("AI providers", providers),
        ("optional / other platforms (safe to miss)", optional),
    ]
}
