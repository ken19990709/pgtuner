"""planbench tests: plan-tree diff / accuracy sorting / structure-hash pure functions + mocked-connection
EXPLAIN / flip check / variant orchestration (no running PG needed).

Fixture field names follow real PG EXPLAIN (ANALYZE, FORMAT JSON) output:
node row counts Plan Rows / Actual Rows / Actual Loops, node timing Actual Total Time;
the top-level dict additionally has the three keys Plan / Planning Time / Execution Time (whole-statement time).
"""
import hashlib
import json
import unicodedata

import psycopg2
import pytest

from pgtuner import planbench
from pgtuner.planbench import Variant


# ---------------------------- synthetic EXPLAIN JSON fixtures ----------------------------

def _node(node_type, *, analyze=True, **over):
    """Synthetic plan node: by default the common Seq Scan on t shape; over overrides/appends fields."""
    n = {
        "Node Type": node_type,
        "Parallel Aware": False,
        "Relation Name": "t",
        "Alias": "t",
        "Startup Cost": 0.0,
        "Total Cost": 1541.0,
        "Plan Rows": 100,
        "Plan Width": 4,
        "Plans": [],
    }
    if analyze:
        n.update({
            "Actual Startup Time": 0.015,
            "Actual Total Time": 10.0,
            "Actual Rows": 1000,
            "Actual Loops": 1,
            "Shared Hit Blocks": 845,
            "Shared Read Blocks": 0,
        })
    n.update(over)
    return n


def _doc_json(plan, *, execution_ms=1.0):
    """Synthetic raw cursor.fetchone()[0]: the single-row single-column JSON text of EXPLAIN (ANALYZE, FORMAT JSON)."""
    return json.dumps([{"Plan": plan, "Planning Time": 0.05,
                        "Execution Time": execution_ms}])


# ---------------------------- FakeConn: execute records SQL / fetchone drains a queue ----------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append(sql)
        if self._conn.fail_on and self._conn.fail_on in sql:
            raise psycopg2.ProgrammingError(self._conn.fail_msg)

    def fetchone(self):
        return (self._conn.results.pop(0),)


class FakeConn:
    """The cursor's execute records SQL (including the placeholder form of parameterized SET); fetchone returns the results queue in order."""

    def __init__(self, results=(), fail_on=None, fail_msg="mock pg error"):
        self.executed = []
        self.results = list(results)
        self.fail_on = fail_on
        self.fail_msg = fail_msg
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def _disp_width(s):
    """Terminal display width (East Asian full-width counts as 2 columns) — same width criterion as planbench's rendering."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


# ---------------------------- diff_plans ----------------------------

def test_diff_plans_type_change_removed_added_pair():
    a = _node("Seq Scan")
    b = _node("Bitmap Heap Scan", Plans=[
        _node("Bitmap Index Scan", **{"Index Name": "t_a_idx"})])
    diffs = planbench.diff_plans(a, b)
    # root path type changed → old removed + new added as a pair; b-side child → added @ "0.0"
    assert [(d.path, d.change, d.node_type) for d in diffs] == [
        ("0", "removed", "Seq Scan"),
        ("0", "added", "Bitmap Heap Scan"),
        ("0.0", "added", "Bitmap Index Scan"),
    ]
    added = diffs[2]
    assert added.est_rows_a is None and added.est_rows_b == 100     # one-sided fields
    assert added.actual_rows_a is None and added.actual_rows_b == 1000
    # reverse diff: a-side-only child → removed @ "0.0"
    rev = planbench.diff_plans(b, a)
    assert (rev[-1].path, rev[-1].change, rev[-1].node_type) == ("0.0", "removed", "Bitmap Index Scan")


def test_diff_plans_est_shift_and_ratio():
    b = _node("Seq Scan", **{"Plan Rows": 10000})     # 100→10000, >2x
    (d,) = planbench.diff_plans(_node("Seq Scan"), b)
    assert d.change == "est_shift"
    assert d.est_rows_a == 100 and d.est_rows_b == 10000
    assert d.actual_rows_a == d.actual_rows_b == 1000
    assert d.ratio == pytest.approx(10.0)             # est/actual, using the b side: 10000/1000
    assert d.time_ms_a == d.time_ms_b == 10.0         # timing unchanged


def test_diff_plans_timing_shift():
    b = _node("Seq Scan", **{"Actual Total Time": 45.0})    # 10→45, >2x
    (d,) = planbench.diff_plans(_node("Seq Scan"), b)
    assert d.change == "timing_shift"
    assert d.time_ms_a == 10.0 and d.time_ms_b == 45.0
    assert d.est_rows_a == d.est_rows_b == 100          # row counts unchanged


def test_diff_plans_twox_boundary_is_same():
    # exactly 2x (100→200 / 10→20) does not count as a shift (strictly greater); ratio is still filled in
    b = _node("Seq Scan", **{"Plan Rows": 200, "Actual Total Time": 20.0})
    (d,) = planbench.diff_plans(_node("Seq Scan"), b)
    assert d.change == "same"
    assert d.ratio == pytest.approx(0.2)


def test_diff_plans_zero_to_nonzero_is_shift_and_est_wins():
    b = _node("Seq Scan", **{"Plan Rows": 0, "Actual Total Time": 99.0})
    (d,) = planbench.diff_plans(_node("Seq Scan"), b)
    assert d.change == "est_shift"          # 0↔non-0 counts as a structural change; when timing hits too, est wins
    assert d.est_rows_a == 100 and d.est_rows_b == 0
    assert d.time_ms_a == 10.0 and d.time_ms_b == 99.0
    assert d.ratio == 0.0                   # est=0/actual=1000 is defined → 0.0; None only when the field is missing or actual=0


def test_diff_plans_path_order_numeric():
    # multi-child paths sorted by numeric segments: 0.0/0.1/0.2 (b side swaps the second child's type)
    a = _node("Nested Loop", Plans=[
        _node("Seq Scan", **{"Relation Name": "a"}),
        _node("Seq Scan", **{"Relation Name": "b"}),
    ])
    b = _node("Nested Loop", Plans=[
        _node("Seq Scan", **{"Relation Name": "a"}),
        _node("Index Scan", **{"Relation Name": "b", "Index Name": "b_idx"}),
    ])
    diffs = planbench.diff_plans(a, b)
    assert [d.path for d in diffs] == ["0", "0.0", "0.1", "0.1"]
    assert [d.change for d in diffs] == ["same", "same", "removed", "added"]


# ---------------------------- accuracy_report ----------------------------

def test_accuracy_report_sorted_by_abs_log2():
    plan = _node("Nested Loop", **{"Plan Rows": 1, "Actual Rows": 1}, Plans=[
        _node("Seq Scan", **{"Relation Name": "a"}),                       # 100/1000 → log2=-3.32
        _node("Index Scan", **{"Index Name": "t_pkey",
                               "Plan Rows": 1, "Actual Rows": 1}),          # 1/1 → 0
        _node("Sort", **{"Plan Rows": 8000, "Actual Rows": 1000}),          # 8 → +3
    ])
    rows = planbench.accuracy_report(plan)
    assert [r.path for r in rows] == ["0.0", "0.2", "0", "0.1"]   # descending |log2|; ties stably keep path order
    assert rows[0].ratio == pytest.approx(0.1)
    assert rows[0].log2_error == pytest.approx(-3.3219, abs=1e-4)  # negative = underestimate (log2 0.1)
    assert rows[1].log2_error == pytest.approx(3.0)               # positive = overestimate
    assert rows[2].log2_error == 0.0 and rows[3].log2_error == 0.0


def test_accuracy_report_skips_missing_actual_and_zero():
    # non-ANALYZE plans (no Actual Rows) and actual=0 (log2 undefined) produce no rows
    no_analyze = _node("Seq Scan", analyze=False, **{"Plan Rows": 50})
    assert planbench.accuracy_report(no_analyze) == []
    zero_actual = _node("Seq Scan", **{"Actual Rows": 0})
    assert planbench.accuracy_report(zero_actual) == []


# ---------------------------- plan_hash ----------------------------

def test_plan_hash_known_vector_and_stability():
    base = _node("Seq Scan")
    # serialization format locked: pre-order "Node Type|Relation Name|Index Name" joined per node with semicolons → first 8 hex of sha1
    assert planbench.plan_hash(base) == hashlib.sha1(b"Seq Scan|t|").hexdigest()[:8]
    assert planbench.plan_hash(base) == planbench.plan_hash(_node("Seq Scan"))
    # irrelevant fields such as row counts/cost/timing/block counts do not affect it
    noisy = _node("Seq Scan", **{"Plan Rows": 999, "Actual Rows": 1,
                                 "Total Cost": 9.9, "Shared Hit Blocks": 5})
    assert planbench.plan_hash(noisy) == planbench.plan_hash(base)


def test_plan_hash_sensitive_to_structure():
    base = _node("Seq Scan")
    h = planbench.plan_hash
    idx_scan = h(_node("Index Scan", **{"Index Name": "t_a_idx"}))
    other_rel = h(_node("Seq Scan", **{"Relation Name": "u"}))
    assert idx_scan != h(base)                            # node type / index name changed
    assert other_rel != h(base)                           # relation name changed
    c1 = _node("Seq Scan", **{"Relation Name": "a"})
    c2 = _node("Seq Scan", **{"Relation Name": "b"})
    assert h(_node("Nested Loop", Plans=[c1, c2])) != h(_node("Nested Loop", Plans=[c2, c1]))
    assert len(h(base)) == 8                              # first 8 hex of sha1


# ---------------------------- explain() ----------------------------

def test_explain_returns_plan_strips_sql(monkeypatch):
    conn = FakeConn([_doc_json(_node("Seq Scan"))])
    monkeypatch.setattr(planbench.pg, "connect_rw", lambda dsn=None, autocommit=True: conn)
    plan = planbench.explain("host=/tmp dbname=x", "SELECT 1;\n  ")
    assert plan["Node Type"] == "Seq Scan"
    # trailing semicolon stripped + full option set; ANALYZE uses the read-write connection (what is verified here is the SQL orchestration passed to the mock)
    assert conn.executed == ["EXPLAIN (ANALYZE, BUFFERS, SETTINGS, WAL, FORMAT JSON) SELECT 1"]
    assert conn.closed


def test_explain_plan_cache_mode_and_pg_error(monkeypatch):
    conn = FakeConn([_doc_json(_node("Seq Scan"))])
    monkeypatch.setattr(planbench.pg, "connect_rw", lambda dsn=None, autocommit=True: conn)
    plan = planbench.explain(None, "SELECT 1", plan_cache_mode="force_generic_plan")
    assert plan["Relation Name"] == "t"
    assert conn.executed[0] == "SET plan_cache_mode = %s"          # SET first, then EXPLAIN
    assert conn.executed[1].startswith("EXPLAIN (ANALYZE,")

    # psycopg2.Error → RuntimeError carrying the first 300 chars of the original error
    err = FakeConn([], fail_on="EXPLAIN", fail_msg='syntax error at or near "FROM"')
    monkeypatch.setattr(planbench.pg, "connect_rw", lambda dsn=None, autocommit=True: err)
    with pytest.raises(RuntimeError, match="syntax error"):
        planbench.explain(None, "SELECT FROM")

    long_err = FakeConn([], fail_on="EXPLAIN", fail_msg="E" * 500)
    monkeypatch.setattr(planbench.pg, "connect_rw", lambda dsn=None, autocommit=True: long_err)
    with pytest.raises(RuntimeError) as ei:
        planbench.explain(None, "SELECT 1")
    # the original error is truncated to the first 300 chars (1 extra E comes from the prefix "EXPLAIN 失败: ")
    assert str(ei.value).count("E") == 301


# ---------------------------- run_flip_check ----------------------------

def test_plan_cache_modes_are_legal_pg_values():
    """Round-5 D5-F12: the flip-check candidates must be PostgreSQL's legal
    plan_cache_mode spellings — force_custom/force_generic were refused by the
    server, so the second group's SET failed and the channel died at mode 2."""
    assert planbench.PLAN_CACHE_MODES == ("auto", "force_custom_plan", "force_generic_plan")


def test_run_flip_check_hashes_flips_and_naming(monkeypatch):
    seq = _node("Seq Scan")
    idx = _node("Index Scan", **{"Index Name": "t_a_idx"})
    seq_doc, idx_doc = _doc_json(seq), _doc_json(idx)
    # auto: all seq ×3; force_custom_plan: seq/idx/seq (1 flip); force_generic_plan: seq/idx/idx (1 flip)
    conn = FakeConn([seq_doc] * 3 + [seq_doc, idx_doc, seq_doc] + [seq_doc, idx_doc, idx_doc])
    monkeypatch.setattr(planbench.pg, "connect_rw", lambda dsn=None, autocommit=True: conn)

    out = planbench.run_flip_check(None, "SELECT * FROM t WHERE a = 1;\n", runs=3)
    assert out["auto"]["flips"] == 0
    assert out["auto"]["hashes"] == [planbench.plan_hash(seq)] * 3
    assert out["force_custom_plan"]["flips"] == 1 and len(out["force_custom_plan"]["hashes"]) == 3
    assert out["force_generic_plan"]["flips"] == 1
    assert len(set(out["force_generic_plan"]["hashes"])) == 2

    # SQL orchestration: each round SET→PREPARE (names p0/p1/p2) → EXECUTE × runs; the PREPARE body drops the trailing semicolon
    e = conn.executed
    assert e[0] == "SET plan_cache_mode = %s"
    assert e[1] == "PREPARE p0 AS SELECT * FROM t WHERE a = 1"
    assert e[2:5] == ["EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE p0"] * 3
    assert e[5] == "SET plan_cache_mode = %s"
    assert e[6] == "PREPARE p1 AS SELECT * FROM t WHERE a = 1"
    assert e[7:10] == ["EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE p1"] * 3
    assert e[11] == "PREPARE p2 AS SELECT * FROM t WHERE a = 1"
    assert e[12:15] == ["EXPLAIN (ANALYZE, FORMAT JSON) EXECUTE p2"] * 3
    assert conn.closed


def test_run_flip_check_pg_error_becomes_runtime(monkeypatch):
    conn = FakeConn([], fail_on="PREPARE", fail_msg="syntax error in prepared statement")
    monkeypatch.setattr(planbench.pg, "connect_rw", lambda dsn=None, autocommit=True: conn)
    with pytest.raises(RuntimeError, match="prepared"):
        planbench.run_flip_check(None, "SELECT $1", runs=1)   # a parameterized statement fails at PREPARE, converted to RuntimeError


# ---------------------------- run_variants ----------------------------

def test_run_variants_median_baseline_autofill_and_diffs(monkeypatch):
    seq, idx = _node("Seq Scan"), _node("Index Scan", **{"Index Name": "t_a_idx"})
    # one connection queue per variant: baseline (auto-filled) idx variant: setup creates the index, query0 becomes an Index Scan
    baseline_conn = FakeConn([_doc_json(seq, execution_ms=10), _doc_json(seq, execution_ms=20),
                              _doc_json(seq, execution_ms=30),
                              _doc_json(seq, execution_ms=100), _doc_json(seq, execution_ms=110),
                              _doc_json(seq, execution_ms=120)])
    idx_conn = FakeConn([_doc_json(idx, execution_ms=5), _doc_json(idx, execution_ms=7),
                         _doc_json(idx, execution_ms=9),
                         _doc_json(seq, execution_ms=1), _doc_json(seq, execution_ms=2),
                         _doc_json(seq, execution_ms=3)])
    conns = [baseline_conn, idx_conn]
    monkeypatch.setattr(planbench.pg, "connect_rw", lambda dsn=None, autocommit=True: conns.pop(0))

    result = planbench.run_variants(None, ["SELECT 1;", "SELECT 2"],
                                    [Variant("idx", "CREATE INDEX ON t(a)")], runs=3)
    # baseline auto-filled first; median taken from the top-level Execution Time
    assert [v["name"] for v in result["variants"]] == ["baseline", "idx"]
    v0, v1 = result["variants"]
    assert v0["queries"][0]["median_ms"] == 20.0        # median(10,20,30)
    assert v1["queries"][0]["median_ms"] == 7.0         # median(5,7,9)
    assert v0["queries"][1]["median_ms"] == 110.0       # median(100,110,120)
    assert v1["queries"][1]["median_ms"] == 2.0
    assert v1["queries"][0]["plan"]["Index Name"] == "t_a_idx"   # keeps the last plan
    assert v0["queries"][1]["plan"] == v1["queries"][1]["plan"]  # same plan → diff all same
    # connection orchestration: baseline has no setup; idx does setup first then per-query EXPLAIN ×3; closed when done
    assert all(c.closed for c in (baseline_conn, idx_conn))
    assert baseline_conn.executed == ["EXPLAIN (ANALYZE, FORMAT JSON) SELECT 1"] * 3 \
        + ["EXPLAIN (ANALYZE, FORMAT JSON) SELECT 2"] * 3
    assert idx_conn.executed[0] == "CREATE INDEX ON t(a)"
    assert idx_conn.executed[1:] == ["EXPLAIN (ANALYZE, FORMAT JSON) SELECT 1"] * 3 \
        + ["EXPLAIN (ANALYZE, FORMAT JSON) SELECT 2"] * 3
    # diff: only the baseline↔idx pair; query0 structure swapped (removed+added), query1 unchanged
    diffs = result["diffs"]
    assert list(diffs) == [("baseline", "idx")]
    assert {(d.change, d.node_type) for d in diffs[("baseline", "idx")][0]} \
        == {("removed", "Seq Scan"), ("added", "Index Scan")}
    assert all(d.change == "same" for d in diffs[("baseline", "idx")][1])


def test_run_variants_setup_error_recorded_continues(monkeypatch):
    seq = _node("Seq Scan")
    baseline_conn = FakeConn([_doc_json(seq, execution_ms=10), _doc_json(seq, execution_ms=20)])
    idx_conn = FakeConn([_doc_json(seq, execution_ms=4), _doc_json(seq, execution_ms=6)],
                        fail_on="CREATE INDEX",
                        fail_msg='relation "t_a_idx" already exists')
    conns = [baseline_conn, idx_conn]
    monkeypatch.setattr(planbench.pg, "connect_rw", lambda dsn=None, autocommit=True: conns.pop(0))

    result = planbench.run_variants(None, ["SELECT 1"], [Variant("idx", "CREATE INDEX ON t(a)")],
                                    runs=2)
    v1 = result["variants"][1]
    assert "already exists" in v1["error"]              # setup failure recorded
    assert v1["queries"][0]["median_ms"] == 5.0         # but the query is still measured (the index may already exist)
    assert result["variants"][0].get("error") is None   # baseline lacks the error key by default


def test_run_variants_query_error_excluded_from_diff(monkeypatch):
    ok = _node("Seq Scan")
    bad_conn = FakeConn([_doc_json(ok, execution_ms=1), _doc_json(ok, execution_ms=2)],
                        fail_on="FROM missing", fail_msg='relation "missing" does not exist')
    good_conn = FakeConn([_doc_json(ok, execution_ms=5)] * 4)
    conns = [bad_conn, good_conn]
    monkeypatch.setattr(planbench.pg, "connect_rw", lambda dsn=None, autocommit=True: conns.pop(0))

    result = planbench.run_variants(None, ["SELECT 1", "SELECT * FROM missing"],
                                    [Variant("idx", "")], runs=2)
    q_ok, q_bad = result["variants"][0]["queries"]
    assert q_ok["median_ms"] == 1.5                     # median(1,2)
    assert q_bad["median_ms"] is None and q_bad["plan"] is None
    assert "does not exist" in q_bad["error"]
    # queries whose baseline-side plan is missing stay out of the diff; only query 0 remains
    pair = result["diffs"][("baseline", "idx")]
    assert set(pair) == {0}


# ---------------------------- rendering (compact Chinese table, ≤100 columns) ----------------------------

def test_render_tables_within_100_columns():
    a = _node("Seq Scan")
    b = _node("Bitmap Heap Scan", Plans=[
        _node("Bitmap Index Scan", **{"Index Name": "t_a_idx"}),
        _node("Parallel Index Only Scan", **{"Index Name": "t_a_b_c_idx",
                                             "Plan Rows": 1234567}),
    ], **{"Plan Rows": 9999999, "Actual Rows": 8888888, "Actual Total Time": 123.456})
    diffs = planbench.diff_plans(a, b)
    out = planbench.render_diff(diffs)
    assert all(_disp_width(line) <= 100 for line in out.splitlines())
    assert "removed" in out and "added" in out and "Bitmap Heap Scan" in out
    # an all-same diff → summary line instead of an empty table
    assert "(无差异)" in planbench.render_diff(planbench.diff_plans(a, _node("Seq Scan")))
    assert "(其余 1 个节点无变化)" in planbench.render_diff(planbench.diff_plans(a, _node("Seq Scan")))

    rows = planbench.accuracy_report(b)
    assert rows[0].path == "0.1"                        # 1234567/1000 has the largest deviation
    acc = planbench.render_accuracy(rows)
    assert all(_disp_width(line) <= 100 for line in acc.splitlines())
    assert "log2偏差" in acc and "0.1" in acc
    # empty data renders quietly
    assert "无 ANALYZE 行数数据" in planbench.render_accuracy([])


def test_doc_plan_accepts_parsed_object():
    """D2 regression: psycopg2 auto-deserializes json columns (yielding list/dict); str is also accepted."""
    plan = {"Node Type": "Seq Scan", "Plan Rows": 1}
    doc = [{"Plan": plan, "Execution Time": 1.0}]
    assert planbench._doc_plan(doc) == plan
    assert planbench._doc_plan(json.dumps(doc)) == plan
