"""Markdown tables, reshaped for a client that has none.

Discord's markdown has no table syntax at all — `|` is an ordinary character to
it — so a GitHub table arrives as a run of pipes that the reader's own width
then folds at arbitrary places. There is nothing to fix; there is only a choice
of what to send instead.

Two shapes are worth sending, and which one wins is a matter of measurement
rather than taste:

* **Aligned, inside a code fence.** Keeps the grid, which is the thing a table
  is for. Only works while every line fits: Discord wraps a code block rather
  than scrolling it, and one wrapped line destroys the alignment of all of
  them — that is exactly how a table ends up looking like the mess this module
  exists to stop.
* **Flattened into a list.** Loses the grid, and for three columns or more has
  to repeat the column names. But a wrapped list item is just a wrapped list
  item; there is no alignment left to break.

So: lay it out aligned, measure it, and keep it only if it fits. The asymmetry
is what settles the threshold — a grid that fits is a little better than a
list, while a grid that does not fit is far worse, so the measurement is taken
against the narrowest client rather than the roomiest.

Widths are counted in display columns, not characters. A CJK character occupies
two, and using `len()` here misaligns every table with Chinese in it — which is
all of them.
"""

import os
import re
import unicodedata

#: A fence, so that a shell pipeline inside a code block is never mistaken for
#: a table. Both fence characters, because GitHub takes either.
_FENCE = re.compile(r"^[ \t]*(?P<ticks>`{3,}|~{3,})")

#: The `| --- | :--: |` line, which is what actually distinguishes a table from
#: a paragraph that happens to contain a pipe.
_DELIMITER = re.compile(r"^:?-+:?$")

#: A cell boundary. GitHub lets a cell contain a literal pipe by escaping it.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")

#: Two spaces between columns: one is too tight to read a CJK table by, three
#: wastes width that the measurement below is trying to save.
_GUTTER = "  "

#: The widest a laid-out table may be before it is flattened instead.
#:
#: Discord publishes no character width for its message area, and the real
#: figure moves with the window, the font scale, and whether the sidebar is
#: open. This default is the narrow end — a phone — because that is the client
#: the grid has to survive, and it is an estimate rather than a measurement.
#: Raise it if your readers are all on desktop.
DEFAULT_WIDTH = 38


def _limit() -> int:
    try:
        return max(1, int(os.getenv("TABLE_WIDTH", DEFAULT_WIDTH)))
    except ValueError:
        return DEFAULT_WIDTH


def width(text: str) -> int:
    """How many columns a string occupies in a monospaced font."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, columns: int) -> str:
    return text + " " * max(0, columns - width(text))


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return [cell.strip().replace("\\|", "|") for cell in _UNESCAPED_PIPE.split(stripped)]


def _table_at(lines: list[str], start: int) -> tuple[list[list[str]] | None, int]:
    """The table beginning at `start`, and the line after it.

    `None` unless the second line is a delimiter row. Without that check any
    prose containing a pipe reads as a one-row table.
    """
    if start + 1 >= len(lines) or "|" not in lines[start]:
        return None, start

    header = _cells(lines[start])
    if len(header) < 2:
        return None, start

    delimiter = _cells(lines[start + 1])
    if len(delimiter) != len(header) or not all(_DELIMITER.match(c) for c in delimiter):
        return None, start

    rows = [header]
    index = start + 2
    while index < len(lines) and "|" in lines[index] and lines[index].strip():
        row = _cells(lines[index])[: len(header)]
        rows.append(row + [""] * (len(header) - len(row)))
        index += 1

    # A header with no rows under it is a table in name only, and flattening it
    # would produce nothing at all.
    if len(rows) < 2:
        return None, start
    return rows, index


def _aligned(rows: list[list[str]]) -> str:
    columns = [max(width(row[i]) for row in rows) for i in range(len(rows[0]))]
    out = [_GUTTER.join(_pad(c, w) for c, w in zip(rows[0], columns)).rstrip()]
    out.append(_GUTTER.join("─" * w for w in columns))
    for row in rows[1:]:
        out.append(_GUTTER.join(_pad(c, w) for c, w in zip(row, columns)).rstrip())
    return "\n".join(out)


def _flattened(rows: list[str]) -> str:
    """Each row as its own little block, led by whatever its first column says.

    The column names come along only from three columns up. With two, the
    header is almost always a restatement of what the values obviously are —
    "情況 / 結果" over a row that reads "密碼太短 / 說出政策的範圍" — and
    repeating it on every row is noise.
    """
    header, body = rows[0], rows[1:]
    labelled = len(header) > 2
    out: list[str] = []
    for row in body:
        out.append(f"**{row[0]}**" if row[0] else "**—**")
        for name, value in zip(header[1:], row[1:]):
            if not value:
                continue
            # An ideographic space, so the indent survives Discord collapsing
            # runs of ordinary spaces.
            out.append(f"　{name} · {value}" if labelled else f"　{value}")
    return "\n".join(out)


def _render(rows: list[list[str]], indent: str) -> str:
    grid = _aligned(rows)
    if max(width(line) for line in grid.split("\n")) <= _limit():
        body = f"```\n{grid}\n```"
    else:
        body = _flattened(rows)
    return "\n".join(indent + line if line else line for line in body.split("\n"))


def convert(body: str) -> str:
    """Every markdown table in a comment, replaced by something Discord shows."""
    if "|" not in body:
        return body

    lines = body.split("\n")
    out: list[str] = []
    fence: str | None = None
    index = 0

    while index < len(lines):
        line = lines[index]

        # Inside a code block nothing is a table, however many pipes it has.
        if fence is not None:
            out.append(line)
            if line.strip() and set(line.strip()) == {fence[0]} and len(line.strip()) >= len(fence):
                fence = None
            index += 1
            continue

        opening = _FENCE.match(line)
        if opening:
            fence = opening.group("ticks")
            out.append(line)
            index += 1
            continue

        rows, after = _table_at(lines, index)
        if rows is None:
            out.append(line)
            index += 1
            continue

        out.append(_render(rows, line[: len(line) - len(line.lstrip())]))
        index = after

    return "\n".join(out)
