//! Explicit, repeated audio practice: bounded segments, offline Whisper, no persistence.
//! This is segmented listening, not full streaming ASR or speaker diarization.
use crate::live_events::Emitter;
use crate::turn_gate::{SpokenTranscript, clean_spoken_segment, with_spoken_context};
use socket_stream_core::ai::{Provider, answer};
use socket_stream_core::audio::{WhisperConfig, record_audio, record_system_audio, transcribe};
use socket_stream_core::context::Deduplicator;
use std::path::{Path, PathBuf};
use tokio::time::Duration;

fn option(args: &[String], flag: &str) -> Option<String> {
    args.windows(2)
        .find(|pair| pair[0] == flag)
        .map(|pair| pair[1].clone())
}

fn bounded(args: &[String], flag: &str, default: u32, min: u32, max: u32) -> Result<u32, String> {
    let value = option(args, flag)
        .map(|text| text.parse::<u32>().map_err(|_| format!("invalid {flag}")))
        .transpose()?
        .unwrap_or(default);
    if !(min..=max).contains(&value) {
        return Err(format!("{flag} must be {min}..={max}"));
    }
    Ok(value)
}

fn looks_like_question(text: &str) -> bool {
    let lower = text.trim().to_ascii_lowercase();
    lower.len() >= 20
        && lower.len() <= 60_000
        && (lower.contains('?')
            || [
                "what ",
                "why ",
                "how ",
                "explain ",
                "describe ",
                "implement ",
                "can you ",
                "could you ",
                "tell me ",
                "write ",
            ]
            .iter()
            .any(|prefix| lower.starts_with(prefix)))
}

/// A cheap silence filter on 16-bit mono PCM WAV, not speaker classification.
/// Whisper is invoked only when audio energy exceeds a low threshold.
fn has_audio_energy(path: &Path) -> bool {
    let Ok(data) = std::fs::read(path) else {
        return false;
    };
    if data.len() < 44 || &data[..4] != b"RIFF" || &data[8..12] != b"WAVE" {
        return false;
    }
    let mut offset = 12_usize;
    while offset + 8 <= data.len() {
        let size = u32::from_le_bytes(data[offset + 4..offset + 8].try_into().unwrap()) as usize;
        let start = offset + 8;
        if &data[offset..offset + 4] == b"data" {
            let end = start.saturating_add(size).min(data.len());
            let samples: Vec<f64> = data[start..end]
                .as_chunks::<2>()
                .0
                .iter()
                .step_by(16)
                .map(|bytes| i16::from_le_bytes([bytes[0], bytes[1]]) as f64 / 32768.0)
                .collect();
            if samples.is_empty() {
                return false;
            }
            let mean_square = samples.iter().map(|v| v * v).sum::<f64>() / samples.len() as f64;
            return mean_square > 0.00002;
        }
        offset = start.saturating_add(size + size % 2);
    }
    false
}

fn capture_segment(
    source: &str,
    device: &str,
    seconds: u32,
    wav: &Path,
) -> Result<(), &'static str> {
    let result = if source == "microphone" {
        record_audio(true, device, seconds, wav)
    } else {
        record_system_audio(
            true,
            seconds,
            wav,
            if device == "default" {
                None
            } else {
                Some(device)
            },
        )
    };
    result.map_err(|_| "audio input or OS permission unavailable")
}

pub async fn run(args: &[String], provider: Provider) -> Result<(), String> {
    match option(args, "--stt").as_deref().unwrap_or("moonshine") {
        "moonshine" => return crate::moonshine_live::run(args, provider).await,
        "whisper" => {}
        _ => return Err("--stt must be moonshine or whisper".into()),
    }
    if option(args, "--speaker-role").is_some() {
        return Err("--speaker-role requires the built-in Moonshine engine; legacy Whisper has no turn-role support".into());
    }
    if !args.iter().any(|arg| arg == "--authorized") {
        return Err("explicit --authorized is required for audio listening".into());
    }
    let source = option(args, "--source").unwrap_or_else(|| "microphone".into());
    if source != "microphone" && source != "system" {
        return Err("--source must be microphone or system".into());
    }
    let device = option(args, "--device").unwrap_or_else(|| {
        if cfg!(target_os = "macos") {
            "0".into()
        } else {
            "default".into()
        }
    });
    let seconds = bounded(args, "--chunk-seconds", 6, 3, 60)?;
    let max_segments = bounded(args, "--max-segments", 1000, 1, 1000)?;
    let model = option(args, "--model").ok_or("missing --model for local Whisper")?;
    let config = WhisperConfig {
        model: PathBuf::from(model),
        executable: option(args, "--executable")
            .unwrap_or_else(|| "whisper-cli".into())
            .into(),
    };
    if !config.model.is_file() {
        return Err("local Whisper model unavailable".into());
    }
    let mut emitter = Emitter::new();
    let label = if source == "system" {
        "playback"
    } else {
        "microphone"
    };
    let temporary = tempfile::tempdir().map_err(|_| "cannot create private audio workspace")?;
    let mut dedup = Deduplicator::new(128);
    // Ordered, bounded record of finalized speech. Every transcribed segment is
    // retained (minus fillers/noise and echoes) so a later question resolves
    // against earlier speech instead of being answered as an isolated snippet.
    let mut spoken = SpokenTranscript::default();
    let mut last_answer: Option<String> = None;
    let mut stop = Box::pin(tokio::signal::ctrl_c());
    emitter.send("session_started", label, None, None)?;
    for index in 0..max_segments {
        let path = temporary.path().join("segment.wav");
        let source = source.clone();
        let device = device.clone();
        let config = config.clone();
        let recorded = tokio::task::spawn_blocking(move || {
            capture_segment(&source, &device, seconds, &path)?;
            if !has_audio_energy(&path) {
                let _ = std::fs::remove_file(&path);
                return Ok::<Option<String>, &'static str>(None);
            }
            let transcription =
                transcribe(&config, &path).map_err(|_| "Whisper transcription failed")?;
            let _ = std::fs::remove_file(&path);
            Ok(Some(transcription))
        });
        let transcript = tokio::select! {
            result = recorded => result.map_err(|_| "audio worker failed".to_owned())?,
            _ = &mut stop => break,
        };
        let raw = match transcript {
            Ok(Some(text)) => text,
            Ok(None) => continue,
            Err(_) => {
                emitter.send(
                    "error",
                    label,
                    None,
                    Some("Local audio or Whisper failed; verify device, permissions and model."),
                )?;
                break;
            }
        };
        // Record all finalized speech (cleaned and bounded) before deciding
        // whether this segment is a question. Non-question speech still becomes
        // context for a follow-up later in the session.
        let text = clean_spoken_segment(&raw);
        if text.trim().is_empty() {
            continue;
        }
        if !spoken.finalize_cleaned(&text, last_answer.as_deref())
            || !looks_like_question(&text)
            || !dedup.should_process(&text)
        {
            continue;
        }
        let id = format!("spoken-{}", index + 1);
        emitter.send("question_detected", label, Some(&id), Some(&text))?;
        emitter.send("answer_started", label, Some(&id), None)?;
        let prompt = with_spoken_context(&text, spoken.context());
        let response = tokio::select! {
            result = answer(&provider, &prompt) => Some(result),
            _ = &mut stop => None,
        };
        match response {
            Some(Ok(text)) => {
                // Keep only a bounded copy as an echo filter: a captured TTS /
                // playback of our own answer must not be recorded as new speech.
                last_answer = Some(text.chars().take(4_000).collect());
                emitter.send("answer_delta", label, Some(&id), Some(&text))?;
                emitter.send("answer_completed", label, Some(&id), None)?;
            }
            Some(Err(_)) => {
                emitter.send(
                    "error",
                    label,
                    Some(&id),
                    Some("AI provider failed; check endpoint, login and available credits."),
                )?;
            }
            None => break,
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    emitter.send("session_stopped", label, None, None)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn question_gate_and_bounds() {
        assert!(looks_like_question(
            "What is the time complexity of binary search?"
        ));
        assert!(!looks_like_question("ambient noise"));
        assert!(
            bounded(
                &["--chunk-seconds".into(), "1".into()],
                "--chunk-seconds",
                6,
                3,
                60
            )
            .is_err()
        );
    }
    #[test]
    fn silence_filter_fails_closed() {
        let temp = tempfile::tempdir().unwrap();
        assert!(!has_audio_energy(&temp.path().join("missing.wav")));
    }

    #[test]
    fn whisper_segments_accumulate_ordered_transcript_context_and_drop_answer_echoes() {
        // Every finalized segment enters the ordered history, not only questions.
        let mut spoken = SpokenTranscript::default();
        assert!(spoken.finalize("um what is a closure", None));
        assert!(spoken.finalize_cleaned("it captures its surrounding variables", None));
        assert!(spoken.finalize_cleaned("and why is that useful", None));
        // The first request can already see all finalized segments in order.
        let follow_up = clean_spoken_segment("and why is that useful");
        let prompt = with_spoken_context(&follow_up, spoken.context());
        assert!(prompt.starts_with("and why is that useful\n\n"));
        assert!(prompt.contains("1. what is a closure"));
        assert!(prompt.contains("2. it captures its surrounding variables"));
        assert!(
            prompt.find("1. what is a closure").unwrap()
                < prompt.find("2. it captures its surrounding").unwrap()
        );

        // A captured echo of the assistant's answer never becomes speech.
        let answer = "A closure captures variables from its enclosing scope.";
        assert!(!spoken.finalize_cleaned(answer, Some(answer)));
        assert!(!spoken.finalize(answer, Some(answer)));
    }

    #[test]
    fn pcm_wav_energy_gate_filters_silence_but_keeps_audible_signal() {
        let dir = tempfile::tempdir().unwrap();
        for (name, amplitude, expected) in [
            ("silent.wav", 0_i16, false),
            ("quiet.wav", 100_i16, false),
            ("audible.wav", 3_000_i16, true),
        ] {
            let mut wav = Vec::new();
            let data_length = 16_000_u32 * 2;
            wav.extend_from_slice(b"RIFF");
            wav.extend_from_slice(&(36 + data_length).to_le_bytes());
            wav.extend_from_slice(b"WAVEfmt ");
            wav.extend_from_slice(&16_u32.to_le_bytes());
            wav.extend_from_slice(&1_u16.to_le_bytes());
            wav.extend_from_slice(&1_u16.to_le_bytes());
            wav.extend_from_slice(&16_000_u32.to_le_bytes());
            wav.extend_from_slice(&32_000_u32.to_le_bytes());
            wav.extend_from_slice(&2_u16.to_le_bytes());
            wav.extend_from_slice(&16_u16.to_le_bytes());
            wav.extend_from_slice(b"data");
            wav.extend_from_slice(&data_length.to_le_bytes());
            for _ in 0..16_000 {
                wav.extend_from_slice(&amplitude.to_le_bytes());
            }
            let path = dir.path().join(name);
            std::fs::write(&path, wav).unwrap();
            assert_eq!(has_audio_energy(&path), expected, "{name}");
        }
    }
}
