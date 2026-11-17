"""statements regression: availability three states / enablement branches / top_queries mapping / rendering (no PG, fake injection).

The fake routes by SQL text signature (same "constants are the contract" idea as sqlsplit): SHOW→preload value,
pg_available_extensions/pg_extension→registry rows, pg_stat_statements→statistics rows.
"""
import json

import pytest

from pgtuner import statements as S


class FakeCur:
    def __init__(self, routing):
        self._r = routing          # {substring: rows, or (params)->rows}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        for key, rows in self._r.items():
            if key in sql:
                self._rows = rows(params) if callable(rows) else rows
                return
        raise AssertionError(f"fake 未登记 SQL: {sql[:70]}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows or []


def make_conn(routing):
    class FakeConn:
        def cursor(self):
            return FakeCur(routing)
    return FakeConn()


def test_status_three_states():
    def routing(installed, created, preloaded):
        return {
            "pg_available_extensions": [(1,)] if installed else [],
            "pg_extension": [(1,)] if created else [],
            "SHOW shared_preload_libraries": [(preloaded or "",)],
        }
    conn = make_conn(routing(True, True, "pg_stat_statements"))
    assert S.status(conn) == {"installed": True, "created": True, "preloaded": True}
    conn = make_conn(routing(True, True, ""))
    assert S.status(conn) == {"installed": True, "created": True, "preloaded": False}
    conn = make_conn(routing(False, False, ""))
    assert S.status(conn) == {"installed": False, "created": False, "preloaded": False}


def test_top_queries_maps_rows_and_sort_whitelist():
    routing = {
        "pg_stat_statements s": lambda p: [           # the body matches only before the sort expression
            (12345, 100, 5000, 2500.5, 25.005, 9000, 100, 2, "SELECT * FROM t WHERE k = $1"),
            (999, 1, 1, 0.01, 0.01, 0, 0, 0, "SELECT 1"),
        ],
    }
    conn = make_conn(routing)
    stats = S.top_queries(conn, sort="total", limit=5, min_calls=1)
    assert stats[0].queryid == 12345 and stats[0].calls == 100
    assert stats[0].total_ms == 2500.5 and stats[0].temp_written == 2
    with pytest.raises(ValueError, match="无效排序键"):
        S.top_queries(conn, sort="nope")


def test_render_json_roundtrip_and_text_verse():
    s = S.QueryStat(queryid=1, calls=2, rows=3, total_ms=4.0, mean_ms=2.0,
                    shared_hit=5, shared_read=6, temp_written=7, query="SELECT 1")
    doc = json.loads(S.render_json([s]))
    assert doc[0]["query"] == "SELECT 1" and doc[0]["calls"] == 2
    txt = S.render_text([s], sort="total")
    assert "pg_stat_statements" in txt and "本命令自身的查询也会被记录" in txt
