#!/usr/bin/env python3
"""Optional OCR component check for the owned generated bare-canvas fixture.

Called only by the native browser lab with its owned CDP session. Reads the
selected canvas's PNG, never a desktop screenshot. The oracle is used only
after OCR for validation; no question/answer text is injected into the page,
pixels, CLI arguments, or OCR process. Missing OCR support is never a pass.

  python3 tests/acceptance/native_canvas_ocr.py --self-test
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit
import zlib


ROOT = Path(__file__).resolve().parents[2]
MAX_PNG_BYTES = 1_048_576
PNG_PREFIX = "data:image/png;base64,"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class CanvasFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise CanvasFailure(code)


def normalize_text(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = value.translate(str.maketrans({"×": "x", "−": "-", "–": "-", "“": '"', "”": '"', "’": "'"}))
    return re.sub(r"\s+", "", value)


def decode_canvas_png(value):
    require(isinstance(value, str) and value.startswith(PNG_PREFIX)
            and len(value) <= len(PNG_PREFIX) + 4 * ((MAX_PNG_BYTES + 2) // 3), "canvas_png_encoding_invalid")
    try:
        data = base64.b64decode(value[len(PNG_PREFIX):], validate=True)
    except ValueError:
        raise CanvasFailure("canvas_png_encoding_invalid") from None
    require(33 <= len(data) <= MAX_PNG_BYTES and data.startswith(PNG_SIGNATURE)
            and data[8:16] == b"\x00\x00\x00\rIHDR", "canvas_png_invalid")
    width, height, depth, color, compression, filtering, interlaced = struct.unpack(">IIBBBBB", data[16:29])
    require((width, height) == (720, 160) and depth == 8 and color in (2, 6)
            and (compression, filtering, interlaced) == (0, 0, 0), "canvas_png_dimensions_invalid")
    require(struct.unpack(">I", data[29:33])[0] == zlib.crc32(data[12:29]) & 0xffffffff,
            "canvas_png_header_invalid")
    return data


def _stop_ocr(process):
    # This group was created by this helper, including the CLI's Tesseract
    # child. Reap it on ordinary exit, timeout, cancellation, or any exception.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)


def _run_ocr(cli, image, environment):
    process = subprocess.Popen([str(cli), "ocr", "--file", str(image)], env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        try:
            output, _ = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            raise CanvasFailure("canvas_ocr_timeout") from None
        require(process.returncode == 0, "canvas_ocr_cli_failed")
        require(len(output) <= 65_536, "canvas_ocr_output_too_large")
        try:
            return output.decode("utf-8")
        except UnicodeError:
            raise CanvasFailure("canvas_ocr_invalid_utf8") from None
    finally:
        try:
            _stop_ocr(process)
        finally:
            process.stdout.close()


def check_canvas_ocr(cdp, session, *, cli, environment, private_root, expected_url):
    report = {"case": "canvas-bare", "passed": False, "status": "failed",
              "image_source": "owned_generated_canvas_png", "ocr_invoked": False,
              "ocr_component_tested": False, "desktop_screenshot_taken": False,
              "portal_permissions_tested": False, "automatic_fallback_tested": False,
              "external_ai": False, "oracle_text_injected": False}
    directory_fd, image_name, image_created = None, None, False
    try:
        require(sys.platform.startswith("linux"), "canvas_ocr_linux_required")
        cli, private_root = Path(cli), Path(private_root)
        require(cli.is_absolute() and cli.is_file() and os.access(cli, os.X_OK), "canvas_ocr_cli_invalid")
        require(private_root.is_absolute(), "canvas_private_runtime_invalid")
        directory_fd = os.open(private_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory_fd)
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, "canvas_private_runtime_invalid")
        parsed = urlsplit(expected_url)
        require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                and parsed.username is None and parsed.password is None and not parsed.fragment
                and re.fullmatch(r"/lab-[A-Za-z0-9_-]+/screen-reading-lab\.html", parsed.path),
                "canvas_owned_url_invalid")
        oracle = json.loads((ROOT / "tests/fixtures/screen-reading-lab-cases.json").read_text(encoding="utf-8"))
        bare = next(case for case in oracle["cases"] if case["id"] == "canvas-bare")
        markers = bare["pixel_readable_markers"]
        require(markers and all(isinstance(marker, str) and marker for marker in markers), "canvas_oracle_invalid")
        path = environment.get("PATH", os.defpath)
        if shutil.which("tesseract", path=path) is None:
            report.update(status="unavailable", failure_code="canvas_tesseract_missing")
            return report
        # Only scope guards enter JavaScript. Expected question/answer strings
        # remain exclusively in this validator after the OCR subprocess ends.
        expression = """(() => {
            const state = window.floatyScreenLab?.getState();
            const canvas = document.getElementById('question-canvas');
            if (location.href !== EXPECTED_URL || !document.title.startsWith(TITLE_PREFIX)
                || !state || state.caseId !== 'canvas-bare' || state.ready !== true
                || state.phase === 'render-unavailable' || !(canvas instanceof HTMLCanvasElement)
                || document.querySelectorAll('#question-canvas').length !== 1
                || canvas.width !== 720 || canvas.height !== 160) return {approved: false};
            return {approved: true, caseId: state.caseId, width: canvas.width,
                    height: canvas.height, data: canvas.toDataURL('image/png')};
        })()""".replace("EXPECTED_URL", json.dumps(expected_url)).replace(
            "TITLE_PREFIX", json.dumps(oracle["harness"]["window_title_prefix"]))
        captured = cdp.evaluate(session, expression)
        require(isinstance(captured, dict) and captured.get("approved") is True
                and captured.get("caseId") == "canvas-bare"
                and (captured.get("width"), captured.get("height")) == (720, 160), "canvas_scope_not_ready")
        png = decode_canvas_png(captured.get("data"))
        image_name = "owned-canvas-ocr-" + secrets.token_hex(8) + ".png"
        fd = os.open(image_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        image_created = True
        with os.fdopen(fd, "wb") as output:
            output.write(png)
        # No credentials, desktop-bus values, user profiles, proxies, or oracle
        # data are passed to the OCR-only command.
        ocr_environment = {"PATH": path, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                           "SOCKET_STREAM_HOME": str(ROOT),
                           "SOCKET_STREAM_CONFIG": str(private_root / "absent-ocr-config.json")}
        report["ocr_invoked"] = True
        text = _run_ocr(cli, private_root / image_name, ocr_environment)
        normalized = normalize_text(text)
        missing = [index for index, marker in enumerate(markers) if normalize_text(marker) not in normalized]
        report.update(passed=not missing, status="passed" if not missing else "failed",
                      ocr_component_tested=True, production_cli_command="ocr --file",
                      pixel_width=720, pixel_height=160, png_bytes=len(png),
                      png_sha256=hashlib.sha256(png).hexdigest(), readable_markers_checked=len(markers),
                      missing_marker_indices=missing, normalized_text_sha256=hashlib.sha256(normalized.encode()).hexdigest())
        if missing:
            report["failure_code"] = "canvas_ocr_question_not_extracted"
    except CanvasFailure as error:
        report["failure_code"] = str(error)
    except Exception:
        report["failure_code"] = "canvas_ocr_failed"
    finally:
        if directory_fd is not None:
            if image_created:
                try:
                    os.unlink(image_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            os.close(directory_fd)
    return report


def self_test():
    from unittest.mock import patch

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

    # A valid blank PNG tests the byte/file boundary; fake OCR below is plainly
    # a self-test and makes no claim to extract text from this blank image.
    png = (PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", 720, 160, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff" * (720 * 3)) * 160)) + chunk(b"IEND", b""))
    encoded = PNG_PREFIX + base64.b64encode(png).decode()
    require(decode_canvas_png(encoded) == png, "self_test_png_roundtrip")
    require(normalize_text("What is 9 × 9?") == normalize_text("WHAT is 9 x 9?\n"), "self_test_normalization")
    require(normalize_text("What is 9 + 9?") != normalize_text("What is 9 × 9?"), "self_test_operator_loss")
    for bad in ("data:image/jpeg;base64,AAAA", PNG_PREFIX + "!", PNG_PREFIX + base64.b64encode(b"not png").decode()):
        try:
            decode_canvas_png(bad)
        except CanvasFailure:
            pass
        else:
            raise CanvasFailure("self_test_png_guard")

    class FakeCDP:
        approved = True

        def evaluate(self, _session, expression):
            require("canvas.toDataURL('image/png')" in expression and "captureScreenshot" not in expression
                    and "What is 9" not in expression and "81" not in expression, "self_test_pixel_scope")
            return {"approved": self.approved, "caseId": "canvas-bare", "width": 720, "height": 160, "data": encoded}

    def fake_ocr(_cli, image, environment):
        info = image.stat()
        require(stat.S_IMODE(info.st_mode) == 0o600 and image.read_bytes() == png, "self_test_private_image")
        require(not any(key in environment for key in ("SOCKET_STREAM_API_KEY", "DBUS_SESSION_BUS_ADDRESS", "HTTPS_PROXY")),
                "self_test_ocr_environment")
        return "What is 9 x 9? Reply with the number only."

    with tempfile.TemporaryDirectory(prefix="canvas-ocr-self-test-") as temporary:
        cdp = FakeCDP()
        options = {"cli": Path(sys.executable), "environment": {"PATH": os.defpath},
                   "private_root": Path(temporary), "expected_url": "http://127.0.0.1:12345/lab-fixture/screen-reading-lab.html?case=canvas-bare"}
        with patch.object(shutil, "which", return_value="/usr/bin/tesseract"), patch(__name__ + "._run_ocr", side_effect=fake_ocr) as called:
            result = check_canvas_ocr(cdp, "owned-session", **options)
            require(result["passed"] and result["ocr_component_tested"] and called.call_count == 1, "self_test_component_flow")
            cdp.approved = False
            rejected = check_canvas_ocr(cdp, "owned-session", **options)
            require(not rejected["passed"] and called.call_count == 1, "self_test_unowned_canvas_rejected")
        with patch.object(shutil, "which", return_value=None):
            missing = check_canvas_ocr(cdp, "owned-session", **options)
            require(not missing["passed"] and missing["status"] == "unavailable" and not missing["ocr_invoked"],
                    "self_test_missing_ocr_not_passed")
        require(not list(Path(temporary).iterdir()), "self_test_image_cleanup")

    real_popen, owned = subprocess.Popen, []

    def sleeping_process(_arguments, **options):
        process = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **options)
        owned.append(process)

        def interrupted(*_args, **_kwargs):
            raise KeyboardInterrupt

        process.communicate = interrupted
        return process

    try:
        with patch.object(subprocess, "Popen", side_effect=sleeping_process):
            try:
                _run_ocr(Path(sys.executable), Path("/synthetic/unused.png"), {"PATH": os.defpath})
            except KeyboardInterrupt:
                pass
            else:
                raise CanvasFailure("self_test_cancellation_not_propagated")
        require(len(owned) == 1 and owned[0].poll() is not None, "self_test_cancelled_process_not_reaped")
        try:
            os.killpg(owned[0].pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise CanvasFailure("self_test_cancelled_group_survived")
    finally:
        for process in owned:
            if process.poll() is None:
                _stop_ocr(process)


if __name__ == "__main__":
    result = {"schema": 1, "test": "owned_canvas_ocr_helper", "success": False,
              "self_test": True, "desktop_capture_tested": False, "real_ocr_tested": False, "external_ai": False}
    try:
        require(sys.argv[1:] == ["--self-test"], "use_owned_native_runner_or_self_test")
        self_test()
        result["success"] = True
    except Exception:
        result["failure_code"] = "canvas_helper_self_test_failed"
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["success"] else 1)
