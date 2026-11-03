//! User-authorized, accessibility-first observation with JSONL UI events.
//! OCR is NEVER invoked unless --allow-ocr or --force-ocr is specified.
use crate::live_events::Emitter;
use context_core::accessibility::{AccessibilityError, needs_ocr, read_foreground_text};
use context_core::ai::{AiError, Provider, list_models, stream_answer};
use context_core::capture::{PortalFrameSource, capture_to, requires_linux_portal};
use context_core::context::Deduplicator;
use context_core::ocr::extract_text;
use context_core::session::PracticeSession;
use std::path::Path;
use std::time::Duration;
use tokio::io::AsyncBufReadExt;
use tokio::sync::mpsc;

fn has(args: &[String], flag: &str) -> bool {
    args.iter().any(|arg| arg == flag)
}

fn limit(args: &[String], flag: &str, default: u64, lower: u64, upper: u64) -> Result<u64, String> {
    let value = args
        .windows(2)
        .find(|p| p[0] == flag)
        .map(|p| p[1].parse::<u64>().map_err(|_| format!("invalid {flag}")))
        .transpose()?
        .unwrap_or(default);
    if !(lower..=upper).contains(&value) {
        return Err(format!("{flag} must be {lower}..{upper}"));
    }
    Ok(value)
}

fn plausible_question(text: &str) -> bool {
    text.len() >= 24
        && text.len() <= 64_000
        && text.chars().filter(|ch| ch.is_alphabetic()).count() >= 12
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ControlCommand {
    Pause,
    Resume,
    Stop,
}

enum ModelMessage {
    Delta {
        request: String,
        text: String,
    },
    Finished {
        request: String,
        outcome: Result<(), AiError>,
    },
}

fn control_command(input: &str) -> Option<ControlCommand> {
    match input.trim() {
        "pause" => Some(ControlCommand::Pause),
        "resume" => Some(ControlCommand::Resume),
        "stop" => Some(ControlCommand::Stop),
        _ => None,
    }
}

async fn verify_provider(provider: &Provider) -> Result<(), &'static str> {
    match provider {
        Provider::Mock => Ok(()),
        Provider::OpenAiCompatible { model, .. } => {
            if model.trim().is_empty() {
                return Err("Select a model in Settings before starting practice.");
            }
            let models = list_models(provider).await.map_err(
                |_| "Provider check failed; verify the endpoint, credentials, and model service.",
            )?;
            if !models.iter().any(|available| available == model) {
                return Err("Configured model is not available from the selected provider.");
            }
            Ok(())
        }
        Provider::Codex => {
            let mut auth = context_core::codex_auth::CodexAuthSession::connect()
                .await
                .map_err(|_| "Codex CLI is unavailable; install it and complete sign-in.")?;
            let status = auth.status().await;
            auth.close().await;
            match status {
                Ok(account) if account.signed_in => Ok(()),
                Ok(_) => Err("Codex is signed out; run `context-cli codex-login` first."),
                Err(_) => Err("Could not verify Codex login; check the official Codex CLI."),
            }
        }
        Provider::Fallback(alternatives) => {
            for alternative in alternatives.iter().take(3) {
                if Box::pin(verify_provider(alternative)).await.is_ok() {
                    return Ok(());
                }
            }
            Err("No configured fallback provider is ready; check provider settings.")
        }
    }
}

/// Result of one accessibility read, before any capture decision is made.
#[derive(Debug, PartialEq, Eq)]
enum AccessibilityOutcome {
    Text(String),
    Empty,
    Backend(String),
    OwnWindow,
}

/// What the accessibility-first policy wants to do next. Pure and testable.
#[derive(Debug, PartialEq, Eq)]
enum ReadingPlan {
    UseAccessible,
    TryOcr,
    ReportAccessibility,
    Insufficient,
    Nothing,
}

/// Accessibility first; local OCR only when the user explicitly allowed it.
/// A missing accessibility backend is reported, never silently swapped for capture.
fn plan_reading(outcome: &AccessibilityOutcome, allow_ocr: bool, force_ocr: bool) -> ReadingPlan {
    match outcome {
        AccessibilityOutcome::OwnWindow => ReadingPlan::Nothing,
        AccessibilityOutcome::Backend(_) => {
            if allow_ocr {
                ReadingPlan::TryOcr
            } else {
                ReadingPlan::ReportAccessibility
            }
        }
        AccessibilityOutcome::Empty | AccessibilityOutcome::Text(_) => {
            let text = match outcome {
                AccessibilityOutcome::Text(value) => Some(value.as_str()),
                _ => None,
            };
            if !force_ocr && !needs_ocr(text) {
                ReadingPlan::UseAccessible
            } else if allow_ocr {
                ReadingPlan::TryOcr
            } else {
                ReadingPlan::Insufficient
            }
        }
    }
}

enum Reading {
    Question(String, &'static str),
    Nothing,
    Insufficient,
    AccessibilityUnavailable(String),
    OcrFailed(&'static str),
}

/// Keep only trusted, bounded diagnostic text from the accessibility helper.
fn sanitize_reason(reason: &str) -> String {
    reason
        .chars()
        .filter(|character| !character.is_control())
        .take(200)
        .collect::<String>()
        .trim()
        .to_owned()
}

fn read_question(
    session: &PracticeSession,
    portal: &mut Option<PortalFrameSource>,
    path: &Path,
    interval: u64,
    allow_ocr: bool,
    force_ocr: bool,
    portal_required: bool,
) -> Reading {
    let root = context_core::paths::project_root();
    let outcome = match read_foreground_text(&root) {
        Ok(text) => AccessibilityOutcome::Text(text),
        Err(AccessibilityError::OwnWindow) => AccessibilityOutcome::OwnWindow,
        Err(AccessibilityError::Empty) => AccessibilityOutcome::Empty,
        Err(AccessibilityError::Backend(reason)) => AccessibilityOutcome::Backend(reason),
    };
    match plan_reading(&outcome, allow_ocr, force_ocr) {
        ReadingPlan::Nothing => Reading::Nothing,
        ReadingPlan::Insufficient => Reading::Insufficient,
        ReadingPlan::ReportAccessibility => {
            let reason = match &outcome {
                AccessibilityOutcome::Backend(detail) => sanitize_reason(detail),
                _ => String::new(),
            };
            Reading::AccessibilityUnavailable(reason)
        }
        ReadingPlan::UseAccessible => match outcome {
            AccessibilityOutcome::Text(text) => Reading::Question(text, "accessibility"),
            _ => Reading::Nothing,
        },
        ReadingPlan::TryOcr => {
            if portal_required {
                if portal.is_none() {
                    match PortalFrameSource::start(session, path.to_path_buf(), interval) {
                        Ok(source) => *portal = Some(source),
                        Err(_) => {
                            return Reading::OcrFailed(
                                "Could not start approved ScreenCast source",
                            );
                        }
                    }
                }
                let Some(stream) = portal.as_mut() else {
                    return Reading::OcrFailed("ScreenCast unavailable");
                };
                if stream.finished().unwrap_or(true) {
                    return Reading::OcrFailed("ScreenCast permission revoked or session closed");
                }
                for _ in 0..40 {
                    if stream.path().exists() {
                        break;
                    }
                    if stream.finished().unwrap_or(true) {
                        return Reading::OcrFailed(
                            "ScreenCast permission revoked or session closed",
                        );
                    }
                    std::thread::sleep(Duration::from_millis(250));
                }
                if !stream.path().exists() {
                    return Reading::OcrFailed("No approved ScreenCast frame");
                }
                return match extract_text(stream.path()) {
                    Ok(text) => Reading::Question(text, "ocr"),
                    Err(_) => Reading::OcrFailed("Local OCR failed"),
                };
            }
            if capture_to(session, path).is_err() {
                return Reading::OcrFailed("Local screenshot capture unavailable");
            }
            let text = extract_text(path);
            let _ = std::fs::remove_file(path);
            match text {
                Ok(text) => Reading::Question(text, "ocr"),
                Err(_) => Reading::OcrFailed("Local OCR failed"),
            }
        }
    }
}

pub async fn run(args: &[String], provider: Provider) -> Result<(), String> {
    if !has(args, "--authorized") {
        return Err("observe-live requires explicit --authorized".into());
    }
    let interval = limit(args, "--interval", 4, 2, 3600)?;
    let max = limit(args, "--max-captures", 1000, 1, 1000)?;
    let allow_ocr = has(args, "--allow-ocr") || has(args, "--force-ocr");
    let force_ocr = has(args, "--force-ocr");
    let portal_required = has(args, "--portal") || requires_linux_portal();
    let mut events = Emitter::new();
    if let Err(message) = verify_provider(&provider).await {
        events.send("error", "provider", None, Some(message))?;
        return Err(message.into());
    }
    let mut session = PracticeSession::new();
    session.start(true).map_err(str::to_owned)?;
    let temp_dir = tempfile::tempdir().map_err(|_| "cannot create temporary capture directory")?;
    let frame = temp_dir.path().join("latest.png");
    let mut portal: Option<PortalFrameSource> = None;
    if let Err(error) = events.send("session_started", "system", None, None) {
        session.stop();
        return Err(error);
    }
    let mut dedup = Deduplicator::new(128);
    let mut ticker = tokio::time::interval(Duration::from_secs(interval));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let (sender, mut receiver) = mpsc::channel::<ModelMessage>(32);
    let mut pending = None::<tokio::task::JoinHandle<()>>;
    let mut last_request = String::new();
    let mut request_number = 0_u64;
    let mut observations = 0_u64;
    let mut warned_accessibility = false;
    let mut warned_ocr = false;
    let mut paused = false;
    let mut control_open = true;
    let mut controls = tokio::io::BufReader::new(tokio::io::stdin()).lines();
    let mut event_error = None;
    macro_rules! emit {
        ($kind:expr, $source:expr, $request:expr, $content:expr) => {
            if let Err(error) = events.send($kind, $source, $request, $content) {
                event_error = Some(error);
                break;
            }
        };
    }
    let stop = tokio::signal::ctrl_c();
    tokio::pin!(stop);
    loop {
        tokio::select! {
            _ = &mut stop => break,
            control = controls.next_line(), if control_open => {
                match control {
                    Ok(Some(line)) => match control_command(&line) {
                        Some(ControlCommand::Pause) if !paused => {
                            paused = true;
                            session.pause();
                            portal.take();
                            dedup = Deduplicator::new(128);
                            last_request.clear();
                            if let Some(task) = pending.take() { task.abort(); }
                            emit!("session_paused", "system", None, None);
                        }
                        Some(ControlCommand::Resume) if paused => {
                            paused = false;
                            session.resume();
                            emit!("session_resumed", "system", None, None);
                        }
                        Some(ControlCommand::Stop) => break,
                        _ => {},
                    },
                    Ok(None) | Err(_) => control_open = false,
                }
            }
            _ = ticker.tick(), if observations < max && !paused => {
                observations += 1;
                let reading = read_question(&session, &mut portal, &frame, interval,
                    allow_ocr, force_ocr, portal_required);
                match reading {
                    Reading::Question(text, source) if plausible_question(&text) && dedup.should_process(&text) => {
                        if let Some(previous) = pending.take() { previous.abort(); }
                        request_number += 1;
                        let rid = format!("question-{request_number}");
                        last_request.clone_from(&rid);
                        emit!("question_detected", source, Some(&rid), Some(&text));
                        emit!("answer_started", source, Some(&rid), None);
                        let tx = sender.clone();
                        let model = provider.clone();
                        pending = Some(tokio::spawn(async move {
                            let (parts_tx, mut parts_rx) = mpsc::channel::<String>(16);
                            let stream = stream_answer(&model, &text, &parts_tx);
                            tokio::pin!(stream);
                            loop {
                                tokio::select! {
                                    Some(part) = parts_rx.recv() => {
                                        if tx.send(ModelMessage::Delta {
                                            request: rid.clone(), text: part,
                                        }).await.is_err() { break; }
                                    }
                                    finished = &mut stream => {
                                        while let Ok(part) = parts_rx.try_recv() {
                                            if tx.send(ModelMessage::Delta {
                                                request: rid.clone(), text: part,
                                            }).await.is_err() { return; }
                                        }
                                        let _ = tx.send(ModelMessage::Finished {
                                            request: rid, outcome: finished,
                                        }).await;
                                        break;
                                    }
                                }
                            }
                        }));
                        warned_accessibility = false;
                        warned_ocr = false;
                    }
                    Reading::Question(_, _) => {},
                    Reading::Insufficient if !warned_accessibility => {
                        warned_accessibility = true;
                        emit!("error", "accessibility", None,
                            Some("No accessible question. Focus a permitted window; enable local OCR fallback explicitly if needed."));
                    }
                    Reading::AccessibilityUnavailable(reason) if !warned_accessibility => {
                        warned_accessibility = true;
                        let message = if reason.is_empty() {
                            "Accessibility unavailable; check AT-SPI/AX permission or enable local OCR fallback explicitly.".to_owned()
                        } else {
                            format!("Accessibility unavailable: {reason} Enable local OCR fallback explicitly if needed.")
                        };
                        emit!("error", "accessibility", None, Some(&message));
                    }
                    Reading::OcrFailed(reason) if !warned_ocr => {
                        warned_ocr = true;
                        let message = format!(
                            "Local OCR source unavailable ({reason}). Check OS capture permission and Tesseract installation."
                        );
                        emit!("error", "ocr", None, Some(&message));
                    }
                    _ => {},
                }
                if observations >= max && pending.is_none() { break; }
            }
            Some(message) = receiver.recv(), if !paused => {
                match message {
                    ModelMessage::Delta { request, text } => {
                        if request == last_request {
                            emit!("answer_delta", "system", Some(&request), Some(&text));
                        }
                    }
                    ModelMessage::Finished { request, outcome } => {
                        if request != last_request { continue; }
                        pending = None;
                        match outcome {
                            Ok(()) => emit!("answer_completed", "system", Some(&request), None),
                            Err(_) => emit!("error", "system", Some(&request),
                                Some("Model unavailable. Check provider settings, login or quota.")),
                        }
                        if observations >= max { break; }
                    }
                }
            }
        }
    }
    if let Some(task) = pending {
        task.abort();
    }
    drop(portal);
    session.stop();
    if let Some(error) = event_error {
        return Err(error);
    }
    events.send("session_stopped", "system", None, None)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn question_and_interval_validation() {
        assert!(plausible_question("What is the binary search complexity?"));
        assert!(!plausible_question("abc"));
        assert_eq!(limit(&[], "--interval", 4, 2, 3600).unwrap(), 4);
        assert!(limit(&["--interval".into(), "1".into()], "--interval", 4, 2, 3600).is_err());
    }

    #[test]
    fn recognizes_only_supported_control_commands() {
        assert_eq!(control_command("pause\n"), Some(ControlCommand::Pause));
        assert_eq!(control_command(" resume "), Some(ControlCommand::Resume));
        assert_eq!(control_command("stop"), Some(ControlCommand::Stop));
        assert_eq!(control_command("quit"), None);
    }

    #[test]
    fn pause_revokes_capture_until_resume() {
        let mut session = PracticeSession::new();
        session.start(true).unwrap();
        assert!(session.may_capture());
        session.pause();
        assert!(!session.may_capture());
        session.resume();
        assert!(session.may_capture());
        session.stop();
        assert!(!session.may_capture());
    }

    #[test]
    fn accessibility_is_preferred_and_ocr_is_opt_in() {
        let long = AccessibilityOutcome::Text("a sufficiently long coding question here".into());
        assert_eq!(
            plan_reading(&long, false, false),
            ReadingPlan::UseAccessible
        );
        assert_eq!(plan_reading(&long, true, false), ReadingPlan::UseAccessible);
        assert_eq!(plan_reading(&long, true, true), ReadingPlan::TryOcr);

        let short = AccessibilityOutcome::Text("tiny".into());
        assert_eq!(
            plan_reading(&short, false, false),
            ReadingPlan::Insufficient
        );
        assert_eq!(plan_reading(&short, true, false), ReadingPlan::TryOcr);

        let empty = AccessibilityOutcome::Empty;
        assert_eq!(
            plan_reading(&empty, false, false),
            ReadingPlan::Insufficient
        );
        assert_eq!(plan_reading(&empty, true, false), ReadingPlan::TryOcr);
    }

    #[test]
    fn missing_accessibility_backend_is_reported_not_silently_captured() {
        let backend = AccessibilityOutcome::Backend("AT-SPI unavailable".into());
        assert_eq!(
            plan_reading(&backend, false, false),
            ReadingPlan::ReportAccessibility
        );
        // Only an explicit opt-in may fall back to pixels.
        assert_eq!(plan_reading(&backend, true, false), ReadingPlan::TryOcr);
        // Our own window is never analyzed, even with OCR allowed.
        assert_eq!(
            plan_reading(&AccessibilityOutcome::OwnWindow, true, true),
            ReadingPlan::Nothing
        );
    }

    #[test]
    fn diagnostic_reason_is_bounded_and_control_free() {
        assert_eq!(
            sanitize_reason("AT-SPI\u{7}unavailable\n"),
            "AT-SPIunavailable"
        );
        assert_eq!(sanitize_reason(&"x".repeat(500)).len(), 200);
    }

    #[tokio::test]
    async fn mock_provider_preflight_never_calls_an_external_service() {
        assert!(verify_provider(&Provider::Mock).await.is_ok());
    }

    #[tokio::test]
    async fn configured_provider_requires_a_model_before_capture() {
        let provider = Provider::OpenAiCompatible {
            base_url: "http://127.0.0.1:9/v1".into(),
            model: "  ".into(),
            api_key: String::new(),
        };
        assert_eq!(
            verify_provider(&provider).await,
            Err("Select a model in Settings before starting practice.")
        );
    }
}
