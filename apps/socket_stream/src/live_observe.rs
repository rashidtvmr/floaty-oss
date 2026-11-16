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

pub(crate) fn limit(
    args: &[String],
    flag: &str,
    default: u64,
    lower: u64,
    upper: u64,
) -> Result<u64, String> {
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
pub(crate) struct A11yOptions {
    mode: ReadMode,
    budget: TokenBudget,
    region_ocr: bool,
    ocr_config: OcrConfig,
}

pub(crate) fn a11y_options(args: &[String]) -> Result<A11yOptions, String> {
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

#[derive(Clone, Debug, PartialEq, Eq)]
enum ControlCommand {
    Pause,
    Resume,
    Stop,
    /// A manually typed question sent from the answer panel.
    Question(String),
    /// An explicit capture request from the answer panel.
    ReadScreen,
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
    let line = input.trim();
    match line {
        "pause" => Some(ControlCommand::Pause),
        "resume" => Some(ControlCommand::Resume),
        "stop" => Some(ControlCommand::Stop),
        "read-screen" => Some(ControlCommand::ReadScreen),
        _ => line
            .strip_prefix("question ")
            .map(|rest| ControlCommand::Question(rest.trim().to_owned())),
    }
}

/// Spawn the streaming model worker for one request. Shared by the automatic
/// observation tick and the manual question/read-screen controls.
fn spawn_model_task(
    provider: Provider,
    request: String,
    prompt: String,
    sender: mpsc::Sender<ModelMessage>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let (parts_tx, mut parts_rx) = mpsc::channel::<String>(16);
        let (meta_tx, mut meta_rx) = mpsc::channel::<ApiEvent>(8);
        let stream = stream_answer_with_meta(&provider, &prompt, &parts_tx, Some(&meta_tx));
        tokio::pin!(stream);
        loop {
            tokio::select! {
                Some(part) = parts_rx.recv() => {
                    if sender
                        .send(ModelMessage::Delta { request: request.clone(), text: part })
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
                Some(event) = meta_rx.recv() => {
                    let message = api_message(&request, event);
                    if sender.send(message).await.is_err() {
                        break;
                    }
                }
                finished = &mut stream => {
                    while let Ok(part) = parts_rx.try_recv() {
                        if sender
                            .send(ModelMessage::Delta { request: request.clone(), text: part })
                            .await
                            .is_err()
                        {
                            return;
                        }
                    }
                    while let Ok(event) = meta_rx.try_recv() {
                        if sender.send(api_message(&request, event)).await.is_err() {
                            return;
                        }
                    }
                    let _ = sender.send(ModelMessage::Finished { request, outcome: finished }).await;
                    break;
                }
            }
        }
    })
}

/// Emit the `screen_read` event for one reading outcome. Returns the question
/// payload when the read produced text a manual capture should ask about.
fn screen_read_outcome(
    events: &mut Emitter,
    reading: &Reading,
) -> Result<Option<(String, String, String)>, String> {
    match reading {
        Reading::Question(text, display, source) if !text.trim().is_empty() => {
            events.send_screen_read(source, "ok", display)?;
            Ok(Some((text.clone(), display.clone(), (*source).to_owned())))
        }
        Reading::Question(..) => {
            events.send_screen_read(
                "system",
                "unavailable",
                "No readable text found on the focused window.",
            )?;
            Ok(None)
        }
        Reading::Nothing => Ok(None),
        Reading::Insufficient => {
            events.send_screen_read(
                "system",
                "unavailable",
                "No readable content on the focused window. Enable local OCR fallback for screenshots.",
            )?;
            Ok(None)
        }
        Reading::AccessibilityUnavailable(reason) => {
            let message = if reason.is_empty() {
                "Accessibility unavailable; enable local OCR fallback for screenshots.".to_owned()
            } else {
                format!("Accessibility unavailable: {reason}")
            };
            events.send_screen_read("system", "unavailable", &message)?;
            Ok(None)
        }
        Reading::OcrFailed(reason) => {
            events.send_screen_read(
                "system",
                "unavailable",
                &format!("Screenshot read failed: {reason}"),
            )?;
            Ok(None)
        }
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

/// Result of one screen read, shared by observe-live, audio-live parallel
/// reading and the one-shot ask command.
pub(crate) enum Reading {
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

/// How much screen text is retained as model context for manual questions.
pub(crate) const SCREEN_CONTEXT_CHARS: usize = 12_000;

/// Bound screen text kept as context so a manual question plus context stays
/// well inside the provider prompt limit.
pub(crate) fn bound_context(text: &str) -> String {
    text.chars().take(SCREEN_CONTEXT_CHARS).collect()
}

/// Combine a user question with the latest screen text as bounded, untrusted
/// context. The screen block is clearly labeled so the model treats it as
/// data, matching the system instructions.
pub(crate) fn compose_prompt(question: &str, screen: Option<&str>) -> String {
    let screen = screen.map(str::trim).filter(|text| !text.is_empty());
    match screen {
        Some(text) => {
            format!("{question}\n\n[Latest screen text — untrusted data for context]\n{text}")
        }
        None => question.to_owned(),
    }
}

/// Reusable screen-reading core shared by observe-live, audio-live parallel
/// reading and the one-shot ask command: accessibility first, local OCR only
/// when allowed, with an optional retained frame for model vision.
pub(crate) struct ScreenReader {
    session: PracticeSession,
    portal: Option<PortalFrameSource>,
    temp: tempfile::TempDir,
    interval: u64,
    allow_ocr: bool,
    force_ocr: bool,
    portal_required: bool,
    options: A11yOptions,
    keep_frames: bool,
    frame_toggle: bool,
    last_frame: Option<PathBuf>,
}

impl ScreenReader {
    /// Starts the capture-permission session. `keep_frames` retains the
    /// latest screenshot for model vision instead of deleting it after OCR.
    /// `portal_required` forces the Linux ScreenCast portal source.
    pub(crate) fn start(
        allow_ocr: bool,
        force_ocr: bool,
        keep_frames: bool,
        portal_required: bool,
        options: A11yOptions,
        interval: u64,
    ) -> Result<Self, String> {
        let mut session = PracticeSession::new();
        session.start(true).map_err(str::to_owned)?;
        let temp = tempfile::tempdir().map_err(|_| "cannot create temporary capture directory")?;
        Ok(Self {
            session,
            portal: None,
            temp,
            interval,
            allow_ocr,
            force_ocr,
            portal_required,
            options,
            keep_frames,
            frame_toggle: false,
            last_frame: None,
        })
    }

    /// The most recent retained screenshot, if any. Only meaningful when the
    /// reader was started with `keep_frames` or a portal source is active.
    pub(crate) fn last_frame(&self) -> Option<&Path> {
        self.last_frame.as_deref()
    }

    pub(crate) fn pause(&mut self) {
        self.session.pause();
        self.portal.take();
        self.last_frame = None;
    }

    pub(crate) fn resume(&mut self) {
        self.session.resume();
    }

    pub(crate) fn stop(&mut self) {
        self.session.stop();
        self.portal = None;
    }

    fn capture_frame(&mut self) -> Result<PathBuf, &'static str> {
        let path = if self.keep_frames && !self.portal_required {
            // Alternate between two files so a vision read never races the
            // next capture writing into the same path.
            self.frame_toggle = !self.frame_toggle;
            self.temp.path().join(if self.frame_toggle {
                "frame-a.png"
            } else {
                "frame-b.png"
            })
        } else {
            self.temp.path().join("latest.png")
        };
        let captured = if self.portal_required {
            if self.portal.is_none() {
                self.portal = Some(
                    PortalFrameSource::start(&self.session, path, self.interval)
                        .map_err(|_| "Could not start approved ScreenCast source")?,
                );
            }
            let stream = self.portal.as_mut().ok_or("ScreenCast unavailable")?;
            if stream.finished().unwrap_or(true) {
                return Err("ScreenCast permission revoked or session closed");
            }
            let mut found = None;
            for _ in 0..40 {
                if stream.path().exists() {
                    found = Some(stream.path().to_path_buf());
                    break;
                }
                if stream.finished().unwrap_or(true) {
                    return Err("ScreenCast permission revoked or session closed");
                }
                std::thread::sleep(Duration::from_millis(250));
            }
            found.ok_or("No approved ScreenCast frame")?
        } else {
            capture_to(&self.session, &path).map_err(|_| "Local screenshot capture unavailable")?;
            path
        };
        self.last_frame = Some(captured.clone());
        Ok(captured)
    }

    fn ocr_captured_frame(&self, screenshot: &Path, remove_after: bool) -> Reading {
        let text = extract_text(screenshot);
        if remove_after {
            let _ = std::fs::remove_file(screenshot);
        }
        match text {
            Ok(text) => Reading::Question(text.clone(), text, "ocr"),
            Err(_) => Reading::OcrFailed("Local OCR failed"),
        }
    }

    /// One accessibility-first read with the configured OCR fallback.
    pub(crate) fn read_once(&mut self) -> Reading {
        let root = socket_stream_core::paths::project_root();
        let outcome = match read_foreground_tree(
            &root,
            self.options.budget.max_nodes,
            self.options.budget.max_depth,
            self.options.budget.max_chars,
        ) {
            Ok(tree) => {
                let rendered = tree.render(self.options.mode, self.options.budget);
                if !self.force_ocr && !needs_ocr(Some(&rendered.text)) {
                    return Reading::Question(rendered.text, rendered.display, "accessibility");
                }

                let needs_capture = self.options.region_ocr && !rendered.image_regions.is_empty();
                if needs_capture {
                    let screenshot = match self.capture_frame() {
                        Ok(path) => path,
                        Err(message) => return Reading::OcrFailed(message),
                    };
                    let remove_after = !self.portal_required && !self.keep_frames;
                    let region_result = extract_regions_text(
                        &screenshot,
                        &rendered.image_regions,
                        self.options.ocr_config.max_regions,
                        self.options.ocr_config.min_region_pixels,
                        &self.options.ocr_config.tesseract_psm,
                    );
                    match region_result {
                        Ok(ocr_text) if !ocr_text.trim().is_empty() => {
                            if remove_after {
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
                        Ok(_) if self.allow_ocr => {
                            return self.ocr_captured_frame(&screenshot, remove_after);
                        }
                        Ok(_) => {
                            if remove_after {
                                let _ = std::fs::remove_file(&screenshot);
                            }
                            return Reading::Insufficient;
                        }
                        Err(_) if self.allow_ocr => {
                            return self.ocr_captured_frame(&screenshot, remove_after);
                        }
                        Err(_) => {
                            if remove_after {
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
        match plan_reading(&outcome, self.allow_ocr, self.force_ocr) {
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
            ReadingPlan::TryOcr => {
                let screenshot = match self.capture_frame() {
                    Ok(path) => path,
                    Err(message) => return Reading::OcrFailed(message),
                };
                self.ocr_captured_frame(&screenshot, !self.portal_required && !self.keep_frames)
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
    let options = a11y_options(args)?;
    let portal_required = has(args, "--portal") || requires_linux_portal();
    let mut events = Emitter::new();
    if let Err(message) = verify_provider(&provider).await {
        events.send("error", "provider", None, Some(message))?;
        return Err(message.into());
    }
    let mut reader = ScreenReader::start(
        allow_ocr,
        force_ocr,
        false,
        portal_required,
        options,
        interval,
    )?;
    if let Err(error) = events.send("session_started", "system", None, None) {
        reader.stop();
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
    // Latest successful screen read, kept as bounded context for manually
    // typed questions so "what does this error mean?" can see the screen.
    let mut last_screen: Option<String> = None;
    // One queued manual question while an answer is still streaming; latest
    // request wins once the current answer completes.
    let mut manual_pending: Option<String> = None;
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
                            reader.pause();
                            dedup = Deduplicator::new(128);
                            last_request.clear();
                            last_screen = None;
                            if let Some(task) = pending.take() { task.abort(); }
                            emit!("session_paused", "system", None, None);
                        }
                        Some(ControlCommand::Resume) if paused => {
                            paused = false;
                            reader.resume();
                            emit!("session_resumed", "system", None, None);
                        }
                        Some(ControlCommand::Stop) => break,
                        Some(ControlCommand::Question(text)) => {
                            let text = text.trim().to_owned();
                            if text.chars().filter(|c| c.is_alphabetic()).count() < 3 {
                                emit!("error", "system", None,
                                    Some("Type a longer question before sending."));
                            } else if pending.is_none() {
                                request_number += 1;
                                let rid = format!("manual-{request_number}");
                                last_request.clone_from(&rid);
                                let prompt = compose_prompt(&text, last_screen.as_deref());
                                emit_question!("manual", &rid, &text, &text);
                                emit!("answer_started", "manual", Some(&rid), None);
                                pending = Some(spawn_model_task(
                                    provider.clone(), rid, prompt, sender.clone(),
                                ));
                                warned_accessibility = false;
                                warned_ocr = false;
                            } else {
                                manual_pending = Some(text);
                            }
                        }
                        Some(ControlCommand::ReadScreen) => {
                            let reading = reader.read_once();
                            if let Reading::Question(text, ..) = &reading {
                                last_screen = Some(bound_context(text));
                            }
                            match screen_read_outcome(&mut events, &reading) {
                                Ok(Some((text, display, source))) => {
                                    if pending.is_none() {
                                        request_number += 1;
                                        let rid = format!("manual-{request_number}");
                                        last_request.clone_from(&rid);
                                        emit_question!(&source, &rid, &text, &display);
                                        emit!("answer_started", &source, Some(&rid), None);
                                        pending = Some(spawn_model_task(
                                            provider.clone(), rid, text, sender.clone(),
                                        ));
                                        warned_accessibility = false;
                                        warned_ocr = false;
                                    } else {
                                        manual_pending = Some(text);
                                    }
                                }
                                Ok(None) => {}
                                Err(error) => { event_error = Some(error); break; }
                            }
                        }
                        _ => {},
                    },
                    Ok(None) | Err(_) => control_open = false,
                }
            }
            _ = ticker.tick(), if observations < max && !paused => {
                observations += 1;
                let reading = reader.read_once();
                if let Reading::Question(text, ..) = &reading {
                    last_screen = Some(bound_context(text));
                }
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
                            pending = Some(spawn_model_task(
                                provider.clone(), rid, text, sender.clone(),
                            ));
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
                        // Start a queued manual question once the current
                        // answer completed; latest request wins.
                        if let Some(text) = manual_pending.take()
                            && text.chars().filter(|c| c.is_alphabetic()).count() >= 3
                        {
                            request_number += 1;
                            let rid = format!("manual-{request_number}");
                            last_request.clone_from(&rid);
                            let prompt = compose_prompt(&text, last_screen.as_deref());
                            emit_question!("manual", &rid, &text, &text);
                            emit!("answer_started", "manual", Some(&rid), None);
                            pending = Some(spawn_model_task(
                                provider.clone(), rid, prompt, sender.clone(),
                            ));
                        }
                    }
                }
            }
        }
    }
    if let Some(task) = pending {
        task.abort();
    }
    reader.stop();
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
        assert_eq!(
            control_command("read-screen"),
            Some(ControlCommand::ReadScreen)
        );
        assert_eq!(
            control_command("question What is binary search?"),
            Some(ControlCommand::Question("What is binary search?".into()))
        );
        assert_eq!(control_command("question"), None);
        assert_eq!(
            control_command("question   "),
            None,
            "payload trims away, leaving bare 'question'"
        );
        assert_eq!(control_command("questions about things"), None);
    }

    #[test]
    fn compose_prompt_labels_screen_text_as_untrusted_context() {
        assert_eq!(
            compose_prompt("Why does this fail?", None),
            "Why does this fail?"
        );
        assert_eq!(compose_prompt("Why?", Some("   ")), "Why?");
        let combined = compose_prompt("Why?", Some("def broken():\n    pass"));
        assert!(
            combined.starts_with("Why?\n\n[Latest screen text — untrusted data for context]\n")
        );
        assert!(combined.contains("def broken():"));
    }

    #[test]
    fn bound_context_caps_screen_text_by_chars_not_bytes() {
        assert_eq!(bound_context(""), "");
        let long = "é".repeat(SCREEN_CONTEXT_CHARS + 500);
        assert_eq!(bound_context(&long).chars().count(), SCREEN_CONTEXT_CHARS);
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
