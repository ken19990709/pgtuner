"""hypo — hypothetical index evaluation: let the planner "try out" an index that does not
exist yet, with zero residue.

Mechanism (v0.5 design note §3.2, historical; environment conclusion measured 2026-11):
  - All hypopg versions (including master 2.0.0beta) depend on get_relation_info_hook,
    which upstream removed (commit 91f33a2ae9, replaced by build_simple_rel_hook);
    19devel/20devel fail to compile → the default is the **transaction-rollback
    mechanism**: BEGIN; CREATE INDEX …; EXPLAIN; ROLLBACK. The index is really built
    (the planner sees exactly what production would), the rollback always cleans it,
    and so does a dropped connection — zero residue, zero dependencies, works on all versions.
  - If the target database already has hypopg installed (installed on <=17), the fast
    path is taken automatically (hypopg_create_index, no physical build).

Metrics and safety:
  - Default is plain EXPLAIN (estimated cost; statements are not executed, nothing is
    timed); only --analyze runs EXPLAIN ANALYZE, and the output is labeled as plan-level
    actuals — TPS-level conclusions must still go through bench/rebench for ledger evidence
  - The rollback mechanism really creates indexes on the target database (rolled back
    right after): the target table is briefly locked during index creation; plain EXPLAIN
    does not pollute pg_stat counters (the --analyze path does count scans, harmless on a clone)
  - After each candidate set finishes, a rollback self-check runs: output is only allowed
    through when the database-wide index count is unchanged
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import pg
from .planbench import _doc, _strip_sql, _walk, plan_hash
from .render import oneline

_ERR = 200


@dataclass
class QueryEval:
    """Evaluation of one query under one candidate set (cost = EXPLAIN top-level Total Cost, estimated metric)."""
    sql: str
    base_cost: float | None = None
    hypo_cost: float | None = None
    ratio: float | None = None                  # hypo/base; <1 means an estimated gain
    base_ms: float | None = None                # Execution Time with --analyze (plan-level actual)
    hypo_ms: float | None = None
    base_node: str | None = None                # top node type (Seq Scan/Index Scan…)
    hypo_node: str | None = None
    base_index: str | None = None               # index name used in the plan (None if none)
    hypo_index: str | None = None
    plan_changed: bool = False                  # plan_hash changed
    error: str | None = None


@dataclass
class SetEval:
    """Evaluation of one candidate set (single candidate or combination) against all queries."""
    label: str
    ddls: list[str]
    queries: list[QueryEval] = field(default_factory=list)
    error: str | None = None                    # failure during index creation (whole set skipped)

    @property
    def avg_ratio(self) -> float | None:
        rs = [q.ratio for q in self.queries if q.ratio is not None]
        return sum(rs) / len(rs) if rs else None


def _explain(cur, sql: str, *, analyze: bool) -> tuple[dict, float, float | None]:
    """EXPLAIN (FORMAT JSON[, ANALYZE]) → (document, top-level cost, Execution Time|None)."""
    opts = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
    cur.execute(f"EXPLAIN ({opts}) {_strip_sql(sql)}")
    doc = _doc(cur.fetchone()[0])[0]
    cost = doc["Plan"]["Total Cost"]
    return doc, float(cost), (float(doc["Execution Time"]) if analyze else None)


def _index_in(plan: dict) -> str | None:
    """The first index name used in the plan tree (the top level usually has none; take the first node tree-wide carrying Index Name)."""
    for _, node in _walk(plan):
        if node.get("Index Name"):
            return node["Index Name"]
    return None


def _set_label(ddls: list[str], idx: int, total: int) -> str:
    """Candidate-set label: single candidate idxN (single-line DDL preview); combination set '组合(全部)'."""
    if total > 1 and len(ddls) > 1:
        return f"组合(全部{len(ddls)}个)"
    return f"idx{idx}: {oneline(ddls[0], 53)}"


def eval_candidates(dsn: str, index_defs: list[str], queries: list[str],
                    *, analyze: bool = False) -> dict:
    """Evaluation matrix: baseline + each single candidate + the combination, comparing
    estimated costs query by query.

    Returns {"mechanism": "rollback"|"hypopg", "analyze": bool, "sets": [SetEval…],
    "index_count_before": n, "index_count_after": n}.
    The baseline is not a separate set: each QueryEval's base_* fields are the baseline
    metric (without any hypothetical index).
    Residue self-check: index_count_before == after; when unequal, output is still
    produced but carries a warning field.
    """
    import psycopg2

    if not index_defs:
        raise ValueError("至少给一个 --index")
    if not queries:
        raise ValueError("至少给一条查询(--query / --queries-file)")

    sets_spec: list[tuple[str, list[str]]] = [
        (_set_label([d], i + 1, len(index_defs)), [d]) for i, d in enumerate(index_defs)]
    if len(index_defs) > 1:
        sets_spec.append((_set_label(index_defs, 0, len(index_defs)), list(index_defs)))

    conn = pg.connect_rw(dsn, autocommit=False)          # explicit transaction: the rollback mechanism's cleanup guarantee
    mechanism = "rollback"
    out_sets: list[SetEval] = []
    warn: str | None = None
    try:
        with conn.cursor() as cur:
            hypopg = bool(pg.fetch_one(conn, "SELECT 1 FROM pg_extension WHERE extname = 'hypopg'"))
            if hypopg:
                mechanism = "hypopg"
            n_before = pg.fetch_one(conn, "SELECT count(*) FROM pg_indexes")[0]

            baseline: list[tuple[dict, float, float | None]] = []
            base_err: list[str | None] = []
            for q in queries:
                try:
                    baseline.append(_explain(cur, q, analyze=analyze))
                    base_err.append(None)
                except psycopg2.Error as e:
                    baseline.append(({}, None, None))
                    base_err.append(str(e).splitlines()[0][:_ERR])

            def _apply(ddls: list[str]) -> None:
                """Land the indexes per mechanism (hypopg virtual build / real build pending rollback) — shared by set creation and post-query-failure rebuild."""
                for d in ddls:
                    if mechanism == "hypopg":
                        cur.execute("SELECT hypopg_create_index(%s)", (d,))
                    else:
                        cur.execute(d)

            for label, ddls in sets_spec:
                se = SetEval(label=label, ddls=ddls)
                conn.rollback()                          # clean up the previous set's residue, back outside a clean transaction
                try:
                    _apply(ddls)
                except psycopg2.Error as e:
                    se.error = f"建索引失败: {str(e).splitlines()[0][:_ERR]}"
                    conn.rollback()
                    se.queries = [QueryEval(sql=q) for q in queries]
                    out_sets.append(se)
                    continue
                for qi, q in enumerate(queries):
                    qe = QueryEval(sql=q)
                    bdoc, bcost, bms = baseline[qi]
                    qe.base_cost, qe.base_ms = bcost, bms
                    qe.base_node = bdoc.get("Plan", {}).get("Node Type") if bdoc else None
                    qe.base_index = _index_in(bdoc["Plan"]) if bdoc.get("Plan") else None
                    qe.error = base_err[qi]
                    try:
                        hdoc, hcost, hms = _explain(cur, q, analyze=analyze)
                        qe.hypo_cost, qe.hypo_ms = hcost, hms
                        qe.hypo_node = hdoc["Plan"].get("Node Type")
                        qe.hypo_index = _index_in(hdoc["Plan"])
                        qe.plan_changed = bool(bdoc.get("Plan")) and \
                            plan_hash(bdoc["Plan"]) != plan_hash(hdoc["Plan"])
                        if qe.base_cost:
                            qe.ratio = qe.hypo_cost / qe.base_cost
                    except psycopg2.Error as e:
                        qe.error = (qe.error + " | " if qe.error else "") + \
                            str(e).splitlines()[0][:_ERR]
                        # the rollback cleared the indexes too: rebuild to evaluate this
                        # set's remaining queries; if it fails again, skip the rest
                        try:
                            conn.rollback()
                            _apply(ddls)
                        except psycopg2.Error as e2:
                            qe.error += f" | 索引重建失败,余下查询跳过: {str(e2).splitlines()[0][:80]}"
                            conn.rollback()
                            se.queries.append(qe)
                            for rj in range(qi + 1, len(queries)):
                                rq = QueryEval(sql=queries[rj], error="本集已中止")
                                bdoc2, bcost2, _ = baseline[rj]
                                rq.base_cost, rq.base_node = bcost2, \
                                    (bdoc2.get("Plan", {}).get("Node Type") if bdoc2 else None)
                                se.queries.append(rq)
                            break
                        se.queries.append(qe)
                        continue
                    se.queries.append(qe)
                if mechanism == "hypopg":
                    cur.execute("SELECT hypopg_reset()")
                else:
                    conn.rollback()
                out_sets.append(se)

            n_after = pg.fetch_one(conn, "SELECT count(*) FROM pg_indexes")[0]
            if n_after != n_before:
                warn = f"残留自检失败:索引数 {n_before}→{n_after}(不应发生,请人工核查)"
    finally:
        try:
            conn.rollback()                              # belt and braces: clear the transaction on every exit path
        except Exception:                                # noqa: BLE001
            pass
        conn.close()
    return {"mechanism": mechanism, "analyze": analyze, "sets": out_sets,
            "index_count_before": n_before, "index_count_after": n_after, "warning": warn}


# ---------------------------- Rendering ----------------------------

def render_text(report: dict) -> str:
    """Compact evaluation report chunked per query (plain text, v0.6 data-plane slimming): baseline + one line per candidate set."""

    lines: list[str] = []
    mech = "hypopg 快路径(不物理构建)" if report["mechanism"] == "hypopg" \
        else "事务回滚(建真实索引后回滚,零残留)"
    lines.append(f"机制:{mech};口径:EXPLAIN 估算代价"
                 + (";--analyze 已执行语句,耗时为 plan 级实际值" if report["analyze"] else
                    "(未执行语句);TPS 级结论仍需 bench/rebench 账本证据"))
    first = report["sets"][0].queries if report["sets"] else []
    for qi, q in enumerate(first):
        lines.append(f"Q{qi + 1}: {oneline(q.sql, 46)}")
        for se in report["sets"]:
            qe = se.queries[qi]
            if se.error:
                lines.append(f"  ✗ {se.label}: {se.error}")
                continue
            parts = []
            if qe.base_cost is not None and qe.hypo_cost is not None:
                parts.append(f"{qe.base_cost:.1f}→{qe.hypo_cost:.1f} (×{qe.ratio:.3f})")
            else:
                parts.append("代价不可得")
            if qe.hypo_node and qe.base_node != qe.hypo_node:
                parts.append(f"{qe.base_node}→{qe.hypo_node}")
            if qe.hypo_index and qe.hypo_index != qe.base_index:
                parts.append(f"用上索引 {qe.hypo_index}")
            if qe.hypo_index and qe.hypo_index == qe.base_index:
                parts.append(f"索引未变 {qe.hypo_index}")
            if qe.error:
                parts.append(f"⚠ {qe.error[:60]}")
            mark = "" if not qe.ratio or qe.ratio >= 1 else " ✓"
            lines.append(f"  · {se.label}: {'; '.join(parts)}{mark}")
    if report.get("warning"):
        lines.append(f"⚠ {report['warning']}")
    return "\n".join(lines)


def render_json(report: dict) -> str:
    """Machine-readable JSON (pure JSON on stdout)."""
    def _d(o):
        return o.__dict__
    return json.dumps(report, ensure_ascii=False, default=_d)
