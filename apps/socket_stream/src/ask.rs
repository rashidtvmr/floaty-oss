//! One-shot manual ask for the answer panel: reads the screen on request,
//! then streams a model answer as versioned JSONL live events. No session
//! state is retained; the process exits after one answer.
//!
//! `--question TEXT` asks a typed question; `--with-screen` adds the latest
//! screen text as untrusted context; `--read-screen` makes the screen read
//! itself the question (accessibility first, local OCR fallback when
//! allowed, plus a screenshot attached for vision-capable providers).
use crate::live_events::Emitter;
use crate::live_observe::{
    Reading, ScreenReader, a11y_options, bound_context, compose_prompt, limit,
};
use socket_stream_core::ai::{ApiEvent, MAX_IMAGE_BYTES, Provider, stream_answer_with_meta_vision};
use std::path::Path;

fn has(args: &[String], flag: &str) -> bool {
    args.iter().any(|arg| arg == flag)
}

fn option(args: &[String], flag: &str) -> Option<String> {
    args.windows(2).find(|p| p[0] == flag).map(|p| p[1].clone())
}

/// PNG magic bytes plus the shared size bound guard the vision attachment.
fn vision_frame(provider: &Provider, frame: Option<&Path>) -> Option<Vec<u8>> {
    if !matches!(provider, Provider::OpenAiCompatible { .. }) {
        return None;
    }
    let bytes = std::fs::read(frame?).ok()?;
    (bytes.len() <= MAX_IMAGE_BYTES && bytes.starts_with(&[0x89, b'P', b'N', b'G']))
        .then_some(bytes)
}

pub async fn run(args: &[String], provider: Provider) -> Result<(), String> {
    let question = option(args, "--question");
    let read_screen = has(args, "--read-screen");
    let with_screen = has(args, "--with-screen");
    if read_screen && with_screen {
        return Err("choose one of --read-screen or --with-screen".into());
    }
    if read_screen && question.is_some() {
        return Err(
            "--read-screen answers the screen itself; drop --question or use --with-screen".into(),
        );
    }
    let needs_screen = read_screen || with_screen;
    if needs_screen && !has(args, "--authorized") {
        return Err("--authorized is required to read the screen".into());
    }
    if question.is_none() && !read_screen {
        return Err("ask needs --question TEXT or --read-screen".into());
    }
    if let Some(text) = question.as_deref()
        && text.trim().is_empty()
    {
        return Err("--question text is empty".into());
    }
    let mut emitter = Emitter::new();
    let result = run_ask(
        args,
        provider,
        question,
        read_screen,
        with_screen,
        &mut emitter,
    )
    .await;
    if let Err(message) = &result {
        emitter.send("error", "system", None, Some(message))?;
    }
    result
}

async fn run_ask(
    args: &[String],
    provider: Provider,
    question: Option<String>,
    read_screen: bool,
    with_screen: bool,
    emitter: &mut Emitter,
) -> Result<(), String> {
    emitter.send("session_started", "manual", None, None)?;
    let result = ask_body(args, provider, question, read_screen, with_screen, emitter).await;
    match result {
        Ok(()) => emitter.send("session_stopped", "manual", None, None),
        Err(message) => {
            emitter.send("session_stopped", "manual", None, None)?;
            Err(message)
        }
    }
}

async fn ask_body(
    args: &[String],
    provider: Provider,
    question: Option<String>,
    read_screen: bool,
    with_screen: bool,
    emitter: &mut Emitter,
) -> Result<(), String> {
    let allow_ocr = has(args, "--allow-ocr") || has(args, "--force-ocr");
    let force_ocr = has(args, "--force-ocr");
    let vision = !has(args, "--no-vision");
    let options = a11y_options(args)?;
    let interval = limit(args, "--read-interval", 2, 2, 3600)?;

    // Screen read first: accessibility tree, then OCR when allowed. The read
    // is either the question (--read-screen) or untrusted context
    // (--with-screen). The frame is retained for vision-capable providers.
    let mut screen_question: Option<(String, String, &'static str)> = None;
    let mut screen_context: Option<String> = None;
    let mut frame: Option<std::path::PathBuf> = None;
    if read_screen || with_screen {
        let mut reader = ScreenReader::start(
            allow_ocr,
            force_ocr,
            vision,
            has(args, "--portal") || socket_stream_core::capture::requires_linux_portal(),
            options,
            interval,
        )?;
        let reading = reader.read_once();
        frame = reader.last_frame().map(std::path::Path::to_path_buf);
        reader.stop();
        match reading {
            Reading::Question(text, display, source) if !text.trim().is_empty() => {
                emitter.send_screen_read(source, "ok", &display)?;
                if read_screen {
                    screen_question = Some((bound_context(&text), display, source));
                } else {
                    screen_context = Some(bound_context(&text));
                }
            }
            Reading::Question(..) => {
                emitter.send_screen_read(
                    "system",
                    "unavailable",
                    "No readable text found on the focused window.",
                )?;
                return Err("no readable text found on the focused window".into());
            }
            Reading::Nothing => {
                return Err("nothing to read on the focused window".into());
            }
            Reading::Insufficient => {
                emitter.send_screen_read(
                    "system",
                    "unavailable",
                    "No readable content on the focused window. Enable local OCR fallback for screenshots.",
                )?;
                return Err("no readable content on the focused window".into());
            }
            Reading::AccessibilityUnavailable(reason) => {
                let message = if reason.is_empty() {
                    "Accessibility unavailable; enable local OCR fallback for screenshots."
                        .to_owned()
                } else {
                    format!("Accessibility unavailable: {reason}")
                };
                emitter.send_screen_read("system", "unavailable", &message)?;
                return Err(message);
            }
            Reading::OcrFailed(reason) => {
                let message = format!("Screenshot read failed: {reason}");
                emitter.send_screen_read("system", "unavailable", &message)?;
                return Err(message);
            }
        }
    }

    let (question_text, source) = match (&screen_question, &question) {
        (Some((text, _, source)), _) => (text.clone(), *source),
        (None, Some(text)) => (text.trim().to_owned(), "manual"),
        (None, None) => return Err("ask needs --question TEXT or --read-screen".into()),
    };
    let prompt = if read_screen {
        question_text.clone()
    } else {
        compose_prompt(&question_text, screen_context.as_deref())
    };
    let request = "manual-1";
    emitter.send(
        "question_detected",
        source,
        Some(request),
        Some(question_text.as_str()),
    )?;
    emitter.send("answer_started", source, Some(request), None)?;

    let image = if vision {
        vision_frame(&provider, frame.as_deref())
    } else {
        None
    };
    let (deltas, mut incoming) = tokio::sync::mpsc::channel::<String>(32);
    let (meta_tx, mut meta_rx) = tokio::sync::mpsc::channel::<ApiEvent>(8);
    let stream = stream_answer_with_meta_vision(
        &provider,
        &prompt,
        image.as_deref(),
        &deltas,
        Some(&meta_tx),
    );
    tokio::pin!(stream);
    let mut sent = 0_usize;
    let outcome = loop {
        tokio::select! {
            Some(delta) = incoming.recv() => {
                emitter.send("answer_delta", source, Some(request), Some(&delta))?;
                sent += delta.chars().count();
            }
            Some(event) = meta_rx.recv() => {
                match event {
                    ApiEvent::Request { url, model, prompt } => {
                        emitter.send_api_request(request, &url, &model, &prompt)?;
                    }
                    ApiEvent::Response { status, body } => {
                        emitter.send_api_response(request, status, &body)?;
                    }
                }
            }
            finished = &mut stream => break finished,
        }
    };
    while let Ok(delta) = incoming.try_recv() {
        emitter.send("answer_delta", source, Some(request), Some(&delta))?;
        sent += delta.chars().count();
    }
    while let Ok(event) = meta_rx.try_recv() {
        if let ApiEvent::Response { status, body } = event {
            emitter.send_api_response(request, status, &body)?;
        }
    }
    match outcome {
        Ok(()) => {
            if sent == 0 {
                emitter.send(
                    "error",
                    "provider",
                    Some(request),
                    Some("The provider returned an empty response."),
                )?;
                Err("provider returned an empty response".into())
            } else {
                emitter.send("answer_completed", source, Some(request), None)?;
                Ok(())
            }
        }
        Err(error) => {
            emitter.send(
                "error",
                "provider",
                Some(request),
                Some("AI provider failed; check endpoint, credentials and model."),
            )?;
            Err(error.to_string())
        }
    }
}
