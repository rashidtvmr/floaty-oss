mod audio_live;
mod auth_commands;
mod hotkeys;
mod live_events;
mod live_observe;
mod moonshine_live;
mod overlay;

use socket_stream_core::a11y::A11yTree;
use socket_stream_core::accessibility::{
    AccessibilityError, needs_ocr, read_foreground_text, read_foreground_tree,
};
use socket_stream_core::ai::{Provider, answer, list_models};
use socket_stream_core::audio::{
    WhisperConfig, list_audio_sources, record_audio, record_system_audio, transcribe,
};
use socket_stream_core::capture::{PortalFrameSource, capture_to, requires_linux_portal};
use socket_stream_core::context::{Deduplicator, ScreenContext};
use socket_stream_core::ocr::{extract_text, quality_warnings};
use socket_stream_core::platform::{capture_capabilities, display_name};
use socket_stream_core::session::PracticeSession;
use std::path::{Path, PathBuf};
use std::time::Duration;

fn usage() {
    eprintln!("SocketStream - permission-based coding practice assistant");
    eprintln!("Commands:");
    eprintln!("  doctor");
    eprintln!(
        "  audio-live --authorized --source microphone|system [--stt moonshine|whisper] [--device DEVICE] [--provider PROVIDER]"
    );
    eprintln!("    Moonshine is built in; legacy Whisper requires --stt whisper --model MODEL.");
    eprintln!(
        "  observe-live --authorized [--allow-ocr|--force-ocr] [--ocr-images|--no-ocr-images] [--a11y-mode auto|focused|tree|text] [--max-tokens N] [--config PATH] [--interval 4] [--provider ollama|codex|openai-compatible|mock]"
    );
    eprintln!(
        "  overlay --provider ollama [--stealth|--no-stealth] [--interval 4] [--opacity 0.94] [--allow-ocr] [--portal]"
    );
    eprintln!(
        "                                          floating answer panel; no capture performed by this command"
    );
    eprintln!(
        "  replay-events --file EVENTS.jsonl       replay synthetic events; no capture/inference"
    );
    eprintln!("  codex-login [--method browser|device] [--native]   Codex sign-in");
    eprintln!(
        "  codex-status [--native]                 check Codex login without exposing tokens"
    );
    eprintln!(
        "  codex-logout --confirm [--native]       explicitly sign out of selected credential store"
    );
    eprintln!("  provider-models --provider openai-compatible|ollama");
    eprintln!("  provider-test --provider NAME --confirm-inference   sends a fixed test prompt");
    eprintln!(
        "  analyze-file --file PATH [--provider mock|openai-compatible|ollama|codex] [--fallback-provider ...]"
    );
    eprintln!("  ocr --file IMAGE");
    eprintln!("  analyze-image --file IMAGE [--provider mock|ollama|openai-compatible|codex]");
    eprintln!("  transcribe --file WAV --model WHISPER_GGML_MODEL [--executable whisper-cli]");
    eprintln!(
        "  analyze-audio --file WAV --model WHISPER_GGML_MODEL [--provider mock|ollama|codex|openai-compatible]"
    );
    eprintln!("  capture --authorized --output PATH");
    eprintln!(
        "  a11y --authorized [--mode text|tree] [--max-nodes N] [--max-depth N] [--max-chars N]"
    );
    eprintln!("  hotkeys list|init [--force]|print");
    eprintln!("  observe accepts --force-ocr when accessibility text is incomplete");
    eprintln!("  observe accepts --portal for the Linux ScreenCast permission chooser");
    eprintln!("  record-audio --authorized --device ID --duration 30 --file output.wav");
    eprintln!("  audio-devices                         list mic/Pulse source identifiers");
    eprintln!(
        "  record-system-audio --authorized --duration 30 --file output.wav [--device LINUX_MONITOR]"
    );
    eprintln!(
        "  audio-practice --authorized --source microphone|system --duration 30 --model ggml-model.bin [--device MIC_OR_MONITOR] [--provider mock|codex|openai-compatible|ollama]"
    );
    eprintln!(
        "  observe --authorized [--interval 5] [--max-captures 12] [--provider mock|openai-compatible|codex]"
    );
    eprintln!(
        "Capture requires explicit --authorized and OS screen permissions. Ctrl+C stops observation."
    );
}

fn value(args: &[String], flag: &str) -> Option<String> {
    args.windows(2)
        .find(|pair| pair[0] == flag)
        .map(|pair| pair[1].clone())
}

fn contains(args: &[String], flag: &str) -> bool {
    args.iter().any(|a| a == flag)
}

fn named_provider(selected: &str) -> Result<Provider, String> {
    match selected {
        "mock" => Ok(Provider::Mock),
        "codex" => Ok(Provider::Codex {
            model: std::env::var("SOCKET_STREAM_CODEX_MODEL").unwrap_or_default(),
        }),
        "ollama" => Ok(Provider::OpenAiCompatible {
            base_url: std::env::var("SOCKET_STREAM_OLLAMA_URL")
                .unwrap_or_else(|_| "http://127.0.0.1:11434/v1".to_owned()),
            model: std::env::var("SOCKET_STREAM_OLLAMA_MODEL")
                .unwrap_or_else(|_| "qwen2.5-coder:1.5b".to_owned()),
            api_key: String::new(),
        }),
        "openai-compatible" => Ok(Provider::OpenAiCompatible {
            base_url: std::env::var("SOCKET_STREAM_BASE_URL")
                .map_err(|_| "missing SOCKET_STREAM_BASE_URL")?,
            model: std::env::var("SOCKET_STREAM_MODEL").unwrap_or_default(),
            api_key: std::env::var("SOCKET_STREAM_API_KEY").unwrap_or_default(),
        }),
        _ => Err(format!("unknown provider: {selected}")),
    }
}

fn provider_from_env(args: &[String]) -> Result<Provider, String> {
    let selected = value(args, "--provider").unwrap_or_else(|| "mock".to_owned());
    let primary = named_provider(&selected)?;
    match value(args, "--fallback-provider") {
        Some(fallback) if fallback != selected => {
            // Explicit opt-in only: fallback could incur separate API charges.
            Ok(Provider::Fallback(vec![
                primary,
                named_provider(&fallback)?,
            ]))
        }
        Some(_) => Err("fallback provider must differ from primary provider".into()),
        None => Ok(primary),
    }
}

fn required_path(args: &[String], flag: &str) -> Result<PathBuf, String> {
    value(args, flag)
        .map(PathBuf::from)
        .ok_or_else(|| format!("missing {flag}"))
}

fn bounded_usize(
    args: &[String],
    flag: &str,
    default: usize,
    min: usize,
    max: usize,
) -> Result<usize, String> {
    let result = value(args, flag)
        .map(|raw| raw.parse::<usize>().map_err(|_| format!("invalid {flag}")))
        .transpose()?
        .unwrap_or(default);
    if !(min..=max).contains(&result) {
        return Err(format!("{flag} must be {min}..{max}"));
    }
    Ok(result)
}

fn tree_json(tree: &A11yTree) -> serde_json::Value {
    serde_json::json!({
        "schema": tree.schema,
        "source": &tree.source,
        "app": &tree.app,
        "pid": tree.pid,
        "truncated": tree.truncated,
        "node_count": tree.nodes.len(),
        "nodes": tree.nodes.iter().map(|node| serde_json::json!({
            "id": node.id,
            "parent": node.parent,
            "role": &node.role,
            "name": &node.name,
            "text": if node.protected { "" } else { node.text.as_str() },
            "focused": node.focused,
            "bounds": node.bounds,
            "image": node.image,
            "protected": node.protected,
        })).collect::<Vec<_>>(),
    })
}

async fn analyze_file(path: &Path, provider: &Provider) -> Result<(), String> {
    let text = std::fs::read_to_string(path).map_err(|e| e.to_string())?;
    let context = ScreenContext::new(text, "imported-file");
    println!(
        "Captured text ({} characters):",
        context.text.chars().count()
    );
    println!("{}", context.text);
    println!("\n--- Model output ---\n");
    let output = answer(provider, &context.text)
        .await
        .map_err(|e| e.to_string())?;
    println!("{output}");
    Ok(())
}

async fn run(args: &[String]) -> Result<(), String> {
    let action = args.first().map(String::as_str).unwrap_or("help");
    match action {
        "audio-live" => audio_live::run(args, provider_from_env(args)?).await,
        "observe-live" => live_observe::run(args, provider_from_env(args)?).await,
        "overlay" => overlay::run(args),
        "replay-events" => live_events::replay(&required_path(args, "--file")?),
        "hotkeys" => hotkeys::run(args),
        "codex-login" | "codex-status" | "codex-logout" => auth_commands::run(action, args).await,
        "provider-models" => {
            let models = list_models(&provider_from_env(args)?)
                .await
                .map_err(|e| e.to_string())?;
            println!("Connection and model-list request succeeded; no inference was requested.");
            if models.is_empty() {
                println!("The provider returned no models.");
            }
            for model in models {
                println!("{model}");
            }
            Ok(())
        }
        "provider-test" => {
            if contains(args, "--fallback-provider") {
                return Err("Test one provider at a time; fallback could conceal a primary-provider failure.".into());
            }
            if !contains(args, "--confirm-inference") {
                return Err("This sends a fixed test prompt and can consume provider quota; add --confirm-inference.".into());
            }
            let provider = provider_from_env(args)?;
            if matches!(provider, Provider::Mock) {
                println!("Mock provider only: no AI inference or network request was made.");
                return Ok(());
            }
            let response = answer(&provider, "Reply with the word READY only.")
                .await
                .map_err(|e| e.to_string())?;
            if response.trim().is_empty() {
                return Err("Provider returned an empty response.".into());
            }
            println!(
                "Inference test succeeded ({} response characters). No screen or audio content was sent.",
                response.chars().count()
            );
            Ok(())
        }
        "doctor" => {
            println!(
                "SocketStream host: {} ({})",
                display_name(),
                std::env::consts::ARCH
            );
            for (name, found) in capture_capabilities() {
                println!(
                    "  {}: {}",
                    name,
                    if found { "available" } else { "not available" }
                );
            }
            println!("Info: screen and microphone capture require explicit user permission.");
            Ok(())
        }
        "analyze-file" => {
            analyze_file(&required_path(args, "--file")?, &provider_from_env(args)?).await
        }
        "ocr" => {
            let text = extract_text(&required_path(args, "--file")?).map_err(|e| e.to_string())?;
            println!("{text}");
            Ok(())
        }
        "analyze-image" => {
            let text = extract_text(&required_path(args, "--file")?).map_err(|e| e.to_string())?;
            if text.trim().is_empty() {
                return Err("no readable text in the selected image".into());
            }
            println!("--- Extracted screen text ---\n{text}");
            for warning in quality_warnings(&text) {
                eprintln!("Possible OCR error: {warning}");
            }
            println!(
                "--- Model response ---\n{}",
                answer(&provider_from_env(args)?, &text)
                    .await
                    .map_err(|e| e.to_string())?
            );
            Ok(())
        }
        "audio-devices" => {
            println!("{}", list_audio_sources().map_err(|e| e.to_string())?);
            Ok(())
        }
        "record-audio" => {
            let duration: u32 = value(args, "--duration")
                .unwrap_or_else(|| "30".to_owned())
                .parse()
                .map_err(|_| "invalid audio duration")?;
            let device = value(args, "--device").ok_or("missing --device")?;
            let file = required_path(args, "--file")?;
            record_audio(contains(args, "--authorized"), &device, duration, &file)
                .map_err(|e| e.to_string())?;
            println!("User-authorized recording saved to {}", file.display());
            Ok(())
        }
        "record-system-audio" => {
            let duration: u32 = value(args, "--duration")
                .unwrap_or_else(|| "30".into())
                .parse()
                .map_err(|_| "invalid audio duration")?;
            let file = required_path(args, "--file")?;
            record_system_audio(
                contains(args, "--authorized"),
                duration,
                &file,
                value(args, "--device").as_deref(),
            )
            .map_err(|e| e.to_string())?;
            println!("Authorized system audio saved to {}", file.display());
            Ok(())
        }
        "audio-practice" => {
            if !contains(args, "--authorized") {
                return Err("audio capture requires --authorized".to_owned());
            }
            let duration: u32 = value(args, "--duration")
                .unwrap_or_else(|| "30".to_owned())
                .parse()
                .map_err(|_| "invalid duration")?;
            let source = value(args, "--source").unwrap_or_else(|| "microphone".into());
            let model = required_path(args, "--model")?;
            let provider = provider_from_env(args)?;
            let temp_dir = tempfile::tempdir().map_err(|e| e.to_string())?;
            let output = temp_dir.path().join("practice-audio.wav");
            let recorded = match source.as_str() {
                "microphone" => {
                    let device =
                        value(args, "--device").ok_or("missing --device for microphone")?;
                    record_audio(true, &device, duration, &output)
                }
                "system" => {
                    record_system_audio(true, duration, &output, value(args, "--device").as_deref())
                }
                _ => return Err("--source must be microphone or system".into()),
            };
            if let Err(e) = recorded {
                return Err(e.to_string());
            }
            let cfg = WhisperConfig {
                executable: value(args, "--executable")
                    .unwrap_or_else(|| "whisper-cli".into())
                    .into(),
                model,
            };
            let transcription = transcribe(&cfg, &output);
            let _ = std::fs::remove_file(output);
            let text = transcription.map_err(|e| e.to_string())?;
            println!(
                "Transcribed practice speech:
{text}"
            );
            println!(
                "
--- Answer ---
{}",
                answer(&provider, &text).await.map_err(|e| e.to_string())?
            );
            Ok(())
        }
        "transcribe" => {
            let config = WhisperConfig {
                model: required_path(args, "--model")?,
                executable: value(args, "--executable")
                    .unwrap_or_else(|| "whisper-cli".to_owned())
                    .into(),
            };
            println!(
                "{}",
                transcribe(&config, &required_path(args, "--file")?).map_err(|e| e.to_string())?
            );
            Ok(())
        }
        "analyze-audio" => {
            let config = WhisperConfig {
                model: required_path(args, "--model")?,
                executable: value(args, "--executable")
                    .unwrap_or_else(|| "whisper-cli".to_owned())
                    .into(),
            };
            let text =
                transcribe(&config, &required_path(args, "--file")?).map_err(|e| e.to_string())?;
            if text.trim().is_empty() {
                return Err("speech recognizer returned no words".into());
            }
            println!("--- Local transcription ---\n{text}");
            println!(
                "--- Model response ---\n{}",
                answer(&provider_from_env(args)?, &text)
                    .await
                    .map_err(|e| e.to_string())?
            );
            Ok(())
        }
        "a11y" => {
            let mut session = PracticeSession::new();
            session
                .start(contains(args, "--authorized"))
                .map_err(str::to_owned)?;
            let root = socket_stream_core::paths::project_root();
            match value(args, "--mode").as_deref().unwrap_or("text") {
                "text" => {
                    let text = read_foreground_text(&root).map_err(|e| e.to_string())?;
                    println!("{text}");
                }
                "tree" => {
                    let max_nodes = bounded_usize(args, "--max-nodes", 800, 1, 10_000)?;
                    let max_depth = bounded_usize(args, "--max-depth", 20, 1, 64)?;
                    let max_chars = bounded_usize(args, "--max-chars", 32_000, 1, 1_000_000)?;
                    let tree = read_foreground_tree(&root, max_nodes, max_depth, max_chars)
                        .map_err(|error| error.to_string())?;
                    let output = tree_json(&tree);
                    println!(
                        "{}",
                        serde_json::to_string(&output).map_err(|_| "cannot serialize tree")?
                    );
                }
                _ => return Err("a11y --mode must be text or tree".into()),
            }
            session.stop();
            Ok(())
        }
        "capture" => {
            let mut session = PracticeSession::new();
            session
                .start(contains(args, "--authorized"))
                .map_err(str::to_owned)?;
            let output = required_path(args, "--output")?;
            capture_to(&session, &output).map_err(|e| e.to_string())?;
            println!("Capture saved to {}", output.display());
            session.stop();
            Ok(())
        }
        "observe" => {
            let mut session = PracticeSession::new();
            session
                .start(contains(args, "--authorized"))
                .map_err(str::to_owned)?;
            let interval: u64 = value(args, "--interval")
                .unwrap_or_else(|| "5".to_owned())
                .parse()
                .map_err(|_| "invalid interval")?;
            let max_captures: usize = value(args, "--max-captures")
                .unwrap_or_else(|| "12".to_owned())
                .parse()
                .map_err(|_| "invalid max captures")?;
            if !(2..=3600).contains(&interval) || !(1..=1000).contains(&max_captures) {
                return Err("interval 2..3600, max-captures 1..1000".to_owned());
            }
            let provider = provider_from_env(args)?;
            let mut dedup = Deduplicator::new(64);
            let temp_dir = tempfile::tempdir().map_err(|e| e.to_string())?;
            let use_portal = contains(args, "--portal") || requires_linux_portal();
            let portal_frame = temp_dir.path().join("portal-current.png");
            let mut portal: Option<PortalFrameSource> = None;
            #[cfg(unix)]
            let mut termination =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                    .map_err(|e| e.to_string())?;
            println!(
                "Authorized practice capture ACTIVE; {} iterations at {}s. Ctrl+C to stop.",
                max_captures, interval
            );
            'observation: for n in 0..max_captures {
                let path = temp_dir.path().join(format!("frame-{n}.png"));
                let root = socket_stream_core::paths::project_root();
                let accessible = match read_foreground_text(&root) {
                    Err(AccessibilityError::OwnWindow) => {
                        // Never analyze our own app, and do not fall back to
                        // capturing its contents with OCR.
                        #[cfg(unix)]
                        tokio::select! {
                            _ = tokio::time::sleep(Duration::from_secs(interval)) => {},
                            _ = tokio::signal::ctrl_c() => break 'observation,
                            _ = termination.recv() => break 'observation,
                        }
                        #[cfg(not(unix))]
                        tokio::time::sleep(Duration::from_secs(interval)).await;
                        continue;
                    }
                    result => result.ok(),
                };
                let result = if contains(args, "--force-ocr") || needs_ocr(accessible.as_deref()) {
                    if use_portal {
                        if portal.is_none() {
                            println!(
                                "Waiting for Linux ScreenCast permission and source selection..."
                            );
                            portal = Some(
                                PortalFrameSource::start(&session, portal_frame.clone(), interval)
                                    .map_err(|e| e.to_string())?,
                            );
                        }
                        let stream = portal.as_mut().ok_or("ScreenCast stream unavailable")?;
                        if stream.finished().map_err(|e| e.to_string())? {
                            return Err(
                                "ScreenCast portal session ended; restart observation".into()
                            );
                        }
                        if !stream.path().exists() {
                            for _ in 0..120 {
                                if stream.path().exists() {
                                    break;
                                }
                                if stream.finished().map_err(|e| e.to_string())? {
                                    return Err(
                                        "ScreenCast was denied, unavailable or closed".into()
                                    );
                                }
                                #[cfg(unix)]
                                tokio::select! {
                                    _ = tokio::time::sleep(Duration::from_millis(500)) => {},
                                    _ = tokio::signal::ctrl_c() => break 'observation,
                                    _ = termination.recv() => break 'observation,
                                }
                            }
                        }
                        if !stream.path().exists() {
                            return Err("No portal frame received in 60 seconds; check permissions and GStreamer".into());
                        }
                        extract_text(stream.path()).map_err(|e| e.to_string())
                    } else {
                        let result = capture_to(&session, &path)
                            .map_err(|e| e.to_string())
                            .and_then(|_| extract_text(&path).map_err(|e| e.to_string()));
                        let _ = std::fs::remove_file(&path);
                        result
                    }
                } else {
                    Ok(accessible.unwrap_or_default())
                };
                match result {
                    Ok(text) if dedup.should_process(&text) => {
                        println!("\n--- New screen text ---\n{text}");
                        for warning in quality_warnings(&text) {
                            eprintln!("Potential capture/text-quality issue: {warning}");
                        }
                        #[cfg(unix)]
                        let model_result = tokio::select! {
                            result = answer(&provider, &text) => Some(result),
                            _ = tokio::signal::ctrl_c() => None,
                            _ = termination.recv() => None,
                        };
                        #[cfg(not(unix))]
                        let model_result = Some(answer(&provider, &text).await);
                        match model_result {
                            Some(Ok(response)) => println!("\n--- Answer ---\n{response}"),
                            Some(Err(err)) => eprintln!("Model unavailable: {err}"),
                            None => break 'observation,
                        }
                    }
                    Ok(_) => {}
                    Err(err) => eprintln!("Capture/OCR unavailable: {err}"),
                }
                if n + 1 < max_captures {
                    #[cfg(unix)]
                    tokio::select! {
                        _ = tokio::time::sleep(Duration::from_secs(interval)) => {},
                        _ = tokio::signal::ctrl_c() => break 'observation,
                        _ = termination.recv() => break 'observation,
                    }
                    #[cfg(not(unix))]
                    tokio::time::sleep(Duration::from_secs(interval)).await;
                }
            }
            drop(portal);
            session.stop();
            println!("Observation stopped.");
            Ok(())
        }
        _ => {
            usage();
            Err("missing or unknown command".into())
        }
    }
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if let Err(err) = run(&args).await {
        eprintln!("Error: {err}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod a11y_cli_tests {
    use super::*;

    #[test]
    fn tree_json_keeps_schema_shape_and_redacts_protected_text() {
        let tree = A11yTree::parse(
            r#"{"schema":1,"source":"atspi","app":"Editor","pid":7,"nodes":[
                {"id":1,"parent":null,"role":"frame","text":"Question","focused":true},
                {"id":2,"parent":1,"role":"password text","text":"must-not-leak","protected":true}
            ]}"#,
        )
        .expect("tree");
        let output = tree_json(&tree);
        assert_eq!(output["node_count"], 2);
        assert_eq!(output["source"], "atspi");
        assert_eq!(output["nodes"][0]["parent"], serde_json::Value::Null);
        assert_eq!(output["nodes"][1]["text"], "");
        assert!(!output.to_string().contains("must-not-leak"));
    }

    #[test]
    fn bounded_usize_accepts_default_and_rejects_out_of_range() {
        assert_eq!(
            bounded_usize(&[], "--max-nodes", 250, 1, 10_000).unwrap(),
            250
        );
        assert_eq!(
            bounded_usize(
                &["--max-nodes".into(), "32".into()],
                "--max-nodes",
                250,
                1,
                10_000
            )
            .unwrap(),
            32
        );
        assert!(
            bounded_usize(
                &["--max-nodes".into(), "0".into()],
                "--max-nodes",
                250,
                1,
                10_000
            )
            .is_err()
        );
    }
}
