//! Versioned JSONL events consumed by the live practice UI.
//! This module does not write question text to stderr or persist media.
use serde_json::{Value, json};
use std::io::{self, BufRead, Write};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_EVENT_BYTES: usize = 128 * 1024;
const MAX_REPLAY_BYTES: u64 = 2 * 1024 * 1024;

fn utc_timestamp() -> String {
    // Gregorian calendar conversion; no external process or clock libraries.
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let day = (seconds / 86_400) as i64;
    let z = day + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let mut year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let date = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    let time = seconds % 86_400;
    format!(
        "{year:04}-{month:02}-{date:02}T{:02}:{:02}:{:02}Z",
        time / 3_600,
        (time % 3_600) / 60,
        time % 60
    )
}

pub struct Emitter {
    pub session_id: String,
    sequence: u64,
}

impl Emitter {
    pub fn new() -> Self {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default();
        Self {
            session_id: format!("practice-{}", now.as_nanos()),
            sequence: 0,
        }
    }

    pub fn send(
        &mut self,
        kind: &str,
        source: &str,
        request: Option<&str>,
        content: Option<&str>,
    ) -> Result<(), String> {
        self.sequence += 1;
        let mut event = json!({
            "schema": 1, "type": kind, "session_id": self.session_id,
            "sequence": self.sequence, "source": source, "timestamp": utc_timestamp(),
        });
        if let Some(id) = request {
            event["request_id"] = json!(id);
        }
        if let Some(text) = content {
            // UI event channel is bounded; never persist raw capture.
            event[if kind == "error" { "message" } else { "text" }] = json!(bounded_text(text));
        }
        self.write(&event)
    }

    /// A detected question carries two renderings of the same read: `text`, the
    /// bounded material handed to the model (for accessibility this is the
    /// rendered tree), and `display`, a role-free form for the practice panel.
    /// `display` is omitted when it is identical to `text`.
    pub fn send_question(
        &mut self,
        source: &str,
        request: &str,
        text: &str,
        display: &str,
    ) -> Result<(), String> {
        self.sequence += 1;
        let event = question_event(
            &self.session_id,
            self.sequence,
            source,
            request,
            text,
            display,
        );
        self.write(&event)
    }

    /// The provider request shown in the practice panel's API area. `prompt` is
    /// the user message; the system instructions and the API key are never
    /// included.
    pub fn send_api_request(
        &mut self,
        request: &str,
        url: &str,
        model: &str,
        prompt: &str,
    ) -> Result<(), String> {
        self.sequence += 1;
        let event = api_request_event(&self.session_id, self.sequence, request, url, model, prompt);
        self.write(&event)
    }

    /// The provider response summary. `status` is absent for local providers
    /// (mock, Codex CLI) that do not make an HTTP call.
    pub fn send_api_response(
        &mut self,
        request: &str,
        status: Option<u16>,
        text: &str,
    ) -> Result<(), String> {
        self.sequence += 1;
        let event = api_response_event(&self.session_id, self.sequence, request, status, text);
        self.write(&event)
    }

    fn write(&self, event: &Value) -> Result<(), String> {
        let mut stdout = io::stdout().lock();
        serde_json::to_writer(&mut stdout, event).map_err(|_| "cannot serialize live event")?;
        stdout
            .write_all(b"\n")
            .and_then(|_| stdout.flush())
            .map_err(|_| "live UI stream closed".into())
    }
}

/// Build the bounded `question_detected` event. Pure so the shape can be tested
/// without writing to stdout.
fn question_event(
    session_id: &str,
    sequence: u64,
    source: &str,
    request: &str,
    text: &str,
    display: &str,
) -> Value {
    let mut event = json!({
        "schema": 1, "type": "question_detected", "session_id": session_id,
        "sequence": sequence, "source": source, "timestamp": utc_timestamp(),
        "request_id": request, "text": bounded_text(text),
    });
    if display != text {
        event["display"] = json!(bounded_text(display));
    }
    event
}

/// Bound event text so one capture cannot exhaust the UI channel.
fn bounded_text(text: &str) -> String {
    text.chars().take(64_000).collect()
}

/// Build the bounded `api_request` event. Pure so the shape can be tested
/// without writing to stdout.
fn api_request_event(
    session_id: &str,
    sequence: u64,
    request: &str,
    url: &str,
    model: &str,
    prompt: &str,
) -> Value {
    json!({
        "schema": 1, "type": "api_request", "session_id": session_id,
        "sequence": sequence, "source": "provider", "timestamp": utc_timestamp(),
        "request_id": request,
        "url": bounded_text(url),
        "model": bounded_text(model),
        "prompt": bounded_text(prompt),
    })
}

/// Build the bounded `api_response` event. Pure so the shape can be tested
/// without writing to stdout.
fn api_response_event(
    session_id: &str,
    sequence: u64,
    request: &str,
    status: Option<u16>,
    text: &str,
) -> Value {
    let mut event = json!({
        "schema": 1, "type": "api_response", "session_id": session_id,
        "sequence": sequence, "source": "provider", "timestamp": utc_timestamp(),
        "request_id": request,
        "text": bounded_text(text),
    });
    if let Some(status) = status {
        event["status"] = json!(status);
    }
    event
}

pub fn replay(path: &Path) -> Result<(), String> {
    // Deliberately no provider calls, OS screen reading or audio capture.
    let file = std::fs::File::open(path).map_err(|_| "cannot open replay fixture")?;
    if file
        .metadata()
        .map_err(|_| "cannot stat replay fixture")?
        .len()
        > MAX_REPLAY_BYTES
    {
        return Err("replay fixture exceeds 2MiB".into());
    }
    let mut stdout = io::stdout().lock();
    let mut previous = 0_u64;
    for line in io::BufReader::new(file).split(b'\n') {
        let bytes = line.map_err(|_| "failed to read replay fixture")?;
        if bytes.is_empty() {
            continue;
        }
        if bytes.len() > MAX_EVENT_BYTES {
            return Err("replay event too large".into());
        }
        let value: Value = serde_json::from_slice(&bytes).map_err(|_| "invalid JSON event")?;
        if value.get("schema").and_then(Value::as_u64) != Some(1)
            || value.get("type").and_then(Value::as_str).is_none()
            || value.get("session_id").and_then(Value::as_str).is_none()
        {
            return Err("invalid replay event schema".into());
        }
        let seq = value
            .get("sequence")
            .and_then(Value::as_u64)
            .ok_or("event missing sequence")?;
        if seq <= previous {
            return Err("replay sequence must increase".into());
        }
        previous = seq;
        serde_json::to_writer(&mut stdout, &value).map_err(|_| "cannot write replay event")?;
        stdout
            .write_all(b"\n")
            .map_err(|_| "replay output pipe closed")?;
    }
    stdout
        .flush()
        .map_err(|_| "replay output pipe closed".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn timestamp_is_rfc3339_utc_compatible() {
        let timestamp = utc_timestamp();
        assert_eq!(timestamp.len(), 20);
        assert_eq!(&timestamp[4..5], "-");
        assert_eq!(&timestamp[10..11], "T");
        assert_eq!(&timestamp[19..20], "Z");
    }
    #[test]
    fn versioned_emitter_produces_unique_session_id() {
        assert!(Emitter::new().session_id.starts_with("practice-"));
    }

    #[test]
    fn question_event_carries_display_only_when_it_differs() {
        let with_display = question_event(
            "s1",
            3,
            "accessibility",
            "q1",
            "window Q\n  textarea Q",
            "Q",
        );
        assert_eq!(with_display["type"], "question_detected");
        assert_eq!(with_display["schema"], 1);
        assert_eq!(with_display["text"], "window Q\n  textarea Q");
        assert_eq!(with_display["display"], "Q");
        assert_eq!(with_display["request_id"], "q1");

        let identical = question_event("s1", 4, "ocr", "q2", "same", "same");
        assert!(identical.get("display").is_none());

        let bounded = question_event("s1", 5, "ocr", "q3", &"x".repeat(70_000), "y");
        assert_eq!(bounded["text"].as_str().unwrap().chars().count(), 64_000);
    }

    #[test]
    fn api_events_carry_endpoint_model_and_bounded_prompt() {
        let request = api_request_event(
            "s1",
            6,
            "q1",
            "https://api.example.com/v1/chat/completions",
            "test-model",
            &"p".repeat(70_000),
        );
        assert_eq!(request["type"], "api_request");
        assert_eq!(request["schema"], 1);
        assert_eq!(request["source"], "provider");
        assert_eq!(request["request_id"], "q1");
        assert_eq!(request["model"], "test-model");
        assert_eq!(request["prompt"].as_str().unwrap().chars().count(), 64_000);

        let response = api_response_event("s1", 7, "q1", Some(200), "hello");
        assert_eq!(response["type"], "api_response");
        assert_eq!(response["status"], 200);
        assert_eq!(response["text"], "hello");

        let local = api_response_event("s1", 8, "q1", None, "mock answer");
        assert!(local.get("status").is_none());
        assert_eq!(local["text"], "mock answer");
    }
}
