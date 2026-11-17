"""planbench — EXPLAIN workbench: plan baseline / diff / cost-estimate accuracy / plan
flip detection / variant comparison.

The empirical workflow for "change one GUC / add one index — what exactly changed in
the execution plan":
  explain()          capture one plan (ANALYZE/BUFFERS/SETTINGS/WAL optional,
                     plan_cache_mode injectable)
  diff_plans()       align two plans by tree path -> per-node NodeDiff
                     (added/removed/est_shift/timing_shift)
  render_diff()      compact Chinese table of the diff (<=100 columns, changed nodes
                     only)
  accuracy_report()  per-node log2 deviation of est vs actual (0=exact, positive=
                     overestimate), sorted by |log2| descending
  render_accuracy()  compact accuracy table (<=100 columns)
  plan_hash()        plan structure hash (preorder sequence of node type + relation
                     name + index name -> first 8 hex of sha1)
  run_flip_check()   repeatedly execute the same statement under the three
                     plan_cache_mode values auto/force_custom_plan/force_generic_plan, detecting
                     generic/custom plan flips via the structure hash (bind-aware plan
                     flip)
  run_variants()     baseline + setup_sql variants (create index / change GUC),
                     measure per-query median time and diff plans

Contract compliance (the §D of the historical dev contracts): public signatures match the contract,
no DEVIATION. Self-defined behavior where the contract is silent (noted in the
function comments):
  - when est_shift and timing_shift both hit, record est_shift (change is a
    single-value field; the estimate deviation wins)
  - render_diff hides same nodes, keeping only a one-line count summary (compact
    output)
  - run_variants attaches an "error" key to a failed query dict; the keys of diffs
    are (baseline, variant name) pairs

EXPLAIN ANALYZE actually executes the statement (including writes); every entry
point uses a pg.connect_rw read-write connection.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass

from . import pg
from .render import pad, truncate

# The three groups for run_flip_check. Round-5 D5-F12: the values must be PG's
# legal plan_cache_mode spellings — force_custom/force_generic are NOT accepted
# by the server, so the second group's "SET plan_cache_mode = %s" failed on
# every run and the whole flip-check channel died at mode 2.
PLAN_CACHE_MODES = ("auto", "force_custom_plan", "force_generic_plan")
_SHIFT_FACTOR = 2.0      # a row-count/time change must strictly exceed this factor to count as a shift (exactly 2x does not)
_ERR_SNIPPET = 300       # length (chars) of the raw pg error prefix attached to RuntimeError
TABLE_WIDTH = 100        # max terminal column width of rendered tables (East Asian full-width counts as 2)


# ---------------------------- pure functions: SQL/tree walk/numbers ----------------------------

def _strip_sql(sql: str) -> str:
    """Strip trailing semicolons and surrounding whitespace — the statement body is embedded into EXPLAIN .../PREPARE ... AS, where a semicolon is a syntax error."""
    stmt = sql.strip()
    while stmt.endswith(";"):
        stmt = stmt[:-1].rstrip()
    return stmt


def _walk(plan: dict, path: str = "0"):
    """Preorder walk of the plan tree, yielding (path, node). Root = "0", i-th child = parent path + "." + i."""
    yield path, plan
    for i, child in enumerate(plan.get("Plans") or []):
        yield from _walk(child, f"{path}.{i}")


def _path_sort_key(path: str) -> tuple[int, ...]:
    """Sort paths by numeric segments ("0.10" sorts after "0.2"; lexicographic order gets it wrong)."""
    return tuple(int(seg) for seg in path.split("."))


def _node_metrics(node: dict) -> tuple[float | None, float | None, float | None]:
    """(Plan Rows, Actual Rows, Actual Total Time) — key names follow real PG JSON
    output; the Actual* series only exists in ANALYZE output, defaulting to None."""
    return node.get("Plan Rows"), node.get("Actual Rows"), node.get("Actual Total Time")


def _ratio(est: float | None, actual: float | None) -> float | None:
    """est/actual; either missing or actual=0 (undefined) -> None."""
    if est is None or actual is None or actual == 0:
        return None
    return est / actual


def _shifted(a: float | None, b: float | None) -> bool:
    """a->b change strictly exceeding 2x; 0<->non-0 counts as a flip-level change (the ratio is undefined but it is clearly structural)."""
    if a is None or b is None or a == b:
        return False
    if a == 0 or b == 0:
        return True
    return max(a, b) / min(a, b) > _SHIFT_FACTOR


# ---------------------------- plan diff ----------------------------

@dataclass
class NodeDiff:
    path: str                      # tree path, e.g. "0.1.0" (root="0", sibling ordinal)
    node_type: str
    change: str                    # added|removed|est_shift|timing_shift|same
    est_rows_a: float | None = None
    est_rows_b: float | None = None
    actual_rows_a: float | None = None
    actual_rows_b: float | None = None
    ratio: float | None = None     # est/actual (b side; added/removed use whichever side exists)
    time_ms_a: float | None = None
    time_ms_b: float | None = None


def diff_plans(plan_a: dict, plan_b: dict) -> list[NodeDiff]:
    """Align two plans by tree path and output per-node differences (in path order,
    parents before children).

    - path only in b -> added; only in a -> removed
    - in both with different Node Type -> the node was semantically replaced: old
      removed + new added emitted as a pair
    - in both with the same type -> est_shift (Plan Rows changed >2x) /
      timing_shift (Actual Total Time changed >2x); when both hit, record est_shift
      (change is a single-value field; the estimate deviation wins); otherwise same
    - ratio is always est/actual (b side; added/removed nodes use whichever single
      side exists, None when uncomputable)
    """
    a = dict(_walk(plan_a))
    b = dict(_walk(plan_b))
    out: list[NodeDiff] = []
    for path in sorted(set(a) | set(b), key=_path_sort_key):
        if path not in a:                       # only on the b side -> new node
            node = b[path]
            est, actual, t = _node_metrics(node)
            out.append(NodeDiff(path, node.get("Node Type", "?"), "added",
                                est_rows_b=est, actual_rows_b=actual, time_ms_b=t,
                                ratio=_ratio(est, actual)))
            continue
        if path not in b:                       # only on the a side -> vanished node
            node = a[path]
            est, actual, t = _node_metrics(node)
            out.append(NodeDiff(path, node.get("Node Type", "?"), "removed",
                                est_rows_a=est, actual_rows_a=actual, time_ms_a=t,
                                ratio=_ratio(est, actual)))
            continue
        na, nb = a[path], b[path]
        est_a, act_a, t_a = _node_metrics(na)
        est_b, act_b, t_b = _node_metrics(nb)
        if na.get("Node Type") != nb.get("Node Type"):
            out.append(NodeDiff(path, na.get("Node Type", "?"), "removed",
                                est_rows_a=est_a, actual_rows_a=act_a, time_ms_a=t_a,
                                ratio=_ratio(est_a, act_a)))
            out.append(NodeDiff(path, nb.get("Node Type", "?"), "added",
                                est_rows_b=est_b, actual_rows_b=act_b, time_ms_b=t_b,
                                ratio=_ratio(est_b, act_b)))
            continue
        change = "same"
        if _shifted(est_a, est_b):
            change = "est_shift"
        elif _shifted(t_a, t_b):
            change = "timing_shift"
        out.append(NodeDiff(path, nb.get("Node Type", "?"), change,
                            est_rows_a=est_a, est_rows_b=est_b,
                            actual_rows_a=act_a, actual_rows_b=act_b,
                            ratio=_ratio(est_b, act_b),
                            time_ms_a=t_a, time_ms_b=t_b))
    return out


@dataclass
class NodeAccuracy:
    path: str
    node_type: str
    est_rows: float
    actual_rows: float
    ratio: float
    log2_error: float              # log2(est/actual): 0=exact, positive=overestimate, negative=underestimate


def accuracy_report(plan: dict) -> list[NodeAccuracy]:
    """Per-node estimate accuracy, sorted by |log2_error| descending (worst first; the
    sort is stable, equal deviations keep tree-path order).

    Actual Rows exists only in ANALYZE output and is the "per-loop average" — the
    same per-execution scope as Plan Rows's estimate, so the value is taken directly
    without multiplying by Actual Loops (multiply yourself if you need totals).
    Nodes missing Actual Rows (non-ANALYZE plan) or with actual=0 (log2 undefined)
    are skipped.
    """
    rows: list[NodeAccuracy] = []
    for path, node in _walk(plan):
        est, actual = node.get("Plan Rows"), node.get("Actual Rows")
        ratio = _ratio(est, actual)
        if ratio is None:
            continue
        rows.append(NodeAccuracy(path, node.get("Node Type", "?"),
                                 float(est), float(actual), ratio, math.log2(ratio)))
    rows.sort(key=lambda r: abs(r.log2_error), reverse=True)
    return rows


# ---------------------------- plan structure hash (for flip detection) ----------------------------

def plan_hash(plan: dict) -> str:
    """Structure hash: preorder-serialize (Node Type, Relation Name, Index Name)
    triples, concatenating "type|relation|index" per node -> first 8 hex digits of
    sha1.

    Shape only, no numbers: changes in row counts/costs/timings/block counts are not
    flips; any change in node type, relation name, index name, child count or order
    changes the hash.
    """
    parts: list[str] = []

    def walk(node: dict) -> None:
        parts.append("|".join((str(node.get("Node Type") or ""),
                               str(node.get("Relation Name") or ""),
                               str(node.get("Index Name") or ""))))
        for child in node.get("Plans") or []:
            walk(child)

    walk(plan)
    return hashlib.sha1(";".join(parts).encode()).hexdigest()[:8]


# ---------------------------- compact Chinese-table rendering (<=100 columns) ----------------------------

def _pad(s: str, width: int) -> str:
    """Right-pad with spaces to the display width; over-width values are first
    truncated with a "…" suffix (a combination of render.pad/truncate, keeping whole
    lines within TABLE_WIDTH; the double-width character criterion shares the source
    with render.display_width)."""
    return pad(truncate(s, width), width)


def _fmt_num(v: float | None) -> str:
    """Compact numbers: None->"-"; integral values carry no decimal point; the rest use 3 significant digits."""
    if v is None:
        return "-"
    f = float(v)
    if f == int(f):
        return str(int(f))
    return f"{f:.3g}"


def _fmt_pair(a: float | None, b: float | None) -> str:
    return f"{_fmt_num(a)}→{_fmt_num(b)}"


_DIFF_COLUMNS = (
    ("路径", 9), ("节点", 20), ("变更", 12),
    ("est行 a→b", 15), ("实际行 a→b", 15), ("比值", 8), ("耗时ms a→b", 13),
)   # column widths total 92 + 6 single-space separators = 98 <= 100


def render_diff(diffs: list[NodeDiff]) -> str:
    """Compact Chinese table of the diff (terminal-friendly, agent-readable, line
    width <=100 columns).

    Only changed nodes are listed; same nodes are not expanded row by row but
    summarized in a trailing count line — so a large plan's diff stays scannable.
    """
    lines = [" ".join(_pad(label, w) for label, w in _DIFF_COLUMNS).rstrip()]
    same = 0
    for d in diffs:
        if d.change == "same":
            same += 1
            continue
        cells = (d.path, d.node_type, d.change,
                 _fmt_pair(d.est_rows_a, d.est_rows_b),
                 _fmt_pair(d.actual_rows_a, d.actual_rows_b),
                 _fmt_num(d.ratio),
                 _fmt_pair(d.time_ms_a, d.time_ms_b))
        lines.append(" ".join(_pad(c, w)
                              for c, (_, w) in zip(cells, _DIFF_COLUMNS)).rstrip())
    if len(lines) == 1:
        lines.append("(无差异)")
    if same:
        lines.append(f"(其余 {same} 个节点无变化)")
    return "\n".join(lines)


_ACC_COLUMNS = (
    ("路径", 9), ("节点", 22), ("est行", 12), ("实际行", 12), ("est/实际", 10), ("log2偏差", 10),
)   # column widths total 75 + 5 single-space separators = 80 <= 100


def render_accuracy(rows: list[NodeAccuracy], *, top: int = 15) -> str:
    """Compact Chinese accuracy table (line width <=100 columns): takes the first top rows after accuracy_report's sort."""
    shown = rows[:top]
    lines = [f"代价估算偏差(按 |log2| 降序,共 {len(rows)} 节点,显示 {len(shown)}):"]
    if not shown:
        lines.append("(无 ANALYZE 行数数据)")
        return "\n".join(lines)
    lines.append(" ".join(_pad(label, w) for label, w in _ACC_COLUMNS).rstrip())
    for r in shown:
        cells = (r.path, r.node_type, _fmt_num(r.est_rows), _fmt_num(r.actual_rows),
                 _fmt_num(r.ratio), f"{r.log2_error:+.2f}")
        lines.append(" ".join(_pad(c, w)
                              for c, (_, w) in zip(cells, _ACC_COLUMNS)).rstrip())
    return "\n".join(lines)


# ---------------------------- EXPLAIN capture (read-write connection) ----------------------------

def explain(dsn: str | None, sql: str, *, analyze: bool = True, buffers: bool = True,
            settings: bool = True, wal: bool = True,
            plan_cache_mode: str | None = None) -> dict:
    """Run EXPLAIN on a single SQL statement and return json.loads(...)[0]["Plan"]
    (the plan tree).

    - ANALYZE actually executes the statement (including writes) -> pg.connect_rw
      read-write connection, used once and closed. Without ANALYZE the statement is
      only planned, so the connection is pg.connect (session-level read-only) —
      the write surface shrinks to the analyze=True paths (R1 hardening).
    - trailing semicolons/whitespace stripped from the SQL (a statement body
      embedded in EXPLAIN must not carry a semicolon)
    - non-empty plan_cache_mode -> injected first via a session-level SET
      (auto/force_custom_plan/force_generic_plan, for contrasting custom/generic
      plans); value passed as a parameter, no concatenation injection surface
    - the BUFFERS/WAL options require ANALYZE; keeping them with ANALYZE off is
      rejected by pg (re-raised as RuntimeError as-is)
    - psycopg2.Error -> RuntimeError with the first 300 chars of the original error
      (pg's DETAIL context is in there)
    """
    import psycopg2

    stmt = _strip_sql(sql)
    # R2-D guards: EXPLAIN ANALYZE really executes the statement — COPY … TO PROGRAM
    # / side-effect functions must not reach that point, and the plannable heads are
    # the DML/select family (utility statements are not valid EXPLAIN subjects anyway;
    # planbench's deliberate DDL channel is --variant, which has its own --db gate).
    from .sqlsplit import find_side_effect_calls, statement_head
    head = statement_head(stmt)
    if head not in ("select", "with", "values", "table", "insert", "update", "delete", "merge"):
        raise RuntimeError(f"EXPLAIN 只接受查询/DML 语句头(select/with/values/table/insert/update/delete/merge),得到 {head!r}")
    if analyze:
        calls = find_side_effect_calls(stmt, surface="analyze")
        if calls:
            raise RuntimeError(f"EXPLAIN ANALYZE 拒绝副作用函数: {', '.join(calls)}(会真执行且不可回滚)")
    opts = [name for name, on in (("ANALYZE", analyze), ("BUFFERS", buffers),
                                  ("SETTINGS", settings), ("WAL", wal)) if on]
    opts.append("FORMAT JSON")
    conn = pg.connect_rw(dsn) if analyze else pg.connect(dsn)   # planning alone needs no write access
    try:
        with conn.cursor() as cur:
            if plan_cache_mode:
                cur.execute("SET plan_cache_mode = %s", (plan_cache_mode,))
            cur.execute(f"EXPLAIN ({', '.join(opts)}) {stmt}")
            raw = cur.fetchone()[0]
    except psycopg2.Error as e:
        raise RuntimeError(f"EXPLAIN 失败: {str(e)[:_ERR_SNIPPET]}") from None
    finally:
        conn.close()
    return _doc_plan(raw)


def _doc(raw) -> list:
    """The cursor column of EXPLAIN (FORMAT JSON) -> the document list.

    psycopg2>=2.5 **auto-deserializes** json/jsonb columns into Python objects (B2
    acceptance defect D2: calling json.loads again necessarily raises TypeError); old
    drivers/hand-written FakeCursors may give str. Both forms are accepted."""
    return raw if isinstance(raw, (list, dict)) else json.loads(raw)


def _doc_plan(raw) -> dict:
    """Document list -> the [0]["Plan"] plan tree."""
    return _doc(raw)[0]["Plan"]


def run_flip_check(dsn: str | None, sql: str, *, runs: int = 20) -> dict:
    """Execute the same statement runs times under each of the three plan_cache_mode
    values, detecting execution-plan flips.

    Only **parameter-less SQL** is supported: the implementation is `PREPARE pN AS
    <sql>` (no typed parameter list); statements with $1 placeholders error at
    PREPARE. EXPLAIN ANALYZE EXECUTE executes the statement -> read-write
    connection.

    Each mode round uses its own statement name p0/p1/p2: re-PREPARING the same name
    raises DuplicatePreparedStatement, so switching names avoids an inter-round
    DEALLOCATE ALL (and does not disturb the session's other statements — better
    defense).

    Returns {"auto": {"flips": n, "hashes": [...]}, "force_custom_plan": {...},
    "force_generic_plan": {...}}; flips = deduplicated hash count - 1 (0 = the plan is
    fully stable). The hash comes from plan_hash() (structure only, not
    rows/timings).
    """
    import psycopg2

    stmt = _strip_sql(sql)
    conn = pg.connect_rw(dsn)
    out: dict = {}
    try:
        with conn.cursor() as cur:
            for idx, mode in enumerate(PLAN_CACHE_MODES):
                pname = f"p{idx}"            # a fresh name p0/p1/p2 per round, see docstring
                cur.execute("SET plan_cache_mode = %s", (mode,))
                cur.execute(f"PREPARE {pname} AS {stmt}")
                hashes = []
                for _ in range(runs):
                    cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE {pname}")
                    hashes.append(plan_hash(_doc_plan(cur.fetchone()[0])))
                out[mode] = {"flips": len(set(hashes)) - 1, "hashes": hashes}
    except psycopg2.Error as e:
        raise RuntimeError(f"flip check 失败: {str(e)[:_ERR_SNIPPET]}") from None
    finally:
        conn.close()
    return out


# ---------------------------- variant comparison ----------------------------

@dataclass
class Variant:
    """One experiment variant: a name + the setup_sql that applies its DDL/GUC.

    The baseline convention is name="baseline", setup_sql="" (measure the status quo
    only); setup_sql may contain multiple statements (semicolon-separated, one
    execute commits under autocommit). Variant names must be unique (diff keys are
    name pairs).
    """
    name: str
    setup_sql: str = ""


def run_variants(dsn: str | None, queries: list[str], variants: list[Variant],
                 *, runs: int = 3) -> dict:
    """Per variant, measure each query's median execution time and keep the plan,
    then diff against baseline per query.

    - baseline is always first: when variants lack name="baseline" one is auto-built
      and prepended; if present but not first, it is moved up
    - each variant opens a fresh read-write connection (isolating session state);
      the setup's database-level DDL takes global effect in variant order — variants
      that should roll back write their own DROP-style setup
    - a failed setup records entry["error"] and **still runs the queries** (typical
      case: re-creating an index reports "already exists", but the index is really
      there and the plan really changed, still worth measuring)
    - per query: EXPLAIN (ANALYZE, FORMAT JSON) runs times, taking the median of the
      top-level "Execution Time" (the whole statement's time — note, not the node's
      Actual Total Time); the last successful plan is kept; a failure stops that
      query and records query["error"] (already-successful timings still take the
      median)
    - diffs: baseline paired with each other variant, key (baseline name, variant
      name) -> {query index: [NodeDiff]}; indexes where either side's plan is
      missing (that query failed) do not appear

    Returns {"variants": [{"name", "error"?, "queries": [{"sql","median_ms","plan","error"?}]}],
           "diffs": {...}}.
    """
    import psycopg2

    if any(v.name == "baseline" for v in variants):
        variants = ([v for v in variants if v.name == "baseline"]
                    + [v for v in variants if v.name != "baseline"])
    else:
        variants = [Variant("baseline", ""), *variants]

    out_variants: list[dict] = []
    plans: dict[str, list[dict | None]] = {}       # variant name -> per-query plan (None on failure)
    for v in variants:
        # ddl is ledgered with the variant: state-changing setup statements must
        # enter the ledger (self-describing evidence chain; S8 found old ledgers
        # stored only variant name + timings, with no way to re-check what DDL had
        # been run)
        entry: dict = {"name": v.name, "ddl": v.setup_sql, "queries": []}
        plans[v.name] = [None] * len(queries)
        conn = pg.connect_rw(dsn)                  # fresh connection per variant, see docstring
        try:
            if v.setup_sql.strip():
                try:
                    with conn.cursor() as cur:
                        cur.execute(v.setup_sql)
                except psycopg2.Error as e:
                    entry["error"] = str(e)[:_ERR_SNIPPET]
            for qi, query in enumerate(queries):
                stmt = _strip_sql(query)
                q: dict = {"sql": query, "median_ms": None, "plan": None}
                times: list[float] = []
                try:
                    with conn.cursor() as cur:
                        for _ in range(runs):
                            cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {stmt}")
                            doc0 = _doc(cur.fetchone()[0])[0]
                            if doc0.get("Execution Time") is not None:
                                times.append(doc0["Execution Time"])
                            q["plan"] = doc0.get("Plan")
                except psycopg2.Error as e:
                    q["error"] = str(e)[:_ERR_SNIPPET]
                if times:
                    q["median_ms"] = statistics.median(times)
                    plans[v.name][qi] = q["plan"]
                entry["queries"].append(q)
        finally:
            conn.close()
        out_variants.append(entry)

    diffs: dict = {}
    base_name = out_variants[0]["name"]            # baseline is always first (guaranteed above)
    base_plans = plans[base_name]
    for other in out_variants[1:]:
        pair: dict = {}
        for qi in range(len(queries)):
            pa, pb = base_plans[qi], plans[other["name"]][qi]
            if pa is not None and pb is not None:
                pair[qi] = diff_plans(pa, pb)
        diffs[(base_name, other["name"])] = pair
    return {"variants": out_variants, "diffs": diffs}
