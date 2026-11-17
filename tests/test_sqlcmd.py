"""sqlcmd regression: core behaviors of the read-only execution entry (unit tests need no running PG; the connection is injected via a fake).

The real-PG chain (read-only rejection / timeout / truncation) is covered by selftest
--with-db live-database acceptance (see tests/fixtures/introspect_tpl.sql).
"""
import json

import psycopg2
import psycopg2.errors
import pytest

from pgtuner.sqlcmd import QueryResult, format_cell, render_json, run_query


class FakeCursor:
    """Dispatches by the executed SQL prefix: SET→no-op, everything else goes to the injected handler."""

    def __init__(self, handler, description, fetch_rows):
        self._handler = handler
        self._initial_description = description
        self.description = description
        self._fetch_rows = fetch_rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if sql.startswith("SET statement_timeout"):
            self.description = None            # real psycopg2: SET has no result set
            return
        if self._handler:
            self._handler(sql)
        self.description = self._initial_description   # real psycopg2: result-set description refilled after the query

    def fetchmany(self, n):
        return self._fetch_rows[:n]


class Column:
    def __init__(self, name):
        self.name = name


def make_conn(handler=None, columns=None, rows=()):
    class FakeConn:
        def cursor(self):
            return FakeCursor(handler, [Column(c) for c in columns] if columns else None,
                              list(rows))

        def close(self):
            pass
    return FakeConn()


def test_run_query_basic_and_timeout_set():
    conn = make_conn(columns=["a", "b"], rows=[(1, "x"), (2, "y")])
    res = run_query(None, "SELECT a,b FROM t", max_rows=10, timeout_s=5, conn=conn)
    assert res.columns == ["a", "b"]
    assert res.rows == [(1, "x"), (2, "y")]
    assert res.truncated is False


def test_run_query_truncation_probe():
    """fetchmany(max+1) probe: 5 rows with max=3 → 3 rows + truncated."""
    rows = [(i,) for i in range(5)]
    conn = make_conn(columns=["g"], rows=rows)
    res = run_query(None, "SELECT g", max_rows=3, conn=conn)
    assert len(res.rows) == 3 and res.truncated is True


def test_run_query_no_resultset():
    conn = make_conn(columns=None, rows=[])
    res = run_query(None, "SET work_mem='64MB'", conn=conn)
    assert res.columns == [] and res.rows == [] and res.truncated is False


def test_run_query_error_mapping():
    def raise_canceled(sql):
        raise psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")
    with pytest.raises(RuntimeError, match="--timeout"):
        run_query(None, "SELECT pg_sleep(99)", conn=make_conn(handler=raise_canceled))

    def raise_readonly(sql):
        raise psycopg2.errors.ReadOnlySqlTransaction(
            "cannot execute INSERT in a read-only transaction")
    with pytest.raises(RuntimeError, match="只读会话拒绝写操作"):
        run_query(None, "INSERT INTO t VALUES (1)", conn=make_conn(handler=raise_readonly))


def test_run_query_param_validation():
    with pytest.raises(ValueError):
        run_query(None, "SELECT 1", max_rows=0, conn=make_conn())
    with pytest.raises(ValueError):
        run_query(None, "SELECT 1", timeout_s=0, conn=make_conn())


def test_format_cell():
    assert format_cell(None) == "[dim]NULL[/dim]"
    assert format_cell(True) == "t" and format_cell(False) == "f"
    assert format_cell([1, {"k": "v"}]) == '[1, {"k": "v"}]'   # jsonb deserialized → valid JSON
    wide = "长" * 200
    out = format_cell(wide, width=50)
    assert out.endswith("…") and len(out) <= 51


def test_render_json_roundtrip():
    res = QueryResult(["a"], [(1,)], True)
    doc = json.loads(render_json(res))
    assert doc == {"columns": ["a"], "rows": [[1]], "row_count": 1, "truncated": True}
