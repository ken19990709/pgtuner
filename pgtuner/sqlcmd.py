"""sql — general read-only SQL execution entry (functional checks / data inspection),
the underlying piece of the cli `sql` subcommand.

Design backing iron rule 1 (see AGENTS.md):
  - Read-only enforced: session-level read-only from pg.connect() (SET SESSION
    CHARACTERISTICS AS TRANSACTION READ ONLY) + autocommit — writes/DDL/
    SELECT FOR UPDATE/nextval are rejected directly by the PG server layer
    (25001), not by tool-side self-restraint; under autocommit each statement is
    its own implicit read-only transaction, so the stacked-queries escape that
    terminates a read-only transaction (the first-generation injection case in
    the MCP ecosystem) does not apply to this model;
  - No timing output: results contain only data and a truncation marker, zero
    elapsed-time numbers — performance evidence only comes from bench/rebench/planbench;
  - Row limit: fetchmany(max_rows+1) probes for truncation (over the limit sets
    truncated and discards row N+1; no COUNT, no full fetch, large result sets
    never land in memory, at the cost of not reporting the total count);
  - statement_timeout: session-level timeout (default 30s); QueryCanceled (57014)
    is converted to a human-readable error;
  - Single statement: the caller validates with sqlsplit (this module executes
    whatever it receives);
  - Fresh connection per call, closed when done: no SET/advisory-lock or other
    session state leaks.

Known boundary (also noted in help): SELECT still advances pg_stat counters
(seq_scan/blks_hit etc.) — do not run heavy exploratory queries against the
database under measurement before a benchmark run; diagnostics should target a
clone (lab clone).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from . import pg

CELL_WIDTH = 120          # max display width of a cell in text rendering (overlong values truncated with …; json mode is lossless)
_NULL_CELL = "[dim]NULL[/dim]"


@dataclass
class QueryResult:
    """Result of one read-only query. truncated=True means the actual row count exceeds max_rows (truncated)."""
    columns: list[str]
    rows: list
    truncated: bool


def run_query(dsn: str | None, statement: str, *, max_rows: int = 100,
              timeout_s: float = 30.0, conn=None) -> QueryResult:
    """Execute a single read-only statement (see module docstring). Errors are unified into RuntimeError, classified by SQLSTATE semantics."""
    import psycopg2
    import psycopg2.errors

    if max_rows < 1:
        raise ValueError(f"max_rows 必须 >= 1: {max_rows}")
    if timeout_s <= 0:
        raise ValueError(f"timeout_s 必须为正: {timeout_s}")
    own = conn is None
    if conn is None:
        conn = pg.connect(dsn)                      # read-only + autocommit
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = '{int(timeout_s * 1000)} ms'")
            cur.execute(statement)
            if cur.description is None:             # no result set (e.g. a bare SET): not an error
                return QueryResult([], [], False)
            cols = [d.name for d in cur.description]
            fetched = cur.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            return QueryResult(cols, list(fetched[:max_rows]), truncated)
    except psycopg2.errors.QueryCanceled:           # 57014
        raise RuntimeError(f"查询超时(>{timeout_s:g}s,statement_timeout 已取消):"
                           f"--timeout 调大或收窄查询范围") from None
    except psycopg2.errors.ReadOnlySqlTransaction:  # 25001
        raise RuntimeError("只读会话拒绝写操作(25001):写/DDL 请走 lab 克隆库"
                           "(lab clone → 克隆上执行 → lab release)") from None
    except psycopg2.Error as e:
        raise RuntimeError(f"SQL 执行失败: {str(e).strip()[:300]}") from None
    finally:
        if own:
            conn.close()


# ---------------------------- rendering (text / json dual format) ----------------------------

def format_cell(value, width: int = CELL_WIDTH) -> str:
    """Cell → display text: None→NULL; bool→t/f (PG style); list/dict (psycopg2's
    automatic deserialization of jsonb columns)→ json.dumps (Python repr's single
    quotes are not valid JSON, so str() is not allowed);
    overlong values truncated with … per display width (render.truncate, CJK double-width)."""
    from .render import truncate
    if value is None:
        return _NULL_CELL
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    else:
        value = str(value)
    return truncate(value, width)


def _plain_cell(value) -> str:
    """Plain-text cell (no rich markup; v0.6 data-plane slimming): None→NULL, jsonb→JSON string."""
    cell = format_cell(value)
    return cell.replace("[dim]", "").replace("[/dim]", "")


def render_text(res: QueryResult) -> str:
    """Result → compact plain text (v0.6 data-plane slimming, bytes ≈ psql; returns
    str, caller does a bare print).

    A single column of strings each starting with indentation (EXPLAIN plans /
    view definitions / function bodies) → emitted verbatim line by line
    (the indentation is the plan hierarchy; putting it into a table or stripping
    whitespace would destroy readability); everything else → render.align_table.
    """
    from .render import align_table

    if (len(res.columns) == 1 and res.rows
            and all(isinstance(r[0], str) and r[0][:1] == " " for r in res.rows)):
        return "\n".join(r[0].rstrip() for r in res.rows)
    headers = res.columns
    rows = [[_plain_cell(v) for v in row] for row in res.rows]
    return align_table(headers, rows)


def render_json(res: QueryResult) -> str:
    """Machine-readable output: fully lossless (cells not truncated); datetime/Decimal via default=str."""
    return json.dumps({"columns": res.columns,
                       "rows": [list(r) for r in res.rows],
                       "row_count": len(res.rows),
                       "truncated": res.truncated},
                      ensure_ascii=False, default=str, indent=1)
