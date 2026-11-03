//! Offline Whisper integration for consented practice recordings.
//! This module does not capture background audio or enable microphones by itself.
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Clone, Debug)]
pub struct WhisperConfig {
    pub executable: PathBuf,
    pub model: PathBuf,
}

#[derive(Debug, thiserror::Error)]
pub enum TranscriptionError {
    #[error("missing audio file or whisper model")]
    MissingInput,
    #[error("whisper failed: {0}")]
    Process(String),
    #[error("transcription output is not valid UTF-8")]
    Utf8,
}

pub fn transcribe(config: &WhisperConfig, audio_path: &Path) -> Result<String, TranscriptionError> {
    if !audio_path.is_file() || !config.model.is_file() {
        return Err(TranscriptionError::MissingInput);
    }
    let output = Command::new(&config.executable)
        .args(["-m"])
        .arg(&config.model)
        .args(["-f"])
        .arg(audio_path)
        .args(["-nt"])
        .output()
        .map_err(|e| TranscriptionError::Process(e.to_string()))?;
    if !output.status.success() {
        return Err(TranscriptionError::Process(
            String::from_utf8_lossy(&output.stderr)
                .chars()
                .take(300)
                .collect(),
        ));
    }
    String::from_utf8(output.stdout).map_err(|_| TranscriptionError::Utf8)
}

/// Record a user-chosen audio device. Call only after explicit consent.
/// Separate from transcription to make collection and cloud processing auditable.
#[derive(Debug, thiserror::Error)]
pub enum RecordingError {
    #[error("explicit authorization required")]
    PermissionDenied,
    #[error("recording duration must be between 1 and 900 seconds")]
    InvalidDuration,
    #[error("output already exists")]
    AlreadyExists,
    #[error("invalid device name")]
    InvalidDevice,
    #[error("ffmpeg recording failed: {0}")]
    Failed(String),
}

pub fn record_audio(
    authorized: bool,
    device: &str,
    duration_seconds: u32,
    output_path: &Path,
) -> Result<(), RecordingError> {
    if !authorized {
        return Err(RecordingError::PermissionDenied);
    }
    if !(1..=900).contains(&duration_seconds) {
        return Err(RecordingError::InvalidDuration);
    }
    if device.trim().is_empty() || device.contains('\0') {
        return Err(RecordingError::InvalidDevice);
    }
    if output_path.exists() {
        return Err(RecordingError::AlreadyExists);
    }
    let (format, source) = if cfg!(target_os = "macos") {
        ("avfoundation", format!(":{device}"))
    } else if cfg!(target_os = "linux") {
        ("pulse", device.to_string())
    } else {
        return Err(RecordingError::Failed(
            "unsupported operating system".into(),
        ));
    };
    let result = Command::new("ffmpeg")
        .args(["-nostdin", "-hide_banner", "-loglevel", "error", "-n"])
        .args(["-f", format, "-i", source.as_str()])
        .args([
            "-t",
            &duration_seconds.to_string(),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
        ])
        .arg(output_path)
        .output()
        .map_err(|e| RecordingError::Failed(e.to_string()))?;
    if !result.status.success() {
        return Err(RecordingError::Failed(
            String::from_utf8_lossy(&result.stderr)
                .chars()
                .take(350)
                .collect(),
        ));
    }
    Ok(())
}

/// List available audio inputs without recording.
pub fn list_audio_sources() -> Result<String, RecordingError> {
    let output = if cfg!(target_os = "macos") {
        Command::new("ffmpeg")
            .args([
                "-hide_banner",
                "-f",
                "avfoundation",
                "-list_devices",
                "true",
                "-i",
                "",
            ])
            .output()
    } else if cfg!(target_os = "linux") {
        Command::new("pactl")
            .args(["list", "short", "sources"])
            .output()
    } else {
        return Err(RecordingError::Failed(
            "unsupported operating system".into(),
        ));
    }
    .map_err(|e| RecordingError::Failed(e.to_string()))?;
    if cfg!(target_os = "macos") {
        // AVFoundation device listing normally exits non-zero.
        let stderr = String::from_utf8_lossy(&output.stderr);
        let mut is_audio = false;
        let mut lines = Vec::new();
        for line in stderr.lines() {
            if line.contains("AVFoundation audio devices:") {
                is_audio = true;
                continue;
            }
            if is_audio && line.contains("AVFoundation video devices:") {
                break;
            }
            if is_audio && line.contains("] [") {
                lines.push(line.to_owned());
            }
        }
        if lines.is_empty() {
            return Err(RecordingError::Failed(
                "no AVFoundation audio sources reported".into(),
            ));
        }
        return Ok(lines.join("\n"));
    }
    if !output.status.success() {
        return Err(RecordingError::Failed(
            "pactl could not list audio sources".into(),
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// Record system playback from an explicitly authorized session to a WAV file.
/// On macOS ScreenCaptureKit requests system permission. On Linux, PulseAudio /
/// PipeWire's selected sink monitor is used, without bypassing desktop controls.
pub fn record_system_audio(
    authorized: bool,
    duration_seconds: u32,
    output_path: &Path,
    linux_monitor: Option<&str>,
) -> Result<(), RecordingError> {
    if !authorized {
        return Err(RecordingError::PermissionDenied);
    }
    if !(1..=900).contains(&duration_seconds) {
        return Err(RecordingError::InvalidDuration);
    }
    if output_path.exists() {
        return Err(RecordingError::AlreadyExists);
    }
    if cfg!(target_os = "macos") {
        let root = crate::paths::project_root();
        let helper = root.join("target/native-helpers/context-lens-system-audio");
        if !helper.is_file() {
            return Err(RecordingError::Failed(
                "macOS system audio helper unavailable; run scripts/build-macos-helpers.sh".into(),
            ));
        }
        let temp = tempfile::tempdir().map_err(|e| RecordingError::Failed(e.to_string()))?;
        let intermediate = temp.path().join("system.m4a");
        let result = Command::new(helper)
            .arg(duration_seconds.to_string())
            .arg(&intermediate)
            .output()
            .map_err(|e| RecordingError::Failed(e.to_string()))?;
        if !result.status.success() {
            return Err(RecordingError::Failed(
                String::from_utf8_lossy(&result.stderr)
                    .chars()
                    .take(300)
                    .collect(),
            ));
        }
        let converted = Command::new("ffmpeg")
            .args(["-nostdin", "-hide_banner", "-loglevel", "error", "-n", "-i"])
            .arg(&intermediate)
            .args(["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le"])
            .arg(output_path)
            .output()
            .map_err(|e| RecordingError::Failed(e.to_string()))?;
        if !converted.status.success() {
            return Err(RecordingError::Failed(
                String::from_utf8_lossy(&converted.stderr)
                    .chars()
                    .take(300)
                    .collect(),
            ));
        }
        return Ok(());
    }
    if cfg!(target_os = "linux") {
        let monitor = match linux_monitor {
            Some(source) => source.to_owned(),
            None => {
                let output = Command::new("pactl")
                    .arg("get-default-sink")
                    .output()
                    .map_err(|e| RecordingError::Failed(format!("pactl unavailable: {e}")))?;
                if !output.status.success() {
                    return Err(RecordingError::Failed(
                        "default audio sink not found".into(),
                    ));
                }
                let sink = String::from_utf8_lossy(&output.stdout).trim().to_owned();
                if sink.is_empty() {
                    return Err(RecordingError::InvalidDevice);
                }
                format!("{sink}.monitor")
            }
        };
        return record_audio(true, &monitor, duration_seconds, output_path);
    }
    Err(RecordingError::Failed(
        "unsupported operating system".into(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recording_requires_permission() {
        assert!(matches!(
            record_audio(false, "0", 10, Path::new("/tmp/context-lens-no-record.wav")),
            Err(RecordingError::PermissionDenied)
        ));
        assert!(matches!(
            record_audio(true, "0", 0, Path::new("/tmp/context-lens-no-record.wav")),
            Err(RecordingError::InvalidDuration)
        ));
        assert!(matches!(
            record_system_audio(
                false,
                10,
                Path::new("/tmp/context-lens-no-system.wav"),
                None
            ),
            Err(RecordingError::PermissionDenied)
        ));
    }

    #[test]
    fn require_existing_model_and_audio() {
        let cfg = WhisperConfig {
            executable: "whisper-cli".into(),
            model: "/does/not/exist.bin".into(),
        };
        assert!(matches!(
            transcribe(&cfg, Path::new("/does/not/exist.wav")),
            Err(TranscriptionError::MissingInput)
        ));
    }
}
