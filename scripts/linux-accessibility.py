#!/usr/bin/env python3
"""One-shot AT-SPI extraction from the active/focused application.

Two output modes:

* ``--mode text`` (legacy) prints the same newline-joined text blob as before.
* ``--mode tree`` (default) prints one bounded JSON object, schema 1, as
  specified in ``docs/A11Y-TREE-SHORTCUTS.md`` section 1.2.

Requires python3-gi, gir1.2-atspi-2.0, and a user D-Bus session.
This helper does not capture pixels or request/bypass OS permissions.
"""
import argparse
import json
import os
import signal
import sys

# Legacy text-mode bounds (kept byte-for-byte compatible).
MAX_NODES = 250
MAX_CHARS = 60_000
MAX_DEPTH = 8
MAX_CHILDREN = 80
PROTECTED_ROLES = ("password", "secure")

# Tree-mode defaults and classification.
DEFAULT_TREE_NODES = 800
DEFAULT_TREE_DEPTH = 20
DEFAULT_TREE_CHARS = 32_000
IMAGE_ROLE_TOKENS = ("image", "canvas", "graphic", "icon", "chart", "drawing", "figure")
STRUCTURAL_ROLES = ("panel", "filler", "separator", "scroll pane", "unknown")
COMPOSITOR_APPLICATIONS = frozenset(("gnome-shell", "gnome shell", "mutter-x11-frames"))


class OwnWindowError(RuntimeError):
    """The user is focused on SocketStream, not the practice application."""


class ReadDeadline(BaseException):
    """A process-wide deadline that per-node best-effort reads cannot swallow."""


def has_state(node, state, atspi):
    try:
        return bool(node.get_state_set().contains(getattr(atspi.StateType, state)))
    except Exception:
        return False


def children(node):
    try:
        count = max(0, min(int(node.get_child_count()), MAX_CHILDREN))
    except Exception:
        return
    for index in range(count):
        try:
            child = node.get_child_at_index(index)
        except Exception:
            continue
        if child is not None:
            yield child


def focused_descendant(node, atspi, depth=0, visited=None, max_depth=MAX_DEPTH, max_nodes=MAX_NODES):
    """Find the focused widget without reading any text from other widgets."""
    if visited is None:
        visited = {}
    if node is None or depth > max_depth or len(visited) >= max_nodes:
        return None
    if not remember_node(node, visited):
        return None
    if has_state(node, "DEFUNCT", atspi) or role_is_protected(safe_role(node), has_state(node, "PROTECTED", atspi)):
        return None
    if has_state(node, "FOCUSED", atspi):
        return node
    for child in children(node):
        result = focused_descendant(child, atspi, depth + 1, visited, max_depth, max_nodes)
        if result is not None:
            return result
    return None


# --- Pure classification helpers (unit tested without gi) -------------------


def role_is_image(role):
    """True for image, canvas, graphic, icon, chart or drawing roles."""
    role = (role or "").lower()
    return any(token in role for token in IMAGE_ROLE_TOKENS)


def role_is_protected(role, protected_state=False):
    """True for password/secure roles or an explicit PROTECTED state."""
    role = (role or "").lower()
    return bool(protected_state) or any(token in role for token in PROTECTED_ROLES)


def _number(source, name, index):
    value = getattr(source, name, None)
    if value is None:
        try:
            value = source[index]
        except (TypeError, IndexError, KeyError):
            return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_bounds(extents):
    """Normalise an extents object/tuple to ``[x, y, w, h]`` or ``None``."""
    if extents is None:
        return None
    x = _number(extents, "x", 0)
    y = _number(extents, "y", 1)
    width = _number(extents, "width", 2)
    height = _number(extents, "height", 3)
    if None in (x, y, width, height) or width <= 0 or height <= 0:
        return None
    # Native Wayland apps can expose an undefined global position using
    # G_MININT. Such coordinates must never become a screen OCR region.
    if abs(x) >= 2 ** 30 or abs(y) >= 2 ** 30:
        return None
    return [x, y, width, height]


# --- Node accessors ---------------------------------------------------------


def safe_role(node):
    try:
        return (node.get_role_name() or "").lower()
    except Exception:
        return ""


def safe_name(node):
    try:
        return str(node.get_name() or "").strip()
    except Exception:
        return ""


def text_content(text_iface, count, atspi=None):
    # PyGObject may expose Accessible.get_text() (the interface getter) on the
    # same wrapper as Text.get_text(start, end). Dispatch the Text method
    # explicitly instead of accidentally calling the zero-argument getter.
    read = getattr(getattr(atspi, "Text", None), "get_text", None)
    return read(text_iface, 0, count) if callable(read) else text_iface.get_text(0, count)


def safe_text(node, atspi=None, limit=DEFAULT_TREE_CHARS, truncation=None):
    parts = []
    clipped = False
    try:
        text_iface = node.get_text_iface()
    except Exception:
        text_iface = None
        if truncation is not None:
            truncation["truncated"] = True
    if text_iface is not None:
        try:
            count_text = getattr(getattr(atspi, "Text", None), "get_character_count", None)
            available = max(0, int(count_text(text_iface) if callable(count_text)
                                   else text_iface.get_character_count()))
            count = max(0, min(available, limit))
            clipped = count < available
            if clipped and truncation is not None:
                truncation["truncated"] = True
            if count:
                parts.append(str(text_content(text_iface, count, atspi)))
        except Exception:
            if truncation is not None:
                truncation["truncated"] = True
    if clipped and parts:
        return parts[0]  # Preserve whitespace at a budget boundary/code prefix.
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()


def safe_extents(node, atspi):
    try:
        component = node.get_component_iface() if hasattr(node, "get_component_iface") else node
        if component is None:
            return None
        read = getattr(getattr(atspi, "Component", None), "get_extents", None)
        return (read(component, atspi.CoordType.SCREEN) if callable(read)
                else component.get_extents(atspi.CoordType.SCREEN))
    except Exception:
        return None


def safe_pid(node):
    try:
        return int(node.get_process_id())
    except Exception:
        return None


def select_foreground(desktop, atspi):
    """Return the active/focused application, or ``None``.

    Raises :class:`OwnWindowError` when SocketStream itself is foreground.
    """
    # GNOME exposes frame mirrors for its decorations. They are not the
    # application's content tree and can share its ACTIVE state. Apply the
    # same exclusion as the explicit-window picker, on both XWayland and
    # native Wayland. No X11 active-window query is needed.
    applications = [app for app in children(desktop)
                    if safe_name(app).lower() not in COMPOSITOR_APPLICATIONS
                    and not has_state(app, "DEFUNCT", atspi)]

    def focused(node):
        return focused_descendant(node, atspi, max_depth=DEFAULT_TREE_DEPTH, max_nodes=DEFAULT_TREE_NODES) is not None

    # AT-SPI commonly puts ACTIVE on the top-level frame, not its application.
    # Prefer that frame over stale FOCUSED widgets in background applications,
    # and read only that window, never all of an application's open windows.
    frames = [(app, frame) for app in applications for frame in children(app)
              if safe_role(frame) in ("frame", "window", "dialog")
              and not has_state(frame, "DEFUNCT", atspi)]
    target = os.environ.get("SOCKET_STREAM_ATSPI_TARGET")
    active_frames = [(app, frame) for app, frame in frames if has_state(frame, "ACTIVE", atspi)]
    if target is not None:
        candidates = [(app, frame) for app, frame in frames if window_identity(frame) == target]
        if len(candidates) != 1:
            raise RuntimeError("Selected question window is unavailable. Stop and choose the window again; no other window was read.")
    elif active_frames:
        with_focus = [(app, frame) for app, frame in active_frames if focused(frame)]
        candidates = with_focus or active_frames
    else:
        eligible = [app for app in applications if has_state(app, "ACTIVE", atspi)]
        if not eligible:
            eligible = [app for app in applications if focused(app)]
        candidates = []
        for app in eligible:
            frames = [node for node in children(app) if safe_role(node) in ("frame", "window", "dialog")
                      and not has_state(node, "DEFUNCT", atspi)]
            focused_frames = [frame for frame in frames if focused(frame)]
            if len(focused_frames) == 1:
                candidates.append((app, focused_frames[0]))
            elif len(frames) == 1:
                candidates.append((app, frames[0]))
            elif frames:
                raise RuntimeError("Multiple application windows are available. Focus the intended question window and retry.")
            else:
                # Some toolkits expose a flat application tree with no window
                # nodes; there is no known multi-window subtree to traverse.
                candidates.append((app, app))
    if not candidates:
        return None
    if len(candidates) != 1:
        raise RuntimeError("Multiple accessible windows report focus/active state. Stop and use Choose question window, then Start again.")
    app, selected = candidates[0]

    # The GUI passes its own PID to the capture child; avoid recursively
    # interpreting the assistant's own output as a user question.
    gui_pid = os.environ.get("SOCKET_STREAM_GUI_PID", "")
    try:
        selected_pid = app.get_process_id()
    except Exception:
        selected_pid = None
    try:
        selected_name = (app.get_name() or "").lower()
    except Exception:
        selected_name = ""
    if (gui_pid.isdecimal() and selected_pid == int(gui_pid)) or \
            "socket_stream" in selected_name or "socketstream" in selected_name or \
            safe_name(selected).lower().startswith(("socketstream", "socket_stream")):
        raise OwnWindowError("SocketStream is foreground")
    return selected


def window_identity(frame):
    """Native AT-SPI identity, stable across GI wrappers and helper processes.

    ``Atspi.Object.app.bus_name`` and ``Atspi.Object.path`` identify the
    exported D-Bus object. A PID, title, traversal index or Python object ID
    is not a substitute: none identifies a particular accessibility object.
    """
    try:
        bus = getattr(getattr(frame, "app", None), "bus_name", None)
        path = getattr(frame, "path", None)
    except Exception:
        return None
    if (not isinstance(bus, str) or not bus.startswith(":") or len(bus) > 255
            or not isinstance(path, str) or not path.startswith("/") or len(path) > 2048):
        return None
    return json.dumps([bus, path], separators=(",", ":"))


def remember_node(node, visited):
    """Deduplicate native objects; retain fallback wrappers to prevent id reuse."""
    identity = window_identity(node)
    key = ("atspi", identity) if identity is not None else ("python", id(node))
    if key in visited:
        return False
    visited[key] = node
    return True


def list_windows(desktop, atspi):
    """List window labels/identities only; never traverse their text widgets."""
    result = []
    for app in children(desktop):
        app_name = safe_name(app)
        # GNOME exposes compositor decoration mirrors as frames in addition to
        # the application's real accessibility tree. They contain no app text.
        if app_name.lower() in COMPOSITOR_APPLICATIONS or has_state(app, "DEFUNCT", atspi):
            continue
        if "socketstream" in app_name.lower() or "socket_stream" in app_name.lower():
            continue
        for frame in children(app):
            identity = window_identity(frame)
            if safe_role(frame) not in ("frame", "window", "dialog") or identity is None:
                continue
            if has_state(frame, "PROTECTED", atspi) or has_state(frame, "DEFUNCT", atspi):
                continue
            if safe_pid(app) == os.getpid() or str(safe_pid(app)) == os.environ.get("SOCKET_STREAM_GUI_PID"):
                continue
            title = safe_name(frame)[:200]
            if title.lower().startswith(("socketstream", "socket_stream")):
                continue
            result.append({"id": identity, "title": title or "Untitled window", "application": app_name[:100]})
    return result


def mark_focused_ancestors(nodes):
    """Mark the deepest focused node and its ancestors as focused."""
    by_id = {node["id"]: node for node in nodes}

    def depth(node):
        result = 0
        parent = node["parent"]
        while parent is not None and parent in by_id:
            result += 1
            parent = by_id[parent]["parent"]
        return result

    focused_node = max(
        (node for node in nodes if node["focused"]),
        key=depth,
        default=None,
    )
    for node in nodes:
        node["focused"] = False
    if focused_node is None:
        return

    focused_node["focused"] = True
    parent = focused_node["parent"]
    while parent is not None:
        ancestor = by_id.get(parent)
        if ancestor is None:
            break
        ancestor["focused"] = True
        parent = ancestor["parent"]


def tree_children(node, state):
    """Yield bounded tree children and report when the child cap omits any."""
    try:
        count = max(0, int(node.get_child_count()))
    except Exception:
        state["truncated"] = True
        return
    if count > MAX_CHILDREN:
        state["truncated"] = True
    for index in range(min(count, MAX_CHILDREN)):
        try:
            child = node.get_child_at_index(index)
        except Exception:
            state["truncated"] = True
            continue
        if child is not None:
            yield child


def build_tree(selected, atspi, max_nodes, max_depth, max_chars):
    """Build the schema-1 tree payload for one application node."""
    nodes = []
    visited = {}
    state = {"truncated": False, "chars": 0}

    def walk(node, parent_id, depth):
        if node is None:
            return
        if depth > max_depth or len(nodes) >= max_nodes:
            state["truncated"] = True
            return
        if not remember_node(node, visited) or has_state(node, "DEFUNCT", atspi):
            return

        role = safe_role(node)
        protected = role_is_protected(role, has_state(node, "PROTECTED", atspi))
        # Some toolkits mirror password content into accessible names or
        # descendants; a protected root is an opaque redacted leaf.
        name = "" if protected else safe_name(node)
        text = "" if protected else safe_text(node, atspi, max(0, max_chars - state["chars"]), state)
        # The character budget covers both accessible labels and text. Keep
        # the pre-order allocation deterministic, with each node's name first.
        remaining = max(0, max_chars - state["chars"])
        if len(name) > remaining:
            name = name[:remaining]
            state["truncated"] = True
        state["chars"] += len(name)
        remaining = max(0, max_chars - state["chars"])
        if len(text) > remaining:
            text = text[:remaining]
            state["truncated"] = True
        state["chars"] += len(text)

        node_id = len(nodes) + 1
        nodes.append({
            "id": node_id,
            "stable_id": window_identity(node),
            "parent": parent_id,
            "role": role,
            "name": name,
            "text": text,
            "focused": has_state(node, "FOCUSED", atspi),
            "bounds": normalize_bounds(safe_extents(node, atspi)),
            "image": role_is_image(role),
            "protected": protected,
        })
        if protected:
            return
        for child in tree_children(node, state):
            if len(nodes) >= max_nodes:
                state["truncated"] = True
                break
            walk(child, node_id, depth + 1)

    walk(selected, None, 0)
    mark_focused_ancestors(nodes)
    app_name = ("" if role_is_protected(safe_role(selected), has_state(selected, "PROTECTED", atspi))
                else safe_name(selected))
    if len(app_name) > max_chars:
        state["truncated"] = True
    return {
        "schema": 1,
        "source": "atspi",
        "app": app_name[:max_chars],
        "pid": safe_pid(selected),
        "window_id": window_identity(selected),
        "truncated": state["truncated"],
        "node_count": len(nodes),
        "nodes": nodes,
    }


def empty_tree():
    return {
        "schema": 1,
        "source": "atspi",
        "app": "",
        "pid": None,
        "window_id": None,
        "truncated": False,
        "node_count": 0,
        "nodes": [],
    }


def extract_tree(desktop, atspi, args):
    selected = select_foreground(desktop, atspi)
    if selected is None:
        return empty_tree()
    return build_tree(selected, atspi, args.max_nodes, args.max_depth, args.max_chars)


def extract(desktop, atspi):
    """Return bounded visible accessible text for one foreground application."""
    selected = select_foreground(desktop, atspi)
    if selected is None:
        return ""

    chunks = []
    emitted = set()
    visited = {}
    size = 0

    def emit(value):
        nonlocal size
        if value is None or size >= MAX_CHARS:
            return
        value = str(value).strip()
        if not value or value in emitted:
            return
        value = value[:MAX_CHARS - size]
        if value:
            emitted.add(value)
            chunks.append(value)
            size += len(value)

    def walk(node, depth=0):
        if node is None or depth > MAX_DEPTH or len(visited) >= MAX_NODES or size >= MAX_CHARS:
            return
        if not remember_node(node, visited) or has_state(node, "DEFUNCT", atspi):
            return
        try:
            role = (node.get_role_name() or "").lower()
            if any(token in role for token in PROTECTED_ROLES):
                return
            if has_state(node, "PROTECTED", atspi):
                return
            if role in ("text", "entry", "paragraph", "label", "heading"):
                emit(node.get_name())
            text_iface = node.get_text_iface()
            if text_iface:
                count = max(0, min(text_iface.get_character_count(), MAX_CHARS - size))
                if count:
                    emit(text_content(text_iface, count, atspi))
        except Exception:
            pass
        for child in children(node):
            walk(child, depth + 1)

    walk(selected)
    return "\n".join(chunks)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Read the focused application through AT-SPI."
    )
    parser.add_argument("--mode", choices=("tree", "text", "windows"), default="tree")
    parser.add_argument("--max-nodes", type=int, default=DEFAULT_TREE_NODES)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_TREE_DEPTH)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_TREE_CHARS)
    args = parser.parse_args(argv)
    args.max_nodes = max(0, args.max_nodes)
    args.max_depth = max(0, args.max_depth)
    args.max_chars = max(0, args.max_chars)
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as exc:
        print(f"AT-SPI unavailable: {exc}. Use /usr/bin/python3 and install python3-gi, "
              "gir1.2-atspi-2.0 and at-spi2-core in the logged-in desktop session.", file=sys.stderr)
        return 2
    def deadline(_signum, _frame):
        raise ReadDeadline("Accessibility read timed out; focus the question window and retry")

    previous = signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, 4.0)
    try:
        if hasattr(Atspi, "set_timeout"):
            # Keep the native library's normal 800ms IPC tolerance, with a
            # bounded startup grace period for browser accessibility bridge
            # initialization; the process deadline still wins.
            Atspi.set_timeout(800, 1500)
        if hasattr(Atspi, "init"):
            status = Atspi.init()
            if status not in (0, 1):
                raise RuntimeError("Cannot connect to the AT-SPI accessibility bus. "
                                   "Run from the logged-in desktop user's D-Bus session, not sudo/SSH.")
        desktop = Atspi.get_desktop(0)
        if desktop is None:
            raise RuntimeError("The AT-SPI desktop is unavailable. Check at-spi2-core and the "
                               "logged-in desktop user's D-Bus session.")
        if args.mode == "windows":
            print(json.dumps(list_windows(desktop, Atspi), separators=(",", ":")))
        elif args.mode == "text":
            print(extract(desktop, Atspi))
        else:
            print(json.dumps(extract_tree(desktop, Atspi, args), separators=(",", ":")))
    except OwnWindowError:
        return 4
    except (Exception, ReadDeadline) as exc:
        print(f"AT-SPI read failed: {exc}", file=sys.stderr)
        return 3
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
