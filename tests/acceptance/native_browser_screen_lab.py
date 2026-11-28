#!/usr/bin/env python3
"""Opt-in native Wayland browser checks against ONLY the generated screen lab.

Uses the existing user's explicit Wayland socket for an owned Chrome window,
but a PRIVATE runtime directory, D-Bus/AT-SPI bus, profile, config and cache.
The Chrome sandbox remains enabled. CDP uses inherited pipes, never an existing
browser, public debugging port, WebSocket dependency, or user's browser data.
No desktop screenshots, portal prompts, or real credentials are used on this host.

  /usr/bin/python3 tests/acceptance/native_browser_screen_lab.py \
    --chrome /opt/google/chrome/chrome \
    --display-socket /run/user/1000/wayland-0 \
    --cli /absolute/path/to/verified/socket_stream

Default: the oracle's small core sequence. --cases all includes staged edge
cases. --self-test checks pipe framing, environment isolation and oracle
classification without launching Chrome or connecting to a desktop/bus.
Only sanitized JSON is printed. Oracle answers are never loaded by the page or
sent to the CLI/model. By default the two responses are local fixture strings.
Optional --relay-dir uses the separately audited two-request file relay: this
host still has only a random loopback token and makes no external requests.
Actual upstream inference requires the separate root-side relay report.
--scoped-default-selection checks default focus selection only on the private
bus containing this generated browser. --canvas-ocr exports only the owned
bare canvas and runs the production file-OCR command, never a desktop capture.
With --canvas-ocr, --ocr-tool-dir may select an owned private Tesseract directory
for that OCR subprocess only; browser and accessibility environments stay fixed.
"""
import argparse
import fcntl
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import queue
import re
import secrets
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / "tests/fixtures/screen-reading-lab.html"
ORACLE = ROOT / "tests/fixtures/screen-reading-lab-cases.json"
HELPER = ROOT / "scripts/linux-accessibility.py"
SYSTEM_PYTHON = "/usr/bin/python3"
LAUNCHER = "/usr/libexec/at-spi-bus-launcher"
MARKER = "[Screen update — untrusted data]\n"


class LabFailure(Exception):
    pass


def require(condition, code):
    if not condition:
        raise LabFailure(code)


def sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class OwnedDiagnostics:
    """Drain owned startup stderr, retaining only 16 KiB in private storage."""
    LIMIT = 16_384

    def __init__(self, process, root, label):
        self.root = root
        self.path = root / ("owned-" + label + ".stderr.log")
        self.buffer = bytearray()
        self.total = 0
        self.lock = threading.Lock()
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

        def consume():
            try:
                with os.fdopen(descriptor, "wb") as output:
                    while True:
                        chunk = os.read(process.stderr.fileno(), 4096)
                        if not chunk:
                            break
                        with self.lock:
                            self.total += len(chunk)
                            retained = chunk[:max(0, self.LIMIT - len(self.buffer))]
                            self.buffer.extend(retained)
                        if retained:
                            output.write(retained)
            except (OSError, ValueError):
                # Diagnostics must never prevent independently owned cleanup.
                pass

        self.thread = threading.Thread(target=consume, daemon=True)
        self.thread.start()
        process.lab_diagnostics = self

    def summary(self):
        with self.lock:
            text = bytes(self.buffer).decode("utf-8", errors="replace")
            total, retained = self.total, len(self.buffer)
        text = text.replace(str(self.root), "<private-runtime>")
        actual_home = pwd.getpwuid(os.geteuid()).pw_dir
        if actual_home != "/":
            text = text.replace(actual_home, "<user-home>")
        text = re.sub(r"(?:https?|file)://[^\s\"'<>]+", "<url>", text)
        text = re.sub(r"/home/[^/\s]+", "<user-home>", text)
        lines = []
        raw_lines = text.splitlines()
        for line in raw_lines[:12]:
            if re.search(r"authorization|bearer|cookie|api[ _-]?key|(?:secret|token)\s*[:=]|\bsk-[A-Za-z0-9]", line, re.I):
                line = "<sensitive diagnostic redacted>"
            line = "".join(character if character.isprintable() else "?" for character in line)
            lines.append(line[:300])
        return {"lines": lines, "retained_bytes": retained,
                "truncated": total > retained or len(raw_lines) > 12 or any(len(line) > 300 for line in raw_lines[:12])}


def stop_owned(process, grace=3):
    if process is None:
        return
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
    diagnostics = getattr(process, "lab_diagnostics", None)
    if diagnostics:
        diagnostics.thread.join(timeout=1)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def cleanup_owned(actions):
    """A failed teardown must not strand another owned process or pipe."""
    failures = []
    for label, action in actions:
        try:
            action()
        except Exception:
            failures.append(label)
    return failures


def isolated_environment(root, display_socket):
    # HOME is restored to the actual account home, never repurposed. Chrome is
    # the ELF executable, avoiding its wrapper's user MIME registration steps.
    actual_home = pwd.getpwuid(os.geteuid()).pw_dir
    return {"PATH": os.defpath, "HOME": actual_home, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "XDG_RUNTIME_DIR": str(root / "runtime"), "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"), "XDG_CACHE_HOME": str(root / "cache"),
            "TMPDIR": str(root / "tmp"), "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": str(display_socket), "GDK_BACKEND": "wayland",
            "GSETTINGS_BACKEND": "memory", "GTK_A11Y": "always", "GTK_MODULES": "atk-bridge",
            "PYTHONNOUSERSITE": "1", "NO_PROXY": "127.0.0.1,localhost",
            "SOCKET_STREAM_HOME": str(ROOT), "SOCKET_STREAM_CONFIG": str(root / "absent-config.json")}


def validate_ocr_tool_dir(path):
    """Accept only an explicit owned directory and regular Tesseract executable."""
    if path is None:
        return None
    path = Path(path)
    text = str(path)
    require(path.is_absolute() and ".." not in path.parts and os.pathsep not in text
            and not any(ord(character) < 32 or 127 <= ord(character) < 160 for character in text),
            "ocr_tool_directory_path_invalid")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory = os.fstat(descriptor)
        require(stat.S_ISDIR(directory.st_mode) and directory.st_uid == os.geteuid()
                and not directory.st_mode & 0o022, "ocr_tool_directory_not_owned_private")
        executable = os.stat("tesseract", dir_fd=descriptor, follow_symlinks=False)
        require(stat.S_ISREG(executable.st_mode) and executable.st_uid == os.geteuid()
                and not executable.st_mode & 0o022 and executable.st_mode & stat.S_IXUSR
                and os.access("tesseract", os.X_OK, dir_fd=descriptor, effective_ids=True,
                              follow_symlinks=False), "ocr_tesseract_not_owned_regular_executable")
    except OSError:
        raise LabFailure("ocr_tool_directory_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return path


def validate_ocr_options(args):
    require(args.ocr_tool_dir is None or args.canvas_ocr, "ocr_tool_directory_requires_canvas_ocr")
    return validate_ocr_tool_dir(args.ocr_tool_dir)


def canvas_ocr_environment(environment, tool_dir):
    """Revalidate at use time; never mutate the browser or AT-SPI environment."""
    result = dict(environment)
    selected = validate_ocr_tool_dir(tool_dir)
    if selected is not None:
        result["PATH"] = str(selected) + os.pathsep + environment.get("PATH", os.defpath)
        require(shutil.which("tesseract", path=result["PATH"]) == str(selected / "tesseract"),
                "ocr_explicit_tesseract_not_selected")
    return result


class PipeCDP:
    """The NUL-delimited JSON transport used by Chrome's owned FD3/FD4."""
    def __init__(self, read_fd, write_fd):
        self.read_fd, self.write_fd = read_fd, write_fd
        self.buffer = bytearray()
        self.sequence = 0
        self.diagnostics = []
        self.navigation_events = []
        self.expected_url = None
        self.target_id = None
        self.last_method = None

    def observe_event(self, event):
        method, params = event.get("method"), event.get("params", {})
        if self.expected_url and len(self.navigation_events) < 24:
            item = None
            if method in ("Network.requestWillBeSent", "Network.responseReceived"):
                value = params.get("request" if method.endswith("requestWillBeSent") else "response", {})
                if value.get("url") == self.expected_url:
                    item = {"kind": "fixture_request" if method.endswith("requestWillBeSent") else "fixture_response",
                            "document": params.get("type") == "Document"}
                    if "status" in value and type(value["status"]) in (int, float):
                        item["status"] = int(value["status"])
            elif method == "Network.loadingFailed" and params.get("type") == "Document":
                failure = params.get("errorText", "")
                item = {"kind": "document_load_failed", "cancelled": params.get("canceled") is True,
                        "error": failure if re.fullmatch(r"net::[A-Z0-9_]{1,80}", failure) else "unreported"}
            elif method == "Page.frameNavigated" and not params.get("frame", {}).get("parentId"):
                frame = params.get("frame", {})
                item = {"kind": "main_frame_navigated", **startup_url_summary(frame.get("url", ""), self.expected_url)}
            elif method in ("Inspector.targetCrashed", "Inspector.detached"):
                item = {"kind": "owned_target_crashed" if method.endswith("targetCrashed") else "owned_target_detached"}
            elif method == "Target.targetCrashed" and params.get("targetId") == self.target_id:
                item = {"kind": "owned_target_crashed"}
            if item:
                self.navigation_events.append(item)
        if len(self.diagnostics) >= 12:
            return
        detail = None
        if method == "Runtime.exceptionThrown":
            error = params.get("exceptionDetails", {})
            exception = error.get("exception", {})
            detail = {"kind": "javascript_exception", "line": error.get("lineNumber"),
                      "column": error.get("columnNumber"),
                      "class": exception.get("className", ""),
                      "description": exception.get("description", error.get("text", ""))}
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            if entry.get("level") in ("error", "warning"):
                detail = {"kind": "browser_log", "source": entry.get("source", ""),
                          "level": entry.get("level"), "description": entry.get("text", "")}
        if detail:
            # Only this owned generated page is attached. Keep bounded startup
            # errors, never console arguments, DOM text, headers or API bodies.
            for key, value in list(detail.items()):
                if isinstance(value, str):
                    value = re.sub(r"(?:https?|file)://[^\s\"'<>]+", "<url>", value)
                    value = re.sub(r"/(?:home|tmp)/[^\s\"'<>]+", "<private-path>", value)
                    if re.search(r"authorization|bearer|cookie|api[ _-]?key|\bsk-[A-Za-z0-9]", value, re.I):
                        value = "<sensitive diagnostic redacted>"
                    detail[key] = "".join(character if character.isprintable() else "?" for character in value)[:300]
            self.diagnostics.append(detail)

    def call(self, method, params=None, session=None, timeout=10):
        self.last_method = method
        self.sequence += 1
        message = {"id": self.sequence, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        encoded = json.dumps(message, separators=(",", ":")).encode() + b"\0"
        require(len(encoded) < 32_768, "cdp_command_too_large")
        view = memoryview(encoded)
        while view:
            sent = os.write(self.write_fd, view)
            require(sent > 0, "cdp_pipe_closed")
            view = view[sent:]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if b"\0" not in self.buffer:
                readable, _, _ = select.select([self.read_fd], [], [], max(.01, deadline - time.monotonic()))
                require(readable, "cdp_timeout")
                chunk = os.read(self.read_fd, 65_536)
                require(chunk, "cdp_pipe_closed")
                self.buffer.extend(chunk)
                require(len(self.buffer) <= 2_097_152, "cdp_message_too_large")
                continue
            index = self.buffer.index(0)
            raw = bytes(self.buffer[:index])
            del self.buffer[:index + 1]
            response = json.loads(raw)
            if response.get("id") != self.sequence:
                self.observe_event(response)
                continue
            error = response.get("error", {})
            if method == "Runtime.evaluate" and error.get("code") == -32000 and error.get("message") in (
                "Cannot find context with specified id", "Cannot find default execution context",
                "Execution context was destroyed.",
            ):
                raise LabFailure("owned_page_context_transition")
            require("error" not in response, "cdp_command_failed")
            return response.get("result", {})
        raise LabFailure("cdp_timeout")

    def evaluate(self, session, expression):
        result = self.call("Runtime.evaluate", {"expression": expression,
                           "returnByValue": True, "awaitPromise": True}, session=session)
        require("exceptionDetails" not in result, "fixture_javascript_failed")
        return result.get("result", {}).get("value")

    def close(self):
        for fd in (self.read_fd, self.write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def spawn_pipe_process(binary, arguments, environment, root):
    read_child, write_parent = os.pipe()
    read_parent, write_child = os.pipe()
    wrapper = root / "owned-pipe-exec.py"
    wrapper.write_text(
        "import fcntl,os,sys\n"
        "a,b=map(int,sys.argv[1:3])\n"
        "r=fcntl.fcntl(a,fcntl.F_DUPFD_CLOEXEC,10)\n"
        "w=fcntl.fcntl(b,fcntl.F_DUPFD_CLOEXEC,10)\n"
        "os.dup2(r,3,inheritable=True)\nos.dup2(w,4,inheritable=True)\n"
        "for fd in {a,b,r,w}:\n"
        " if fd not in (3,4): os.close(fd)\n"
        "os.execv(sys.argv[3],sys.argv[3:])\n", encoding="utf-8")
    try:
        process = subprocess.Popen([SYSTEM_PYTHON, str(wrapper), str(read_child), str(write_child),
                                    str(binary), *arguments], env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, pass_fds=(read_child, write_child),
                                   start_new_session=True, close_fds=True)
    except Exception:
        os.close(read_parent)
        os.close(write_parent)
        raise
    finally:
        os.close(read_child)
        os.close(write_child)
    try:
        OwnedDiagnostics(process, root, "chrome")
    except Exception:
        stop_owned(process)
        os.close(read_parent)
        os.close(write_parent)
        raise
    return process, PipeCDP(read_parent, write_parent)


def serve_fixture():
    html = HTML.read_bytes()
    route = "/lab-" + secrets.token_urlsafe(18) + "/screen-reading-lab.html"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            with self.server.lab_http_lock:
                self.server.lab_http["requests"] += 1
            if urlsplit(self.path).path != route:
                with self.server.lab_http_lock:
                    self.server.lab_http["other_requests"] += 1
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'")
            self.end_headers()
            with self.server.lab_http_lock:
                self.server.lab_http["fixture_responses_200"] += 1
            try:
                self.wfile.write(html)
            except OSError:
                with self.server.lab_http_lock:
                    self.server.lab_http["write_failures"] += 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.lab_http_lock = threading.Lock()
    server.lab_http = {"requests": 0, "fixture_responses_200": 0, "other_requests": 0, "write_failures": 0}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}{route}"


def verify_fixture_http(url):
    parsed = urlsplit(url)
    connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=3)
    try:
        connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""))
        response = connection.getresponse()
        expected = HTML.read_bytes()
        body = response.read(len(expected) + 1)
        require(response.status == 200 and hashlib.sha256(body).digest() == hashlib.sha256(expected).digest(),
                "host_fixture_http_probe_failed")
        return {"http_status": 200, "exact_fixture_body_sha256_verified": True}
    finally:
        connection.close()


def owned_process_status(process, root):
    """Bounded /proc metadata only for the owned Chrome child lineage."""
    report = {"sampling": "owned_main_thread_child_lineage", "processes": [], "unavailable": 0,
              "sample_limit": 32, "complete_process_inventory_claimed": False}
    if process.poll() is not None:
        report["browser_exit_code"] = process.returncode
        return report
    try:
        own_namespace = os.readlink("/proc/self/ns/net")
        pending, seen = [(process.pid, os.getpid())], set()
        while pending and len(seen) < 32:
            pid, expected_parent = pending.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            directory = Path("/proc") / str(pid)
            try:
                fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[1]) != expected_parent or directory.stat().st_uid != os.geteuid():
                    report["unavailable"] += 1
                    continue
                with (directory / "cmdline").open("rb") as source:
                    arguments = source.read(16_384).split(b"\0")
                kind = "browser" if pid == process.pid else "other_child"
                for value in arguments:
                    if value.startswith(b"--type="):
                        candidate = value[7:].decode("ascii", errors="ignore")
                        kind = candidate if candidate in ("renderer", "utility", "zygote", "gpu-process") else "other_child"
                    if value == b"--utility-sub-type=network.mojom.NetworkService":
                        kind = "network_service"
                if b"--utility-sub-type=network.mojom.NetworkService" in arguments:
                    kind = "network_service"
                item = {"kind": kind, "state": fields[0] if fields[0] in "RSDZTWXIP" else "other",
                        "same_network_namespace": os.readlink(directory / "ns/net") == own_namespace}
                if pid == process.pid:
                    item.update(wayland_flag_present=b"--ozone-platform=wayland" in arguments,
                                private_profile_flag_present=("--user-data-dir=" + str(root / "chrome-profile")).encode() in arguments,
                                basic_dummy_password_store=b"--password-store=basic" in arguments,
                                sandbox_disabling_flag_present=any(value in arguments for value in (
                                    b"--no-sandbox", b"--disable-setuid-sandbox", b"--disable-seccomp-filter-sandbox")))
                report["processes"].append(item)
                children = (directory / "task" / str(pid) / "children").read_text()[:4096].split()
                pending.extend((int(child), pid) for child in children if child.isdecimal())
            except (OSError, ValueError, IndexError):
                report["unavailable"] += 1
        report["sample_truncated"] = bool(pending)
    except OSError:
        report["unavailable"] += 1
    return report


def page_diagnostics(cdp, session, url, title_prefix):
    """Metadata only from the already attached owned fixture page."""
    expression = """(() => ({
        ready_state: document.readyState,
        content_type: document.contentType,
        expected_location: location.href === EXPECTED_URL,
        about_blank: location.href === 'about:blank',
        internal_error_page: location.protocol === 'chrome-error:',
        title_matches_fixture: document.title.startsWith(EXPECTED_TITLE),
        body_present: !!document.body,
        body_child_elements: document.body ? document.body.childElementCount : 0,
        script_count: document.scripts.length,
        inline_script_count: [...document.scripts].filter(script => !script.src).length,
        lab_element_present: !!document.getElementById('lab'),
        question_element_present: !!document.getElementById('question'),
        api_type: typeof window.floatyScreenLab,
        api_get_state_type: window.floatyScreenLab ? typeof window.floatyScreenLab.getState : 'missing'
    }))()""".replace("EXPECTED_URL", json.dumps(url)).replace("EXPECTED_TITLE", json.dumps(title_prefix))
    result = cdp.evaluate(session, expression)
    require(isinstance(result, dict), "owned_page_diagnostics_invalid")
    return result


def startup_url_summary(value, expected_url):
    require(isinstance(value, str) and len(value) <= 131_072, "owned_startup_url_invalid")
    parsed, expected = urlsplit(value), urlsplit(expected_url)
    scheme = parsed.scheme if re.fullmatch(r"[a-z][a-z0-9+.-]{0,31}", parsed.scheme) else "empty_or_invalid"
    category = "fixture" if value == expected_url else "empty" if not value else (
        "about_blank" if parsed.scheme == "about" and parsed.path == "blank" else
        "browser_internal" if parsed.scheme in ("chrome", "chrome-untrusted", "chrome-error") else
        "data_startup" if parsed.scheme == "data" else "unexpected")
    result = {"url_category": category, "scheme": scheme, "exact_fixture_url": value == expected_url,
              "same_origin": (parsed.scheme, parsed.netloc) == (expected.scheme, expected.netloc),
              "same_path": parsed.path == expected.path, "same_query": parsed.query == expected.query,
              "has_fragment": bool(parsed.fragment), "url_sha256": hashlib.sha256(value.encode()).hexdigest()}
    # Only browser-internal route names are printable; data/external values,
    # query strings, fragments and the owned random HTTP route remain hashes.
    if parsed.scheme == "about" and re.fullmatch(r"[a-zA-Z0-9/_-]{1,80}", parsed.path):
        result["internal_page"] = "about:" + parsed.path
    elif (parsed.scheme == "chrome" and re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", parsed.netloc)
          and re.fullmatch(r"[a-zA-Z0-9/_-]{0,100}", parsed.path)):
        result["internal_page"] = "chrome://" + parsed.netloc + parsed.path
    return result


def page_frame(cdp, session, url):
    frame = cdp.call("Page.getFrameTree", session=session).get("frameTree", {}).get("frame", {})
    frame_id, frame_url = frame.get("id"), frame.get("url")
    require(isinstance(frame_id, str) and frame_id, "owned_page_frame_missing")
    summary = startup_url_summary(frame_url, url)
    summary.update(unreachable_url_present=bool(frame.get("unreachableUrl")),
                   html_mime_type=frame.get("mimeType") == "text/html")
    return frame_id, summary


def private_a11y_address(process):
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        require(process.poll() is None, "private_a11y_launcher_exited")
        present = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner", GLib.Variant("(s)", ("org.a11y.Bus",)),
            GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
        if present:
            owner = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "GetConnectionUnixProcessID", GLib.Variant("(s)", ("org.a11y.Bus",)),
                GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
            require(owner == process.pid, "private_a11y_owner_mismatch")
            address = connection.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus",
                "GetAddress", None, GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
            require(isinstance(address, str) and address.startswith("unix:"), "private_a11y_address_invalid")
            return address
        time.sleep(.1)
    raise LabFailure("private_a11y_start_timeout")


def read_helper(environment, mode="tree"):
    result = subprocess.run([SYSTEM_PYTHON, str(HELPER), "--mode", mode], env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=7)
    require(result.returncode == 0, "native_helper_exit_" + str(result.returncode))
    require(len(result.stdout) <= 1_048_576, "native_helper_output_too_large")
    return json.loads(result.stdout)


def marker_result(tree, stage):
    nodes = tree.get("nodes", [])
    text = "\n".join(str(node.get(key, "")) for node in nodes for key in ("name", "text"))
    readable = stage.get("readable_markers", [])
    absent = stage.get("absent_markers", [])
    missing = [index for index, marker in enumerate(readable) if marker not in text]
    leaked = [index for index, marker in enumerate(absent) if marker in text]
    expected = stage["expected"]["accessibility_only"]
    classification = "readable_task_text" if expected["kind"] == "answer" else (
        "requires_authorized_ocr_or_vision" if expected.get("reason") == "question_only_in_pixels"
        else "insufficient_current_task_context")
    return {"passed": not missing and not leaked, "classification": classification,
            "readable_markers_checked": len(readable), "absent_markers_checked": len(absent),
            "missing_marker_indices": missing, "unexpected_marker_indices": leaked,
            "node_count": len(nodes), "tree_complete": not tree.get("truncated", False),
            "extracted_text_sha256": hashlib.sha256(text.encode()).hexdigest()}


def native_stage(environment, pinned, stage, timeout=9):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        tree = read_helper(environment)
        require(tree.get("schema") == 1 and tree.get("source") == "atspi"
                and tree.get("window_id") == pinned, "native_target_changed")
        last = marker_result(tree, stage)
        if last["passed"]:
            return tree, last
        time.sleep(.15)
    require(last is not None, "native_read_timeout")
    return tree, last


def cli_tree(cli, environment, stage):
    result = subprocess.run([str(cli), "a11y", "--authorized", "--mode", "tree"],
                            env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8)
    require(result.returncode == 0 and len(result.stdout) <= 1_048_576, "native_cli_tree_failed")
    return marker_result(json.loads(result.stdout), stage)


def scoped_default_selection(cdp, session, environment, pinned, stage):
    """One normal-selection read from a bus with only the owned browser."""
    report = {"passed": False, "private_bus_only": True, "pinned_target_used": False,
              "global_gnome_foreground_selection_tested": False, "read_attempts": 0,
              "bring_to_front_requested": False}
    require("SOCKET_STREAM_ATSPI_TARGET" not in environment, "default_selection_must_not_be_pinned")
    cdp.call("Page.bringToFront", session=session)
    report["bring_to_front_requested"] = True
    windows = read_helper(environment, "windows")
    require(len(windows) == 1 and windows[0]["id"] == pinned, "private_selection_scope_changed")
    try:
        report["read_attempts"] = 1
        tree = read_helper(environment)
        require(tree.get("schema") == 1 and tree.get("source") == "atspi"
                and tree.get("window_id") == pinned, "default_selection_owned_window_not_selected")
        report.update(marker_result(tree, stage), selected_owned_window=True)
    except LabFailure as error:
        code = str(error)
        report["failure_code"] = code if re.fullmatch(r"[a-z0-9_-]{1,100}", code) else "default_selection_failed"
    except Exception:
        report["failure_code"] = "default_selection_failed"
    return report


def fixture_case_url(url, case_id):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode({"case": case_id}), ""))


def marker_ids(tree, marker):
    matches = [(len(value), node.get("stable_id")) for node in tree.get("nodes", [])
               for key in ("name", "text") if isinstance(value := node.get(key), str) and marker in value
               and node.get("stable_id")]
    if not matches:
        return set()
    shortest = min(length for length, _identity in matches)
    return {identity for length, identity in matches if length == shortest}


def wait_cli(process, event_type, timeout=15, allow_timeout=False):
    if not hasattr(process, "lab_events"):
        process.lab_events = queue.Queue(maxsize=128)
        process.lab_event_counts = {}
        process.lab_event_lock = threading.Lock()

        def consume():
            try:
                for line in process.stdout:
                    require(len(line) <= 131_072, "native_cli_large_event")
                    event = json.loads(line)
                    kind = event.get("type")
                    counted = kind if kind in ("session_started", "session_stopped", "session_paused", "session_resumed",
                        "question", "question_detected", "screen_read", "answer_started", "answer_delta", "answer_completed",
                        "answer_cancelled", "api_request", "api_response", "token_usage", "error") else "other"
                    with process.lab_event_lock:
                        process.lab_event_counts[counted] = min(100_000, process.lab_event_counts.get(counted, 0) + 1)
                    if kind in ("session_started", "answer_started", "answer_completed", "session_stopped", "error"):
                        process.lab_events.put(kind, timeout=1)
            except Exception:
                try:
                    process.lab_events.put("error", timeout=1)
                except queue.Full:
                    pass
            finally:
                try:
                    process.lab_events.put(None, timeout=1)
                except queue.Full:
                    pass

        threading.Thread(target=consume, daemon=True).start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            kind = process.lab_events.get(timeout=max(.01, deadline - time.monotonic()))
        except queue.Empty:
            if allow_timeout:
                return False
            raise LabFailure("native_cli_event_timeout") from None
        require(kind is not None, "native_cli_closed")
        require(kind != "error", "native_cli_reported_error")
        if kind == event_type:
            return True
    if allow_timeout:
        return False
    raise LabFailure("native_cli_event_timeout")


def initial_read(process, provider):
    # The production Linux helper has a six-second deadline. Observe its first
    # automatic tick for one additional second: short screens may already be
    # answering, while a long outline waits for a second stable observation.
    # Never queue a duplicate manual read behind an observed automatic answer.
    automatic = wait_cli(process, "answer_started", timeout=7, allow_timeout=True)
    with process.lab_event_lock:
        automatic = automatic or process.lab_event_counts.get("answer_started", 0) > 0
    automatic = automatic or len(provider.requests) > 0
    if automatic:
        return {"mode": "automatic_already_started", "explicit_read_screen_commands": 0,
                "startup_observation_limit_seconds": 7, "manual_retry_on_error": False}
    process.stdin.write("read-screen\n")
    process.stdin.flush()
    return {"mode": "explicit_read_screen", "explicit_read_screen_commands": 1,
            "startup_observation_limit_seconds": 7, "manual_retry_on_error": False}


def record_cli_diagnostics(report, process, provider):
    if process is not None and hasattr(process, "lab_event_lock"):
        with process.lab_event_lock:
            report["cli_event_counts"] = dict(process.lab_event_counts)
    if provider is not None:
        report["provider_request_count"] = len(provider.requests)
        if hasattr(provider, "answers"):
            report["completed_relay_answer_count"] = len(provider.answers)


def start_session(cli, environment, *, relay_dir=None, window_id=None, window_title=None):
    if relay_dir is None:
        import test_incremental_screen_e2e as fixtures
        provider = fixtures.Provider()
        port, model, token = provider.server.server_port, "fixture-model", ""
    else:
        from screen_model_relay import FileRelayProvider
        provider = FileRelayProvider(relay_dir, window_id=window_id, window_title=window_title,
                                     private_bus_verified=True)
        port, model, token = provider.port, provider.model, provider.token
    child_env = dict(environment, SOCKET_STREAM_BASE_URL=f"http://127.0.0.1:{port}/v1",
                     SOCKET_STREAM_MODEL=model, SOCKET_STREAM_API_KEY=token)
    try:
        process = subprocess.Popen([str(cli), "observe-live", "--authorized", "--provider", "openai-compatible",
                                    "--interval", "3600", "--a11y-mode", "tree", "--no-ocr-images"],
                                   env=child_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
    except Exception:
        provider.close()
        raise
    return process, provider


def patch_diagnostics(snapshot, patch, before_marker, after_marker, anchor):
    before = {node.get("id"): node for node in snapshot.get("nodes", [])}
    fields = ("id", "parent", "position", "role", "name", "text", "focused", "bounds", "image")
    result = {"updated": [], "added_roles": [], "removed_roles": [], "moved_nodes": len(patch.get("moved", []))}

    def safe_role(node):
        value = node.get("role", "")
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9 _-]{1,64}", value) else "unreported"

    for current in patch.get("updated", [])[:16]:
        previous = before.get(current.get("id"), {})
        old = "\n".join(str(previous.get(key, "")) for key in ("name", "text"))
        new = "\n".join(str(current.get(key, "")) for key in ("name", "text"))
        result["updated"].append({"role": safe_role(current), "previous_node_present": bool(previous),
            "changed_fields": [key for key in fields if previous.get(key) != current.get(key)],
            "previous_question_marker": before_marker in old, "current_question_marker": after_marker in new,
            "previous_anchor_marker": anchor in old, "current_anchor_marker": anchor in new,
            "fixture_url_in_changed_text": "/lab-" in old or "/lab-" in new})
    result["added_roles"] = [safe_role(node) for node in patch.get("added", [])[:16]]
    result["removed_roles"] = [safe_role(before.get(identity, {})) for identity in patch.get("removed", [])[:16]]
    result["details_truncated"] = any(len(patch.get(key, [])) > 16 for key in ("updated", "added", "removed"))
    return result


def normalized_model_node(node):
    return {key: node.get(key, default) for key, default in (
        ("id", None), ("parent", None), ("position", None), ("role", ""),
        ("name", ""), ("text", ""), ("focused", False), ("bounds", None), ("image", False))}


def native_question_projection(tree, window):
    """Independent complete Tree-mode projection of already captured lab data.

    Keep native keys here; bind them to model IDs only after comparing the
    entire first snapshot. This does not call the production differ or read
    another window. Incomplete/budgeted observations cannot prove equality.
    """
    require(isinstance(tree, dict) and tree.get("schema") == 1 and not tree.get("truncated", False)
            and {"source": tree.get("source"), "app": tree.get("app", ""),
                 "pid": tree.get("pid"), "id": tree.get("window_id")} == window,
            "native_projection_window_or_completeness_mismatch")
    raw = tree.get("nodes")
    require(isinstance(raw, list) and 0 < len(raw) <= 4096, "native_projection_nodes_invalid")
    index, children = {}, {}
    for node in raw:
        require(isinstance(node, dict) and type(node.get("id")) is int and node["id"] not in index
                and (node.get("parent") is None or type(node["parent"]) is int)
                and isinstance(node.get("role"), str)
                and all(isinstance(node.get(key, ""), str) for key in ("name", "text"))
                and all(type(node.get(key, False)) is bool for key in ("focused", "image", "protected")),
                "native_projection_node_invalid")
        index[node["id"]] = node
        children.setdefault(node.get("parent"), []).append(node["id"])
    require(all(node.get("parent") is None or node["parent"] in index for node in raw),
            "native_projection_parent_missing")
    stack = list(reversed(children.get(None, [])))
    seen, excluded, ordered = set(), set(), []
    while stack:
        identity = stack.pop()
        require(identity not in seen, "native_projection_cycle")
        seen.add(identity)
        node = index[identity]
        role = node["role"].lower()
        chrome = re.sub(r"[ _-]", "", role) in {"toolbar", "menubar", "menu", "navigation", "banner", "statusbar"}
        if node.get("parent") in excluded or node.get("protected", False) or chrome or "password" in role or "secure" in role:
            excluded.add(identity)
        else:
            node = dict(node)
            if node["role"] in {"application", "window", "frame"}:
                node.update(name="", text="")
            ordered.append(node)
        stack.extend(reversed(children.get(identity, [])))
    require(len(seen) == len(raw), "native_projection_cycle")
    eligible = {node["id"]: node for node in ordered}
    retained = set()
    for node in reversed(ordered):
        if (node["id"] in retained or node.get("parent") is None
                or node["role"].strip().lower() not in {"panel", "filler", "separator", "scroll pane", "unknown"}
                or node.get("name", "") or node.get("text", "") or node.get("focused", False) or node.get("image", False)):
            retained.add(node["id"])
            if node.get("parent") is not None:
                require(node["parent"] in eligible, "native_projection_parent_filtered")
                retained.add(node["parent"])
    projected, stable_keys, sibling_counts = [], set(), {}
    for node in ordered:
        if node["id"] not in retained:
            continue
        stable = node.get("stable_id")
        require(isinstance(stable, str) and stable and len(stable.encode()) <= 1024
                and not any(ord(character) < 32 or 127 <= ord(character) < 160 for character in stable)
                and stable not in stable_keys, "native_projection_identity_invalid")
        stable_keys.add(stable)
        parent = eligible[node["parent"]].get("stable_id") if node.get("parent") is not None else None
        position = sibling_counts.get(parent, 0)
        sibling_counts[parent] = position + 1
        projected.append(normalized_model_node(dict(node, id=stable, parent=parent, position=position)))
    require(0 < len(projected) <= 800, "native_projection_budget_unverified")
    return projected


def verify_native_replay(snapshot, patch, replayed, before_tree, after_tree):
    before = native_question_projection(before_tree, snapshot["window"])
    after = native_question_projection(after_tree, snapshot["window"])
    require(len(before) == len(snapshot["nodes"]), "native_initial_snapshot_size_mismatch")
    identities = {native["id"]: model["id"] for native, model in zip(before, snapshot["nodes"])}

    def model_view(projected):
        require({node["id"] for node in projected} == set(identities), "native_retained_id_set_changed")
        return {identities[node["id"]]: dict(node, id=identities[node["id"]],
                    parent=identities[node["parent"]] if node["parent"] is not None else None)
                for node in projected}

    previous = model_view(before)
    require(previous == {node["id"]: normalized_model_node(node) for node in snapshot["nodes"]},
            "native_initial_snapshot_content_mismatch")
    expected = model_view(after)
    changed = {identity for identity in previous if previous[identity] != expected[identity]}
    require(changed == {node["id"] for node in patch["updated"]}, "native_changed_id_set_mismatch")
    require(expected == {identity: normalized_model_node(node) for identity, node in replayed.items()},
            "native_replayed_snapshot_mismatch")


def check_session(provider, before_marker, after_marker, anchor, *, relay=False, diagnostic_sink=None,
                  native_before=None, native_after=None):
    from screen_model_relay import RelayFailure, validate_question_patch

    require(len(provider.requests) == 2, "native_session_request_count")
    if relay:
        require(len(provider.answers) == 2 and all(provider.answers), "native_relay_answers_incomplete")
    first_answer = provider.answers[0] if relay else "Fixture answer 1."
    first, second = (request["messages"] for request in provider.requests)
    require([message["role"] for message in first] == ["system", "user"]
            and [message["role"] for message in second] == ["system", "user", "assistant", "user"],
            "native_session_history_roles")
    require(second[:2] == first and second[2]["content"] == first_answer, "native_session_history_mismatch")
    snapshot = json.loads(first[-1]["content"].split(MARKER, 1)[1])
    patch = json.loads(second[-1]["content"].split(MARKER, 1)[1])
    require(snapshot["kind"] == "snapshot" and patch["kind"] == "patch", "native_session_not_incremental")
    updated = len(patch.get("updated", []))
    unchanged_omitted = anchor not in second[-1]["content"] and before_marker not in second[-1]["content"]
    try:
        replayed = validate_question_patch(snapshot, patch, before_marker, after_marker, anchor)
        require((native_before is None) == (native_after is None), "native_replay_observation_missing")
        if native_before is not None:
            verify_native_replay(snapshot, patch, replayed, native_before, native_after)
    except (RelayFailure, LabFailure) as error:
        if diagnostic_sink is not None:
            diagnostic_sink["native_patch_diagnostics"] = patch_diagnostics(snapshot, patch, before_marker, after_marker, anchor)
        raise LabFailure(str(error)) from None
    require(before_marker in json.dumps(snapshot, ensure_ascii=False)
            and after_marker in json.dumps(patch, ensure_ascii=False), "native_session_question_missing")
    require(unchanged_omitted, "native_session_repeated_unchanged_text")
    result = {"passed": True,
            "snapshot": True, "patch": True, "updated_nodes": updated,
            "added_nodes": len(patch.get("added", [])), "removed_nodes": len(patch.get("removed", [])),
            "moved_nodes": len(patch.get("moved", [])), "exact_history": True,
            "unchanged_anchor_omitted": True, "completed_cli_answers": 2,
            "native_snapshot_replay_verified": native_before is not None,
            "external_ai_verified_here": False, "model_semantics_tested": False}
    result.update(provider.report() if relay else {"transport": "local_mock", "completed_mock_answers": 2})
    return result


def worker(args, report):
    root = Path(args.private_root)
    require(root.name.startswith("floaty-browser-lab-") and root.stat().st_uid == os.geteuid()
            and root.stat().st_mode & 0o077 == 0, "private_runtime_invalid")
    require(os.environ.get("DBUS_SESSION_BUS_ADDRESS", "").startswith("unix:")
            and not os.environ.get("DISPLAY")
            and Path(os.environ["XDG_RUNTIME_DIR"]).parent == root, "private_worker_environment_invalid")
    oracle = json.loads(ORACLE.read_text(encoding="utf-8"))
    by_id = {case["id"]: case for case in oracle["cases"]}
    selected = (list(by_id) if args.cases == "all" else oracle["harness"]["core_case_ids"]
                if args.cases == "core" else args.cases.split(","))
    require(selected and all(case in by_id for case in selected), "case_selection_invalid")
    require(selected[:2] == ["baseline", "followup"], "native_session_requires_baseline_then_followup")
    require(not args.canvas_ocr or "canvas-bare" in selected, "canvas_ocr_requires_canvas_bare_case")
    validate_ocr_options(args)
    environment = dict(os.environ)
    launcher = chrome = session_process = provider = cdp = server = thread = None
    try:
        report["phase"] = "private_a11y_start"
        launcher = subprocess.Popen([LAUNCHER, "--launch-immediately", "--a11y=1", "--screen-reader=0"],
                                    env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    start_new_session=True)
        OwnedDiagnostics(launcher, root, "atspi")
        environment["AT_SPI_BUS_ADDRESS"] = private_a11y_address(launcher)
        require(read_helper(environment, "windows") == [], "private_bus_not_empty_before_browser")
        server, thread, url = serve_fixture()
        url += "?case=baseline"
        report["phase"] = "host_fixture_http_probe"
        report["host_fixture_http_probe"] = verify_fixture_http(url)
        with server.lab_http_lock:
            host_probe_responses = server.lab_http["fixture_responses_200"]
        report["phase"] = "owned_chrome_start"
        chrome, cdp = spawn_pipe_process(args.chrome, [
            "--ozone-platform=wayland", "--force-renderer-accessibility", "--remote-debugging-pipe",
            "--user-data-dir=" + str(root / "chrome-profile"), "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-extensions",
            "--disable-breakpad", "--disable-crash-reporter", "--window-size=1200,1000",
            "--profile-directory=Default", "--password-store=basic", "--no-startup-window",
        ], environment, root)
        cdp.expected_url = url
        report["phase"] = "owned_browser_version"
        version = cdp.call("Browser.getVersion", timeout=20).get("product", "")
        report["browser_version"] = version if re.fullmatch(r"[A-Za-z]+/[0-9.]+", version) else "unreported"
        report["phase"] = "owned_target_creation"
        pages = [target for target in cdp.call("Target.getTargets").get("targetInfos", []) if target.get("type") == "page"]
        report["owned_startup_targets"] = [startup_url_summary(target.get("url"), url) for target in pages[:4]]
        require(len(pages) <= 4 and all(summary["url_category"] in (
            "empty", "about_blank", "browser_internal", "data_startup",
        ) for summary in report["owned_startup_targets"]), "unexpected_owned_browser_page")
        created = cdp.call("Target.createTarget", {"url": "about:blank", "newWindow": True, "background": False}, timeout=20)
        page = created.get("targetId")
        require(isinstance(page, str) and page and all(target.get("targetId") != page for target in pages),
                "owned_created_target_invalid")
        cdp.target_id = page
        # These IDs came only from this freshly spawned browser's inherited
        # pipe. Closing its startup pages never reaches an existing browser.
        for target in pages:
            require(cdp.call("Target.closeTarget", {"targetId": target["targetId"]}).get("success") is True,
                    "owned_startup_target_close_failed")
        report["owned_target"] = {"explicitly_created": True, "startup_targets_closed": len(pages),
                                  "empty_temporary_profile": True, "dummy_password_store_basic": True}
        report["phase"] = "owned_target_attachment"
        session = cdp.call("Target.attachToTarget", {"targetId": page, "flatten": True})["sessionId"]
        cdp.call("Runtime.enable", session=session)
        cdp.call("Log.enable", session=session)
        cdp.call("Page.enable", session=session)
        cdp.call("Network.enable", session=session)
        cdp.call("Runtime.runIfWaitingForDebugger", session=session)
        initial_frame_id, initial_frame = page_frame(cdp, session, url)
        report["owned_navigation"] = {"initial_frame": initial_frame, "explicit_navigation": True}
        require(initial_frame["url_category"] in ("fixture", "about_blank", "empty", "browser_internal", "data_startup"),
                "unexpected_owned_page_before_navigation")
        report["phase"] = "owned_blank_navigation"
        blank = cdp.call("Page.navigate", {"url": "about:blank"}, session=session)
        require(not blank.get("errorText") and not blank.get("isDownload")
                and blank.get("frameId") == initial_frame_id, "owned_blank_navigation_failed")
        blank_frame_id, blank_frame = page_frame(cdp, session, url)
        report["owned_navigation"]["blank_frame"] = blank_frame
        require(blank_frame_id == initial_frame_id and blank_frame["url_category"] == "about_blank"
                and not blank_frame["has_fragment"], "owned_blank_frame_mismatch")
        report["phase"] = "owned_fixture_navigation"
        navigation = cdp.call("Page.navigate", {"url": url}, session=session)
        require(not navigation.get("errorText") and not navigation.get("isDownload")
                and navigation.get("frameId") == initial_frame_id, "owned_fixture_navigation_failed")
        deadline = time.monotonic() + 10
        ready = False
        metadata = None
        while time.monotonic() < deadline:
            try:
                metadata = page_diagnostics(cdp, session, url, oracle["harness"]["window_title_prefix"])
            except LabFailure as error:
                if str(error) != "owned_page_context_transition":
                    raise
                time.sleep(.1)
                continue
            ready = (metadata.get("expected_location") is True and metadata.get("ready_state") == "complete"
                     and metadata.get("title_matches_fixture") is True
                     and metadata.get("lab_element_present") is True and metadata.get("question_element_present") is True
                     and metadata.get("api_type") == "object" and metadata.get("api_get_state_type") == "function")
            if ready:
                break
            time.sleep(.1)
        final_frame_id, final_frame = page_frame(cdp, session, url)
        report["owned_navigation"].update(final_frame=final_frame, frame_retained=final_frame_id == initial_frame_id)
        if not ready:
            report["owned_page_diagnostics"] = metadata
        require(ready, "fixture_script_not_ready")
        require(final_frame_id == initial_frame_id and final_frame["url_category"] == "fixture"
                and not final_frame["unreachable_url_present"], "owned_fixture_frame_mismatch")
        with server.lab_http_lock:
            require(server.lab_http["fixture_responses_200"] > host_probe_responses, "owned_fixture_http_response_missing")
        report["phase"] = "native_window_selection"
        pinned = pinned_title = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            windows = read_helper(environment, "windows")
            require(all(window["title"].startswith(oracle["harness"]["window_title_prefix"]) for window in windows),
                    "unexpected_private_accessibility_window")
            if len(windows) == 1:
                pinned = windows[0]["id"]
                pinned_title = windows[0]["title"]
                break
            time.sleep(.15)
        require(pinned, "native_fixture_window_not_registered")
        if args.scoped_default_selection:
            report["scoped_default_selection"] = scoped_default_selection(
                cdp, session, environment, pinned, by_id["baseline"]["initial"])
        environment["SOCKET_STREAM_ATSPI_TARGET"] = pinned
        report.update(private_a11y_bus_verified=True, selected_window_pinned=True,
                      native_wayland_browser=True, wayland_evidence="explicit_ozone_wayland_and_native_window_without_DISPLAY")
        baseline = None
        identity = oracle["harness"]["stable_identity_sequence"]
        for case_id in selected:
            case = by_id[case_id]
            cdp.evaluate(session, "window.floatyScreenLab.selectCase(" + json.dumps(case_id) + "); window.floatyScreenLab.getState()")
            stages = [("initial", case["initial"])] + [("step_" + str(index + 1), stage)
                                                            for index, stage in enumerate(case.get("steps") or [])]
            for stage_name, stage in stages:
                report["phase"] = case_id + ":" + stage_name
                action = stage.get("action")
                if action and action["type"] in ("click", "scroll_to_bottom"):
                    selector = json.dumps(action["selector"])
                    operation = "node.click();" if action["type"] == "click" else "node.scrollTop=node.scrollHeight;"
                    cdp.evaluate(session, "(() => {const node=document.querySelector(" + selector + "); if(!node) throw Error('missing fixture control'); " + operation + "return true;})()")
                elif action:
                    require(action["type"] == "wait_ready", "unsupported_fixture_action")
                state = cdp.evaluate(session, "window.floatyScreenLab.getState()")
                if stage.get("ready", True):
                    deadline = time.monotonic() + 5
                    while not state.get("ready") and time.monotonic() < deadline:
                        time.sleep(.05)
                        state = cdp.evaluate(session, "window.floatyScreenLab.getState()")
                    require(state.get("ready"), "fixture_ready_timeout")
                require(state.get("caseId") == case_id and state.get("phase") != "render-unavailable", "fixture_render_failed")
                tree, result = native_stage(environment, pinned, stage)
                result.update(case=case_id, stage=stage_name, oracle_answer_used=False)
                report["cases"].append(result)
                if args.canvas_ocr and case_id == "canvas-bare" and stage_name == "initial":
                    from native_canvas_ocr import check_canvas_ocr
                    report["canvas_ocr"] = check_canvas_ocr(
                        cdp, session, cli=args.cli,
                        environment=canvas_ocr_environment(environment, args.ocr_tool_dir), private_root=root,
                        expected_url=fixture_case_url(url, case_id))
                    report["canvas_ocr"]["explicit_tool_directory"] = args.ocr_tool_dir is not None
                    report["ocr_or_vision_tested"] = report["canvas_ocr"].get("ocr_component_tested", False)
                    report["canvas_pixels_extracted"] = report["canvas_ocr"].get("ocr_invoked", False)
                if case_id in ("baseline", "followup") and stage_name == "initial":
                    result["cli_tree"] = cli_tree(args.cli, environment, stage)
                    if case_id == "baseline":
                        baseline = tree
                        session_process, provider = start_session(args.cli, environment, relay_dir=args.relay_dir,
                                                                  window_id=pinned, window_title=pinned_title)
                        wait_cli(session_process, "session_started")
                        report["initial_read"] = initial_read(session_process, provider)
                        wait_cli(session_process, "answer_completed", timeout=100 if args.relay_dir else 15)
                    else:
                        report["native_identity"] = {
                            "question_retained": bool(marker_ids(baseline, identity["before_question_marker"])
                                                       & marker_ids(tree, identity["after_question_marker"])),
                            "anchor_retained": bool(marker_ids(baseline, identity["anchor_marker"])
                                                     & marker_ids(tree, identity["anchor_marker"])),
                        }
                        session_process.stdin.write("read-screen\n")
                        session_process.stdin.flush()
                        wait_cli(session_process, "answer_completed", timeout=100 if args.relay_dir else 15)
                        report["native_session"] = check_session(provider, identity["before_question_marker"],
                                                                 identity["after_question_marker"], identity["anchor_marker"],
                                                                 relay=args.relay_dir is not None, diagnostic_sink=report,
                                                                 native_before=baseline, native_after=tree)
                        session_process.stdin.write("stop\n")
                        session_process.stdin.flush()
                        wait_cli(session_process, "session_stopped", timeout=5)
                        require(session_process.wait(timeout=5) == 0, "native_session_nonzero_exit")
                        record_cli_diagnostics(report, session_process, provider)
                        stop_owned(session_process)
                        session_process = None
                        provider.close()
                        provider = None
        report["success"] = (all(case["passed"] and case.get("cli_tree", {"passed": True})["passed"] for case in report["cases"])
                             and all(report.get("native_identity", {}).values())
                             and report.get("native_session", {}).get("passed", False)
                             and (not args.scoped_default_selection or report["scoped_default_selection"]["passed"])
                             and (not args.canvas_ocr or report.get("canvas_ocr", {}).get("passed", False)))
    finally:
        record_cli_diagnostics(report, session_process, provider)
        if not report.get("success"):
            if cdp is not None:
                report["last_browser_call"] = cdp.last_method
                report["owned_navigation_events"] = list(cdp.navigation_events)
            if chrome is not None:
                try:
                    report["owned_process_status"] = owned_process_status(chrome, root)
                except Exception:
                    report["owned_process_status"] = {"unavailable": True}
        actions = [("model_cli", lambda: stop_owned(session_process))]
        if provider is not None:
            if args.relay_dir:
                report["relay"] = provider.report()
            actions.append(("loopback_provider", provider.close))
        if cdp is not None:
            try:
                cdp.call("Browser.close", timeout=2)
            except Exception:
                pass
            actions.append(("cdp_pipe", cdp.close))
        actions.extend([("chrome_process", lambda: stop_owned(chrome)),
                        ("atspi_process", lambda: stop_owned(launcher))])
        if server is not None:
            actions.extend([("fixture_server_shutdown", server.shutdown),
                            ("fixture_server_close", server.server_close),
                            ("fixture_server_thread", lambda: thread.join(timeout=3))])
        failures = cleanup_owned(actions)
        if failures:
            report.update(success=False, cleanup_failures=failures)
        if not report.get("success"):
            if server is not None:
                with server.lab_http_lock:
                    report["fixture_http"] = dict(server.lab_http)
            if cdp is not None:
                report["owned_page_events"] = cdp.diagnostics
            report["startup_diagnostics"] = {
                label: {"exit_code": process.poll(), **process.lab_diagnostics.summary()}
                for label, process in (("chrome", chrome), ("atspi", launcher))
                if process is not None and hasattr(process, "lab_diagnostics")}


def self_test(report):
    import copy
    import io
    from types import SimpleNamespace
    from unittest.mock import patch

    oracle = json.loads(ORACLE.read_text(encoding="utf-8"))
    require(oracle["schema_version"] == 1 and oracle["fixture"] == HTML.name, "oracle_contract_invalid")
    by_id = {case["id"]: case for case in oracle["cases"]}
    require(all(case in by_id for case in oracle["harness"]["core_case_ids"]), "oracle_core_missing")
    for case_id in ("baseline", "canvas-accessible", "canvas-bare", "hidden", "password"):
        stage = by_id[case_id]["initial"]
        tree = {"nodes": [{"text": "\n".join(stage["readable_markers"])}]}
        require(marker_result(tree, stage)["passed"], "self_test_marker_match_failed")
        if stage["absent_markers"]:
            tree["nodes"].append({"name": stage["absent_markers"][0]})
            require(not marker_result(tree, stage)["passed"], "self_test_forbidden_marker_failed")
    require(marker_result({"nodes": [{"text": "\n".join(by_id["canvas-bare"]["initial"]["readable_markers"])}]},
                          by_id["canvas-bare"]["initial"])["classification"] == "requires_authorized_ocr_or_vision",
            "self_test_canvas_classification_failed")
    expected_url = "http://127.0.0.1:12345/lab-owned/screen-reading-lab.html?case=baseline"
    for value, category in (("about:blank#blocked", "about_blank"), ("chrome://newtab/", "browser_internal"),
                            ("data:text/html,SYNTHETIC-URL-CONTENT", "data_startup"),
                            ("file:///SYNTHETIC-URL-CONTENT", "unexpected"),
                            ("https://synthetic.invalid/SYNTHETIC-URL-CONTENT", "unexpected")):
        summary = startup_url_summary(value, expected_url)
        require(summary["url_category"] == category and "SYNTHETIC-URL-CONTENT" not in json.dumps(summary)
                and "synthetic.invalid" not in json.dumps(summary), "self_test_startup_url_guard_failed")
    require(startup_url_summary(expected_url, expected_url)["exact_fixture_url"], "self_test_exact_fixture_url_failed")
    for event_seen, started_count, request_count in ((True, 0, 0), (False, 1, 0), (False, 0, 1), (False, 0, 0)):
        process = SimpleNamespace(stdin=io.StringIO(), lab_event_lock=threading.Lock(),
                                  lab_event_counts={"answer_started": started_count})
        provider = SimpleNamespace(requests=[{}] * request_count)
        with patch(__name__ + ".wait_cli", return_value=event_seen) as wait:
            result = initial_read(process, provider)
            automatic = event_seen or started_count or request_count
            require(process.stdin.getvalue() == ("" if automatic else "read-screen\n")
                    and result["explicit_read_screen_commands"] == (0 if automatic else 1)
                    and wait.call_args.kwargs == {"timeout": 7, "allow_timeout": True},
                    "self_test_initial_read_duplicate_guard_failed")
    process.stdin = io.StringIO()
    with patch(__name__ + ".wait_cli", side_effect=LabFailure("native_cli_reported_error")):
        try:
            initial_read(process, provider)
        except LabFailure:
            require(process.stdin.getvalue() == "", "self_test_initial_read_error_retried")
        else:
            raise LabFailure("self_test_initial_read_error_ignored")
    details = patch_diagnostics({"nodes": [
        {"id": 1, "role": "paragraph", "text": "SYNTHETIC-OLD-QUESTION", "bounds": [0, 0, 30, 20]},
        {"id": 2, "role": "entry", "text": "127.0.0.1:12345/lab-owned/?case=baseline"}]}, {
        "updated": [{"id": 1, "role": "paragraph", "text": "SYNTHETIC-NEW-QUESTION", "bounds": [0, 0, 60, 20]},
                    {"id": 2, "role": "entry", "text": "127.0.0.1:12345/lab-owned/?case=followup"}],
        "added": [], "removed": [], "moved": []}, "SYNTHETIC-OLD-QUESTION", "SYNTHETIC-NEW-QUESTION", "SYNTHETIC-ANCHOR")
    require(details["updated"][0]["changed_fields"] == ["text", "bounds"]
            and details["updated"][1]["fixture_url_in_changed_text"]
            and "SYNTHETIC" not in json.dumps(details) and "127.0.0.1" not in json.dumps(details),
            "self_test_patch_metadata_scope_failed")
    server, server_thread, served_url = serve_fixture()
    try:
        require(verify_fixture_http(served_url)["exact_fixture_body_sha256_verified"], "self_test_host_fixture_probe_failed")
        with server.lab_http_lock:
            require(server.lab_http["fixture_responses_200"] == 1, "self_test_host_probe_count_failed")
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)
    with tempfile.TemporaryDirectory(prefix="floaty-browser-pipe-self-test-") as temporary:
        root = Path(temporary)
        environment = isolated_environment(root, Path("/synthetic/wayland-socket"))
        forbidden = ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS", "SOCKET_STREAM_API_KEY", "HTTP_PROXY", "HTTPS_PROXY")
        require(not any(key in environment for key in forbidden), "self_test_environment_failed")
        tool_dir = root / "owned-ocr-bin"
        tool_dir.mkdir(mode=0o700)
        tesseract = tool_dir / "tesseract"
        tesseract.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tesseract.chmod(0o700)
        original_environment = dict(environment)
        selected_environment = canvas_ocr_environment(environment, tool_dir)
        require(selected_environment["PATH"] == str(tool_dir) + os.pathsep + environment["PATH"]
                and shutil.which("tesseract", path=selected_environment["PATH"]) == str(tesseract)
                and environment == original_environment
                and set(selected_environment) == set(environment)
                and all(selected_environment[key] == value for key, value in environment.items() if key != "PATH")
                and "LD_LIBRARY_PATH" not in selected_environment,
                "self_test_ocr_environment_scope_failed")
        require(canvas_ocr_environment(environment, None) == environment
                and validate_ocr_options(SimpleNamespace(canvas_ocr=True, ocr_tool_dir=tool_dir)) == tool_dir,
                "self_test_ocr_default_or_option_failed")

        def rejected_ocr(action):
            try:
                action()
            except LabFailure:
                return
            raise LabFailure("self_test_ocr_tool_guard_failed")

        rejected_ocr(lambda: validate_ocr_options(SimpleNamespace(canvas_ocr=False, ocr_tool_dir=tool_dir)))
        for invalid in (Path("relative/bin"), root / "invalid:bin", root / "invalid\nbin", root / "missing-bin"):
            rejected_ocr(lambda: canvas_ocr_environment(environment, invalid))
        linked = root / "linked-ocr-bin"
        linked.symlink_to(tool_dir, target_is_directory=True)
        rejected_ocr(lambda: canvas_ocr_environment(environment, linked))
        tool_dir.chmod(0o770)
        rejected_ocr(lambda: canvas_ocr_environment(environment, tool_dir))
        tool_dir.chmod(0o700)
        for mode in (0o600, 0o720, 0o702):
            tesseract.chmod(mode)
            rejected_ocr(lambda: canvas_ocr_environment(environment, tool_dir))
        tesseract.chmod(0o700)
        with patch(__name__ + ".os.geteuid", return_value=os.geteuid() + 1):
            rejected_ocr(lambda: canvas_ocr_environment(environment, tool_dir))
        with patch(__name__ + ".os.stat", return_value=SimpleNamespace(
                st_mode=tesseract.stat().st_mode, st_uid=os.geteuid() + 1)):
            rejected_ocr(lambda: canvas_ocr_environment(environment, tool_dir))
        real_tesseract = root / "other-synthetic-tesseract"
        tesseract.rename(real_tesseract)
        tesseract.symlink_to(real_tesseract)
        rejected_ocr(lambda: canvas_ocr_environment(environment, tool_dir))
        tesseract.unlink()
        os.mkfifo(tesseract, mode=0o700)
        rejected_ocr(lambda: canvas_ocr_environment(environment, tool_dir))
        tesseract.unlink()
        # A previously valid override becoming unavailable must not select a
        # system/fallback Tesseract, even when one is available later on PATH.
        with patch(__name__ + ".shutil.which", return_value="/synthetic/fallback/tesseract"):
            rejected_ocr(lambda: canvas_ocr_environment(environment, tool_dir))
        real_tesseract.rename(tesseract)
        with patch(__name__ + ".shutil.which", return_value="/synthetic/fallback/tesseract"):
            rejected_ocr(lambda: canvas_ocr_environment(environment, tool_dir))
        echo = root / "echo-pipe.py"
        echo.write_text("import json,os\nos.write(2,b'Authorization: Bearer SYNTHETIC-SECRET\\nhttps://synthetic.invalid/private\\n'+b'x'*20000)\nb=b''\nwhile b'\\0' not in b: b+=os.read(3,4096)\nm=json.loads(b.split(b'\\0')[0])\nos.write(4,json.dumps({'id':m['id'],'result':{'owned_pipe':True}}).encode()+b'\\0')\n", encoding="utf-8")
        process, pipe = spawn_pipe_process(SYSTEM_PYTHON, [str(echo)], environment, root)
        try:
            require(pipe.call("Synthetic.ping")["owned_pipe"] is True, "self_test_cdp_pipe_failed")
            require(process.wait(timeout=3) == 0, "self_test_pipe_process_failed")
        finally:
            pipe.close()
            stop_owned(process)
        diagnostics = process.lab_diagnostics
        summary = diagnostics.summary()
        require(diagnostics.path.stat().st_size == diagnostics.LIMIT
                and diagnostics.path.stat().st_mode & 0o077 == 0, "self_test_diagnostic_limit_failed")
        require(summary["truncated"] and "SYNTHETIC-SECRET" not in json.dumps(summary)
                and "synthetic.invalid" not in json.dumps(summary), "self_test_diagnostic_redaction_failed")
        observer = PipeCDP(-1, -1)
        observer.expected_url = expected_url
        observer.observe_event({"method": "Network.requestWillBeSent", "params": {
            "type": "Document", "request": {"url": expected_url, "headers": {"Authorization": "SYNTHETIC-HEADER-SECRET"}}}})
        observer.observe_event({"method": "Network.responseReceived", "params": {
            "type": "Document", "response": {"url": expected_url, "status": 200}}})
        observer.observe_event({"method": "Network.requestWillBeSent", "params": {
            "type": "Other", "request": {"url": "https://synthetic.invalid/SYNTHETIC-BACKGROUND"}}})
        require(len(observer.navigation_events) == 2 and observer.navigation_events[1]["status"] == 200
                and "SYNTHETIC" not in json.dumps(observer.navigation_events), "self_test_navigation_event_scope_failed")
        for _ in range(20):
            observer.observe_event({"method": "Runtime.exceptionThrown", "params": {"exceptionDetails": {
                "lineNumber": 8, "columnNumber": 2, "exception": {"className": "ReferenceError",
                "description": "Authorization: Bearer SYNTHETIC-CDP-SECRET https://synthetic.invalid/private"}}}})
        require(len(observer.diagnostics) == 12
                and "SYNTHETIC-CDP-SECRET" not in json.dumps(observer.diagnostics)
                and "synthetic.invalid" not in json.dumps(observer.diagnostics), "self_test_cdp_diagnostic_redaction_failed")
        visited = []

        def fail_cleanup():
            visited.append("first")
            raise RuntimeError("synthetic teardown failure")

        require(cleanup_owned([("first", fail_cleanup), ("second", lambda: visited.append("second"))]) == ["first"]
                and visited == ["first", "second"], "self_test_independent_cleanup_failed")
        class SelectionCDP:
            def call(self, method, *, session):
                require(method == "Page.bringToFront" and session == "owned-session",
                        "self_test_default_selection_target_failed")

        selection_tree = {"schema": 1, "source": "atspi", "window_id": "owned-window", "nodes": [
            {"text": "\n".join(by_id["baseline"]["initial"]["readable_markers"])}]}
        with patch(__name__ + ".read_helper", side_effect=[[{"id": "owned-window"}], selection_tree]) as read:
            selection = scoped_default_selection(SelectionCDP(), "owned-session", environment,
                                                 "owned-window", by_id["baseline"]["initial"])
            require(selection["passed"] and selection["read_attempts"] == 1 and read.call_count == 2
                    and not selection["pinned_target_used"]
                    and not selection["global_gnome_foreground_selection_tested"], "self_test_default_selection_scope_failed")
        with patch(__name__ + ".read_helper", return_value=[{"id": "owned-window"}, {"id": "unexpected-window"}]) as read:
            try:
                scoped_default_selection(SelectionCDP(), "owned-session", environment,
                                         "owned-window", by_id["baseline"]["initial"])
            except LabFailure as error:
                require(str(error) == "private_selection_scope_changed" and read.call_count == 1,
                        "self_test_default_selection_refusal_failed")
            else:
                raise LabFailure("self_test_default_selection_extra_window_accepted")
        require(fixture_case_url("http://127.0.0.1:12345/lab-owned/screen-reading-lab.html?case=baseline", "canvas-bare")
                == "http://127.0.0.1:12345/lab-owned/screen-reading-lab.html?case=canvas-bare",
                "self_test_canvas_case_url_failed")
        # The optional wiring is tested without starting a native browser,
        # provider listener, reading credentials, or making external requests.
        class FakeRelay:
            port, model, token = 43210, "gpt-6-luna", "synthetic-loopback-bearer"
            answers = ["Synthetic completed first answer.", "Synthetic completed second answer."]

            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            def report(self):
                return {"transport": "owned_file_relay", "completed_relay_answers": 2,
                        "upstream_inference_verified_here": False, "requires_root_relay_report": True}

        relay = FakeRelay()
        with patch("screen_model_relay.FileRelayProvider", return_value=relay) as make_relay:
            with patch("subprocess.Popen") as popen:
                start_session(Path("/synthetic/verified-cli"), environment, relay_dir=root,
                              window_id='[":1.2","/synthetic/window"]', window_title="Floaty Generated Screen Lab")
                forwarded = popen.call_args.kwargs["env"]
                require(forwarded["SOCKET_STREAM_API_KEY"] == relay.token
                        and forwarded["SOCKET_STREAM_MODEL"] == "gpt-6-luna"
                        and forwarded["SOCKET_STREAM_BASE_URL"] == "http://127.0.0.1:43210/v1"
                        and make_relay.call_args.kwargs["private_bus_verified"] is True,
                        "self_test_relay_wiring_failed")
            with patch("subprocess.Popen", side_effect=OSError("synthetic spawn failure")):
                try:
                    start_session(Path("/synthetic/verified-cli"), environment, relay_dir=root,
                                  window_id='[":1.2","/synthetic/window"]', window_title="Floaty Generated Screen Lab")
                except OSError:
                    pass
                require(relay.closed, "self_test_relay_spawn_cleanup_failed")
        before, after, anchor = "Synthetic previous question", "Synthetic updated question", "Synthetic unchanged hint"
        window = {"source": "atspi", "app": "Synthetic browser", "pid": 1234, "id": "synthetic-window"}
        snapshot = {"kind": "snapshot", "schema": 1, "revision": 1, "complete": True,
                    "reason": "initial", "window": window, "nodes": [
                        {"id": 1, "parent": None, "position": 0, "role": "frame"},
                        {"id": 2, "parent": 1, "position": 0, "role": "document web", "name": "Generated page"},
                        {"id": 3, "parent": 2, "position": 0, "role": "paragraph", "text": before},
                        {"id": 4, "parent": 3, "position": 0, "role": "static", "name": before,
                         "text": before, "bounds": [0, 0, 100, 20]},
                        {"id": 5, "parent": 2, "position": 1, "role": "paragraph", "text": anchor},
                    ]}
        delta = {"kind": "patch", "schema": 1, "base_revision": 1, "revision": 2, "window": window,
                 "updated": [dict(snapshot["nodes"][2], text=after),
                             dict(snapshot["nodes"][3], name=after, text=after, bounds=[0, 0, 200, 40])],
                 "added": [], "moved": [], "removed": []}
        initial = [{"role": "system", "content": "Synthetic system"}, {"role": "user", "content": MARKER
                   + json.dumps(snapshot)}]
        current = initial + [{"role": "assistant", "content": relay.answers[0]}, {"role": "user", "content": MARKER
                             + json.dumps(delta)}]
        relay.requests = [{"messages": initial}, {"messages": current}]
        native_before = {"schema": 1, "source": window["source"], "app": window["app"],
                         "pid": window["pid"], "window_id": window["id"], "truncated": False, "nodes": []}
        for node in snapshot["nodes"]:
            native = dict(node, id=node["id"] + 100, stable_id="native-" + str(node["id"]),
                          parent=node["parent"] + 100 if node["parent"] is not None else None)
            native.pop("position")
            native_before["nodes"].append(native)
        native_before["nodes"][0]["name"] = "Synthetic window title cleared by question scope"
        native_before["nodes"][1:1] = [
            {"id": 800, "parent": 101, "role": "toolbar", "stable_id": "native-toolbar"},
            {"id": 801, "parent": 800, "role": "entry", "stable_id": "native-address", "text": "Generated URL before"},
            {"id": 802, "parent": 101, "role": "filler", "stable_id": "native-empty"},
            {"id": 803, "parent": 101, "role": "password text", "stable_id": "native-protected", "protected": True},
            {"id": 804, "parent": 803, "role": "static", "stable_id": "native-private-child", "text": "Generated protected sentinel"},
        ]
        native_after = copy.deepcopy(native_before)
        replacements = {"native-" + str(node["id"]): node for node in delta["updated"]}
        for node in native_after["nodes"]:
            if node["stable_id"] in replacements:
                replacement = replacements[node["stable_id"]]
                node.update({key: replacement[key] for key in ("name", "text", "bounds") if key in replacement})
            if node["stable_id"] == "native-address":
                node["text"] = "Generated URL after"
            # Traversal numbers may change; native keys must remain decisive.
            node["id"] += 1000
            if node["parent"] is not None:
                node["parent"] += 1000
        result = check_session(relay, before, after, anchor, relay=True,
                               native_before=native_before, native_after=native_after)
        require(result["passed"] and result["requires_root_relay_report"]
                and result["updated_nodes"] == 2 and result["native_snapshot_replay_verified"]
                and not result["upstream_inference_verified_here"], "self_test_relay_result_scope_failed")

        def rejected_native(action):
            try:
                action()
            except LabFailure:
                return
            raise LabFailure("self_test_native_replay_false_positive")

        # The old exact-one assertion accepted this stale-child counterexample.
        current[-1]["content"] = MARKER + json.dumps(dict(delta, updated=delta["updated"][:1]))
        rejected_native(lambda: check_session(relay, before, after, anchor, relay=True))
        current[-1]["content"] = MARKER + json.dumps(delta)
        for mutate in (
            lambda tree: tree.update(truncated=True),
            lambda tree: tree["nodes"][-1].update(text="Unexpected changed anchor"),
            lambda tree: tree["nodes"][-2].update(text=before),
            lambda tree: tree["nodes"][-2].update(bounds=[0, 0, 900, 40]),
            lambda tree: tree["nodes"][-2].update(stable_id="different-native-child"),
            lambda tree: tree["nodes"][-2].update(stable_id=tree["nodes"][-3]["stable_id"]),
            lambda tree: tree["nodes"][-2].update(parent=999999),
            lambda tree: tree["nodes"][0].update(parent=tree["nodes"][-2]["id"]),
        ):
            invalid = copy.deepcopy(native_after)
            mutate(invalid)
            rejected_native(lambda: check_session(relay, before, after, anchor, relay=True,
                                                   native_before=native_before, native_after=invalid))
        current[2]["content"] = "Unacknowledged synthetic answer"
        try:
            check_session(relay, before, after, anchor, relay=True)
        except LabFailure as error:
            require(str(error) == "native_session_history_mismatch", "self_test_relay_history_error_failed")
        else:
            raise LabFailure("self_test_relay_history_accepted")
    report.update(success=True, self_test=True, native_wayland_browser=False,
                  checks=["oracle_contract", "marker_presence_absence", "bare_canvas_needs_ocr", "environment_allowlist",
                          "owned_startup_url_classification_and_redaction",
                          "owned_http_body_hash_and_probe_count", "fixture_only_network_event_metadata",
                          "initial_read_duplicate_and_error_guards", "safe_native_patch_diagnostics",
                          "explicit_ocr_directory_trust_guards", "ocr_only_path_scope_and_no_fallback",
                          "owned_fd3_fd4_cdp_pipe", "bounded_redacted_owned_diagnostics", "independent_cleanup",
                          "bounded_redacted_owned_page_errors",
                          "private_default_selection_and_scope_refusal", "exact_owned_canvas_case_url",
                          "relay_loopback_wiring", "relay_spawn_cleanup", "relay_exact_acknowledged_history",
                          "native_alias_patch_and_stale_child_rejection", "independent_full_native_snapshot_replay"])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chrome", type=Path)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--display-socket", type=Path)
    parser.add_argument("--relay-dir", type=Path, help="optional pre-created private file-relay directory; no real key on this host")
    parser.add_argument("--scoped-default-selection", action="store_true", help="test default selection on only the private generated-browser bus")
    parser.add_argument("--canvas-ocr", action="store_true", help="test production file OCR of only the owned bare-canvas PNG")
    parser.add_argument("--ocr-tool-dir", type=Path,
                        help="with --canvas-ocr only: owned, not group/world-writable directory containing regular executable tesseract")
    parser.add_argument("--cases", default="core", help="core, all, or comma-separated IDs starting baseline,followup")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--private-root", help=argparse.SUPPRESS)
    args = parser.parse_args()
    report = {"schema": 1, "test": "native_browser_generated_screen_lab", "success": False,
              "external_ai": False, "only_generated_html": True, "screenshots_taken": False,
              "ocr_or_vision_tested": False, "global_foreground_selection_tested": False,
              "portal_permissions_tested": False, "chrome_sandbox_disabled": False, "cases": []}
    try:
        require(sys.platform.startswith("linux"), "linux_required")
        if args.self_test:
            require(args.relay_dir is None, "self_test_must_not_receive_relay_directory")
            require(args.ocr_tool_dir is None, "self_test_must_not_receive_ocr_tool_directory")
            self_test(report)
        else:
            validate_ocr_options(args)
            require(os.geteuid() != 0, "run_as_logged_in_nonroot_user")
            require(args.chrome and args.chrome.is_absolute() and args.chrome.is_file()
                    and os.access(args.chrome, os.X_OK), "chrome_binary_invalid")
            with args.chrome.open("rb") as source:
                require(source.read(4) == b"\x7fELF", "use_chrome_elf_not_mime_wrapper")
            require(args.cli and args.cli.is_absolute() and args.cli.is_file()
                    and os.access(args.cli, os.X_OK), "cli_binary_invalid")
            require(args.display_socket and args.display_socket.is_absolute()
                    and stat.S_ISSOCK(args.display_socket.stat().st_mode)
                    and args.display_socket.stat().st_uid == os.geteuid(), "owned_wayland_socket_required")
            if args.relay_dir is not None:
                from screen_model_relay import RelayDirectory
                directory = RelayDirectory(args.relay_dir)
                directory.close()
                report.update(external_ai=None, provider_transport="owned_file_relay",
                              local_external_requests=0, upstream_inference_verified_here=False,
                              requires_root_relay_report=True)
            report.update(uid=os.geteuid(), fixture_sha256=sha256(HTML), oracle_sha256=sha256(ORACLE),
                          cli_sha256=sha256(args.cli))
            if args.worker:
                worker(args, report)
            else:
                require(shutil.which("dbus-run-session") and Path(LAUNCHER).is_file(), "native_dependency_missing")
                with tempfile.TemporaryDirectory(prefix="floaty-browser-lab-") as temporary:
                    root = Path(temporary)
                    root.chmod(0o700)
                    for name in ("runtime", "config", "data", "cache", "tmp"):
                        (root / name).mkdir(mode=0o700)
                    environment = isolated_environment(root, args.display_socket)
                    arguments = [shutil.which("dbus-run-session"), "--", SYSTEM_PYTHON,
                        str(Path(__file__).resolve()), "--worker", "--private-root", str(root),
                        "--chrome", str(args.chrome), "--cli", str(args.cli),
                        "--display-socket", str(args.display_socket), "--cases", args.cases]
                    if args.relay_dir is not None:
                        arguments.extend(["--relay-dir", str(args.relay_dir)])
                    if args.scoped_default_selection:
                        arguments.append("--scoped-default-selection")
                    if args.canvas_ocr:
                        arguments.append("--canvas-ocr")
                    if args.ocr_tool_dir is not None:
                        arguments.extend(["--ocr-tool-dir", str(args.ocr_tool_dir)])
                    process = subprocess.Popen(arguments,
                        env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        text=True, start_new_session=True)
                    try:
                        worker_timeout = (300 if args.cases == "core" else 600) + (180 if args.relay_dir else 0)
                        output, _ = process.communicate(timeout=worker_timeout)
                        require(len(output) <= 262_144, "worker_report_too_large")
                        report = json.loads(output)
                    finally:
                        stop_owned(process, grace=20)
    except LabFailure as error:
        report["success"] = False
        code = str(error)
        report["failure_code"] = code if re.fullmatch(r"[a-z0-9_-]{1,100}", code) else "native_check_failed"
    except KeyboardInterrupt:
        report["success"] = False
        report["failure_code"] = "cancelled"
    except Exception as error:
        report["success"] = False
        report["failure_code"] = "native_harness_failed"
        report["failure_type"] = type(error).__name__
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("success") else 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signum, _frame: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
