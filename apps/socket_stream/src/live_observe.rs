//! User-authorized, accessibility-first observation with JSONL UI events.
//! OCR is never invoked without an explicit OCR flag or config opt-in.
use crate::live_events::Emitter;
use socket_stream_core::a11y::{ReadMode, TokenBudget};
use socket_stream_core::accessibility::{AccessibilityError, needs_ocr, read_foreground_tree};
use socket_stream_core::ai::{AiError, ApiEvent, Provider, list_models, stream_answer_with_meta};
use socket_stream_core::capture::{PortalFrameSource, capture_to, requires_linux_portal};
use socket_stream_core::config::{Config, OcrConfig};
use socket_stream_core::context::Deduplicator;
use socket_stream_core::ocr::{extract_regions_text, extract_text};
use socket_stream_core::session::PracticeSession;
use std::path::{Path, PathBuf};
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

#[derive(Clone)]
struct A11yOptions {
    mode: ReadMode,
    budget: TokenBudget,
    region_ocr: bool,
    ocr_config: OcrConfig,
}

fn a11y_options(args: &[String]) -> Result<A11yOptions, String> {
    let mut config = match args.windows(2).find(|pair| pair[0] == "--config") {
        Some(pair) => Config::load_from(Path::new(&pair[1])).map_err(|error| error.to_string())?,
        None => Config::load().map_err(|error| error.to_string())?,
    };
    if let Some(value) = args.windows(2).find(|pair| pair[0] == "--a11y-mode") {
        if !matches!(value[1].as_str(), "auto" | "focused" | "tree" | "text") {
            return Err("--a11y-mode must be auto, focused, tree, or text".into());
        }
        config.a11y.mode.clone_from(&value[1]);
    }
    if let Some(value) = args.windows(2).find(|pair| pair[0] == "--max-tokens") {
        config.a11y.max_tokens = value[1]
            .parse::<usize>()
            .map_err(|_| "--max-tokens must be a positive integer")?;
        if config.a11y.max_tokens == 0 || config.a11y.max_tokens > 100_000 {
            return Err("--max-tokens must be 1..100000".into());
        }
    }
    if has(args, "--ocr-images") && has(args, "--no-ocr-images") {
        return Err("choose only one of --ocr-images or --no-ocr-images".into());
    }
    let explicit_region_ocr = has(args, "--ocr-images");
    if has(args, "--ocr-images") {
        config.a11y.ocr_images = true;
    } else if has(args, "--no-ocr-images") {
        config.a11y.ocr_images = false;
    }
    let mut budget = config.budget();
    // Config is user-authored; keep helper and renderer work bounded even if a
    // file contains values far beyond the documented defaults.
    budget.max_tokens = budget.max_tokens.clamp(1, 100_000);
    budget.max_nodes = budget.max_nodes.clamp(1, 10_000);
    budget.max_depth = budget.max_depth.clamp(1, 64);
    budget.max_chars = budget.max_chars.clamp(1, 1_000_000);
    Ok(A11yOptions {
        mode: config.a11y_mode(),
        budget,
        region_ocr: explicit_region_ocr || (config.ocr.enabled && config.a11y.ocr_images),
        ocr_config: config.ocr,
    })
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
    ApiRequest {
        request: String,
        url: String,
        model: String,
        prompt: String,
    },
    ApiResponse {
        request: String,
        status: Option<u16>,
        text: String,
    },
    Finished {
        request: String,
        outcome: Result<(), AiError>,
    },
}

/// How much of a request or response the practice panel's API area receives.
/// The full prompt can be tens of thousands of characters; the panel shows a
/// bounded preview so one event cannot flood the UI channel.
const API_PREVIEW_CHARS: usize = 4000;

fn preview(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

/// Convert a provider exchange event into the message the UI loop forwards.
fn api_message(request: &str, event: ApiEvent) -> ModelMessage {
    match event {
        ApiEvent::Request { url, model, prompt } => ModelMessage::ApiRequest {
            request: request.to_owned(),
            url,
            model,
            prompt: preview(&prompt, API_PREVIEW_CHARS),
        },
        ApiEvent::Response { status, body } => ModelMessage::ApiResponse {
            request: request.to_owned(),
            status,
            text: preview(&body, API_PREVIEW_CHARS),
        },
    }
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
        Provider::Codex { .. } => {
            let mut auth = socket_stream_core::codex_auth::CodexAuthSession::connect()
                .await
                .map_err(|_| "Codex CLI is unavailable; install it and complete sign-in.")?;
            let status = auth.status().await;
            auth.close().await;
            match status {
                Ok(account) if account.signed_in => Ok(()),
                Ok(_) => Err("Codex is signed out; run `socket_stream codex-login` first."),
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
    /// Rendered capture for the model, role-free text for the panel, and source.
    Question(String, String, &'static str),
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

struct ReadContext<'a> {
    session: &'a PracticeSession,
    portal: &'a mut Option<PortalFrameSource>,
    path: &'a Path,
    interval: u64,
    allow_ocr: bool,
    force_ocr: bool,
    portal_required: bool,
    options: A11yOptions,
}

fn read_question(context: &mut ReadContext<'_>) -> Reading {
    let session = context.session;
    let portal = &mut *context.portal;
    let path = context.path;
    let interval = context.interval;
    let allow_ocr = context.allow_ocr;
    let force_ocr = context.force_ocr;
    let portal_required = context.portal_required;
    let options = &context.options;
    let root = socket_stream_core::paths::project_root();
    let outcome = match read_foreground_tree(
        &root,
        options.budget.max_nodes,
        options.budget.max_depth,
        options.budget.max_chars,
    ) {
        Ok(tree) => {
            let rendered = tree.render(options.mode, options.budget);
            if !force_ocr && !needs_ocr(Some(&rendered.text)) {
                return Reading::Question(rendered.text, rendered.display, "accessibility");
            }

            let needs_capture = options.region_ocr && !rendered.image_regions.is_empty();
            if needs_capture {
                let screenshot =
                    match capture_frame(session, portal, path, interval, portal_required) {
                        Ok(path) => path,
                        Err(message) => return Reading::OcrFailed(message),
                    };
                let region_result = extract_regions_text(
                    &screenshot,
                    &rendered.image_regions,
                    options.ocr_config.max_regions,
                    options.ocr_config.min_region_pixels,
                    &options.ocr_config.tesseract_psm,
                );
                match region_result {
                    Ok(ocr_text) if !ocr_text.trim().is_empty() => {
                        if !portal_required {
                            let _ = std::fs::remove_file(&screenshot);
                        }
                        let text = if rendered.text.trim().is_empty() {
                            ocr_text.clone()
                        } else {
                            format!("{}\n{}", rendered.text.trim(), ocr_text.trim())
                        };
                        let display = if rendered.display.trim().is_empty() {
                            ocr_text
                        } else {
                            format!("{}\n{}", rendered.display.trim(), ocr_text.trim())
                        };
                        return Reading::Question(text, display, "ocr");
                    }
                    Ok(_) if allow_ocr => {
                        return ocr_captured_frame(&screenshot, !portal_required);
                    }
                    Ok(_) => {
                        if !portal_required {
                            let _ = std::fs::remove_file(&screenshot);
                        }
                        return Reading::Insufficient;
                    }
                    Err(_) if allow_ocr => {
                        return ocr_captured_frame(&screenshot, !portal_required);
                    }
                    Err(_) => {
                        if !portal_required {
                            let _ = std::fs::remove_file(&screenshot);
                        }
                        return Reading::OcrFailed("Targeted region OCR failed");
                    }
                }
            }
            AccessibilityOutcome::Text(rendered.text)
        }
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
            AccessibilityOutcome::Text(text) => {
                Reading::Question(text.clone(), text, "accessibility")
            }
            _ => Reading::Nothing,
        },
        ReadingPlan::TryOcr => whole_frame_ocr(session, portal, path, interval, portal_required),
    }
}

fn capture_frame(
    session: &PracticeSession,
    portal: &mut Option<PortalFrameSource>,
    path: &Path,
    interval: u64,
    portal_required: bool,
) -> Result<PathBuf, &'static str> {
    if portal_required {
        if portal.is_none() {
            *portal = Some(
                PortalFrameSource::start(session, path.to_path_buf(), interval)
                    .map_err(|_| "Could not start approved ScreenCast source")?,
            );
        }
        let stream = portal.as_mut().ok_or("ScreenCast unavailable")?;
        if stream.finished().unwrap_or(true) {
            return Err("ScreenCast permission revoked or session closed");
        }
        for _ in 0..40 {
            if stream.path().exists() {
                return Ok(stream.path().to_path_buf());
            }
            if stream.finished().unwrap_or(true) {
                return Err("ScreenCast permission revoked or session closed");
            }
            std::thread::sleep(Duration::from_millis(250));
        }
        return Err("No approved ScreenCast frame");
    }
    capture_to(session, path).map_err(|_| "Local screenshot capture unavailable")?;
    Ok(path.to_path_buf())
}

fn whole_frame_ocr(
    session: &PracticeSession,
    portal: &mut Option<PortalFrameSource>,
    path: &Path,
    interval: u64,
    portal_required: bool,
) -> Reading {
    let screenshot = match capture_frame(session, portal, path, interval, portal_required) {
        Ok(path) => path,
        Err(message) => return Reading::OcrFailed(message),
    };
    ocr_captured_frame(&screenshot, !portal_required)
}

fn ocr_captured_frame(screenshot: &Path, remove_after: bool) -> Reading {
    let text = extract_text(screenshot);
    if remove_after {
        let _ = std::fs::remove_file(screenshot);
    }
    match text {
        Ok(text) => Reading::Question(text.clone(), text, "ocr"),
        Err(_) => Reading::OcrFailed("Local OCR failed"),
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
    let a11y = a11y_options(args)?;
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
    macro_rules! emit_question {
        ($source:expr, $request:expr, $text:expr, $display:expr) => {
            if let Err(error) = events.send_question($source, $request, $text, $display) {
                event_error = Some(error);
                break;
            }
        };
    }
    macro_rules! emit_api_request {
        ($request:expr, $url:expr, $model:expr, $prompt:expr) => {
            if let Err(error) = events.send_api_request($request, $url, $model, $prompt) {
                event_error = Some(error);
                break;
            }
        };
    }
    macro_rules! emit_api_response {
        ($request:expr, $status:expr, $text:expr) => {
            if let Err(error) = events.send_api_response($request, $status, $text) {
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
                let reading = {
                    let mut context = ReadContext {
                        session: &session,
                        portal: &mut portal,
                        path: &frame,
                        interval,
                        allow_ocr,
                        force_ocr,
                        portal_required,
                        options: a11y.clone(),
                    };
                    read_question(&mut context)
                };
                match reading {
                    Reading::Question(text, display, source) if plausible_question(&text) => {
                        // Never abort an in-flight model call. A page with a
                        // ticking timer or live counter changes its text every
                        // observation; aborting on each change meant no answer
                        // ever completed. Observe every tick so the volatile
                        // lines are learned even while a call is pending, and
                        // answer only once the screen has settled and is new.
                        let (signature, stable) = dedup.observe(&text);
                        if pending.is_none() && stable && dedup.is_new(signature) {
                            dedup.remember(signature);
                            request_number += 1;
                            let rid = format!("question-{request_number}");
                            last_request.clone_from(&rid);
                            emit_question!(source, &rid, &text, &display);
                            emit!("answer_started", source, Some(&rid), None);
                            let tx = sender.clone();
                            let model = provider.clone();
                            pending = Some(tokio::spawn(async move {
                                let (parts_tx, mut parts_rx) = mpsc::channel::<String>(16);
                                let (meta_tx, mut meta_rx) = mpsc::channel::<ApiEvent>(8);
                                let stream = stream_answer_with_meta(
                                    &model, &text, &parts_tx, Some(&meta_tx),
                                );
                                tokio::pin!(stream);
                                loop {
                                    tokio::select! {
                                        Some(part) = parts_rx.recv() => {
                                            if tx.send(ModelMessage::Delta {
                                                request: rid.clone(), text: part,
                                            }).await.is_err() { break; }
                                        }
                                        Some(event) = meta_rx.recv() => {
                                            let message = api_message(&rid, event);
                                            if tx.send(message).await.is_err() { break; }
                                        }
                                        finished = &mut stream => {
                                            while let Ok(part) = parts_rx.try_recv() {
                                                if tx.send(ModelMessage::Delta {
                                                    request: rid.clone(), text: part,
                                                }).await.is_err() { return; }
                                            }
                                            while let Ok(event) = meta_rx.try_recv() {
                                                if tx.send(api_message(&rid, event)).await.is_err() { return; }
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
                    }
                    Reading::Question(_, _, _) => {},
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
                    ModelMessage::ApiRequest { request, url, model, prompt } => {
                        if request == last_request {
                            emit_api_request!(&request, &url, &model, &prompt);
                        }
                    }
                    ModelMessage::ApiResponse { request, status, text } => {
                        if request == last_request {
                            emit_api_response!(&request, status, &text);
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

    fn config_file(value: &str) -> (tempfile::TempDir, PathBuf) {
        let directory = tempfile::tempdir().expect("tempdir");
        let path = directory.path().join("observe.json");
        std::fs::write(&path, value).expect("write config");
        (directory, path)
    }

    #[test]
    fn a11y_cli_overrides_config_and_enables_region_ocr() {
        let (_directory, path) = config_file(
            r#"{
                "a11y":{"mode":"text","max_tokens":500,"max_nodes":50,"max_depth":4,"max_chars":2000,"ocr_images":false},
                "ocr":{"enabled":false,"max_regions":2,"min_region_pixels":400,"tesseract_psm":"11"}
            }"#,
        );
        let options = a11y_options(&[
            "--config".into(),
            path.display().to_string(),
            "--a11y-mode".into(),
            "tree".into(),
            "--max-tokens".into(),
            "321".into(),
            "--ocr-images".into(),
        ])
        .expect("options");
        assert_eq!(options.mode, ReadMode::Tree);
        assert_eq!(options.budget.max_tokens, 321);
        assert_eq!(options.budget.max_nodes, 50);
        assert!(options.region_ocr);
        assert_eq!(options.ocr_config.max_regions, 2);
        assert_eq!(options.ocr_config.tesseract_psm, "11");
    }

    #[test]
    fn config_opt_in_can_enable_region_ocr_and_cli_can_disable_it() {
        let (_directory, path) =
            config_file(r#"{"a11y":{"ocr_images":true},"ocr":{"enabled":true}}"#);
        let options =
            a11y_options(&["--config".into(), path.display().to_string()]).expect("options");
        assert!(options.region_ocr);
        let disabled = a11y_options(&[
            "--config".into(),
            path.display().to_string(),
            "--no-ocr-images".into(),
        ])
        .expect("disabled options");
        assert!(!disabled.region_ocr);
    }

    #[test]
    fn invalid_or_conflicting_a11y_overrides_are_reported() {
        assert!(a11y_options(&["--a11y-mode".into(), "unknown".into()]).is_err());
        assert!(a11y_options(&["--ocr-images".into(), "--no-ocr-images".into(),]).is_err());
        assert!(a11y_options(&["--max-tokens".into(), "0".into()]).is_err());
        assert!(a11y_options(&["--max-tokens".into(), "not-a-number".into()]).is_err());
    }

    #[test]
    fn malformed_config_is_not_silently_ignored() {
        let (_directory, path) = config_file("{bad json");
        let args = ["--config".into(), path.display().to_string()];
        assert!(a11y_options(&args).is_err());
    }

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
