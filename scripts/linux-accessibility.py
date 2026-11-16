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


class OwnWindowError(RuntimeError):
    """The user is focused on SocketStream, not the practice application."""


def has_state(node, state, atspi):
    try:
        return bool(node.get_state_set().contains(getattr(atspi.StateType, state)))
    except (AttributeError, RuntimeError, TypeError):
        return False


def children(node):
    try:
        count = max(0, min(node.get_child_count(), MAX_CHILDREN))
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
        visited = set()
    if node is None or depth > max_depth or len(visited) >= max_nodes:
        return None
    identity = id(node)
    if identity in visited:
        return None
    visited.add(identity)
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


def safe_text(node):
    parts = []
    try:
        text_iface = node.get_text_iface()
    except Exception:
        text_iface = None
    if text_iface is not None:
        try:
            count = max(0, int(text_iface.get_character_count()))
            if count:
                parts.append(str(text_iface.get_text(0, count)))
        except Exception:
            pass
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()


def safe_extents(node, atspi):
    try:
        return node.get_extents(atspi.CoordType.SCREEN)
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
    applications = list(children(desktop))

    def focused(node):
        return focused_descendant(node, atspi, max_depth=DEFAULT_TREE_DEPTH, max_nodes=DEFAULT_TREE_NODES) is not None

    # AT-SPI commonly puts ACTIVE on the top-level frame, not its application.
    # Prefer that frame over stale FOCUSED widgets in background applications,
    # and read only that window, never all of an application's open windows.
    active_frames = [(app, frame) for app in applications for frame in children(app)
                     if safe_role(frame) in ("frame", "window", "dialog") and has_state(frame, "ACTIVE", atspi)]
    if active_frames:
        with_focus = [(app, frame) for app, frame in active_frames if focused(frame)]
        candidates = with_focus or active_frames
    else:
        eligible = [app for app in applications if has_state(app, "ACTIVE", atspi)]
        if not eligible:
            eligible = [app for app in applications if focused(app)]
        candidates = []
        for app in eligible:
            frames = [node for node in children(app) if safe_role(node) in ("frame", "window", "dialog")]
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
        raise RuntimeError("Multiple accessible windows report focus/active state. Focus the intended question window and retry.")
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
        return
    if count > MAX_CHILDREN:
        state["truncated"] = True
    for index in range(min(count, MAX_CHILDREN)):
        try:
            child = node.get_child_at_index(index)
        except Exception:
            continue
        if child is not None:
            yield child


def build_tree(selected, atspi, max_nodes, max_depth, max_chars):
    """Build the schema-1 tree payload for one application node."""
    nodes = []
    visited = set()
    state = {"truncated": False, "chars": 0}

    def walk(node, parent_id, depth):
        if node is None:
            return
        if depth > max_depth or len(nodes) >= max_nodes:
            state["truncated"] = True
            return
        identity = id(node)
        if identity in visited:
            return
        visited.add(identity)

        role = safe_role(node)
        protected = role_is_protected(role, has_state(node, "PROTECTED", atspi))
        name = safe_name(node)
        text = "" if protected else safe_text(node)
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
            "parent": parent_id,
            "role": role,
            "name": name,
            "text": text,
            "focused": has_state(node, "FOCUSED", atspi),
            "bounds": normalize_bounds(safe_extents(node, atspi)),
            "image": role_is_image(role),
            "protected": protected,
        })
        for child in tree_children(node, state):
            if len(nodes) >= max_nodes:
                state["truncated"] = True
                break
            walk(child, node_id, depth + 1)

    walk(selected, None, 0)
    mark_focused_ancestors(nodes)
    app_name = safe_name(selected)
    if len(app_name) > max_chars:
        state["truncated"] = True
    return {
        "schema": 1,
        "source": "atspi",
        "app": app_name[:max_chars],
        "pid": safe_pid(selected),
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
    visited = set()
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
        identity = id(node)
        if identity in visited:
            return
        visited.add(identity)
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
                    emit(text_iface.get_text(0, count))
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
    parser.add_argument("--mode", choices=("tree", "text"), default="tree")
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
        print(f"AT-SPI unavailable: {exc}", file=sys.stderr)
        return 2
    def deadline(_signum, _frame):
        raise TimeoutError("Accessibility read timed out; focus the question window and retry")

    previous = signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, 4.0)
    try:
        if hasattr(Atspi, "set_timeout"):
            Atspi.set_timeout(250, 250)
        if hasattr(Atspi, "init"):
            Atspi.init()
        desktop = Atspi.get_desktop(0)
        if args.mode == "text":
            print(extract(desktop, Atspi))
        else:
            print(json.dumps(extract_tree(desktop, Atspi, args), separators=(",", ":")))
    except OwnWindowError:
        return 4
    except Exception as exc:
        print(f"AT-SPI read failed: {exc}", file=sys.stderr)
        return 3
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
