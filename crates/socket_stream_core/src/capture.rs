//! Explicit, one-shot screenshot capture; caller owns permission/session gate.
use crate::a11y::ImageRegion;
use crate::session::PracticeSession;
use std::path::Path;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::Duration;

#[derive(Debug, thiserror::Error)]
pub enum CaptureError {
    #[error("session not authorized or is paused")]
    PermissionDenied,
    #[error("capture tool unsupported on this platform or environment")]
    Unsupported,
    #[error("capture command failed: {0}")]
    Process(String),
}

pub fn capture_to(session: &PracticeSession, destination: &Path) -> Result<(), CaptureError> {
    if !session.may_capture() {
        return Err(CaptureError::PermissionDenied);
    }
    let mut command = if cfg!(target_os = "macos") {
        let mut c = Command::new("screencapture");
        c.arg("-x").arg(destination);
        c
    } else if cfg!(target_os = "linux") && std::env::var_os("WAYLAND_DISPLAY").is_some() {
        let mut c = Command::new("grim");
        c.arg(destination);
        c
    } else if cfg!(target_os = "linux") && std::env::var_os("DISPLAY").is_some() {
        let mut c = Command::new("import");
        c.args(["-window", "root"]).arg(destination);
        c
    } else {
        return Err(CaptureError::Unsupported);
    };
    let output = command
        .output()
        .map_err(|e| CaptureError::Process(e.to_string()))?;
    if !output.status.success() {
        return Err(CaptureError::Process(
            String::from_utf8_lossy(&output.stderr)
                .chars()
                .take(300)
                .collect(),
        ));
    }
    Ok(())
}

/// Wayland compositors such as GNOME/KDE commonly require a persistent portal
/// session, rather than a wlroots-only 'grim' screenshot command.
pub fn needs_portal(desktop: &str, is_wayland: bool, grim_present: bool) -> bool {
    if !is_wayland {
        return false;
    }
    let desktop = desktop.to_ascii_lowercase();
    desktop.contains("gnome")
        || desktop.contains("kde")
        || desktop.contains("plasma")
        || !grim_present
}

pub fn requires_linux_portal() -> bool {
    if !cfg!(target_os = "linux") {
        return false;
    }
    let desktop = std::env::var("XDG_CURRENT_DESKTOP").unwrap_or_default();
    let is_wayland = std::env::var_os("WAYLAND_DISPLAY").is_some();
    let grim_present = Command::new("grim").arg("--help").output().is_ok();
    needs_portal(&desktop, is_wayland, grim_present)
}

/// Image cropping backends, in preference order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CropTool {
    Magick,
    Convert,
    Sips,
}

/// First available crop tool: `magick`, then `convert`, then `sips` on macOS.
/// Spawning the tool is enough to consider it present; a non-zero exit is fine.
pub(crate) fn crop_tool() -> Option<CropTool> {
    if command_available("magick") {
        Some(CropTool::Magick)
    } else if command_available("convert") {
        Some(CropTool::Convert)
    } else if cfg!(target_os = "macos") && command_available("sips") {
        Some(CropTool::Sips)
    } else {
        None
    }
}

fn command_available(name: &str) -> bool {
    Command::new(name).arg("--version").output().is_ok()
}

/// Crop `source` to `region` and write it to `destination`.
/// Returns `CaptureError::Unsupported` when no crop tool exists.
pub fn crop_to(source: &Path, region: ImageRegion, destination: &Path) -> Result<(), CaptureError> {
    if !source.is_file() {
        return Err(CaptureError::Process("source image does not exist".into()));
    }
    let tool = crop_tool().ok_or(CaptureError::Unsupported)?;
    let output = match tool {
        CropTool::Magick | CropTool::Convert => {
            let name = if tool == CropTool::Magick {
                "magick"
            } else {
                "convert"
            };
            Command::new(name)
                .arg(source)
                .arg("-crop")
                .arg(format!(
                    "{}x{}+{}+{}",
                    region.width, region.height, region.x, region.y
                ))
                .arg("+repage")
                .arg(destination)
                .output()
        }
        CropTool::Sips => Command::new("sips")
            .args(["-c", &region.height.to_string(), &region.width.to_string()])
            .args(["--cropOffset", &region.y.to_string(), &region.x.to_string()])
            .arg(source)
            .arg("--out")
            .arg(destination)
            .output(),
    }
    .map_err(|e| CaptureError::Process(e.to_string()))?;
    if !output.status.success() {
        return Err(CaptureError::Process(
            String::from_utf8_lossy(&output.stderr)
                .chars()
                .take(300)
                .collect(),
        ));
    }
    Ok(())
}

/// Owns a single approved portal screen stream and terminates it on drop.
/// The child writes one latest PNG atomically; no frame history is retained.
pub struct PortalFrameSource {
    child: Child,
    path: PathBuf,
}

impl PortalFrameSource {
    pub fn start(
        session: &PracticeSession,
        path: PathBuf,
        interval: u64,
    ) -> Result<Self, CaptureError> {
        if !session.may_capture() {
            return Err(CaptureError::PermissionDenied);
        }
        if !(2..=3600).contains(&interval) {
            return Err(CaptureError::Unsupported);
        }
        if !cfg!(target_os = "linux") {
            return Err(CaptureError::Unsupported);
        }
        let helper = crate::paths::project_root().join("scripts/linux-portal-capture.py");
        let child = Command::new(crate::paths::system_python())
            .arg(helper)
            .args(["--authorized", "--output"])
            .arg(&path)
            .args(["--interval", &interval.to_string()])
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| CaptureError::Process(e.to_string()))?;
        Ok(Self { child, path })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn finished(&mut self) -> Result<bool, CaptureError> {
        self.child
            .try_wait()
            .map(|status| status.is_some())
            .map_err(|e| CaptureError::Process(e.to_string()))
    }
}

impl Drop for PortalFrameSource {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_some() {
            return;
        }
        // SIGTERM lets the helper close the portal session cleanly. Avoid
        // leaving a recording active after a practice session ends.
        let _ = Command::new("kill")
            .arg("-TERM")
            .arg(self.child.id().to_string())
            .status();
        for _ in 0..5 {
            if self.child.try_wait().ok().flatten().is_some() {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn denied_before_capture_command_invoked() {
        let session = PracticeSession::new();
        assert!(matches!(
            capture_to(&session, Path::new("/tmp/socket_stream_test-denied.png")),
            Err(CaptureError::PermissionDenied)
        ));
    }

    #[test]
    fn selects_portal_on_supported_wayland_desktops() {
        assert!(needs_portal("GNOME", true, true));
        assert!(needs_portal("KDE", true, true));
        assert!(needs_portal("sway", true, false));
        assert!(!needs_portal("sway", true, true));
        assert!(!needs_portal("GNOME", false, false));
    }

    #[test]
    fn crop_to_missing_source_is_process_error() {
        let region = ImageRegion {
            x: 0,
            y: 0,
            width: 10,
            height: 10,
        };
        let result = crop_to(
            Path::new("/does/not/exist.png"),
            region,
            Path::new("/tmp/socket_stream_crop-out.png"),
        );
        assert!(matches!(result, Err(CaptureError::Process(_))));
    }
}
