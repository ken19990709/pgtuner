"""render — compact plain-text rendering for data-plane commands (v0.6 design
note D2, historical).

Why: measured during the v0.6 investigation, the schema command's output was
7-9x the bytes of the psql equivalent (rich table borders + padding), and the
agent re-sending its context every turn amplifies that into a token bill.
psql's aligned text is equally readable for agents, so the data plane
(sql/schema/statements/hypo/advise/diagnose) all goes through here; management
commands (instance/results/lab/watch/selftest) keep rich tables and are not
affected by this module.

Constraint: output must be **plain text** (no rich markup, no escape sequences)
— callers use a bare print(), and stdout is directly consumable by scripts/
agents; double-width characters (CJK) align by display width (this module is
the repo's single implementation; planbench/sqlcmd both import from it).
"""
from __future__ import annotations

import unicodedata

# Numeric/placeholder cells right-aligned (consistent with psql); everything else left-aligned
_NUMERIC_CHARS = set("0123456789.,+-%")


def display_width(s: str) -> int:
    """Display width: East Asian Wide/Fullwidth counts as 2, everything else 1 (single source repo-wide: planbench/sqlcmd use this too)."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, width: int, right: bool = False) -> str:
    """Pad with spaces to `width` by display width (no truncation; truncation is truncate's job — the two concerns are not mixed)."""
    gap = max(0, width - display_width(s))
    return (" " * gap + s) if right else (s + " " * gap)


def truncate(s: str, width: int) -> str:
    """Truncate by display width and append … (width includes the ellipsis)."""
    if display_width(s) <= width:
        return s
    out = ""
    for ch in s:
        if display_width(out + ch) > width - 1:
            break
        out += ch
    return out + "…"


def oneline(s: str, width: int = 48) -> str:
    """Collapse to a single line and truncate by display width (SQL previews etc.:
    the single shared implementation for hypo/diagnose/statements, merging the
    original _q_head triplets; CJK literals in SQL count as double-width so they
    don't blow up the aligned table that follows)."""
    one = " ".join(s.split())
    return one if display_width(one) <= width else truncate(one, width)


def _is_right_aligned(cell: str) -> bool:
    """Pure numeric / percentage / thousands-separated / NULL placeholder → right-aligned."""
    return bool(cell) and all(c in _NUMERIC_CHARS for c in cell.replace("NULL", ""))


def align_table(headers: list[str], rows: list[list[str]], *, total_width: int = 120,
                min_col: int = 3) -> str:
    """Aligned text table: one header line + separator + data rows, two spaces between columns.

    Column width = max(header, rows) display width; if the total width exceeds
    total_width, columns are squeezed one by one starting from the widest
    (each column at least min_col; squeezed cells go through truncate).
    """
    ncol = len(headers)
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row[:ncol]):
            widths[i] = max(widths[i], display_width(cell))
    # Total width over the limit: shave 1 at a time from the widest column in turn until it fits (total_width - separators)
    while sum(widths) + 2 * (ncol - 1) > total_width and max(widths) > min_col:
        i = widths.index(max(widths))
        widths[i] -= 1
    fixed_rows = [[truncate(c, widths[i]) for i, c in enumerate(row[:ncol])]
                  + list(row[ncol:]) for row in rows]
    header_line = "  ".join(pad(truncate(h, widths[i]), widths[i]) for i, h in enumerate(headers))
    sep_line = "  ".join("-" * widths[i] for i in range(ncol))
    body = ["  ".join(
        pad(c, widths[i], right=_is_right_aligned(c)) for i, c in enumerate(r))
        for r in fixed_rows]
    return "\n".join([header_line, sep_line] + body)
