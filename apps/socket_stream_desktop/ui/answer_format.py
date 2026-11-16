"""Small, non-executable Markdown presentation for streamed answers.

No HTML, browser, network loading or third-party parser is involved. The raw
Markdown stays available for Copy; these runs only style the GTK reading view.
"""
import re
import unicodedata

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


def _table_cells(line):
    """Split unescaped pipes outside inline code, never execute cell content."""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    cells, cell, code, index = [], [], 0, 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] == "|":
            cell.append("|")
            index += 2
            continue
        if char == "`":
            end = index + 1
            while end < len(text) and text[end] == "`":
                end += 1
            count = end - index
            code = 0 if code == count else count if not code else code
            cell.append(text[index:end])
            index = end
            continue
        if char == "|" and not code:
            cells.append("".join(cell).strip())
            cell = []
        else:
            cell.append(char)
        index += 1
    if cell or not text.endswith("|"):
        cells.append("".join(cell).strip())
    return cells


def _width(text):
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def _cell_lines(text, width):
    lines, current, used = [], [], 0
    for char in text:
        size = _width(char)
        if used + size > width and current:
            lines.append("".join(current))
            current, used = [], 0
        current.append(char)
        used += size
    lines.append("".join(current))
    return lines


def _table_runs(rows):
    # Render in a non-wrapping monospace tag so GTK preserves alignment and
    # horizontal scrolling. Copy continues to use the untouched raw Markdown.
    rows = [["".join(part for part, _ in _inline(cell)) for cell in row] for row in rows]
    widths = [max(3, min(42, max(_width(row[col]) for row in rows)))
              for col in range(len(rows[0]))]
    runs = []
    for index, row in enumerate(rows):
        wrapped = [_cell_lines(cell, width) for cell, width in zip(row, widths)]
        for line_number in range(max(map(len, wrapped))):
            cells = [lines[line_number] if line_number < len(lines) else "" for lines in wrapped]
            line = "│ " + " │ ".join(cell + " " * (width - _width(cell))
                                       for cell, width in zip(cells, widths)) + " │\n"
            runs.append((line, "table_header" if index == 0 else "table_row"))
        if index == 0:
            runs.append(("├─" + "─┼─".join("─" * width for width in widths) + "─┤\n", "table_row"))
    runs.append(("\n", "body"))
    return runs


def markdown_runs(text):
    """Return (text, tag) runs, including unfinished fenced code while streaming."""
    runs = []
    fence = ""
    lines = (text or "")[:MAX_ANSWER].splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
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
        if index < len(lines) and "|" in line:
            header = _table_cells(line)
            separator = _table_cells(lines[index])
            if (2 <= len(header) <= 8 and len(separator) == len(header)
                    and all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator)):
                index += 1
                rows = [header]
                while index < len(lines) and len(rows) < 256 and "|" in lines[index]:
                    cells = _table_cells(lines[index])
                    if len(cells) != len(header):
                        break
                    rows.append(cells)
                    index += 1
                runs.extend(_table_runs(rows))
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
