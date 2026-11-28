//! Continuous, local Moonshine STT feeding the normal streaming answer UI.
//! Capture and inference are separate owned children. Pause destroys both and
//! discards pending speech; Resume starts a fresh audio stream. With
//! --read-screen, a parallel reader keeps the latest screen text (and, for
//! OpenAI-compatible providers, a screenshot for model vision) so spoken and
//! manually typed questions are answered with on-screen context. New stable
//! screen questions also dispatch independently, without interrupting answers.
use crate::live_context::{LiveContext, LiveTurn, SCREEN_QUESTION, ScreenInput};
use crate::live_events::Emitter;
use crate::live_observe::{ScreenReader, a11y_options, bound_context, plausible_question};
use crate::turn_gate::{SpeechRole, TurnGate};
use socket_stream_core::a11y::{A11yTree, TokenBudget};
use socket_stream_core::ai::{
    ApiEvent, MAX_IMAGE_BYTES, Provider, TokenUsage, stream_answer_with_history,
};
use socket_stream_core::context::Deduplicator;
use socket_stream_core::conversation::ConversationMessage;
use std::collections::VecDeque;
use std::io::BufRead;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use tokio::time::{Duration, Instant};

const MAX_EVENT: u64 = 128 * 1024;
const MAX_TEXT: usize = 8000;
/// Longest stdin control line: "question " plus a bounded manual question.
const MAX_CONTROL_LINE: usize = 9_000;
/// Longest manual question accepted over stdin after the "question " prefix.
const MAX_MANUAL_QUESTION: usize = 8_000;

fn option(args: &[String], flag: &str) -> Option<String> {
    args.windows(2).find(|p| p[0] == flag).map(|p| p[1].clone())
}

fn has(args: &[String], flag: &str) -> bool {
    args.iter().any(|arg| arg == flag)
}

fn bounded_u64(
    args: &[String],
    flag: &str,
    default: u64,
    lower: u64,
    upper: u64,
) -> Result<u64, String> {
    let value = option(args, flag)
        .map(|raw| {
            raw.parse::<u64>()
                .map_err(|_| format!("{flag} must be an integer"))
        })
        .transpose()?
        .unwrap_or(default);
    if !(lower..=upper).contains(&value) {
        return Err(format!("{flag} must be {lower}..{upper}"));
    }
    Ok(value)
}

/// One parallel screen-reading outcome delivered to the session loop.
struct ScreenUpdate {
    generation: u64,
    /// Role-free panel rendering (bounded).
    display: String,
    /// Bounded model context text.
    context: String,
    tree: Option<A11yTree>,
    source: &'static str,
    /// "ok" or "unavailable".
    status: &'static str,
    /// Path of the retained screenshot, when vision is enabled.
    frame: Option<PathBuf>,
    /// True when the read was explicitly requested via the read-screen
    /// control; such reads become questions, automatic reads do not.
    manual: bool,
}

/// Signals from the session loop to the blocking reader thread.
enum ReaderSignal {
    Read,
    Pause,
    Resume(u64),
    Stop,
}

/// Decode one control line. Pure so the protocol is testable without stdin.
fn control_line(line: &str) -> Option<Result<Control, String>> {
    let trimmed = line.trim();
    match trimmed {
        "pause" => return Some(Ok(Control::Pause)),
        "resume" => return Some(Ok(Control::Resume)),
        "stop" => return Some(Ok(Control::Stop)),
        "read-screen" => return Some(Ok(Control::ReadScreen)),
        "" => return None,
        _ => {}
    }
    if let Some(role) = trimmed.strip_prefix("speaker ") {
        return Some(SpeechRole::parse(role).map(Control::Speaker));
    }
    trimmed.strip_prefix("question ").map(|rest| {
        let text = rest.trim();
        if text.is_empty() {
            Err("Type a question after the question command.".into())
        } else if text.len() > MAX_MANUAL_QUESTION {
            Err("Manual question is too long.".into())
        } else {
            Ok(Control::Question(text.to_owned()))
        }
    })
}

enum Control {
    Pause,
    Resume,
    Stop,
    ReadScreen,
    Question(String),
    Speaker(SpeechRole),
}

#[derive(Debug, PartialEq)]
enum SpeechEvent {
    Ready,
    Partial(String),
    Final(String),
}

fn parse_event(bytes: &[u8]) -> Result<SpeechEvent, String> {
    let value: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|_| "Invalid Moonshine event; rebuild the speech runtime.")?;
    let text = value["text"].as_str().unwrap_or("");
    if text.chars().count() > MAX_TEXT {
        return Err("Moonshine transcript exceeded its limit.".into());
    }
    match value["type"].as_str() {
        Some("ready") => Ok(SpeechEvent::Ready),
        Some("partial") => Ok(SpeechEvent::Partial(text.into())),
        Some("final") => Ok(SpeechEvent::Final(text.into())),
        _ => Err("Built-in Moonshine failed; verify the speech runtime and model.".into()),
    }
}

async fn read_event<R: tokio::io::AsyncBufRead + Unpin>(
    reader: &mut R,
) -> Result<SpeechEvent, String> {
    let mut bytes = Vec::new();
    let count = reader
        .take(MAX_EVENT + 1)
        .read_until(b'\n', &mut bytes)
        .await
        .map_err(|_| "Moonshine event channel failed.")?;
    if count == 0 {
        return Err("Audio stream ended; check the selected device and OS permission.".into());
    }
    if bytes.len() as u64 > MAX_EVENT || bytes.last() != Some(&b'\n') {
        return Err("Invalid or oversized Moonshine event.".into());
    }
    parse_event(&bytes)
}

fn child(command: &mut Command) -> Result<Child, String> {
    command
        .kill_on_drop(true)
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| {
            "Audio helper unavailable; rebuild the speech runtime and native helpers.".into()
        })
}

async fn capture_command(root: &Path, source: &str, device: &str) -> Result<Command, String> {
    if cfg!(target_os = "macos") {
        if device != "default" {
            return Err(
                "Built-in macOS listening uses the default input selected in System Settings."
                    .into(),
            );
        }
        let mut cmd = Command::new(root.join("target/native-helpers/socket_stream_audio_stream"));
        cmd.args(["--authorized", "--source", source]);
        return Ok(cmd);
    }
    if !cfg!(target_os = "linux") {
        return Err("Built-in audio capture is supported on macOS and Linux only.".into());
    }
    let mut input = device.to_owned();
    if source == "system" && device == "default" {
        let result = tokio::time::timeout(
            Duration::from_secs(3),
            Command::new("pactl")
                .arg("get-default-sink")
                .kill_on_drop(true)
                .output(),
        )
        .await
        .map_err(|_| "Audio sink lookup timed out.")?
        .map_err(|_| "Install PulseAudio/PipeWire's pactl tool.")?;
        let sink = String::from_utf8_lossy(&result.stdout).trim().to_owned();
        if !result.status.success() || sink.is_empty() || sink.len() > 1024 {
            return Err(
                "Default playback monitor unavailable; select an explicit monitor device.".into(),
            );
        }
        input = format!("{sink}.monitor");
    }
    let mut cmd = Command::new("ffmpeg");
    cmd.args([
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "pulse",
        "-i",
        &input,
        "-ar",
        "16000",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "pipe:1",
    ]);
    Ok(cmd)
}

struct SpeechSession {
    worker: Child,
    capture: Option<Child>,
    tasks: Vec<JoinHandle<()>>,
}

impl Drop for SpeechSession {
    fn drop(&mut self) {
        for task in &self.tasks {
            task.abort();
        }
        // Child::kill_on_drop also covers cancellation during initialization.
    }
}

impl SpeechSession {
    async fn start(
        root: &Path,
        source: &str,
        device: &str,
        generation: u64,
        tx: mpsc::Sender<(u64, Result<SpeechEvent, String>)>,
    ) -> Result<Self, String> {
        let executable = root.join(
            "target/native-helpers/moonshine/socket_stream_moonshine/socket_stream_moonshine",
        );
        if !executable.is_file() {
            return Err("Built-in Moonshine is missing. Build it with Python 3.10+: scripts/build-moonshine.py".into());
        }
        let worker = child(
            Command::new(executable)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped()),
        )?;
        let mut session = Self {
            worker,
            capture: None,
            tasks: Vec::new(),
        };
        let mut reader = BufReader::new(
            session
                .worker
                .stdout
                .take()
                .ok_or("Missing speech event pipe.")?,
        );
        if tokio::time::timeout(Duration::from_secs(30), read_event(&mut reader))
            .await
            .map_err(|_| "Moonshine model initialization timed out.")??
            != SpeechEvent::Ready
        {
            return Err("Moonshine did not initialize correctly.".into());
        }
        // No device is opened until the local model is ready.
        let mut cmd = capture_command(root, source, device).await?;
        let mut capture = child(cmd.stdin(Stdio::null()).stdout(Stdio::piped()))?;
        let mut audio = capture.stdout.take().ok_or("Missing audio pipe.")?;
        session.capture = Some(capture);
        let mut input = session
            .worker
            .stdin
            .take()
            .ok_or("Missing speech input pipe.")?;
        let pump_tx = tx.clone();
        session.tasks.push(tokio::spawn(async move {
            // Tokio copy uses a fixed buffer; OS pipes provide backpressure.
            let _ = tokio::io::copy(&mut audio, &mut input).await;
            drop(input);
            let _ = pump_tx
                .send((
                    generation,
                    Err("Audio input stopped; check the device and OS permission.".into()),
                ))
                .await;
        }));
        session.tasks.push(tokio::spawn(async move {
            loop {
                let event = read_event(&mut reader).await;
                let failed = event.is_err();
                if tx.send((generation, event)).await.is_err() || failed {
                    break;
                }
            }
        }));
        Ok(session)
    }

    async fn stop(&mut self) {
        for task in &self.tasks {
            task.abort();
        }
        if let Some(capture) = &mut self.capture {
            let _ = capture.kill().await;
        }
        let _ = self.worker.kill().await;
        for task in self.tasks.drain(..) {
            let _ = task.await;
        }
    }
}

#[derive(Default)]
struct Transcript {
    lines: VecDeque<String>,
    pending: String,
    last_final: Option<String>,
}

impl Transcript {
    fn finalize(&mut self, text: &str) -> bool {
        let text = text.trim();
        if text.is_empty() || self.last_final.as_deref() == Some(text) {
            return false;
        }
        self.last_final = Some(text.to_owned());
        self.lines.push_back(text.to_owned());
        while self.lines.len() > 20 || self.lines.iter().map(String::len).sum::<usize>() > 24_000 {
            self.lines.pop_front();
        }
        if !self.pending.is_empty() {
            self.pending.push(' ');
        }
        self.pending.push_str(text);
        self.pending = self
            .pending
            .chars()
            .rev()
            .take(MAX_TEXT)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        true
    }

    fn display(&self, partial: &str) -> String {
        self.lines
            .iter()
            .map(String::as_str)
            .chain(std::iter::once(partial))
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join("\n")
    }
}

#[derive(Clone, Default)]
struct AnswerView {
    text: String,
    done: bool,
    failed: bool,
    error: Option<String>,
    usage: Option<TokenUsage>,
}

struct AnswerTask(JoinHandle<()>);
impl Drop for AnswerTask {
    fn drop(&mut self) {
        self.0.abort();
    }
}

fn start_answer(
    provider: Provider,
    prompt: String,
    image: Option<Vec<u8>>,
    history: Vec<ConversationMessage>,
) -> (AnswerTask, watch::Receiver<AnswerView>) {
    let (tx, rx) = watch::channel(AnswerView::default());
    let task = tokio::spawn(async move {
        let (deltas, mut incoming) = mpsc::channel::<String>(32);
        let (meta, mut metadata) = mpsc::channel::<ApiEvent>(8);
        let inference = stream_answer_with_history(
            &provider,
            &prompt,
            &history,
            image.as_deref(),
            &deltas,
            Some(&meta),
        );
        tokio::pin!(inference);
        let append = |delta: String| {
            tx.send_modify(|view| {
                let room = 64_000_usize.saturating_sub(view.text.chars().count());
                view.text.extend(delta.chars().take(room));
            });
        };
        let result = loop {
            tokio::select! {
                Some(delta) = incoming.recv() => append(delta),
                Some(event) = metadata.recv() => {
                    if let ApiEvent::Usage(usage) = event {
                        tx.send_modify(|view| view.usage = Some(usage));
                    }
                }
                result = &mut inference => break result,
            }
        };
        while let Ok(delta) = incoming.try_recv() {
            append(delta);
        }
        while let Ok(event) = metadata.try_recv() {
            if let ApiEvent::Usage(usage) = event {
                tx.send_modify(|view| view.usage = Some(usage));
            }
        }
        tx.send_modify(|view| {
            view.done = true;
            view.failed = result.is_err();
            view.error = result
                .as_ref()
                .err()
                .map(|error| error.to_string().chars().take(500).collect());
        });
    });
    (AnswerTask(task), rx)
}

fn controls() -> mpsc::Receiver<String> {
    let (tx, rx) = mpsc::channel(8);
    std::thread::spawn(move || {
        let stdin = std::io::stdin();
        let mut reader = stdin.lock();
        loop {
            let mut bytes = Vec::new();
            if std::io::Read::take(&mut reader, MAX_CONTROL_LINE as u64)
                .read_until(b'\n', &mut bytes)
                .unwrap_or(0)
                == 0
            {
                break;
            }
            if bytes.len() > MAX_CONTROL_LINE {
                break;
            }
            let line = String::from_utf8_lossy(&bytes).trim().to_owned();
            // Forward pause/resume/stop/read-screen and bounded "question …"
            // lines; anything else is ignored so stray input cannot kill the
            // session.
            let recognized = matches!(
                line.as_str(),
                "pause" | "resume" | "stop" | "read-screen" | "next-question"
            ) || line.starts_with("question ")
                || line.starts_with("speaker ");
            if recognized && tx.blocking_send(line).is_err() {
                break;
            }
        }
    });
    rx
}

/// Read the screen once for context and automatic/manual question detection.
fn deliver_read(
    reader: &mut ScreenReader,
    updates: &tokio::sync::mpsc::UnboundedSender<ScreenUpdate>,
    manual: bool,
    generation: u64,
) {
    use crate::live_observe::Reading;
    let reading = reader.read_once();
    let frame = reader.last_frame().map(Path::to_path_buf);
    let tree = reader.last_tree().cloned();
    let update = match reading {
        Reading::Question(text, display, source) => {
            let context = bound_context(&text);
            let empty = context.trim().is_empty();
            ScreenUpdate {
                generation,
                display,
                context,
                tree,
                source,
                status: if empty { "unavailable" } else { "ok" },
                frame,
                manual,
            }
        }
        Reading::Nothing => ScreenUpdate {
            generation,
            display: "No focused readable question window. Focus the question window; enable accessibility or opt-in OCR if needed.".into(),
            context: String::new(),
            tree: None,
            source: "system",
            status: "unavailable",
            frame,
            manual,
        },
        Reading::Insufficient => ScreenUpdate {
            generation,
            display: "No readable content on the focused window. Enable local OCR fallback for screenshots.".into(),
            context: String::new(),
            tree: None,
            source: "system",
            status: "unavailable",
            frame,
            manual,
        },
        Reading::AccessibilityUnavailable(reason) => ScreenUpdate {
            generation,
            display: if reason.is_empty() {
                "Accessibility unavailable; enable local OCR fallback for screenshots.".into()
            } else {
                format!("Accessibility unavailable: {reason}")
            },
            context: String::new(),
            tree: None,
            source: "system",
            status: "unavailable",
            frame,
            manual,
        },
        Reading::OcrFailed(reason) => ScreenUpdate {
            generation,
            display: format!("Screenshot read failed: {reason}"),
            context: String::new(),
            tree: None,
            source: "system",
            status: "unavailable",
            frame,
            manual,
        },
    };
    let _ = updates.send(update);
}

/// Start the parallel screen-reading thread. It performs one immediate read,
/// then reads on the interval, delivering every outcome to the session loop.
/// Dropping the returned signal sender stops the thread.
fn start_screen_reader(
    args: &[String],
) -> (
    tokio::sync::mpsc::UnboundedReceiver<ScreenUpdate>,
    std::sync::mpsc::Sender<ReaderSignal>,
) {
    let (updates_tx, updates_rx) = tokio::sync::mpsc::unbounded_channel::<ScreenUpdate>();
    let (signals_tx, signals_rx) = std::sync::mpsc::channel::<ReaderSignal>();
    let args = args.to_vec();
    std::thread::spawn(move || {
        let interval = bounded_u64(&args, "--read-interval", 5, 2, 3600).unwrap_or(5);
        let (reader, options_error) = match a11y_options(&args) {
            Ok(options) => {
                let allow_ocr = has(&args, "--allow-ocr") || has(&args, "--force-ocr");
                let force_ocr = has(&args, "--force-ocr");
                let vision = !has(&args, "--no-vision");
                match ScreenReader::start(
                    allow_ocr,
                    force_ocr,
                    vision,
                    socket_stream_core::capture::requires_linux_portal(),
                    options,
                    interval,
                ) {
                    Ok(reader) => (Some(reader), None),
                    Err(message) => (None, Some(message)),
                }
            }
            Err(message) => (None, Some(message)),
        };
        let mut reader = match reader {
            Some(reader) => reader,
            None => {
                let _ = updates_tx.send(ScreenUpdate {
                    generation: 1,
                    display: options_error.unwrap_or_default(),
                    context: String::new(),
                    tree: None,
                    source: "system",
                    status: "unavailable",
                    frame: None,
                    manual: false,
                });
                return;
            }
        };
        // One immediate read so context exists before the first interval.
        let mut generation = 1;
        deliver_read(&mut reader, &updates_tx, false, generation);
        loop {
            match signals_rx.recv_timeout(Duration::from_secs(interval)) {
                Ok(ReaderSignal::Stop) | Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                    break;
                }
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                    deliver_read(&mut reader, &updates_tx, false, generation);
                }
                Ok(ReaderSignal::Read) => deliver_read(&mut reader, &updates_tx, true, generation),
                Ok(ReaderSignal::Pause) => reader.pause(),
                Ok(ReaderSignal::Resume(epoch)) => {
                    generation = epoch;
                    reader.resume();
                    deliver_read(&mut reader, &updates_tx, false, generation);
                }
            }
        }
        reader.stop();
    });
    (updates_rx, signals_tx)
}

/// The latest screenshot is attached to provider requests only for
/// OpenAI-compatible endpoints; Codex CLI and the mock provider receive the
/// OCR'd screen text instead. PNG magic bytes guard against stale files.
fn vision_frame(provider: &Provider, frame: Option<&Path>) -> Option<Vec<u8>> {
    if !matches!(provider, Provider::OpenAiCompatible { .. }) {
        return None;
    }
    let bytes = std::fs::read(frame?).ok()?;
    (bytes.len() <= MAX_IMAGE_BYTES && bytes.starts_with(&[0x89, b'P', b'N', b'G']))
        .then_some(bytes)
}

pub async fn run(args: &[String], provider: Provider) -> Result<(), String> {
    // The OS pipe is our bounded output queue. Never wait for a stalled GUI:
    // Emitter returns an error on EAGAIN/EPIPE, dropping the session and killing
    // capture. Do not change a terminal's shared open-file description: stdin
    // may share it, and making it nonblocking would turn idle input into EOF.
    #[cfg(unix)]
    unsafe {
        if libc::isatty(libc::STDOUT_FILENO) == 0 {
            let flags = libc::fcntl(libc::STDOUT_FILENO, libc::F_GETFL);
            if flags == -1
                || libc::fcntl(libc::STDOUT_FILENO, libc::F_SETFL, flags | libc::O_NONBLOCK) == -1
            {
                return Err("Cannot configure nonblocking audio event delivery.".into());
            }
        }
    }
    let mut emitter = Emitter::new();
    let result = run_session(args, provider, &mut emitter).await;
    if let Err(message) = &result {
        emitter.send("error", "system", None, Some(message))?;
    }
    result
}

async fn run_session(
    args: &[String],
    provider: Provider,
    emitter: &mut Emitter,
) -> Result<(), String> {
    if !args.iter().any(|a| a == "--authorized") {
        return Err("Explicit --authorized is required for audio listening.".into());
    }
    let source = option(args, "--source").unwrap_or_else(|| "microphone".into());
    if !matches!(source.as_str(), "microphone" | "system") {
        return Err("--source must be microphone or system.".into());
    }
    let device = option(args, "--device").unwrap_or_else(|| "default".into());
    let role =
        SpeechRole::parse(&option(args, "--speaker-role").unwrap_or_else(|| "practice".into()))?;
    let mut turn_gate = TurnGate::new(role);
    if device.trim().is_empty() || device.len() > 1024 || device.contains('\0') {
        return Err("Invalid audio device.".into());
    }
    let label = if source == "system" {
        "playback"
    } else {
        "microphone"
    };
    let read_screen = has(args, "--read-screen");
    let mut context_budget = TokenBudget::default();
    if read_screen {
        // Validate reader options up front so a bad flag fails fast instead
        // of surfacing later as an unavailable screen read.
        bounded_u64(args, "--read-interval", 5, 2, 3600)?;
        context_budget = a11y_options(args)?.budget;
    }
    let root = socket_stream_core::paths::project_root();
    let mut control = controls();
    let (tx, mut events) = mpsc::channel(32);
    let (mut screen_updates, reader_signals) = if read_screen {
        start_screen_reader(args)
    } else {
        let (updates_tx, updates_rx) = tokio::sync::mpsc::unbounded_channel::<ScreenUpdate>();
        let (signals_tx, signals_rx) = std::sync::mpsc::channel::<ReaderSignal>();
        drop(updates_tx);
        drop(signals_rx);
        (updates_rx, signals_tx)
    };
    let mut screen_context = String::new();
    let mut screen_tree: Option<A11yTree> = None;
    let mut model_context = LiveContext::default();
    let mut pending_context: Option<LiveTurn> = None;
    let mut pending_gate: Option<TurnGate> = None;
    let mut pending_screen_signature = None;
    let mut screen_frame: Option<PathBuf> = None;
    let mut manual_pending: Option<String> = None;
    let mut manual_number = 0_u64;
    let mut warned_screen = false;
    let mut generation = 1;
    let mut session: Option<SpeechSession> = None;
    let mut starting = true;
    let mut paused = false;
    let mut transcript = Transcript::default();
    let mut answer_task = None;
    let (_unused, mut answer_rx) = watch::channel(AnswerView::default());
    let mut answer_id = String::new();
    let mut answer_sent = 0;
    let mut request = 0_u64;
    let mut due: Option<Instant> = None;
    let mut interrupt = Box::pin(tokio::signal::ctrl_c());
    #[cfg(unix)]
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .map_err(|_| "Cannot install audio stop handler.")?;
    emitter.send("session_started", label, None, None)?;
    let mut screen_dedup = Deduplicator::new(64);
    emitter.send(
        "speech_status",
        label,
        None,
        Some(match turn_gate.role {
            SpeechRole::Interviewee => {
                "My answers mode · automatic replies suppressed; choose Solo practice"
            }
            SpeechRole::Unknown => "Mixed/unknown mode · manual Send only",
            SpeechRole::Interviewer => "Question mode · waiting for finalized questions",
            SpeechRole::Practice => "Solo practice · waiting for finalized speech",
        }),
    )?;
    // Start one manual request. A typed question gets the latest screen text
    // as context; a manual capture's read IS the question, so no extra
    // context is added. Vision frames are attached when the provider can use
    // them.
    macro_rules! start_manual {
        ($text:expr, $source:expr, $with_context:expr) => {{
            manual_number += 1;
            answer_id = format!("manual-{manual_number}");
            emitter.send("question_detected", $source, Some(&answer_id), Some(&$text))?;
            emitter.send("answer_started", $source, Some(&answer_id), None)?;
            let mut staged_gate = turn_gate.clone();
            let question_prompt = if $with_context {
                staged_gate.manual(&$text)
            } else {
                // Track the actual detected question for speech refinement,
                // while the model receives its structured screen update only.
                // Reusing a generic gate key would label every changed screen
                // as a clarification and duplicate the previous tree/answer.
                let _ = staged_gate.manual(&$text);
                SCREEN_QUESTION.to_owned()
            };
            let input = if !read_screen {
                ScreenInput::None
            } else if let Some(tree) = &screen_tree {
                ScreenInput::Tree(tree)
            } else if !screen_context.is_empty() {
                ScreenInput::Text(&screen_context)
            } else {
                ScreenInput::Unavailable
            };
            let turn = match model_context.prepare(&question_prompt, input, context_budget) {
                Ok(turn) => turn,
                Err(message) => {
                    pending_screen_signature = None;
                    transcript.last_final = None;
                    emitter.send("error", "system", Some(&answer_id), Some(&message))?;
                    continue;
                }
            };
            let prompt = turn.prompt().to_owned();
            let history = turn.history().to_vec();
            let image = vision_frame(&provider, screen_frame.as_deref());
            emitter.send("ai_input", $source, Some(&answer_id), Some(&prompt))?;
            let (task, rx) = start_answer(provider.clone(), prompt, image, history);
            pending_context = Some(turn);
            pending_gate = Some(staged_gate);
            answer_task = Some(task);
            answer_rx = rx;
            answer_sent = 0;
        }};
    }
    loop {
        // Startup remains cancellable: no capture survives a cancelled future.
        let startup = async {
            if starting {
                SpeechSession::start(&root, &source, &device, generation, tx.clone()).await
            } else {
                std::future::pending().await
            }
        };
        tokio::select! {
            biased;
            _ = &mut interrupt => break,
            _ = async {
                #[cfg(unix)] { terminate.recv().await; }
                #[cfg(not(unix))] { std::future::pending::<()>().await; }
            } => break,
            command = control.recv() => {
                match command.as_deref() {
                    Some("stop") => {
                        let _ = reader_signals.send(ReaderSignal::Stop);
                        break;
                    }
                    Some("pause") if !paused => {
                        starting = false;
                        paused = true;
                        generation += 1;
                        if let Some(mut old) = session.take() { old.stop().await; }
                        answer_task = None;
                        pending_context = None;
                        pending_gate = None;
                        pending_screen_signature = None;
                        screen_tree = None;
                        screen_context.clear();
                        screen_frame = None;
                        transcript.pending.clear();
                        manual_pending = None;
                        due = None;
                        let _ = reader_signals.send(ReaderSignal::Pause);
                        emitter.send("session_paused", label, None, None)?;
                    }
                    Some("resume") if paused => {
                        paused = false;
                        starting = true;
                        transcript.last_final = None;
                        let _ = reader_signals.send(ReaderSignal::Resume(generation));
                        emitter.send("session_resumed", label, None, None)?;
                        if let Some(text) = manual_pending.take() {
                            start_manual!(text, "manual", true);
                        }
                    }
                    Some("read-screen") => {
                        let _ = reader_signals.send(ReaderSignal::Read);
                    }
                    Some("next-question") if !paused => {
                        if answer_task.take().is_some() {
                            emitter.send("answer_cancelled", label, Some(&answer_id), None)?;
                        }
                        pending_context = None;
                        pending_gate = None;
                        pending_screen_signature = None;
                        transcript.last_final = None;
                        if let Some(text) = manual_pending.take() {
                            start_manual!(text, "manual", true);
                        } else if !transcript.pending.is_empty() {
                            due = Some(Instant::now());
                        }
                    }
                    Some(line) if line.starts_with("question ") => match control_line(line) {
                        Some(Ok(Control::Question(text))) => {
                            if text.chars().filter(|c| c.is_alphabetic()).count() < 3 {
                                emitter.send(
                                    "error", "system", None,
                                    Some("Type a longer question before sending."),
                                )?;
                            } else if answer_task.is_none() && !paused {
                                start_manual!(text, "manual", true);
                            } else {
                                manual_pending = Some(text);
                            }
                        }
                        Some(Err(message)) => {
                            emitter.send("error", "system", None, Some(&message))?;
                        }
                        _ => {}
                    },
                    Some(line) if line.starts_with("speaker ") => match control_line(line) {
                        Some(Ok(Control::Speaker(role))) => {
                            turn_gate.role = role;
                            // Pending speech belongs to the previous role; never
                            // reinterpret it as a new speaker after a switch.
                            transcript.pending.clear();
                            transcript.last_final = None;
                            due = None;
                        }
                        Some(Err(message)) => emitter.send("error", "system", None, Some(&message))?,
                        _ => {}
                    },
                    None => break,
                    _ => {}
                }
            }
            Some(update) = screen_updates.recv(), if read_screen => {
                let ScreenUpdate { generation: screen_generation, display, context, tree, source, status, frame, manual } = update;
                if paused || screen_generation != generation { continue; }
                screen_tree = tree;
                if status == "ok" && !context.trim().is_empty() {
                    screen_context = context;
                    warned_screen = false;
                } else {
                    screen_context.clear();
                }
                // Never retain a screenshot from an earlier read after a new
                // text-only or failed read.
                screen_frame = frame;
                if status == "ok" {
                    emitter.send_screen_read(source, "ok", &display)?;
                } else if manual || !warned_screen {
                    emitter.send_screen_read("system", "unavailable", &display)?;
                    warned_screen = true;
                    if manual {
                        emitter.send("error", "system", None, Some(&display))?;
                    }
                }
                // Selected screen capture must work independently of speech.
                // Learn volatility while busy, but never replace an active
                // answer or consume the signature until dispatch is possible.
                if !manual && status == "ok" && plausible_question(&screen_context) {
                    let (signature, stable) = screen_dedup.observe(&screen_context);
                    if stable && screen_dedup.is_new(signature) && answer_task.is_none() && !paused {
                        pending_screen_signature = Some(signature);
                        let text = screen_context.clone();
                        start_manual!(text, source, false);
                    }
                }
                // A manual capture becomes a question: the read itself is the
                // question text, so the model answers what is on screen.
                if manual && status == "ok" && !screen_context.trim().is_empty() {
                    if answer_task.is_none() && !paused {
                        let text = screen_context.clone();
                        start_manual!(text, source, false);
                    } else {
                        manual_pending = Some(SCREEN_QUESTION.to_owned());
                    }
                }
            }
            result = startup, if starting => {
                session = Some(result?);
                starting = false;
            }
            Some((epoch, event)) = events.recv() => {
                if epoch != generation || paused { continue; }
                match event? {
                    SpeechEvent::Partial(text) => {
                        if !text.is_empty() && !transcript.pending.is_empty() { due = Some(Instant::now() + Duration::from_millis(900)); }
                        emitter.send("transcript_updated", label, None, Some(&transcript.display(&text)))?;
                        emitter.send("speech_status", label, None, Some("Transcribing · waiting for a speech pause"))?;
                    }
                    SpeechEvent::Final(text) => {
                        if transcript.finalize(&text) {
                            due = Some(Instant::now() + Duration::from_millis(900));
                            emitter.send("transcript_updated", label, None, Some(&transcript.display("")))?;
                            emitter.send("speech_status", label, None, Some("Final speech received · checking request gate"))?;
                        }
                    }
                    SpeechEvent::Ready => {}
                }
            }
            _ = tokio::time::sleep_until(due.unwrap_or_else(|| Instant::now() + Duration::from_secs(3600))),
                if due.is_some() && answer_task.is_none() && !paused => {
                let prompt = std::mem::take(&mut transcript.pending);
                due = None;
                if prompt.chars().filter(|c| c.is_alphabetic()).count() < 3 {
                    emitter.send("speech_status", label, None, Some("Not sent · speech too short"))?;
                    continue;
                }
                let mut staged_gate = turn_gate.clone();
                let Some(permitted_prompt) = staged_gate.speech(&prompt) else {
                    let reason = match turn_gate.role {
                        SpeechRole::Interviewee => "Not sent · My answers mode; choose Solo practice to answer your speech",
                        SpeechRole::Unknown => "Not sent · Mixed/unknown mode; use manual Send",
                        _ => "Not sent · no new question detected",
                    };
                    emitter.send("speech_status", label, None, Some(reason))?;
                    continue;
                };
                request += 1;
                answer_id = format!("spoken-{request}");
                emitter.send("question_detected", label, Some(&answer_id), Some(&prompt))?;
                emitter.send("answer_started", label, Some(&answer_id), None)?;
                let input = if !read_screen {
                    ScreenInput::None
                } else if let Some(tree) = &screen_tree {
                    ScreenInput::Tree(tree)
                } else if !screen_context.is_empty() {
                    ScreenInput::Text(&screen_context)
                } else {
                    ScreenInput::Unavailable
                };
                let turn = match model_context.prepare(&permitted_prompt, input, context_budget) {
                    Ok(turn) => turn,
                    Err(message) => {
                        transcript.last_final = None;
                        emitter.send("error", "system", Some(&answer_id), Some(&message))?;
                        continue;
                    }
                };
                let context_prompt = turn.prompt().to_owned();
                let history = turn.history().to_vec();
                let image = vision_frame(&provider, screen_frame.as_deref());
                emitter.send("ai_input", label, Some(&answer_id), Some(&context_prompt))?;
                let (task, rx) = start_answer(provider.clone(), context_prompt, image, history);
                pending_context = Some(turn);
                pending_gate = Some(staged_gate);
                pending_screen_signature = None;
                answer_task = Some(task);
                answer_rx = rx;
                answer_sent = 0;
            }
            changed = answer_rx.changed(), if answer_task.is_some() => {
                if changed.is_err() { return Err("Answer worker stopped unexpectedly.".into()); }
                let view = answer_rx.borrow_and_update().clone();
                if view.text.len() > answer_sent {
                    emitter.send("answer_delta", label, Some(&answer_id), Some(&view.text[answer_sent..]))?;
                    answer_sent = view.text.len();
                }
                if view.done {
                    if let Some(usage) = &view.usage {
                        emitter.send("token_usage", "provider", Some(&answer_id), Some(&usage.summary()))?;
                    }
                    if !view.failed {
                        if let Some(turn) = pending_context.take() {
                            model_context.commit(turn, &view.text)?;
                        }
                        if let Some(signature) = pending_screen_signature.take() {
                            screen_dedup.remember(signature);
                        }
                        if let Some(mut accepted_gate) = pending_gate.take() {
                            // Speaker changes are immediate user choices;
                            // completing an older request cannot undo them.
                            accepted_gate.role = turn_gate.role;
                            turn_gate = accepted_gate;
                        }
                    } else {
                        pending_context = None;
                        pending_gate = None;
                        pending_screen_signature = None;
                        transcript.last_final = None;
                    }
                    if view.failed { emitter.send("error", "provider", Some(&answer_id), Some(view.error.as_deref().unwrap_or("AI provider failed; check endpoint, credentials and model.")))?; }
                    else { emitter.send("answer_completed", label, Some(&answer_id), None)?; }
                    answer_task = None;
                    // Start a queued manual request once the current answer
                    // completed; latest request wins.
                    if !paused
                        && let Some(text) = manual_pending.take()
                        && text.chars().filter(|c| c.is_alphabetic()).count() >= 3
                    {
                        start_manual!(text, "manual", true);
                    }
                }
            }
        }
    }
    drop(answer_task);
    if let Some(mut session) = session {
        session.stop().await;
    }
    emitter.send("session_stopped", label, None, None)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transcript_deduplicates_finals_and_bounds_history_and_pending() {
        let mut t = Transcript::default();
        assert!(t.finalize("How does binary search work?"));
        assert!(!t.finalize("How does binary search work?"));
        assert!(t.finalize("Explain its complexity."));
        assert_eq!(
            t.pending,
            "How does binary search work? Explain its complexity."
        );
        for n in 0..100 {
            t.finalize(&format!("{n} {}", "é".repeat(8000)));
        }
        assert!(t.pending.chars().count() <= MAX_TEXT);
        assert!(t.lines.iter().map(String::len).sum::<usize>() <= 24_000);
    }

    #[tokio::test]
    async fn worker_protocol_rejects_truncation_oversize_and_errors() {
        assert_eq!(
            read_event(&mut &b"{\"type\":\"final\",\"text\":\"hello\"}\n"[..])
                .await
                .unwrap(),
            SpeechEvent::Final("hello".into())
        );
        assert!(read_event(&mut &b"{\"type\":\"ready\"}"[..]).await.is_err());
        assert!(
            read_event(&mut &b"{\"type\":\"error\",\"text\":\"private diagnostic\"}\n"[..])
                .await
                .unwrap_err()
                .find("private diagnostic")
                .is_none()
        );
        assert!(
            read_event(&mut vec![b'x'; MAX_EVENT as usize + 2].as_slice())
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn mock_answer_stream_completes_in_bounded_view() {
        let (_task, mut rx) = start_answer(
            Provider::Mock,
            "Explain binary search".into(),
            None,
            Vec::new(),
        );
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                rx.changed().await.unwrap();
                let value = rx.borrow_and_update().clone();
                if value.done {
                    assert!(!value.failed);
                    assert!(!value.text.is_empty());
                    break;
                }
            }
        })
        .await
        .unwrap();
    }

    #[test]
    fn control_lines_accept_pause_resume_stop_readscreen_and_questions() {
        assert!(matches!(control_line("pause"), Some(Ok(Control::Pause))));
        assert!(matches!(
            control_line("  resume  "),
            Some(Ok(Control::Resume))
        ));
        assert!(matches!(control_line("stop\n"), Some(Ok(Control::Stop))));
        assert!(matches!(
            control_line("read-screen"),
            Some(Ok(Control::ReadScreen))
        ));
        assert!(matches!(
            control_line("question What is binary search?"),
            Some(Ok(Control::Question(text))) if text == "What is binary search?"
        ));
        assert!(control_line("").is_none());
        assert!(control_line("quit").is_none());
        assert!(
            control_line("question").is_none(),
            "bare question without payload is ignored"
        );
        assert!(
            control_line("question    ").is_none(),
            "payload trims away, leaving bare 'question'"
        );
        assert!(matches!(
            control_line(&format!("question {}", "x".repeat(MAX_MANUAL_QUESTION + 1))),
            Some(Err(message)) if message.contains("too long")
        ));
    }

    #[test]
    fn vision_frames_require_png_magic_and_openai_compatible_provider() {
        let directory = tempfile::tempdir().expect("tempdir");
        let png = directory.path().join("frame.png");
        std::fs::write(&png, [0x89_u8, b'P', b'N', b'G', 0x0D, 0x0A]).expect("write frame");
        let openai = Provider::OpenAiCompatible {
            base_url: "http://localhost:11434/v1".into(),
            model: "m".into(),
            api_key: "k".into(),
        };
        assert!(vision_frame(&openai, Some(&png)).is_some());
        // Mock and Codex providers never receive pixels.
        assert!(vision_frame(&Provider::Mock, Some(&png)).is_none());
        let text_file = directory.path().join("note.txt");
        std::fs::write(&text_file, b"not an image").expect("write note");
        assert!(vision_frame(&openai, Some(&text_file)).is_none());
        assert!(vision_frame(&openai, None).is_none());
        let big_path = directory.path().join("big.png");
        let mut big = vec![0_u8; MAX_IMAGE_BYTES + 1];
        big[..4].copy_from_slice(&[0x89, b'P', b'N', b'G']);
        std::fs::write(&big_path, big).expect("write big");
        assert!(vision_frame(&openai, Some(&big_path)).is_none());
    }
}
