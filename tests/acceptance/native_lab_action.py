#!/usr/bin/env python3
"""Opt-in native Next-button action for the owned generated screen lab.

Called only by native_browser_screen_lab with its existing private D-Bus and
AT-SPI bus, owned launcher PID, and exact pinned window ID. This helper never
uses coordinates, a global desktop bus, screenshots, OCR, or an AI provider.
It reads button labels only inside the verified generated window and invokes
one explicitly named AT-SPI Action. The caller verifies the resulting case.

  /usr/bin/python3 tests/acceptance/native_lab_action.py --self-test
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile


TITLE_PREFIX = "Floaty Generated Screen Lab"
BUTTON_LABEL = "Next"
SYSTEM_PYTHON = "/usr/bin/python3"
MAX_NODES, MAX_DEPTH, MAX_CHILDREN = 1200, 32, 128
ACTION_NAMES = frozenset(("click", "press", "activate"))


class ActionFailure(Exception):
    pass


class ActionDeadline(BaseException):
    pass


def require(condition, code):
    if not condition:
        raise ActionFailure(code)


def empty_report():
    return {"schema": 1, "test": "owned_generated_next_action", "passed": False,
            "private_bus_only": True, "action_attempted": False,
            "action_returned_success": False, "coordinate_fallback": False,
            "desktop_capture": False, "external_ai": False, "oracle_answers_used": False}


def parse_identity(value):
    require(isinstance(value, str) and len(value) <= 4096, "native_action_window_id_invalid")
    try:
        parts = json.loads(value)
    except (ValueError, TypeError):
        raise ActionFailure("native_action_window_id_invalid") from None
    require(isinstance(parts, list) and len(parts) == 2
            and isinstance(parts[0], str) and re.fullmatch(r":[0-9]+\.[0-9]+", parts[0])
            and isinstance(parts[1], str) and re.fullmatch(r"/(?:[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*)?", parts[1]),
            "native_action_window_id_invalid")
    return tuple(parts)


def identity(node):
    try:
        bus, path = node.app.bus_name, node.path
    except Exception:
        raise ActionFailure("native_action_identity_unavailable") from None
    require(isinstance(bus, str) and isinstance(path, str), "native_action_identity_unavailable")
    return parse_identity(json.dumps([bus, path], separators=(",", ":")))


def private_environment(environment, private_root, window_id, launcher_pid):
    """Refuse the ordinary /run/user runtime and drop unrelated credentials."""
    root = Path(private_root)
    require(root.is_absolute() and root.name.startswith("floaty-browser-lab-"), "native_action_runtime_invalid")
    for path in (root, root / "runtime"):
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                and not info.st_mode & 0o077, "native_action_runtime_invalid")
    require(type(launcher_pid) is int and launcher_pid > 0, "native_action_launcher_invalid")
    parse_identity(window_id)
    require(environment.get("XDG_RUNTIME_DIR") == str(root / "runtime")
            and environment.get("SOCKET_STREAM_ATSPI_TARGET") == window_id
            and not environment.get("DISPLAY"), "native_action_scope_invalid")
    session = environment.get("DBUS_SESSION_BUS_ADDRESS")
    accessibility = environment.get("AT_SPI_BUS_ADDRESS")
    require(isinstance(session, str) and session.startswith("unix:") and len(session) <= 4096
            and isinstance(accessibility, str) and accessibility.startswith("unix:") and len(accessibility) <= 4096
            and session != accessibility, "native_action_private_bus_required")
    return {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "XDG_RUNTIME_DIR": str(root / "runtime"), "XDG_SESSION_TYPE": "wayland",
            "DBUS_SESSION_BUS_ADDRESS": session, "AT_SPI_BUS_ADDRESS": accessibility,
            "SOCKET_STREAM_ATSPI_TARGET": window_id, "PYTHONNOUSERSITE": "1",
            "GSETTINGS_BACKEND": "memory"}


def verify_bus_owner(environment, launcher_pid, Gio, GLib):
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    owner = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "GetConnectionUnixProcessID", GLib.Variant("(s)", ("org.a11y.Bus",)),
        GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
    require(owner == launcher_pid, "native_action_bus_owner_mismatch")
    address = connection.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress", None,
        GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
    require(address == environment["AT_SPI_BUS_ADDRESS"], "native_action_bus_address_mismatch")


def children(node):
    count = node.get_child_count()
    require(type(count) is int and 0 <= count <= MAX_CHILDREN, "native_action_children_incomplete")
    result = [node.get_child_at_index(index) for index in range(count)]
    require(all(child is not None for child in result), "native_action_children_incomplete")
    return result


def state_set(node, Atspi):
    states = node.get_state_set()
    require(states is not None, "native_action_state_unavailable")
    return states


def has_state(states, Atspi, name):
    value = getattr(Atspi.StateType, name, None)
    return value is not None and bool(states.contains(value))


def protected(role, states, Atspi):
    return role == getattr(Atspi.Role, "PASSWORD_TEXT", object()) or has_state(states, Atspi, "PROTECTED")


def owned_window(desktop, expected, Atspi):
    """Inventory only: never read descendant labels before unique scope holds."""
    windows = []
    window_roles = {Atspi.Role.FRAME, Atspi.Role.WINDOW, Atspi.Role.DIALOG}
    for application in children(desktop):
        require(application.get_role() == Atspi.Role.APPLICATION, "native_action_inventory_unrecognized")
        require(not has_state(state_set(application, Atspi), Atspi, "DEFUNCT"), "native_action_inventory_incomplete")
        for window in children(application):
            role = window.get_role()
            states = state_set(window, Atspi)
            require(role in window_roles and not protected(role, states, Atspi)
                    and not has_state(states, Atspi, "DEFUNCT"), "native_action_inventory_incomplete")
            windows.append(window)
    require(len(windows) == 1 and identity(windows[0]) == expected, "native_action_window_scope_changed")
    title = windows[0].get_name()
    require(isinstance(title, str) and title.startswith(TITLE_PREFIX), "native_action_window_title_mismatch")
    return windows[0]


def find_next(window, expected, Atspi):
    candidates, visited = [], set()
    button_roles = {getattr(Atspi.Role, name, None) for name in ("PUSH_BUTTON", "BUTTON")} - {None}

    def visit(node, depth):
        key = identity(node)
        if key in visited:
            return
        require(depth <= MAX_DEPTH and len(visited) < MAX_NODES, "native_action_tree_incomplete")
        visited.add(key)
        role, states = node.get_role(), state_set(node, Atspi)
        require(role is not None, "native_action_role_unavailable")
        if protected(role, states, Atspi) or has_state(states, Atspi, "DEFUNCT"):
            return
        if role in button_roles:
            name = node.get_name()
            require(isinstance(name, str), "native_action_name_unavailable")
            if (name == BUTTON_LABEL and has_state(states, Atspi, "ENABLED")
                    and has_state(states, Atspi, "SENSITIVE")):
                require(key[0] == expected[0], "native_action_button_scope_changed")
                candidates.append(node)
        for child in children(node):
            visit(child, depth + 1)

    visit(window, 0)
    require(len(candidates) == 1, "native_action_next_not_unique_enabled")
    return candidates[0], len(visited)


def invoke_next(desktop, window_id, Atspi, recheck_bus, report):
    expected = parse_identity(window_id)
    button, visited = find_next(owned_window(desktop, expected, Atspi), expected, Atspi)
    selected_id = identity(button)
    # Complete scope and uniqueness checks again immediately before mutation.
    recheck_bus()
    current, _ = find_next(owned_window(desktop, expected, Atspi), expected, Atspi)
    require(identity(current) == selected_id, "native_action_button_changed")
    iface = Atspi.Accessible.get_action_iface(current)
    require(iface is not None, "native_action_interface_unavailable")
    count = Atspi.Action.get_n_actions(iface)
    require(type(count) is int and 0 < count <= 8, "native_action_list_invalid")
    # GI may return an Accessible proxy for the Action interface. Call the
    # explicit interface methods; Accessible.get_name() has a different ABI.
    get_name = getattr(Atspi.Action, "get_action_name", None) or getattr(Atspi.Action, "get_name", None)
    require(callable(get_name), "native_action_names_unavailable")
    matches = []
    for index in range(count):
        name = get_name(iface, index)
        require(isinstance(name, str), "native_action_name_unavailable")
        if name.casefold() in ACTION_NAMES:
            matches.append(index)
    require(len(matches) == 1, "native_action_activation_ambiguous")
    report.update(action_attempted=True, visited_nodes=visited,
                  button_id_sha256=hashlib.sha256(json.dumps(selected_id).encode()).hexdigest())
    accepted = Atspi.Action.do_action(iface, matches[0])
    require(accepted is True, "native_action_not_accepted")
    report.update(passed=True, action_returned_success=True)


def execute(window_id, private_root, launcher_pid):
    report = empty_report()
    previous = None
    try:
        require(sys.platform.startswith("linux") and os.geteuid() != 0, "native_action_nonroot_linux_required")
        environment = private_environment(os.environ, private_root, window_id, launcher_pid)
        import gi
        gi.require_version("Atspi", "2.0")
        gi.require_version("Gio", "2.0")
        from gi.repository import Atspi, Gio, GLib

        def deadline(_signum, _frame):
            raise ActionDeadline()

        previous = signal.signal(signal.SIGALRM, deadline)
        signal.setitimer(signal.ITIMER_REAL, 8)
        recheck = lambda: verify_bus_owner(environment, launcher_pid, Gio, GLib)
        recheck()  # Verify ownership before any accessibility-desktop access.
        Atspi.set_timeout(500, 1000)
        require(Atspi.init() in (0, 1), "native_action_atspi_unavailable")
        desktop = Atspi.get_desktop(0)
        require(desktop is not None, "native_action_desktop_unavailable")
        invoke_next(desktop, window_id, Atspi, recheck, report)
    except ActionDeadline:
        report["failure_code"] = "native_action_deadline"
    except ActionFailure as error:
        report["failure_code"] = str(error)
    except Exception:
        report["failure_code"] = "native_action_unavailable"
    finally:
        if previous is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    return report


def activate_next(environment, *, window_id, private_root, launcher_pid):
    """One action attempt in a fresh private-bus process; never retry/fallback."""
    report, process = empty_report(), None
    try:
        child_env = private_environment(environment, private_root, window_id, launcher_pid)
        process = subprocess.Popen([SYSTEM_PYTHON, "-I", "-B", str(Path(__file__).resolve()),
            "--execute-next", "--window-id", window_id, "--private-root", str(private_root),
            "--launcher-pid", str(launcher_pid)], env=child_env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        output, _ = process.communicate(timeout=12)
        require(len(output) <= 4096, "native_action_report_too_large")
        value = json.loads(output)
        require(isinstance(value, dict) and value.get("schema") == 1
                and value.get("test") == report["test"] and type(value.get("passed")) is bool,
                "native_action_report_invalid")
        require(process.returncode == 0 or value["passed"] is False, "native_action_process_failed")
        return value
    except subprocess.TimeoutExpired:
        report.update(action_attempted=None, failure_code="native_action_process_timeout")
    except ActionFailure as error:
        report["failure_code"] = str(error)
    except Exception:
        report["failure_code"] = "native_action_process_failed"
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=3)
            finally:
                process.stdout.close()
    return report


def self_test():
    """Fake native objects only: no GI import, bus, desktop, or button action."""
    from types import SimpleNamespace
    from unittest.mock import patch

    role = SimpleNamespace(APPLICATION="application", FRAME="frame", WINDOW="window", DIALOG="dialog",
                           PUSH_BUTTON="button", PASSWORD_TEXT="password")
    state = SimpleNamespace(ENABLED="enabled", SENSITIVE="sensitive", DEFUNCT="defunct", PROTECTED="protected")

    class Node:
        def __init__(self, path, kind, name="", kids=(), enabled=True):
            self.app = SimpleNamespace(bus_name=":1.7")
            self.path, self.kind, self.name, self.kids = path, kind, name, list(kids)
            self.states = {"enabled", "sensitive"} if enabled else set()
            self.actions, self.calls, self.accepted = ["click"], [], True
            self.protected_read = False

        def get_role(self):
            return self.kind

        def get_name(self):
            require(not self.protected_read, "self_test_protected_name_read")
            return self.name

        def get_child_count(self):
            require(not self.protected_read, "self_test_protected_children_read")
            return len(self.kids)

        def get_child_at_index(self, index):
            return self.kids[index]

        def get_state_set(self):
            return SimpleNamespace(contains=lambda item: item in self.states)

    class Actions:
        get_n_actions = staticmethod(lambda node: len(node.actions))
        get_action_name = staticmethod(lambda node, index: node.actions[index])

        @staticmethod
        def do_action(node, index):
            node.calls.append(index)
            return node.accepted

    Atspi = SimpleNamespace(Role=role, StateType=state, Action=Actions,
                            Accessible=SimpleNamespace(get_action_iface=lambda node: node))

    def fixture():
        button = Node("/next", "button", "Next")
        password = Node("/secret", "password")
        password.protected_read = True
        window = Node("/window", "frame", TITLE_PREFIX, [button, password])
        application = Node("/application", "application", kids=[window])
        desktop = Node("/desktop", "desktop", kids=[application])
        return desktop, window, button

    def rejected(mutate, expected_code):
        desktop, window, button = fixture()
        mutate(desktop, window, button)
        report = empty_report()
        try:
            invoke_next(desktop, '[":1.7","/window"]', Atspi, lambda: None, report)
        except ActionFailure as error:
            require(str(error) == expected_code and not button.calls, "self_test_wrong_refusal")
        else:
            raise ActionFailure("self_test_unsafe_action_accepted")

    desktop, window, button = fixture()
    report = empty_report()
    checks = []
    invoke_next(desktop, '[":1.7","/window"]', Atspi, lambda: checks.append("bus"), report)
    require(report["passed"] and report["action_attempted"] and button.calls == [0] and checks == ["bus"],
            "self_test_native_dispatch")
    rejected(lambda _d, w, _b: w.kids.append(Node("/other_next", "button", "Next")), "native_action_next_not_unique_enabled")
    rejected(lambda _d, _w, b: b.states.clear(), "native_action_next_not_unique_enabled")
    rejected(lambda d, _w, _b: d.kids[0].kids.append(Node("/other", "frame", TITLE_PREFIX)), "native_action_window_scope_changed")
    rejected(lambda _d, w, _b: setattr(w, "name", "Unrelated window"), "native_action_window_title_mismatch")
    rejected(lambda _d, _w, b: setattr(b, "actions", ["menu", "focus"]), "native_action_activation_ambiguous")
    rejected(lambda _d, _w, b: setattr(b, "actions", ["click", "press"]), "native_action_activation_ambiguous")
    rejected(lambda _d, _w, b: setattr(b.app, "bus_name", ":1.8"), "native_action_button_scope_changed")
    rejected(lambda _d, _w, b: setattr(b, "path", None), "native_action_identity_unavailable")
    rejected(lambda _d, w, _b: setattr(w, "get_child_count", lambda: MAX_CHILDREN + 1), "native_action_children_incomplete")
    desktop, window, button = fixture()
    try:
        invoke_next(desktop, '[":1.7","/window"]', Atspi,
                    lambda: window.kids.append(Node("/late_next", "button", "Next")), empty_report())
    except ActionFailure as error:
        require(str(error) == "native_action_next_not_unique_enabled" and not button.calls, "self_test_revalidation")
    else:
        raise ActionFailure("self_test_revalidation_missing")
    desktop, _window, button = fixture()
    button.accepted = False
    report = empty_report()
    try:
        invoke_next(desktop, '[":1.7","/window"]', Atspi, lambda: None, report)
    except ActionFailure as error:
        require(str(error) == "native_action_not_accepted" and button.calls == [0]
                and report["action_attempted"] and not report["passed"], "self_test_no_retry")
    else:
        raise ActionFailure("self_test_action_false_accepted")

    # Ownership failures must fail before action dispatch; these are plain
    # Python stand-ins, not connections to either a private or a global bus.
    bus = {"owner": 123, "address": "unix:path=/synthetic/a11y", "calls": []}

    def bus_call(*args):
        bus["calls"].append(args[3])
        value = bus["owner"] if args[3] == "GetConnectionUnixProcessID" else bus["address"]
        return SimpleNamespace(unpack=lambda: (value,))

    Gio = SimpleNamespace(bus_get_sync=lambda *_: SimpleNamespace(call_sync=bus_call),
                          BusType=SimpleNamespace(SESSION=1), DBusCallFlags=SimpleNamespace(NONE=0))
    GLib = SimpleNamespace(Variant=lambda *_: None, VariantType=SimpleNamespace(new=lambda _: None))
    verify_bus_owner({"AT_SPI_BUS_ADDRESS": bus["address"]}, 123, Gio, GLib)
    require(bus["calls"] == ["GetConnectionUnixProcessID", "GetAddress"], "self_test_bus_owner_check")
    for field, value, code in (("owner", 456, "native_action_bus_owner_mismatch"),
                               ("address", "unix:path=/different", "native_action_bus_address_mismatch")):
        bus.update(owner=123, address="unix:path=/synthetic/a11y", calls=[])
        bus[field] = value
        desktop, _window, button = fixture()
        try:
            invoke_next(desktop, '[":1.7","/window"]', Atspi,
                        lambda: verify_bus_owner({"AT_SPI_BUS_ADDRESS": "unix:path=/synthetic/a11y"}, 123, Gio, GLib),
                        empty_report())
        except ActionFailure as error:
            require(str(error) == code and not button.calls, "self_test_bus_mismatch_action")
        else:
            raise ActionFailure("self_test_bus_mismatch_accepted")

    with tempfile.TemporaryDirectory(prefix="floaty-browser-lab-") as temporary:
        root = Path(temporary)
        (root / "runtime").mkdir(mode=0o700)
        environment = {"XDG_RUNTIME_DIR": str(root / "runtime"), "DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic/session",
                       "AT_SPI_BUS_ADDRESS": "unix:path=/synthetic/a11y", "SOCKET_STREAM_ATSPI_TARGET": '[":1.7","/window"]',
                       "SOCKET_STREAM_API_KEY": "SYNTHETIC-NOT-INHERITED", "HTTPS_PROXY": "http://synthetic.invalid"}
        isolated = private_environment(environment, root, environment["SOCKET_STREAM_ATSPI_TARGET"], 123)
        require("SOCKET_STREAM_API_KEY" not in isolated and "HTTPS_PROXY" not in isolated
                and isolated["AT_SPI_BUS_ADDRESS"] == environment["AT_SPI_BUS_ADDRESS"], "self_test_private_environment")

        # An actual owned sleeper exercises cancellation cleanup without
        # importing GI or executing this helper's native entry point.
        original_popen, owned = subprocess.Popen, []

        def interrupted_spawn(arguments, **options):
            require(arguments[:3] == [SYSTEM_PYTHON, "-I", "-B"] and "--execute-next" in arguments
                    and options["env"] == isolated, "self_test_child_environment")
            process = original_popen([SYSTEM_PYTHON, "-c", "import time; time.sleep(30)"], **options)
            owned.append(process)

            def interrupted(*_args, **_kwargs):
                raise KeyboardInterrupt()

            process.communicate = interrupted
            return process

        try:
            with patch.object(subprocess, "Popen", side_effect=interrupted_spawn):
                try:
                    activate_next(environment, window_id=environment["SOCKET_STREAM_ATSPI_TARGET"],
                                  private_root=root, launcher_pid=123)
                except KeyboardInterrupt:
                    pass
                else:
                    raise ActionFailure("self_test_cancellation_not_propagated")
            require(owned and owned[0].poll() is not None, "self_test_cancelled_child_not_reaped")
            try:
                os.killpg(owned[0].pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise ActionFailure("self_test_cancelled_group_remaining")
        finally:
            for process in owned:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)
        environment["XDG_RUNTIME_DIR"] = "/run/user/1000"
        try:
            private_environment(environment, root, environment["SOCKET_STREAM_ATSPI_TARGET"], 123)
        except ActionFailure:
            pass
        else:
            raise ActionFailure("self_test_global_runtime_accepted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--execute-next", action="store_true")
    parser.add_argument("--window-id")
    parser.add_argument("--private-root")
    parser.add_argument("--launcher-pid", type=int)
    args = parser.parse_args()
    if args.self_test:
        try:
            self_test()
            result = {"schema": 1, "test": "owned_generated_next_action", "self_test": True,
                      "passed": True, "native_action_tested": False}
        except Exception:
            result = {"schema": 1, "test": "owned_generated_next_action", "self_test": True,
                      "passed": False, "failure_code": "native_action_self_test_failed"}
    elif args.execute_next:
        result = execute(args.window_id, args.private_root, args.launcher_pid)
    else:
        result = empty_report()
        result["failure_code"] = "use_owned_native_runner_or_self_test"
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
