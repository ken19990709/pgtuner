"""Unit tests for lab clone ownership and auditing (tool-w1-clone-vanished regression).

Defect: with multiple agents in parallel, anyone could `lab release` someone else's active clone (no ownership protection),
and the clone pool had no per-clone audit log (who/when released), so a vanished clone could not be attributed — the entire experiment subject was lost.

Security invariants of the fix (every test guards one):
  - clone() records ownership (clone_meta.owner/created_at) + a clone audit entry;
  - a non-owner release is refused: the database stays in the pool, no DROP/terminate is sent, and a release-denied audit entry is written
    (owner=the owner, actor=the trespasser — attribution evidence);
  - --force lets the trespasser through but still audits (forced flag);
  - legacy registrations without ownership records / databases outside the pool are not blocked (backward compatible: make_template cleanup, sweeping ownerless clones);
  - a successful release synchronously clears clone_meta and writes a release audit entry.

fake_pg mode (cf. tests/test_instance.py / test_lab_sweep.py): mock psycopg2.connect,
no dependency on a running PG.
"""
import json
import re

import pytest

from pgtuner import instance

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
    """Fake pg_database: only understands the two query kinds pg_stat_file listing and pg_database (for sweep);
    other SQL (CREATE DATABASE / terminate / DROP) just executes and returns empty rows."""

    def __init__(self, rows):
        self.rows = rows

    def rows_for(self, sql):
        if "pg_stat_file" in sql or "FROM pg_database" in sql:
            return [(r[0], r[1], r[2], r[3], None, None) for r in self.rows]
        return []


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
def lab_env(tmp_path, fake_pg):
    """Registry + a fake instance on port 5599 ("lab", template scratch_tpl sealed)."""
    servers, _ = fake_pg
    reg = tmp_path / "r.json"
    instance._save(
        {"instances": [dict(name="lab", bindir="/nonexistent", pg_version="20devel",
                            build_type="release", data_dir="", port=5599,
                            socket_dir="/tmp", status="running", managed=True)],
         "clones": {}},
        reg)
    servers["5599"] = FakeServer([
        ("postgres", 5, True, False),
        ("template1", 1, True, True),
        ("template0", 4, False, True),
        ("scratch_tpl", 16384, False, False),
    ])
    return reg


def audit_rows(reg):
    path = instance._audit_log_path(reg)
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def sqls_of(conns):
    return [s for c in conns for s in c.sqls]


# ---------------------------- clone registration (owner/created_at) ----------------------------

def test_clone_records_owner_and_created_at(lab_env, monkeypatch):
    monkeypatch.setenv("PGTUNER_AGENT", "agentA")
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    data = json.loads(lab_env.read_text(encoding="utf-8"))
    meta = data["clone_meta"]["lab"][db]
    assert meta["owner"] == "agentA"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", meta["created_at"])
    assert instance.pool_stats(lab_env) == {"lab": [db]}            # pool_stats contract unchanged
    assert instance.pool_details(lab_env) == {"lab": {db: meta}}    # details carry ownership
    assert audit_rows(lab_env) == [                                 # clone event audited
        {"ts": meta["created_at"], "event": "clone", "instance": "lab",
         "db": db, "owner": "agentA", "actor": "agentA"}]


def test_load_clone_meta_roundtrip_and_absent(lab_env):
    """_load surfaces clone_meta only when the file really has it (old registries do not sprout keys out of thin air; existing == contract unbroken)."""
    loaded = instance._load(lab_env)
    assert "clone_meta" not in loaded                               # absent → not surfaced
    loaded["clone_meta"] = {"lab": {"x_c1a2b3c4": {"owner": "a", "created_at": "t"}}}
    instance._save(loaded, lab_env)
    assert instance._load(lab_env)["clone_meta"] == {"lab": {"x_c1a2b3c4": {"owner": "a", "created_at": "t"}}}


# ---------------------------- owner release: success + audit ----------------------------

def test_owner_release_succeeds_cleans_meta_and_audits(lab_env, monkeypatch):
    monkeypatch.setenv("PGTUNER_AGENT", "agentA")
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    instance.drop_db("lab", db, registry_file=lab_env)
    assert instance.pool_stats(lab_env) == {"lab": []}              # out of the pool
    assert instance.pool_details(lab_env) == {"lab": {}}            # ownership record synchronously cleared
    assert json.loads(lab_env.read_text(encoding="utf-8"))["clone_meta"] == {}
    events = [(r["event"], r["owner"], r["actor"]) for r in audit_rows(lab_env)]
    assert events == [("clone", "agentA", "agentA"), ("release", "agentA", "agentA")]


# ---------------------------- foreign release: denial + attribution audit (core regression) ----------------------------

def test_foreign_release_denied_keeps_clone_and_audits(lab_env, monkeypatch, fake_pg):
    _, conns = fake_pg
    monkeypatch.setenv("PGTUNER_AGENT", "agentA")
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    before = len(conns)

    monkeypatch.setenv("PGTUNER_AGENT", "agentB")                   # a different agent attempts the release
    with pytest.raises(RuntimeError, match="无权释放"):
        instance.drop_db("lab", db, registry_file=lab_env)

    assert len(conns) == before                                     # denial comes first: zero SQL sent
    assert instance.pool_stats(lab_env) == {"lab": [db]}            # clone still in the pool
    assert instance.pool_details(lab_env)["lab"][db]["owner"] == "agentA"
    denied = audit_rows(lab_env)[-1]                                # attribution evidence lands in the audit log
    assert (denied["event"], denied["owner"], denied["actor"],
            denied["db"], denied["instance"]) == \
        ("release-denied", "agentA", "agentB", db, "lab")
    assert not [s for s in sqls_of(conns) if s.startswith("DROP DATABASE")]


def test_foreign_release_force_allowed_but_audited(lab_env, monkeypatch):
    monkeypatch.setenv("PGTUNER_AGENT", "agentA")
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    monkeypatch.setenv("PGTUNER_AGENT", "agentB")
    instance.drop_db("lab", db, registry_file=lab_env, force=True)  # emergency channel
    assert instance.pool_stats(lab_env) == {"lab": []}
    last = audit_rows(lab_env)[-1]
    assert (last["event"], last["owner"], last["actor"], last["forced"]) \
        == ("release", "agentA", "agentB", True)                    # the trespass is not silent: the audit carries forced


# ---------------------------- backward compatibility: no ownership record, no blocking ----------------------------

def test_drop_db_refuses_non_clone_and_system_databases(lab_env, fake_pg):
    """Adversarial review R1-D: `lab release` must never DROP a real business database
    (not pool-registered, not clone-named), and system databases are refused even
    with --force."""
    import pytest
    with pytest.raises(RuntimeError, match="不在克隆池登记"):
        instance.drop_db("lab", "bench_new", registry_file=lab_env)   # a real business db
    # R2 tightening: clone NAME SHAPE alone must not suffice — a real business db
    # named like snapshot_c20241116 would slip past the pattern
    with pytest.raises(RuntimeError, match="不在克隆池登记"):
        instance.drop_db("lab", "snapshot_c20241116", registry_file=lab_env)
    instance.drop_db("lab", "snapshot_c20241116", registry_file=lab_env, force=True)  # explicit consent
    with pytest.raises(RuntimeError, match="系统库"):
        instance.drop_db("lab", "postgres", registry_file=lab_env, force=True)
    with pytest.raises(RuntimeError, match="系统库"):
        instance.drop_db("lab", "template1", registry_file=lab_env, force=True)
    # the non-clone refusal is audited (system-db refusals raise before the audit)
    events = [row["event"] for row in audit_rows(lab_env)]
    assert events.count("release-denied") >= 1


def test_release_legacy_registration_without_meta_still_works(lab_env, fake_pg):
    """Clones registered in old registries (no clone_meta) are not blocked — make_template cleanup / sweep of legacy targets works as before."""
    _, conns = fake_pg
    reg = instance._load(lab_env)
    reg["clones"]["lab"] = ["scratch_tpl_caaaa1111"]                # legacy style: only a database-name list
    instance._save(reg, lab_env)
    instance.drop_db("lab", "scratch_tpl_caaaa1111", registry_file=lab_env)
    assert 'DROP DATABASE IF EXISTS "scratch_tpl_caaaa1111"' in sqls_of(conns)
    assert instance.pool_stats(lab_env) == {"lab": []}
    assert audit_rows(lab_env)[-1]["event"] == "release"            # still leaves a trace (owner=null)


def test_sweep_yes_denies_foreign_owned_clone(lab_env, monkeypatch, fake_pg):
    """sweep --yes goes through the drop_db single point and gets the same ownership protection: someone else's owned clone goes into errors, no DROP is sent
    (zero changes to the sweep code; the protection comes from drop_db; ownerless/legacy-registered clones are unaffected)."""
    servers, conns = fake_pg
    monkeypatch.setattr(instance, "ensure_running", lambda n, registry_file=None: None)
    monkeypatch.setenv("PGTUNER_AGENT", "agentA")
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    servers["5599"].rows = servers["5599"].rows + [(db, 16390, True, False)]

    monkeypatch.setenv("PGTUNER_AGENT", "agentB")                   # another agent runs sweep
    rep = instance.sweep("lab", yes=True, registry_file=lab_env)
    assert rep["targets"][0]["dbname"] == db and rep["targets"][0]["kind"] == "registered"
    assert rep["dropped"] == [] and len(rep["errors"]) == 1         # denied → recorded in errors, no crash
    assert "无权释放" in rep["errors"][0]["error"]
    assert not [s for s in sqls_of(conns) if s.startswith("DROP DATABASE")]
    assert instance.pool_stats(lab_env) == {"lab": [db]}            # clone preserved
    assert audit_rows(lab_env)[-1]["event"] == "release-denied"


# ---------------------------- audit_tail: filtering / limit / bad lines ----------------------------

def test_audit_tail_filter_limit_and_bad_lines(lab_env, monkeypatch):
    monkeypatch.setenv("PGTUNER_AGENT", "agentA")
    for _ in range(3):
        instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    reg = instance._load(lab_env)
    reg["clones"]["other"] = []                                     # create a few clones of another instance too
    instance._save(reg, lab_env)
    path = instance._audit_log_path(lab_env)
    with open(path, "a", encoding="utf-8") as f:
        f.write("{broken json\n")                                   # bad line: skipped without crashing
    rows = instance.audit_tail(registry_file=lab_env)
    assert len(rows) == 3 and all(r["event"] == "clone" for r in rows)
    assert instance.audit_tail(limit=2, registry_file=lab_env) == rows[-2:]
    assert instance.audit_tail(instance_name="nope", registry_file=lab_env) == []
    assert instance.audit_tail(registry_file=lab_env.parent / "none.json") == []


# ---------------------------- CLI: release protection / stats owner / audit ----------------------------

def test_cli_release_denied_then_force(monkeypatch, lab_env, fake_pg):
    from click.testing import CliRunner
    from pgtuner import cli
    monkeypatch.setattr(instance, "INSTANCE_REGISTRY", lab_env)
    monkeypatch.setenv("PGTUNER_AGENT", "agentA")
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)

    monkeypatch.setenv("PGTUNER_AGENT", "agentB")
    denied = CliRunner().invoke(cli.cli, ["lab", "release", "lab", db])
    assert denied.exit_code != 0
    assert "无权释放" in denied.output and "agentA" in denied.output and "--force" in denied.output
    assert instance.pool_stats(lab_env) == {"lab": [db]}            # clone preserved

    forced = CliRunner().invoke(cli.cli, ["lab", "release", "lab", db, "--force"])
    assert forced.exit_code == 0, forced.output
    assert instance.pool_stats(lab_env) == {"lab": []}
    assert audit_rows(lab_env)[-1]["forced"] is True


def test_cli_stats_shows_owner_and_audit_lists_events(monkeypatch, lab_env):
    from click.testing import CliRunner
    from pgtuner import cli
    monkeypatch.setattr(instance, "INSTANCE_REGISTRY", lab_env)
    monkeypatch.setenv("PGTUNER_AGENT", "agentA")
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)

    stats = CliRunner().invoke(cli.cli, ["lab", "stats"])
    assert stats.exit_code == 0, stats.output
    assert f"{db} @agentA" in stats.output                          # who holds what at a glance

    audit = CliRunner().invoke(cli.cli, ["lab", "audit", "--last", "5"])
    assert audit.exit_code == 0, audit.output
    assert "clone" in audit.output and "agentA" in audit.output and db in audit.output
