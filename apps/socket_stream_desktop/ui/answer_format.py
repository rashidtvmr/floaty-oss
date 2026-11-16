"""Small, non-executable Markdown presentation for streamed answers.

No HTML, browser, network loading or third-party parser is involved. The raw
Markdown stays available for Copy; these runs only style the GTK reading view.
"""
import re

MAX_ANSWER = 64000
_ANSWER = re.compile(r"(?m)^\s*(?:\*\*)?Answer:(?:\*\*)?[ \t]*")
_QUESTION = re.compile(r"^\s*(?:\*\*)?Question:(?:\*\*)?[ \t]*")
_INLINE = re.compile(r"(`+)([^`\n]+)\1|\*\*([^*\n]+)\*\*|(?<!\*)\*([^*\n]+)\*(?!\*)")
_HEADING = re.compile(r"^#{1,4}\s+(.+?)[ \t]*#*[ \t]*(\n?)$")


def answer_parts(raw):
    """Split the shared Q/A envelope without repeating it inside the answer."""
    raw = (raw or "")[:MAX_ANSWER]
    question = _QUESTION.match(raw)
    if question:
        marker = _ANSWER.search(raw, question.end())
        if marker:
            return raw[question.end():marker.start()].strip(), raw[marker.end():].lstrip("\n")
        # The provider is still writing its short question summary.
        return raw[question.end():].strip(), ""
    marker = _ANSWER.match(raw)
    if marker:
        return "", raw[marker.end():].lstrip("\n")
    if raw.strip() and ("Question:".startswith(raw.strip()) or "Answer:".startswith(raw.strip())):
        return "", ""
    return "", raw


def _inline(text, base="body"):
    runs = []
    start = 0
    for match in _INLINE.finditer(text):
        if match.start() > start:
            runs.append((text[start:match.start()], base))
        if match.group(2) is not None:
            runs.append((match.group(2), "code"))
        elif match.group(3) is not None:
            runs.append((match.group(3), "strong"))
        else:
            runs.append((match.group(4), "emphasis"))
        start = match.end()
    if start < len(text):
        runs.append((text[start:], base))
    return runs


def markdown_runs(text):
    """Return (text, tag) runs, including unfinished fenced code while streaming."""
    runs = []
    fence = ""
    for line in (text or "")[:MAX_ANSWER].splitlines(keepends=True):
        stripped = line.lstrip()
        match = re.match(r"(`{3,}|~{3,})([^\n]*)", stripped)
        if not fence and match:
            fence = match.group(1)
            language = match.group(2).strip()[:32]
            if language:
                runs.append((language.upper() + "\n", "code_language"))
            continue
        if fence:
            if (stripped.startswith(fence)
                    and not stripped[len(fence):].strip("`~ \t\r\n")):
                fence = ""
                runs.append(("\n", "body"))
            else:
                runs.append((line, "code_block"))
            continue
        heading = _HEADING.match(line)
        if heading:
            runs.extend(_inline(heading.group(1) + heading.group(2), "heading"))
        elif re.match(r"^\s*[-*+]\s+", line):
            content = re.sub(r"^(\s*)[-*+]\s+", r"\1•  ", line, count=1)
            runs.extend(_inline(content))
        elif re.match(r"^\s*([-*_])(?:\s*\1){2,}\s*$", line):
            runs.append(("────────────────────────\n", "muted"))
        else:
            runs.extend(_inline(line))
    return runs


def question_title(question, answer="", limit=220):
    """A short heading; the full recognized question remains in the disclosure."""
    summarized, _ = answer_parts(answer)
    text = " ".join((summarized or question or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
