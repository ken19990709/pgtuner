"""hypo regression: evaluation matrix / error isolation / residue self-check for the transaction-rollback mechanism (no PG, fake connection injection).

The fake cursor routes by SQL signature; key state: _indexes (whether an index was created in the current transaction) — cleared by rollback(),
set by CREATE INDEX, and EXPLAIN returns a seq/index plan accordingly. Residue self-check = the index count is always read from
pg_indexes (independent of _indexes, constantly 1 in the fake).
"""
import json
import sys
import types

import pytest

from pgtuner import hypo as H


class _pg_err(Exception):
    """Stand-in for psycopg2.Error (hypo only uses str(e) and the exception type)."""


# hypo imports psycopg2 inside the function and catches psycopg2.Error: inject a stand-in module,
# a fake raising _pg_err then exercises hypo's real except branch
_FAKE_PSYCOPG2 = types.SimpleNamespace(Error=_pg_err)


class FakeCur:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        c = self._conn
        if sql.startswith("SELECT 1 FROM pg_extension"):
            c._rows = (1,) if c.extensions else None       # no hypopg → fetchone returns None
        elif sql.startswith("SELECT count(*) FROM pg_indexes"):
            c._rows = 1                                    # index count constant: no residue after rollback
        elif sql.startswith("EXPLAIN"):
            stmt = sql.split(") ", 1)[1]
            if c.bad_queries and stmt in c.bad_queries:
                raise _pg_err("EXPLAIN 失败: 语法错误")
            if c._indexes and stmt not in c.index_useless_for:
                plan = {"Node Type": "Index Scan", "Index Name": c._index_name,
                        "Total Cost": 100.0, "Plans": []}
            else:
                plan = {"Node Type": "Seq Scan", "Total Cost": 2000.0, "Plans": []}
            c._rows = [{"Plan": plan,
                        "Execution Time": 7.5 if "ANALYZE" in sql else None}]
        elif sql.startswith("CREATE INDEX"):
            if c.fail_ddl:
                raise _pg_err(f"建索引失败: {c.fail_ddl}")
            c._indexes = True
            c.created.append(sql)
            c._rows = None
        elif sql.startswith("SELECT hypopg"):
            c._rows = None
        else:
            raise AssertionError(f"fake 未登记 SQL: {sql[:70]}")

    def fetchone(self):
        # psycopg2 semantics: no rows → None; with rows → returns the row tuple, (single-column)[0] is the column value
        return None if self._conn._rows is None else (self._conn._rows,)


class FakeConn:
    def __init__(self):
        self._indexes = False
        self._rows = None
        self.extensions = False             # whether hypopg is available
        self.created = []                   # CREATE INDEX statements executed
        self.rollbacks = 0
        self.fail_ddl = None                # force a DDL failure
        self.bad_queries = []               # force an EXPLAIN failure
        self.index_useless_for = []         # queries that do not use the index even after creation
        self._index_name = "t_k_idx"
        self.closed = False

    def cursor(self):
        return FakeCur(self)

    def rollback(self):
        self._indexes = False
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def conn(monkeypatch):
    c = FakeConn()
    monkeypatch.setattr(H.pg, "connect_rw", lambda dsn, autocommit=True: c)
    monkeypatch.setitem(sys.modules, "psycopg2", _FAKE_PSYCOPG2)
    return c


def test_eval_matrix_and_rollback(conn):
    rep = H.eval_candidates("dsn", ["CREATE INDEX ON t(k)"],
                            ["SELECT * FROM t WHERE k = 1"], analyze=True)
    assert rep["mechanism"] == "rollback"
    assert rep["index_count_before"] == rep["index_count_after"] == 1
    assert not rep["warning"]
    se = rep["sets"][0]
    assert se.label.startswith("idx1") and not se.error
    qe = se.queries[0]
    assert qe.base_cost == 2000.0 and qe.hypo_cost == 100.0
    assert qe.ratio == pytest.approx(0.05)
    assert qe.base_node == "Seq Scan" and qe.hypo_node == "Index Scan"
    assert qe.hypo_index == "t_k_idx" and qe.plan_changed
    assert qe.base_ms == 7.5                 # with analyze, the baseline also has Execution Time
    assert conn.created == ["CREATE INDEX ON t(k)"]
    assert conn.rollbacks >= 1              # rollback at set end + finally, double insurance
    assert conn.closed


def test_combined_set_and_ddl_failure_isolation(conn):
    conn.fail_ddl = "t(k) 列不存在"
    rep = H.eval_candidates("dsn", ["CREATE INDEX ON t(k)", "CREATE INDEX ON t(j)"],
                            ["SELECT * FROM t WHERE k = 1"])
    labels = [s.label for s in rep["sets"]]
    assert any("组合" in ln for ln in labels)       # two candidates auto-add the combined set
    assert all(s.error and s.error.startswith("建索引失败") for s in rep["sets"][:2])
    assert all(q.ratio is None for s in rep["sets"] for q in s.queries)


def test_explain_failure_recovers_and_rebuilds(conn):
    conn.bad_queries = ["SELECT * FROM t WHERE k = 1"]
    rep = H.eval_candidates("dsn", ["CREATE INDEX ON t(k)"],
                            ["SELECT * FROM t WHERE k = 1", "SELECT count(*) FROM t"])
    qe = rep["sets"][0].queries[0]
    assert qe.error and "语法错误" in qe.error
    qe2 = rep["sets"][0].queries[1]
    assert qe2.ratio is not None               # rebuild succeeded; subsequent queries evaluate as usual
    assert rep["index_count_before"] == rep["index_count_after"]


def test_input_validation(conn):
    with pytest.raises(ValueError, match="--index"):
        H.eval_candidates("dsn", [], ["SELECT 1"])
    with pytest.raises(ValueError, match="查询"):
        H.eval_candidates("dsn", ["CREATE INDEX ON t(k)"], [])


def test_render_json_roundtrip(conn):
    rep = H.eval_candidates("dsn", ["CREATE INDEX ON t(k)"],
                            ["SELECT * FROM t WHERE k = 1"])
    doc = json.loads(H.render_json(rep))
    assert doc["mechanism"] == "rollback"
    assert doc["sets"][0]["queries"][0]["hypo_index"] == "t_k_idx"
    txt = H.render_text(rep)
    assert "机制:事务回滚" in txt and "零残留" in txt and "✓" in txt
