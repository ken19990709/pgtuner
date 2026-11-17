"""lab sweep unit tests: clone-residue cleanup — databases registered in the clone pool + orphan databases not registered but named like <template>_c<8-hex>.

fake_pg mode (cf. tests/test_instance.py style): mock psycopg2.connect, no dependency on a running PG.
Safety invariants (every test guards one):
  - system databases / sealed template databases / ordinary databases not clone-named never enter DROP;
  - databases that look like clone names but whose prefix is not a template of this instance are reported but not cleaned;
  - the default dry-run writes nothing (no DROP/terminate sent, registry untouched).
"""
import re

import pytest

from pgtuner import instance

CLI = None  # lazy import of click.testing (used only by CLI tests)


# ---------------------------- fakes ----------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.sqls.append(sql)
        self._rows = self._conn.server.rows_for(sql)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return (False,)

    @property
    def rowcount(self):
        return 0


class FakeConn:
    def __init__(self, dsn, server):
        self.dsn = dsn
        self.sqls = []
        self.server = server
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def set_session(self, **kw):
        pass

    def close(self):
        self.closed = True


class FakeServer:
    """Fake pg_database behind the fake psycopg2.connect (routed by port).

    rows tuple: (datname, oid, datallowconn, datistemplate, created_at, age_minutes)
    stat_file_ok=False simulates lacking permission for pg_stat_file → mtime-carrying queries raise (falling back to the time-less version).
    Only the two query kinds are understood (routed by substring); other SQL (terminate/DROP) is execute-only and returns empty rows.
    """
    def __init__(self, rows, stat_file_ok=True):
        self.rows = rows
        self.stat_file_ok = stat_file_ok

    def rows_for(self, sql):
        if "pg_stat_file" in sql:
            if not self.stat_file_ok:
                raise RuntimeError("permission denied for function pg_stat_file")
            return list(self.rows)
        if "FROM pg_database" in sql:
            return [(r[0], r[1], r[2], r[3]) for r in self.rows]
        return []                                   # execute-only statements (terminate/DROP)


@pytest.fixture
def fake_pg(monkeypatch):
    """Mocked psycopg2.connect: dsns route by port to a FakeServer; returns (servers, conns)."""
    import psycopg2
    servers = {}
    conns = []

    def fake_connect(dsn, **kw):
        port = re.search(r"port=(\d+)", dsn).group(1)
        conn = FakeConn(dsn, servers[port])
        conns.append(conn)
        return conn

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    return servers, conns


@pytest.fixture
def no_ensure(monkeypatch):
    """Skip ensure_running's pg_isready/pg_ctl probing (unit tests do not depend on real binaries)."""
    monkeypatch.setattr(instance, "ensure_running",
                        lambda name, registry_file=None: instance.get(name, registry_file))


def dbrow(name, oid, allowconn=True, istemplate=False, created_at=None, age_min=None):
    return (name, oid, allowconn, istemplate, created_at, age_min)


def std_rows():
    """A typical instance: system databases + sealed template scratch_tpl + ordinary database bench_new."""
    return [
        dbrow("postgres", 5),
        dbrow("template1", 1, istemplate=True),           # system template with allowconn=True
        dbrow("template0", 4, allowconn=False, istemplate=True),
        dbrow("scratch_tpl", 16384, allowconn=False),     # sealed template (same criterion as lab templates)
        dbrow("bench_new", 17202),                        # ordinary database, not clone-named
    ]


def put_env(tmp_path, servers, insts=(("lab", 5599),), clones=None, stat_file_ok=True):
    """Write the registry (1..n instances) + attach a FakeServer to each instance."""
    reg = tmp_path / "r.json"
    instance._save(
        {"instances": [dict(name=n, bindir="/nonexistent", pg_version="20devel",
                            build_type="release", data_dir="", port=p,
                            socket_dir="/tmp", status="running", managed=True)
                       for n, p in insts],
         "clones": {n: list(clones or []) if n == "lab" else []
                    for n, _ in insts}},
        reg)
    for n, p in insts:
        servers[str(p)] = FakeServer(std_rows(), stat_file_ok=stat_file_ok)
    return reg


def all_sqls(conns):
    return [s for c in conns for s in c.sqls]


# ---------------------------- core semantics: dry-run classifies with zero writes ----------------------------

def test_dry_run_lists_registered_and_orphan_no_writes(tmp_path, fake_pg, no_ensure):
    servers, conns = fake_pg
    reg = put_env(tmp_path, servers, clones=["scratch_tpl_caaaa1111"])
    servers["5599"].rows += [
        dbrow("scratch_tpl_caaaa1111", 16390, age_min=42.0),    # registered in the pool
        dbrow("scratch_tpl_cbbbb2222", 16391, age_min=130.0),   # orphan
        dbrow("myapp_c0bad1234", 16392, age_min=99.0),           # looks alike but the prefix is not a template
    ]
    rep = instance.sweep("lab", registry_file=reg)
    by_db = {t["dbname"]: t for t in rep["targets"]}
    assert set(by_db) == {"scratch_tpl_caaaa1111", "scratch_tpl_cbbbb2222"}
    assert by_db["scratch_tpl_caaaa1111"]["kind"] == "registered"
    assert by_db["scratch_tpl_cbbbb2222"]["kind"] == "orphan"
    assert by_db["scratch_tpl_cbbbb2222"]["template"] == "scratch_tpl"
    assert by_db["scratch_tpl_caaaa1111"]["age_minutes"] == pytest.approx(42.0)
    # dry-run writes nothing: no DROP / no terminate / registry untouched
    assert not [s for s in all_sqls(conns)
                if s.startswith("DROP DATABASE") or "pg_terminate_backend" in s]
    assert instance.pool_stats(reg) == {"lab": ["scratch_tpl_caaaa1111"]}
    # looks like a clone name but the prefix is not a template of this instance → reported only, never a target
    ignored = {i["dbname"] for i in rep["ignored"]}
    assert ignored == {"myapp_c0bad1234"}


# ---------------------------- --yes: real cleanup ----------------------------

def test_yes_drops_both_kinds_and_cleans_registry(tmp_path, fake_pg, no_ensure):
    servers, conns = fake_pg
    reg = put_env(tmp_path, servers, clones=["scratch_tpl_caaaa1111"])
    servers["5599"].rows += [
        dbrow("scratch_tpl_caaaa1111", 16390, age_min=42.0),
        dbrow("scratch_tpl_cbbbb2222", 16391, age_min=130.0),
        dbrow("myapp_c0bad1234", 16392, age_min=99.0),
    ]
    rep = instance.sweep("lab", yes=True, registry_file=reg)
    sqls = all_sqls(conns)
    # drop only these two; connections to the database are terminated before the drop (drop_db semantics)
    assert sorted(s for s in sqls if s.startswith("DROP DATABASE")) == [
        'DROP DATABASE IF EXISTS "scratch_tpl_caaaa1111"',
        'DROP DATABASE IF EXISTS "scratch_tpl_cbbbb2222"',
    ]
    for db in ("scratch_tpl_caaaa1111", "scratch_tpl_cbbbb2222"):
        assert f"datname = '{db}'" in " ".join(sqls)          # terminate already sent
    assert set(rep["dropped"]) == {"lab/scratch_tpl_caaaa1111", "lab/scratch_tpl_cbbbb2222"}
    assert instance.pool_stats(reg) == {"lab": []}            # registered entries leave the pool with the cleanup
    assert all(c.closed for c in conns)


# ---------------------------- --older-than: filter by creation time ----------------------------

def test_older_than_filters_by_dir_mtime(tmp_path, fake_pg, no_ensure):
    servers, conns = fake_pg
    reg = put_env(tmp_path, servers, clones=["scratch_tpl_caaaa1111"])
    servers["5599"].rows += [
        dbrow("scratch_tpl_caaaa1111", 16390, age_min=5.0),    # registered but too new
        dbrow("scratch_tpl_cbbbb2222", 16391, age_min=130.0),  # orphan and old
    ]
    rep = instance.sweep("lab", older_than=30, yes=True, registry_file=reg)
    assert [t["dbname"] for t in rep["targets"]] == ["scratch_tpl_cbbbb2222"]
    assert rep["dropped"] == ["lab/scratch_tpl_cbbbb2222"]
    assert instance.pool_stats(reg) == {"lab": ["scratch_tpl_caaaa1111"]}   # the new database is kept
    # boundary: age == older_than counts as "of age" and can be cleaned (>= semantics)
    servers["5599"].rows[-1] = dbrow("scratch_tpl_cbbbb2222", 16391, age_min=30.0)
    rep2 = instance.sweep("lab", older_than=30, registry_file=reg)
    assert [t["dbname"] for t in rep2["targets"]] == ["scratch_tpl_cbbbb2222"]


def test_older_than_age_unknown_never_blind_dropped(tmp_path, fake_pg, no_ensure):
    """pg_stat_file unavailable (age unknown) + --older-than → skipped with a note, never blindly dropped;
    without --older-than the cleanup does not depend on mtime and works as usual."""
    servers, conns = fake_pg
    reg = put_env(tmp_path, servers, clones=["scratch_tpl_caaaa1111"], stat_file_ok=False)
    servers["5599"].rows += [dbrow("scratch_tpl_caaaa1111", 16390),
                             dbrow("scratch_tpl_cbbbb2222", 16391)]
    rep = instance.sweep("lab", older_than=30, yes=True, registry_file=reg)
    assert rep["targets"] == []
    assert any("scratch_tpl_cbbbb2222" == i["dbname"] and "未知" in i["reason"]
               for i in rep["ignored"])
    assert not [s for s in all_sqls(conns) if s.startswith("DROP DATABASE")]
    rep2 = instance.sweep("lab", yes=True, registry_file=reg)
    assert set(rep2["dropped"]) == {"lab/scratch_tpl_caaaa1111", "lab/scratch_tpl_cbbbb2222"}


# ---------------------------- stale registry entries (stale-entry) ----------------------------

def test_stale_registry_entry_reported_and_pruned(tmp_path, fake_pg, no_ensure):
    """Registered in the pool but no longer on the instance: reported in dry-run; --yes only prunes the registry, sends no DROP."""
    servers, conns = fake_pg
    reg = put_env(tmp_path, servers, clones=["scratch_tpl_cdeadbeef"])   # no such database server-side
    rep = instance.sweep("lab", registry_file=reg)
    kinds = {t["dbname"]: t["kind"] for t in rep["targets"]}
    assert kinds == {"scratch_tpl_cdeadbeef": "stale-entry"}
    rep2 = instance.sweep("lab", yes=True, registry_file=reg)
    assert rep2["stale_removed"] == ["lab/scratch_tpl_cdeadbeef"]
    assert rep2["dropped"] == []
    assert instance.pool_stats(reg) == {"lab": []}
    assert not [s for s in all_sqls(conns) if s.startswith("DROP DATABASE")]


# ---------------------------- scope and fault tolerance ----------------------------

def test_default_sweeps_all_registered_instances(tmp_path, fake_pg, no_ensure):
    servers, conns = fake_pg
    reg = put_env(tmp_path, servers, insts=(("lab", 5599), ("lab2", 5600)))
    servers["5599"].rows.append(dbrow("scratch_tpl_cbbbb2222", 16391, age_min=130.0))
    servers["5600"].rows.append(dbrow("scratch_tpl_ccccc3333", 16395, age_min=10.0))
    rep = instance.sweep(registry_file=reg)
    assert rep["scanned"] == ["lab", "lab2"]
    assert {t["instance"] for t in rep["targets"]} == {"lab", "lab2"}


def test_unknown_instance_raises(tmp_path, fake_pg, no_ensure):
    servers, _ = fake_pg
    reg = put_env(tmp_path, servers)
    with pytest.raises(ValueError, match="未注册"):
        instance.sweep("nope", registry_file=reg)


def test_unreachable_instance_skipped_not_fatal(tmp_path, fake_pg, no_ensure, monkeypatch):
    servers, _ = fake_pg
    reg = put_env(tmp_path, servers, insts=(("lab", 5599), ("lab2", 5600)))
    real_ensure = instance.ensure_running

    def ensure_or_raise(name, registry_file=None):
        if name == "lab2":
            raise RuntimeError("实例 lab2 为外部注册（无 data_dir），无法 pg_ctl start")
        return real_ensure(name, registry_file)

    monkeypatch.setattr(instance, "ensure_running", ensure_or_raise)
    servers["5599"].rows.append(dbrow("scratch_tpl_cbbbb2222", 16391, age_min=130.0))
    rep = instance.sweep(registry_file=reg)
    assert rep["scanned"] == ["lab"]
    assert [t["dbname"] for t in rep["targets"]] == ["scratch_tpl_cbbbb2222"]
    assert any(s["instance"] == "lab2" for s in rep["skipped_instances"])


# ---------------------------- CLI: dry-run by default, acts only with --yes ----------------------------

def test_cli_sweep_default_is_dry_run(monkeypatch, tmp_path, fake_pg, no_ensure):
    from click.testing import CliRunner
    from pgtuner import cli
    servers, conns = fake_pg
    reg = put_env(tmp_path, servers, clones=["scratch_tpl_caaaa1111"])
    monkeypatch.setattr(instance, "INSTANCE_REGISTRY", reg)
    servers["5599"].rows.append(dbrow("scratch_tpl_cbbbb2222", 16391, age_min=130.0))
    result = CliRunner().invoke(cli.cli, ["lab", "sweep"])
    assert result.exit_code == 0, result.output
    assert "scratch_tpl_caaaa1111" in result.output          # registered clone listed
    assert "scratch_tpl_cbbbb2222" in result.output          # orphan listed
    assert "DRY-RUN" in result.output
    assert not [s for s in all_sqls(conns)
                if s.startswith("DROP DATABASE") or "pg_terminate_backend" in s]
    assert instance.pool_stats(reg) == {"lab": ["scratch_tpl_caaaa1111"]}


def test_cli_sweep_yes_drops_and_older_than(monkeypatch, tmp_path, fake_pg, no_ensure):
    from click.testing import CliRunner
    from pgtuner import cli
    servers, conns = fake_pg
    reg = put_env(tmp_path, servers, clones=["scratch_tpl_caaaa1111"])
    monkeypatch.setattr(instance, "INSTANCE_REGISTRY", reg)
    servers["5599"].rows += [
        dbrow("scratch_tpl_caaaa1111", 16390, age_min=5.0),
        dbrow("scratch_tpl_cbbbb2222", 16391, age_min=130.0),
    ]
    result = CliRunner().invoke(cli.cli, ["lab", "sweep", "--older-than", "30", "--yes"])
    assert result.exit_code == 0, result.output
    assert "已清理" in result.output
    drops = [s for s in all_sqls(conns) if s.startswith("DROP DATABASE")]
    assert drops == ['DROP DATABASE IF EXISTS "scratch_tpl_cbbbb2222"']
    assert instance.pool_stats(reg) == {"lab": ["scratch_tpl_caaaa1111"]}
