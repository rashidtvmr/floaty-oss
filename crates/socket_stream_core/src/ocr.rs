//! Offline Tesseract integration. No image bytes are sent to a cloud service.
use crate::a11y::ImageRegion;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{LazyLock, Mutex};
use std::time::SystemTime;

#[derive(Debug, thiserror::Error)]
pub enum OcrError {
    #[error("OCR input path does not exist")]
    MissingInput,
    #[error("OCR command failed: {0}")]
    Process(String),
    #[error("OCR result was not UTF-8")]
    Utf8,
}

pub fn extract_text(path: &Path) -> Result<String, OcrError> {
    extract_text_with_psm(path, None)
}

fn extract_text_with_psm(path: &Path, psm_override: Option<&str>) -> Result<String, OcrError> {
    if !path.is_file() {
        return Err(OcrError::MissingInput);
    }
    let output = if cfg!(target_os = "macos") {
        // macOS Vision OCR works offline and requires no Tesseract installation.
        let root = crate::paths::project_root();
        let binary = root.join("target/native-helpers/socket_stream_ocr");
        if binary.is_file() {
            Command::new(binary).arg(path).output()
        } else {
            Command::new("swift")
                .arg(root.join("scripts/macos-ocr.swift"))
                .arg(path)
                .output()
        }
    } else {
        let psm = psm_override
            .filter(|value| matches!(*value, "3" | "6" | "11" | "13"))
            .map(str::to_owned)
            .or_else(|| {
                std::env::var("SOCKET_STREAM_TESSERACT_PSM")
                    .ok()
                    .filter(|value| matches!(value.as_str(), "3" | "6" | "11" | "13"))
            })
            .unwrap_or_else(|| "6".into());
        Command::new("tesseract")
            .arg(path)
            .args(["stdout", "--psm", psm.as_str()])
            .output()
    }
    .map_err(|e| OcrError::Process(e.to_string()))?;
    if !output.status.success() {
        return Err(OcrError::Process(
            String::from_utf8_lossy(&output.stderr)
                .chars()
                .take(300)
                .collect(),
        ));
    }
    String::from_utf8(output.stdout).map_err(|_| OcrError::Utf8)
}

/// Maximum cached `(screenshot, region)` OCR results.
const REGION_CACHE_CAPACITY: usize = 8;

/// Cache key: screenshot path plus `[x, y, width, height]`.
type RegionCacheKey = (PathBuf, [i64; 4]);

#[derive(Clone)]
struct CachedRegionText {
    file_len: u64,
    modified: Option<SystemTime>,
    psm: String,
    text: String,
}

static REGION_CACHE: LazyLock<Mutex<HashMap<RegionCacheKey, CachedRegionText>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

fn cache_key(screenshot: &Path, region: ImageRegion) -> RegionCacheKey {
    (
        screenshot.to_path_buf(),
        [region.x, region.y, region.width, region.height],
    )
}

fn cache_get(
    key: &RegionCacheKey,
    file_len: u64,
    modified: Option<SystemTime>,
    psm: &str,
) -> Option<String> {
    REGION_CACHE
        .lock()
        .ok()
        .and_then(|map| map.get(key).cloned())
        .filter(|entry| {
            entry.file_len == file_len && entry.modified == modified && entry.psm == psm
        })
        .map(|entry| entry.text)
}

fn cache_put(
    key: RegionCacheKey,
    file_len: u64,
    modified: Option<SystemTime>,
    psm: &str,
    value: String,
) {
    if let Ok(mut map) = REGION_CACHE.lock() {
        if !map.contains_key(&key)
            && map.len() >= REGION_CACHE_CAPACITY
            && let Some(evict) = map.keys().next().cloned()
        {
            map.remove(&evict);
        }
        map.insert(
            key,
            CachedRegionText {
                file_len,
                modified,
                psm: psm.to_owned(),
                text: value,
            },
        );
    }
}

/// Pure region selection: drop regions below `min_pixels`, deduplicate identical
/// rectangles, and cap at `max_regions`. Order is preserved.
fn select_regions(
    regions: &[ImageRegion],
    max_regions: usize,
    min_pixels: i64,
) -> Vec<ImageRegion> {
    let mut seen = HashSet::new();
    regions
        .iter()
        .filter(|region| region.is_worth_ocr(min_pixels))
        .filter(|region| seen.insert([region.x, region.y, region.width, region.height]))
        .take(max_regions)
        .copied()
        .collect()
}

/// OCR a sub-rectangle of a screenshot. Crops with the platform tool, then runs
/// the existing [`extract_text`]. Never uploads pixels.
///
/// `psm` is accepted for CLI compatibility; the OCR backend selects its own
/// page-segmentation mode (`SOCKET_STREAM_TESSERACT_PSM` on Linux).
pub fn extract_region_text(
    screenshot: &Path,
    region: ImageRegion,
    psm: &str,
) -> Result<String, OcrError> {
    if !screenshot.is_file() {
        return Err(OcrError::MissingInput);
    }
    let key = cache_key(screenshot, region);
    let metadata = std::fs::metadata(screenshot).map_err(|_| OcrError::MissingInput)?;
    let file_len = metadata.len();
    let modified = metadata.modified().ok();
    if let Some(cached) = cache_get(&key, file_len, modified, psm) {
        return Ok(cached);
    }
    let temp = tempfile::Builder::new()
        .prefix("socket_stream_region-")
        .suffix(".png")
        .tempfile()
        .map_err(|e| OcrError::Process(e.to_string()))?;
    crate::capture::crop_to(screenshot, region, temp.path()).map_err(|e| match e {
        crate::capture::CaptureError::Unsupported => {
            OcrError::Process("no image crop tool available".into())
        }
        other => OcrError::Process(other.to_string()),
    })?;
    let text = extract_text_with_psm(temp.path(), Some(psm))?;
    cache_put(key, file_len, modified, psm, text.clone());
    Ok(text)
}

/// OCR several regions from one screenshot, bounded and deduplicated.
/// Regions below `min_pixels` are skipped and results are joined with `\n`.
/// Returns `Ok(String::new())` when nothing qualifies.
pub fn extract_regions_text(
    screenshot: &Path,
    regions: &[ImageRegion],
    max_regions: usize,
    min_pixels: i64,
    psm: &str,
) -> Result<String, OcrError> {
    let selected = select_regions(regions, max_regions, min_pixels);
    let mut parts = Vec::new();
    for region in selected {
        let text = extract_region_text(screenshot, region, psm)?;
        let trimmed = text.trim();
        if !trimmed.is_empty() {
            parts.push(trimmed.to_owned());
        }
    }
    Ok(parts.join("\n"))
}

/// Detect simple syntax incompleteness before sending extracted code to a model.
/// Conservative: strings/comments may contain punctuation, so warnings aren't definitive.
pub fn quality_warnings(text: &str) -> Vec<String> {
    let mut warnings = Vec::new();
    if text.trim().is_empty() {
        warnings.push("OCR returned no readable text".to_owned());
        return warnings;
    }
    let pairs = [('{', '}'), ('(', ')'), ('[', ']')];
    for (left, right) in pairs {
        let opens = text.chars().filter(|c| *c == left).count();
        let closes = text.chars().filter(|c| *c == right).count();
        if opens != closes {
            warnings.push(format!(
                "Possible missing or misrecognized bracket: {left} count={opens}, {right} count={closes}"
            ));
        }
    }
    if text.contains('�') {
        warnings.push("OCR contains replacement characters".to_owned());
    }
    warnings
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn detects_unbalanced_brackets() {
        let warnings = quality_warnings("function a() { return 1;");
        assert!(warnings.iter().any(|w| w.contains("bracket")));
        assert!(quality_warnings("function a() { return 1; }").is_empty());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn native_vision_reads_golden_fixture() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/sample-code.png");
        let text = extract_text(&path).expect("native Vision OCR should run on a provided image");
        assert!(text.contains("twoSum"), "unexpected OCR: {text}");
        assert!(text.contains("target"), "unexpected OCR: {text}");
    }

    #[test]
    fn missing_image_cannot_process() {
        assert!(matches!(
            extract_text(Path::new("/does/not/exist.png")),
            Err(OcrError::MissingInput)
        ));
    }

    fn region(x: i64, y: i64, width: i64, height: i64) -> ImageRegion {
        ImageRegion {
            x,
            y,
            width,
            height,
        }
    }

    #[test]
    fn select_regions_filters_below_min_pixels() {
        let regions = [
            region(0, 0, 10, 10),   // 100 px
            region(0, 0, 100, 100), // 10000 px
        ];
        let selected = select_regions(&regions, 10, 1000);
        assert_eq!(selected.len(), 1);
        assert_eq!(selected[0].width, 100);
    }

    #[test]
    fn select_regions_caps_at_max_regions() {
        let regions = [
            region(0, 0, 100, 100),
            region(200, 0, 100, 100),
            region(400, 0, 100, 100),
        ];
        let selected = select_regions(&regions, 2, 0);
        assert_eq!(selected.len(), 2);
        assert_eq!(selected[0].x, 0);
        assert_eq!(selected[1].x, 200);
    }

    #[test]
    fn select_regions_deduplicates_identical_rectangles() {
        let regions = [
            region(5, 5, 50, 50),
            region(5, 5, 50, 50),
            region(6, 5, 50, 50),
        ];
        let selected = select_regions(&regions, 10, 0);
        assert_eq!(selected.len(), 2);
    }

    #[test]
    fn select_regions_empty_input_is_empty() {
        assert!(select_regions(&[], 10, 0).is_empty());
    }

    #[test]
    fn cache_key_uses_path_and_rectangle() {
        let key = cache_key(Path::new("/tmp/frame.png"), region(1, 2, 3, 4));
        assert_eq!(key.0, PathBuf::from("/tmp/frame.png"));
        assert_eq!(key.1, [1, 2, 3, 4]);
    }

    #[test]
    fn region_cache_invalidates_when_frame_or_psm_changes() {
        let key = cache_key(
            Path::new("/tmp/socket_stream_cache-frame.png"),
            region(1, 2, 3, 4),
        );
        cache_put(key.clone(), 10, None, "6", "cached text".into());
        assert_eq!(
            cache_get(&key, 10, None, "6").as_deref(),
            Some("cached text")
        );
        assert_eq!(cache_get(&key, 11, None, "6"), None);
        assert_eq!(cache_get(&key, 10, None, "11"), None);
    }

    #[test]
    fn cache_round_trips_and_evicts_beyond_capacity() {
        // Use unique keys so the shared cache is not disturbed by other tests.
        let base = PathBuf::from("/tmp/socket_stream_cache-test");
        for i in 0..(REGION_CACHE_CAPACITY + 2) {
            let key = (base.join(i.to_string()), [i as i64, 0, 10, 10]);
            cache_put(key.clone(), i as u64, None, "6", format!("value-{i}"));
            assert_eq!(
                cache_get(&key, i as u64, None, "6"),
                Some(format!("value-{i}"))
            );
        }
        let mut cached = 0;
        for i in 0..(REGION_CACHE_CAPACITY + 2) {
            let key = (base.join(i.to_string()), [i as i64, 0, 10, 10]);
            if cache_get(&key, i as u64, None, "6").is_some() {
                cached += 1;
            }
        }
        assert!(cached <= REGION_CACHE_CAPACITY);
    }

    #[test]
    fn empty_region_list_returns_empty_string() {
        let text = extract_regions_text(Path::new("/does/not/exist.png"), &[], 4, 0, "6").unwrap();
        assert!(text.is_empty());
    }

    #[test]
    fn regions_below_min_pixels_return_empty_string() {
        let regions = [region(0, 0, 5, 5)];
        let text =
            extract_regions_text(Path::new("/does/not/exist.png"), &regions, 4, 1000, "6").unwrap();
        assert!(text.is_empty());
    }

    #[test]
    fn region_ocr_missing_screenshot_is_missing_input() {
        assert!(matches!(
            extract_region_text(Path::new("/does/not/exist.png"), region(0, 0, 10, 10), "6"),
            Err(OcrError::MissingInput)
        ));
    }
}
