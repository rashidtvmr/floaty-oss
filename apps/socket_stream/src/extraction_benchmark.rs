//! Reproducible, local-only baselines. Fixtures contain synthetic questions;
//! reports contain metrics, never captured desktop or model response content.
use serde::Deserialize;
use serde_json::{Value, json};
use socket_stream_core::a11y::{A11yReading, A11yTree, ReadMode, TokenBudget, estimate_tokens};
use socket_stream_core::accessibility::needs_ocr;
use socket_stream_core::ocr::{extract_text, quality_warnings};
use std::hint::black_box;
use std::time::Instant;

const FIXTURES: &str = include_str!("../../../tests/fixtures/extraction/cases.json");

#[derive(Deserialize)]
struct Fixture {
    name: String,
    tree: Value,
    keep: Vec<String>,
    drop: Vec<String>,
    text_needs_ocr: bool,
    image_regions: usize,
}

fn fixtures() -> Result<Vec<Fixture>, String> {
    serde_json::from_str(FIXTURES).map_err(|_| "invalid bundled extraction fixtures".into())
}

fn fidelity(fixture: &Fixture, reading: &A11yReading) -> bool {
    fixture
        .keep
        .iter()
        .all(|part| reading.text.contains(part) && reading.display.contains(part))
        && fixture
            .drop
            .iter()
            .all(|part| !reading.text.contains(part) && !reading.display.contains(part))
        && needs_ocr(Some(&reading.display)) == fixture.text_needs_ocr
        && reading.image_regions.len() == fixture.image_regions
        && !reading.truncated
}

/// Nearest-rank percentiles; every caller supplies at least one sample.
fn timing_ms(mut samples: Vec<f64>) -> Value {
    let first = samples[0];
    samples.sort_by(f64::total_cmp);
    let rank = |percent: usize| (samples.len() * percent).div_ceil(100) - 1;
    json!({
        "samples": samples.len(),
        "first": first,
        "p50": samples[rank(50)],
        "p95": samples[rank(95)],
    })
}

fn benchmark_case(fixture: &Fixture, iterations: usize) -> Result<Value, String> {
    let input = fixture.tree.to_string();
    let tree = A11yTree::parse(&input).map_err(|error| error.to_string())?;
    let budget = TokenBudget::default();
    let baseline = tree.render(ReadMode::Tree, budget);
    let question = tree.render_question(ReadMode::Tree, budget);
    if !fidelity(fixture, &question) {
        return Err(format!("extraction fidelity failed: {}", fixture.name));
    }
    let mut baseline_times = Vec::with_capacity(iterations);
    let mut question_times = Vec::with_capacity(iterations);
    for _ in 0..iterations {
        let started = Instant::now();
        let parsed = A11yTree::parse(black_box(&input)).map_err(|error| error.to_string())?;
        black_box(parsed.render(ReadMode::Tree, budget));
        baseline_times.push(started.elapsed().as_secs_f64() * 1000.0);

        let started = Instant::now();
        let parsed = A11yTree::parse(black_box(&input)).map_err(|error| error.to_string())?;
        black_box(parsed.render_question(ReadMode::Tree, budget));
        question_times.push(started.elapsed().as_secs_f64() * 1000.0);
    }
    let baseline_tokens = estimate_tokens(&baseline.text);
    let question_tokens = estimate_tokens(&question.text);
    Ok(json!({
        "case": fixture.name,
        "input_nodes": tree.nodes.len(),
        "baseline_tokens_estimate": baseline_tokens,
        "question_tokens_estimate": question_tokens,
        "token_reduction_percent": if baseline_tokens == 0 { 0.0 } else {
            (1.0 - question_tokens as f64 / baseline_tokens as f64) * 100.0
        },
        "question_characters": question.display.chars().count(),
        "text_needs_ocr": needs_ocr(Some(&question.display)),
        "image_regions": question.image_regions.len(),
        "fidelity_passed": true,
        "baseline_parse_render_ms": timing_ms(baseline_times),
        "question_parse_render_ms": timing_ms(question_times),
    }))
}

fn benchmark_ocr(iterations: usize) -> Result<Value, String> {
    let path = socket_stream_core::paths::project_root().join("tests/fixtures/sample-code.png");
    let mut samples = Vec::with_capacity(iterations);
    let mut characters = 0;
    let mut warnings = 0;
    for _ in 0..iterations {
        let started = Instant::now();
        let text = extract_text(&path).map_err(|error| error.to_string())?;
        samples.push(started.elapsed().as_secs_f64() * 1000.0);
        if !["twoSum", "target"]
            .iter()
            .all(|fragment| text.contains(fragment))
        {
            return Err("bundled OCR fixture did not retain both required code identifiers".into());
        }
        characters = text.chars().count();
        warnings = quality_warnings(&text).len();
    }
    Ok(json!({
        "fixture": "sample-code.png",
        "backend": if cfg!(target_os = "macos") { "Apple Vision helper" } else { "Tesseract subprocess" },
        "full_image_ms": timing_ms(samples),
        "characters": characters,
        "quality_warning_count": warnings,
        "required_identifiers_retained": 2,
        "accuracy_scope": "two golden identifiers, not a character/word accuracy score",
        "persistent_engine": false,
    }))
}

pub(crate) fn run(args: &[String]) -> Result<(), String> {
    let iterations = crate::bounded_usize(args, "--iterations", 100, 1, 10_000)?;
    let ocr_iterations = crate::bounded_usize(args, "--ocr-iterations", 3, 1, 10)?;
    let cases = fixtures()?
        .iter()
        .map(|fixture| benchmark_case(fixture, iterations))
        .collect::<Result<Vec<_>, _>>()?;
    let ocr = if crate::contains(args, "--with-ocr") {
        benchmark_ocr(ocr_iterations)?
    } else {
        Value::Null
    };
    let output = json!({
        "schema": 1,
        "platform": std::env::consts::OS,
        "architecture": std::env::consts::ARCH,
        "optimized_build": !cfg!(debug_assertions),
        "scope": "synthetic parse/render baseline; excludes OS accessibility traversal, capture and inference",
        "token_estimator": "ceil(Unicode character count / 4), not provider tokenization or billing",
        "cases": cases,
        "ocr": ocr,
    });
    println!(
        "{}",
        serde_json::to_string_pretty(&output).map_err(|error| error.to_string())?
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn golden_question_fixtures_keep_content_and_drop_chrome() {
        let fixtures = fixtures().unwrap();
        assert!(fixtures.len() >= 6);
        for fixture in fixtures {
            let report = benchmark_case(&fixture, 1).unwrap();
            assert_eq!(report["fidelity_passed"], true, "{}", fixture.name);
        }
    }

    #[test]
    fn percentile_uses_nearest_rank_and_keeps_first_run_separate() {
        let metrics = timing_ms(vec![9.0, 1.0, 3.0, 2.0, 4.0]);
        assert_eq!(metrics["first"], 9.0);
        assert_eq!(metrics["p50"], 3.0);
        assert_eq!(metrics["p95"], 9.0);
        assert_eq!(timing_ms(vec![2.0])["p95"], 2.0);
    }
}
