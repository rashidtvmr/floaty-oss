#!/usr/bin/env python3
"""One-shot AT-SPI text extraction from the active/focused application.

Requires python3-gi, gir1.2-atspi-2.0, and a user D-Bus session.
This helper does not capture pixels or request/bypass OS permissions.
"""
import os
import sys

MAX_NODES = 250
MAX_CHARS = 60_000
MAX_DEPTH = 8
MAX_CHILDREN = 80
PROTECTED_ROLES = ("password", "secure")


class OwnWindowError(RuntimeError):
    """The user is focused on ContextLens, not the practice application."""


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


def focused_descendant(node, atspi, depth=0, visited=None):
    """Find the focused widget without reading any text from other widgets."""
    if visited is None:
        visited = set()
    if node is None or depth > MAX_DEPTH or len(visited) >= MAX_NODES:
        return None
    identity = id(node)
    if identity in visited:
        return None
    visited.add(identity)
    if has_state(node, "FOCUSED", atspi):
        return node
    for child in children(node):
        result = focused_descendant(child, atspi, depth + 1, visited)
        if result is not None:
            return result
    return None


def extract(desktop, atspi):
    """Return bounded visible accessible text for one foreground application."""
    applications = list(children(desktop))
    selected = None
    for app in applications:
        if has_state(app, "ACTIVE", atspi) or has_state(app, "FOCUSED", atspi):
            selected = app
            break
    if selected is None:
        for app in applications:
            if focused_descendant(app, atspi) is not None:
                selected = app
                break
    if selected is None:
        return ""

    # The GUI passes its own PID to the capture child; avoid recursively
    # interpreting the assistant's own output as a user question.
    gui_pid = os.environ.get("CONTEXT_LENS_GUI_PID", "")
    try:
        selected_pid = selected.get_process_id()
    except Exception:
        selected_pid = None
    try:
        selected_name = (selected.get_name() or "").lower()
    except Exception:
        selected_name = ""
    if (gui_pid.isdecimal() and selected_pid == int(gui_pid)) or \
            "contextlens" in selected_name or "context-lens" in selected_name:
        raise OwnWindowError("ContextLens is foreground")

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


def main():
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as exc:
        print(f"AT-SPI unavailable: {exc}", file=sys.stderr)
        return 2
    try:
        print(extract(Atspi.get_desktop(0), Atspi))
    except OwnWindowError:
        return 4
    except Exception as exc:
        print(f"AT-SPI read failed: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
