"""introspect regression: table listing / single-table overview / DDL reconstruction (unit tests need no running PG; catalog rows are injected by fakes).

The fake routes by introspect._SQL_* constants (the constants are the contract): tests inject "what the catalog looks like"
and assert on "what the data model / rendering / DDL reconstruction looks like" — SQL text changes do not affect the tests.
"""
import json

import pytest

from pgtuner import introspect as I


class CatalogCursor:
    def __init__(self, data):
        self._data = data          # {sql constant: rows, or (params) -> rows}
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        # trailing ORDER BY/filter clauses in sql do not affect routing: constants are matched by statement-start prefix
        for key, rows in self._data.items():
            if sql.strip().startswith(key.strip()[:60]):
                self._rows = rows(params) if callable(rows) else rows
                return
        raise AssertionError(f"fake catalog 未登记的 SQL: {sql.strip()[:80]}")

    def fetchall(self):
        return self._rows


def make_conn(data):
    class FakeConn:
        def cursor(self):
            return CatalogCursor(data)

        def close(self):
            pass
    return FakeConn()


# ---------------------------- list_tables ----------------------------

def test_list_tables_maps_and_reluples_unknown():
    data = {I._SQL_TABLES: [
        ("public", "accounts", "r", 8192, 16384, 100000, 100003, 7, "pgbench 账户"),
        ("public", "events", "p", 40960, 81920, -1, None, None, None),   # -1=unknown, None=no statistics
        ("public", "v_bal", "v", 0, 0, None, None, None, None),
    ]}
    out = I.list_tables(make_conn(data))
    assert [t.name for t in out] == ["accounts", "events", "v_bal"]
    a = out[0]
    assert (a.kind, a.kind_label, a.row_est, a.live_tup, a.comment) == \
        ("r", "表", 100000, 100003, "pgbench 账户")
    assert out[1].row_est is None and out[1].live_tup is None   # no value guessed for unknown figures
    assert out[1].kind_label == "分区表"
    assert out[2].kind_label == "视图" and out[2].total_bytes == 0


def test_list_tables_schema_filter_param():
    seen = {}

    def rows(params):
        seen["params"] = params
        return []

    I.list_tables(make_conn({I._SQL_TABLES: rows}), schema="sales")
    assert seen["params"][-1] == "sales"


# ---------------------------- describe_table ----------------------------

DESCRIBE_DATA = {
    I._SQL_RESOLVE: lambda p: [(99, "r", 1000, "p", "alice", "主表")] if p == ["public", "t1"] else [],
    I._SQL_SIZE_OF: [(16384, 32768)],
    I._SQL_COLUMNS: [("id", "integer", True, "nextval('t1_id_seq'::regclass)", "主键列"),
                     ("payload", "jsonb", False, None, None)],
    I._SQL_INDEXES: [("t1_pkey", "CREATE UNIQUE INDEX t1_pkey ON public.t1 USING btree (id)",
                      True, True, True, 8192, 5),
                     ("t1_payload_idx", "CREATE INDEX t1_payload_idx ON public.t1 USING gin (payload)",
                      False, False, True, 4096, None)],
    I._SQL_CONSTRAINTS: [("t1_pkey", "p", "PRIMARY KEY (id)", True),
                         ("t1_payload_chk", "c", "CHECK (jsonb_typeof(payload) = 'object'::jsonb)",
                          False)],
    I._SQL_TRIGGERS: [("t1_trg", "CREATE TRIGGER t1_trg AFTER INSERT ON t1 FOR EACH ROW "
                                 "EXECUTE FUNCTION log_chg()", "O")],
    I._SQL_STATS: [(1005, 3, 7, 12, None, "2026-11-01", None, "2026-11-10")],
    I._SQL_PARENT: [],
}


def test_describe_table_sections():
    desc = I.describe_table(make_conn(DESCRIBE_DATA), "t1")
    assert (desc.kind, desc.owner, desc.unlogged, desc.row_est) == ("r", "alice", False, 1000)
    assert desc.columns[0].default.startswith("nextval(")
    assert desc.columns[0].comment == "主键列"
    assert [i.name for i in desc.indexes] == ["t1_pkey", "t1_payload_idx"]
    assert desc.indexes[1].scans is None                     # track_counts missing → None, not a guessed 0
    assert desc.constraints[0].kind_label == "主键"
    assert desc.constraints[1].validated is False
    assert desc.triggers[0].enabled_label == "原生"
    assert desc.stats["live_tup"] == 1005 and desc.stats["last_autoanalyze"] == "2026-11-10"
    assert desc.data_bytes == 16384 and desc.total_bytes == 32768


def test_describe_table_not_found_hint():
    with pytest.raises(ValueError, match="对象不存在"):
        I.describe_table(make_conn({**DESCRIBE_DATA, I._SQL_TABLES: []}), "nope")


def test_describe_partitioned_and_view():
    part_data = {
        **DESCRIBE_DATA,
        I._SQL_RESOLVE: lambda p: [(7, "p", -1, "p", "alice", None)] if p == ["public", "ev"] else
                                 [(8, "v", None, "p", "alice", None)],
        I._SQL_VIEWDEF: [("SELECT bid, count(*) FROM accounts GROUP BY bid",)],
        I._SQL_PARTKEY: [("RANGE (ts)",)],
        I._SQL_PARTITIONS: [("public", "ev_2026", 1, True, "FOR VALUES FROM ('2026-01-01') TO ('2026-02-01')")],
    }
    desc = I.describe_table(make_conn(part_data), "ev")
    assert desc.partition_key == "RANGE (ts)"
    assert desc.partitions[0].bound.startswith("FOR VALUES FROM")
    assert desc.row_est is None                                 # -1 → unknown

    vd = I.describe_table(make_conn(part_data), "v_bal")
    assert vd.view_def.startswith("SELECT bid")


# ---------------------------- synthesize_ddl ----------------------------

def test_synthesize_ddl_plain_table():
    desc = I.describe_table(make_conn(DESCRIBE_DATA), "t1")
    ddl = I.synthesize_ddl(desc)
    assert 'CREATE TABLE "public"."t1" (' in ddl
    assert '"id" integer NOT NULL DEFAULT nextval(' in ddl
    assert '"payload" jsonb' in ddl
    assert 'CONSTRAINT "t1_pkey" PRIMARY KEY (id)' in ddl          # constraint inlined
    assert 'CONSTRAINT "t1_payload_chk" CHECK' in ddl
    assert "CREATE INDEX t1_payload_idx" in ddl                     # standalone index kept
    assert "CREATE UNIQUE INDEX t1_pkey" not in ddl                 # constraint index deduped
    assert "CREATE TRIGGER t1_trg" in ddl
    assert "COMMENT ON TABLE" in ddl and "COMMENT ON COLUMN" in ddl
    assert ddl.startswith("--")                                     # honesty-boundary header


def test_synthesize_ddl_partitioned():
    part_data = {
        **DESCRIBE_DATA,
        I._SQL_RESOLVE: lambda p: [(7, "p", -1, "p", "alice", None)],
        I._SQL_PARTKEY: [("RANGE (ts)",)],
        I._SQL_PARTITIONS: [("public", "ev_2026", 1, True,
                             "FOR VALUES FROM ('2026-01-01') TO ('2026-02-01')")],
    }
    ddl = I.synthesize_ddl(I.describe_table(make_conn(part_data), "ev"))
    assert ") PARTITION BY RANGE (ts);" in ddl
    assert 'CREATE TABLE "public"."ev_2026" PARTITION OF "public"."ev" ' \
           "FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');" in ddl


def test_synthesize_ddl_view():
    view_data = {**DESCRIBE_DATA,
                 I._SQL_RESOLVE: lambda p: [(8, "v", None, "p", "alice", "视图注释")],
                 I._SQL_VIEWDEF: [("SELECT bid, count(*) FROM accounts GROUP BY bid",)]}
    ddl = I.synthesize_ddl(I.describe_table(make_conn(view_data), "v_bal"))
    assert 'CREATE VIEW "public"."v_bal" AS' in ddl.splitlines()
    assert "COMMENT ON VIEW" in ddl


# ---------------------------- rendering (JSON criterion) ----------------------------

def test_render_json_roundtrip():
    out = I.list_tables(make_conn({I._SQL_TABLES: [
        ("public", "accounts", "r", 8192, 16384, 100000, 100003, 7, None)]}))
    doc = json.loads(I.render_tables_json(out))
    assert doc[0]["name"] == "accounts" and doc[0]["kind_label"] == "表"
