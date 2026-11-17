"""Regression for bench --init auto-creating a missing database (tool-w1 fix): with --init, a missing
target database no longer errors out directly; instead it connects to the maintenance database postgres,
runs CREATE DATABASE, then reconnects. Non-missing-db causes (service not started) and the non---init
path still report errors with the original classified hints. Does not depend on a running PG
(psycopg2.connect fully mocked).
"""
import os

import psycopg2
import pytest
from click.testing import CliRunner

from pgtuner import bench, cli, hardware, pg
from pgtuner.signals import Signals

KNOWLEDGE_PATH = os.path.join(os.path.dirname(__file__), "..", "pgtuner", "data", "guc_knowledge.json")


class FakeCursor:
    def __init__(self, sqls, execute_exc=None):
        self.sqls = sqls
        self.execute_exc = execute_exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        if self.execute_exc:
            raise self.execute_exc


class FakeConn:
    def __init__(self, sqls, execute_exc=None):
        self.sqls = sqls
        self.closed = False
        self.cursor_obj = FakeCursor(sqls, execute_exc)

    def cursor(self):
        return self.cursor_obj

    def set_session(self, **kw):
        pass

    def close(self):
        self.closed = True


# ---------------------------- pg.create_db unit tests ----------------------------

def test_create_db_maintains_dsn_and_quotes(monkeypatch):
    """The dsn only borrows host/port/credentials, dbname is replaced with the maintenance database postgres; identifiers in the create statement are quoted."""
    captured = {}

    def fake_connect(conninfo, connect_timeout=None):
        captured["conninfo"] = conninfo
        return FakeConn(captured.setdefault("sqls", []))

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    pg.create_db("host=/tmp port=5436 dbname=bench_new user=who password=p")
    kw = psycopg2.extensions.parse_dsn(captured["conninfo"])
    assert kw["dbname"] == "postgres"                     # maintenance database
    assert kw["host"] == "/tmp" and kw["port"] == "5436"
    assert kw["user"] == "who" and kw["password"] == "p"  # credentials preserved
    assert captured["sqls"] == ['CREATE DATABASE "bench_new"']


def test_create_db_from_uri_dsn(monkeypatch):
    """A postgresql:// URI-form dsn likewise overrides dbname=postgres and keeps host/password."""
    captured = {}

    def fake_connect(conninfo, connect_timeout=None):
        captured["conninfo"] = conninfo
        return FakeConn(captured.setdefault("sqls", []))

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    pg.create_db("postgresql://u:pw@localhost:5437/bench_new")
    kw = psycopg2.extensions.parse_dsn(captured["conninfo"])
    assert kw["dbname"] == "postgres" and kw["port"] == "5437"
    assert kw["user"] == "u" and kw["password"] == "pw"
    assert captured["sqls"] == ['CREATE DATABASE "bench_new"']


def test_create_db_default_target_and_explicit_name(monkeypatch):
    """Without a dsn, environment/default target is used (/tmp:5432); explicit dbname takes precedence over the parsed value, embedded double quotes are escaped."""
    captured = {}

    def fake_connect(conninfo, connect_timeout=None):
        captured["conninfo"] = conninfo
        return FakeConn(captured.setdefault("sqls", []))

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    for var in ("PGHOST", "PGPORT", "PGDATABASE"):
        monkeypatch.delenv(var, raising=False)             # environment defaults → /tmp:5432/bench_new
    pg.create_db()
    kw = psycopg2.extensions.parse_dsn(captured["conninfo"])
    assert kw["dbname"] == "postgres" and kw["port"] == "5432"
    assert captured["sqls"] == ['CREATE DATABASE "bench_new"']     # default target database name
    pg.create_db("host=/tmp port=5436 dbname=bench_new", 'we"ird')
    assert captured["sqls"][-1] == 'CREATE DATABASE "we""ird"'     # identifier escaping


def test_create_db_idempotent_on_duplicate(monkeypatch):
    """Database already exists (DuplicateDatabase) silently skipped — idempotent."""
    dup = psycopg2.errors.DuplicateDatabase("already exists")

    def fake_connect(conninfo, connect_timeout=None):
        return FakeConn([], execute_exc=dup)

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    pg.create_db("host=/tmp port=5436 dbname=bench_new")     # passes if nothing raises


def test_create_db_connect_failure_raises_connection_error(monkeypatch):
    """Maintenance database unreachable (service not started) → ConnectionError; the caller falls back to reporting the original connection error."""
    monkeypatch.setattr(psycopg2, "connect",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            psycopg2.OperationalError(
                                'connection to server on socket "/tmp/.s.PGSQL.9999" failed: '
                                "No such file or directory")))
    with pytest.raises(ConnectionError) as ei:
        pg.create_db("host=/tmp port=9999 dbname=bench_new")
    assert "维护库 postgres 连接失败" in str(ei.value)


def test_create_db_permission_error_raises_runtime_error(monkeypatch):
    """CREATE DATABASE itself fails (e.g. insufficient privilege) → RuntimeError carrying the last error line."""
    denied = psycopg2.errors.InsufficientPrivilege('permission denied to create database')

    def fake_connect(conninfo, connect_timeout=None):
        return FakeConn([], execute_exc=denied)

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    with pytest.raises(RuntimeError) as ei:
        pg.create_db("host=/tmp port=5436 dbname=bench_new")
    assert "CREATE DATABASE bench_new 失败" in str(ei.value)


# ---------------------------- bench command flow (full chain mocked) ----------------------------

def _fake_hw():
    return hardware.HardwareInfo(total_ram_gb=16, cpu_cores=8, os_type="linux",
                                 is_wsl2=False, disk_type="ssd", disk_confidence="high")


def _fake_sig():
    sig = Signals()
    sig.settings = {
        "server_version": {"setting": "20devel", "unit": None, "context": "internal"},
        "server_version_num": {"setting": "200002", "unit": None, "context": "internal"},
        "max_connections": {"setting": "100", "unit": None, "context": "user"},
    }
    return sig


class FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _make_exe(tmp_path):
    exe = tmp_path / "pgbench"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


MISSING_DB = ConnectionError(
    "库不存在？目标 /tmp:5436/bench_new。创建：createdb -h /tmp -p 5436 bench_new，"
    "或用 --dsn 指向已有库\n原始错误: FATAL:  database \"bench_new\" does not exist")


def test_bench_init_creates_missing_db_then_inits(monkeypatch, tmp_path):
    """tool-w1 regression: --init with a missing database → create_db → reconnect → pgbench_accounts still missing → pgbench -i."""
    calls = {"connect": 0}
    conn = object()

    def fake_connect(dsn=None):
        calls["connect"] += 1
        if calls["connect"] == 1:
            raise MISSING_DB                       # first connect fails due to missing database
        return conn

    monkeypatch.setattr(cli.pg, "connect", fake_connect)
    monkeypatch.setattr(cli.pg, "create_db", lambda dsn, dbname: calls.update(created=dbname))
    monkeypatch.setattr(cli.pg, "fetch_one", lambda c, sql: (None,))   # still no tables after database creation
    monkeypatch.setattr(cli.hardware, "probe", lambda p: _fake_hw())
    monkeypatch.setattr(cli.signals, "collect", lambda c, cpu_cores: _fake_sig())
    monkeypatch.setattr(cli.bench_mod, "run_init",
                        lambda *a, **kw: calls.update(init=a) or FakeProc())
    ok_result = bench.BenchResult(bench.Candidate("baseline"), tps=100.0, latency_ms=1.0)
    monkeypatch.setattr(cli.bench_mod, "run_candidates",
                        lambda cands, **kw: [ok_result] * len(cands))
    result = CliRunner().invoke(cli.cli, [
        "bench", "--dsn", "host=/tmp port=5436 dbname=bench_new",
        "--knowledge", KNOWLEDGE_PATH,                        # absolute path, runs from any cwd
        "--pgbench", _make_exe(tmp_path), "--init", "--duration", "1"])
    assert result.exit_code == 0, result.output
    assert calls["created"] == "bench_new"                   # missing database → automatic CREATE DATABASE
    assert calls["connect"] == 2                             # reconnect after creation succeeds
    assert calls["init"] == (_make_exe(tmp_path), "/tmp", "5436", "bench_new")  # then pgbench -i
    assert "已自动创建" in result.output


def test_bench_init_service_down_reports_original_error(monkeypatch, tmp_path):
    """When the service is not started, create_db also cannot reach the maintenance database → report the original connection error (no longer stacking the create-database failure hint)."""
    down = ConnectionError("服务没起？目标 /tmp:5436/bench_new。…")
    monkeypatch.setattr(cli.pg, "connect", lambda dsn=None: (_ for _ in ()).throw(down))

    def create_db_boom(dsn, dbname):
        raise ConnectionError("维护库 postgres 连接失败: No such file or directory")

    monkeypatch.setattr(cli.pg, "create_db", create_db_boom)
    result = CliRunner().invoke(cli.cli, [
        "bench", "--dsn", "host=/tmp port=5436 dbname=bench_new",
        "--knowledge", KNOWLEDGE_PATH,
        "--pgbench", _make_exe(tmp_path), "--init", "--duration", "1"])
    assert result.exit_code != 0
    assert "服务没起" in result.output                    # original classified hint
    assert "自动建库失败" not in result.output            # no create-database noise stacked on top


def test_bench_without_init_keeps_friendly_error(monkeypatch, tmp_path):
    """Without --init: a missing database still errors with the original friendly hint, and never auto-creates the database."""
    created = []
    monkeypatch.setattr(cli.pg, "connect", lambda dsn=None: (_ for _ in ()).throw(MISSING_DB))
    monkeypatch.setattr(cli.pg, "create_db", lambda dsn, dbname: created.append(dbname))
    result = CliRunner().invoke(cli.cli, [
        "bench", "--dsn", "host=/tmp port=5436 dbname=bench_new",
        "--knowledge", KNOWLEDGE_PATH,
        "--pgbench", _make_exe(tmp_path), "--duration", "1"])
    assert result.exit_code != 0
    assert "库不存在" in result.output and "createdb" in result.output
    assert created == []                                  # database untouched


def test_bench_init_create_denied_reports_both(monkeypatch, tmp_path):
    """Database creation itself fails (insufficient privilege): the original missing-db hint + the create failure reason are both given."""
    monkeypatch.setattr(cli.pg, "connect", lambda dsn=None: (_ for _ in ()).throw(MISSING_DB))

    def create_db_boom(dsn, dbname):
        raise RuntimeError("CREATE DATABASE bench_new 失败: permission denied")

    monkeypatch.setattr(cli.pg, "create_db", create_db_boom)
    result = CliRunner().invoke(cli.cli, [
        "bench", "--dsn", "host=/tmp port=5436 dbname=bench_new",
        "--knowledge", KNOWLEDGE_PATH,
        "--pgbench", _make_exe(tmp_path), "--init", "--duration", "1"])
    assert result.exit_code != 0
    assert "库不存在" in result.output and "自动建库失败" in result.output
    assert "permission denied" in result.output
