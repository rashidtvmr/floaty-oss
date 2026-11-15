//! Continuous, local Moonshine STT feeding the normal streaming answer UI.
//! Capture and inference are separate owned children. Pause destroys both and
//! discards pending speech; Resume starts a fresh audio stream.
use crate::live_events::Emitter;
use socket_stream_core::ai::{Provider, stream_answer};
use std::collections::VecDeque;
use std::io::BufRead;
use std::path::Path;
use std::process::Stdio;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use tokio::time::{Duration, Instant};

const MAX_EVENT: u64 = 128 * 1024;
const MAX_TEXT: usize = 8000;

fn option(args: &[String], flag: &str) -> Option<String> {
    args.windows(2).find(|p| p[0] == flag).map(|p| p[1].clone())
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
}

struct AnswerTask(JoinHandle<()>);
impl Drop for AnswerTask {
    fn drop(&mut self) {
        self.0.abort();
    }
}

fn start_answer(provider: Provider, prompt: String) -> (AnswerTask, watch::Receiver<AnswerView>) {
    let (tx, rx) = watch::channel(AnswerView::default());
    let task = tokio::spawn(async move {
        let (deltas, mut incoming) = mpsc::channel::<String>(32);
        let inference = stream_answer(&provider, &prompt, &deltas);
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
                result = &mut inference => break result,
            }
        };
        while let Ok(delta) = incoming.try_recv() {
            append(delta);
        }
        tx.send_modify(|view| {
            view.done = true;
            view.failed = result.is_err();
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
            if std::io::Read::take(&mut reader, 65)
                .read_until(b'\n', &mut bytes)
                .unwrap_or(0)
                == 0
            {
                break;
            }
            if bytes.len() > 64 {
                break;
            }
            let line = String::from_utf8_lossy(&bytes).trim().to_owned();
            if matches!(line.as_str(), "pause" | "resume" | "stop")
                && tx.blocking_send(line).is_err()
            {
                break;
            }
        }
    });
    rx
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
    if device.trim().is_empty() || device.len() > 1024 || device.contains('\0') {
        return Err("Invalid audio device.".into());
    }
    let label = if source == "system" {
        "playback"
    } else {
        "microphone"
    };
    let root = socket_stream_core::paths::project_root();
    let mut control = controls();
    let (tx, mut events) = mpsc::channel(32);
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
                    Some("stop") => break,
                    Some("pause") if !paused => {
                        starting = false;
                        paused = true;
                        generation += 1;
                        if let Some(mut old) = session.take() { old.stop().await; }
                        answer_task = None;
                        transcript.pending.clear();
                        due = None;
                        emitter.send("session_paused", label, None, None)?;
                    }
                    Some("resume") if paused => {
                        paused = false;
                        starting = true;
                        transcript.last_final = None;
                        emitter.send("session_resumed", label, None, None)?;
                    }
                    None => break,
                    _ => {}
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
                    }
                    SpeechEvent::Final(text) => {
                        if transcript.finalize(&text) {
                            due = Some(Instant::now() + Duration::from_millis(900));
                            emitter.send("transcript_updated", label, None, Some(&transcript.display("")))?;
                        }
                    }
                    SpeechEvent::Ready => {}
                }
            }
            _ = tokio::time::sleep_until(due.unwrap_or_else(|| Instant::now() + Duration::from_secs(3600))),
                if due.is_some() && answer_task.is_none() && !paused => {
                let prompt = std::mem::take(&mut transcript.pending);
                due = None;
                if prompt.chars().filter(|c| c.is_alphabetic()).count() < 3 { continue; }
                request += 1;
                answer_id = format!("spoken-{request}");
                emitter.send("question_detected", label, Some(&answer_id), Some(&prompt))?;
                emitter.send("answer_started", label, Some(&answer_id), None)?;
                let (task, rx) = start_answer(provider.clone(), prompt);
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
                    if view.failed { emitter.send("error", "provider", Some(&answer_id), Some("AI provider failed; check endpoint, credentials and model."))?; }
                    else { emitter.send("answer_completed", label, Some(&answer_id), None)?; }
                    answer_task = None;
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
        let (_task, mut rx) = start_answer(Provider::Mock, "Explain binary search".into());
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
}
