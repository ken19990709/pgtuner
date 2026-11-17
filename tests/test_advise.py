"""advise regression: candidate-extraction heuristics (pure functions) / placeholder detection / weighted ranking and recipe (fake hypo injection)."""
import json

import pytest

from pgtuner import advise as A
from pgtuner import hypo as H


# ---------------------------- extract_candidates ----------------------------

def test_single_table_eq_range_composite():
    cands = A.extract_candidates([
        "SELECT * FROM accounts WHERE bid = 42 AND abalance > 5000"])
    by_cols = {c.cols: c for c in cands if c.table == "accounts"}
    assert ("bid",) in by_cols and by_cols[("bid",)].kind == "等值"
    assert ("bid", "abalance") in by_cols                     # equality + range combination
    assert by_cols[("bid", "abalance")].evidence == ["Q1: bid= 等值, abalance 范围"]


def test_join_both_sides_and_order_by():
    cands = A.extract_candidates([
        "SELECT * FROM a JOIN b ON a.k = b.ak WHERE b.x > 1 ORDER BY b.x"])
    tbls = {(c.table, c.cols) for c in cands}
    assert ("a", ("k",)) in tbls and ("b", ("ak",)) in tbls   # JOIN produces candidates on both sides
    assert ("b", ("x",)) in tbls                              # range / sort column


def test_bare_col_only_single_table():
    one = A.extract_candidates(["SELECT * FROM t WHERE k = 1"])
    assert any(c.cols == ("k",) and c.table == "t" for c in one)
    two = A.extract_candidates(["SELECT * FROM t JOIN u ON t.id = u.id WHERE k = 1"])
    assert not any(c.cols == ("k",) for c in two)             # bare column in a two-table query is not guessed (conservative)


def test_system_tables_filtered_and_placeholder_flow():
    cands = A.extract_candidates([
        "SELECT 1 FROM pg_available_extensions WHERE name = 'x'"])
    assert cands == []                                        # system tables produce no candidates
    assert A.has_placeholders(["SELECT * FROM t WHERE k = $1"])
    assert not A.has_placeholders(["SELECT * FROM t WHERE k = 1"])


def test_placeholder_extraction_still_works():
    cands = A.extract_candidates(
        ["SELECT count(*) FROM accounts WHERE bid = $1 AND abalance BETWEEN $2 AND $3"])
    assert any(c.cols == ("bid", "abalance") for c in cands)  # extraction works on normalized text


# ---------------------------- evaluate(fake hypo) ----------------------------

def test_evaluate_ranking_and_recipe(monkeypatch):
    def fake_eval(dsn, ddls, queries, *, analyze=False):
        sets = []
        for i, d in enumerate(ddls):
            se = H.SetEval(label=f"idx{i + 1}", ddls=[d])
            for q in queries:
                qe = H.QueryEval(sql=q, base_cost=2000.0, hypo_cost=1000.0
                                 if i == 0 else 2000.0, ratio=0.5 if i == 0 else 1.0)
                se.queries.append(qe)
            sets.append(se)
        return {"mechanism": "rollback", "analyze": analyze, "sets": sets,
                "index_count_before": 1, "index_count_after": 1, "warning": None}
    monkeypatch.setattr(H, "eval_candidates", fake_eval)

    out = A.evaluate("dsn", ["SELECT * FROM t WHERE k = 1"], weights=[10.0])
    ddl0 = out["candidates"][0].ddl
    # benefit = 10·(1-0.5) = 5 (candidate 0); candidate 1 ratio 1.0 → 0
    assert out["ranking"][0]["benefit"] == pytest.approx(5.0)
    assert out["ranking"][0]["ddl"] == ddl0
    assert out["recipe"] and "planbench" in out["recipe"][1] and "rebench" in out["recipe"][3]


def test_evaluate_placeholder_short_circuits(monkeypatch):
    called = []

    def _must_not_run(*a, **k):
        called.append(1)
        raise AssertionError("占位路径不应触发 EXPLAIN 评估")

    monkeypatch.setattr(H, "eval_candidates", _must_not_run)
    out = A.evaluate("dsn", ["SELECT * FROM t WHERE k = $1"])
    assert out["placeholders"] and out["eval"] is None and not called
    assert out["candidates"], "占位路径仍应给候选"

    doc = json.loads(A.render_json(out))
    assert doc["candidates"][0]["table"] == "t"


def test_range_no_space_and_end_of_string():
    """Range predicates without spaces / at end of string must still be recalled (v0.6: the A/B benchmark query shape abalance>5000 was once missed)."""
    cands = A.extract_candidates(
        ["SELECT * FROM accounts WHERE bid=42 AND abalance>5000"])
    cols = {c.cols for c in cands if c.table == "accounts"}
    # with an equality column present, the range column goes into the composite candidate (single-column range candidate omitted — the equality+range composite already covers it)
    assert ("bid",) in cols and ("bid", "abalance") in cols
    cands2 = A.extract_candidates(["SELECT * FROM t WHERE k>=1 AND m < 5 OR n != 0"])
    cols2 = {c.cols for c in cands2 if c.table == "t"}
    assert ("k",) in cols2 and ("m",) in cols2 and ("n",) in cols2


def test_order_by_strips_desc_limit_suffix():
    """ORDER BY x DESC LIMIT 20 yields only column x (the S3 benchmark once swallowed "DESC LIMIT" into the column name)."""
    cands = A.extract_candidates(
        ["SELECT * FROM t WHERE k=1 ORDER BY created_at DESC LIMIT 20"])
    cols = {c.cols for c in cands if c.table == "t"}
    assert ("created_at",) in cols
    assert not any("DESC" in col or "LIMIT" in col for c in cands for col in c.cols)


def test_include_candidate_agg_query():
    """Aggregate queries yield covering candidates: predicate keys + INCLUDE for referenced columns (a shape hand-built twice by benchmark agents in S3/S4)."""
    cands = A.extract_candidates(
        ["SELECT count(*), sum(amount) FROM orders WHERE customer_id=1234 AND status='paid'"])
    cover = {(c.table, c.cols, c.include) for c in cands if c.kind == "覆盖"}
    assert ("orders", ("customer_id", "status"), ("amount",)) in cover
    # the covering candidate's ddl form is executable
    ddl = next(c.ddl for c in cands
               if c.kind == "覆盖" and c.cols == ("customer_id", "status"))
    assert ddl == "CREATE INDEX ON orders (customer_id, status) INCLUDE (amount)"


def test_include_skips_select_star():
    """SELECT * cannot enumerate referenced columns, so no covering candidate (prefer missing over wrong)."""
    cands = A.extract_candidates(
        ["SELECT * FROM orders WHERE customer_id=777 AND created_at>'2026-06-01'"])
    assert not any(c.kind == "覆盖" for c in cands)


def test_include_join_query():
    """JOIN queries: per-table predicate keys + INCLUDE (JOIN columns go into INCLUDE, avoiding heap-fetch probes)."""
    cands = A.extract_candidates(
        ["SELECT c.region, sum(o.amount) FROM orders o JOIN customers c ON c.id=o.customer_id "
         "WHERE o.status='new' AND c.region='region-3' GROUP BY 1"])
    cover = {(c.table, c.cols, frozenset(c.include)) for c in cands if c.kind == "覆盖"}
    assert ("orders", ("status",), frozenset({"customer_id", "amount"})) in cover
    assert ("customers", ("region",), frozenset({"id"})) in cover


# ---------------------------- partial index candidates (v0.7.1) ----------------------------

def test_partial_candidate_join_query():
    """Partial covering candidates (traced to S5: shapes independently hand-built by two psql agents, B2/B3):
    categorical string-literal equality columns become the WHERE, the remaining predicate columns (JOIN columns) become the key, and referenced columns become INCLUDE."""
    cands = A.extract_candidates(
        ["SELECT c.region, sum(o.amount) FROM orders o JOIN customers c ON c.id=o.customer_id "
         "WHERE o.status='new' AND c.region='region-3' GROUP BY 1"])
    partial = [c for c in cands if c.kind.startswith("部分")]
    ddl = {c.ddl for c in partial}
    # B2/B3 hand-built original: orders(customer_id) INCLUDE(amount) WHERE status='new'
    assert "CREATE INDEX ON orders (customer_id) INCLUDE (amount) WHERE status='new'" in ddl
    # customers side: region='region-3' is also a categorical literal, key=id (JOIN column);
    # region itself is a WHERE column, not included in INCLUDE → key-only partial candidate
    assert "CREATE INDEX ON customers (id) WHERE region='region-3'" in ddl


def test_partial_candidate_key_prefers_other_eq():
    """Key selection: other equality columns take precedence over JOIN/range columns."""
    cands = A.extract_candidates(
        ["SELECT count(*) FROM orders WHERE tenant='acme' AND customer_id=1234 "
         "AND created_at>'2026-06-01'"])
    partial = {c.cols: c for c in cands if c.kind.startswith("部分")}
    assert ("customer_id",) in partial           # equality column as key, not the range column created_at
    assert partial[("customer_id",)].where == "tenant='acme'"


def test_partial_skips_numeric_literal_and_where_only():
    """Numeric literals (customer_id=1234) are instance values, not categories, so not used as WHERE;
    with no other predicate columns (only a single literal-equality predicate) no partial candidate is emitted (the pure equality candidate already covers it)."""
    cands = A.extract_candidates(
        ["SELECT count(*) FROM orders WHERE customer_id=1234"])
    assert not any(c.kind.startswith("部分") for c in cands)
    # string literal but no other predicates → also not emitted (key is empty)
    cands = A.extract_candidates(
        ["SELECT count(*) FROM orders WHERE status='paid'"])
    assert not any(c.kind.startswith("部分") for c in cands)


def test_partial_dedup_separate_from_plain():
    """Partial/non-partial on the same key columns are two separate candidates (the dedup key includes where)."""
    cands = A.extract_candidates(
        ["SELECT count(*), sum(amount) FROM orders "
         "WHERE customer_id=1234 AND status='paid'"])
    plain = [c for c in cands if c.cols == ("customer_id",) and not c.where]
    part = [c for c in cands if c.cols == ("customer_id",) and c.where]
    assert plain and part                        # both exist, neither swallows the other
    assert part[0].where == "status='paid'"
