"""instance tests: registry read/write round-trips, build_type detection, port allocation, create/clone flows
(mocked subprocess.run + mocked psycopg2.connect, no running PG needed), destroy guards."""
import json
import re
from pathlib import Path

import pytest

from pgtuner import instance

BINDIR_TOOLS = ("pg_config", "initdb", "pg_ctl", "pg_isready")


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeCursor:
    def __init__(self, sqls, conn=None):
        self._sqls = sqls
        self._conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._sqls.append(sql)

    def fetchone(self):
        # default (False,) = sealed; tests can override the return value via conn.fetch_queue
        if self._conn is not None and self._conn.fetch_queue:
            return self._conn.fetch_queue.pop(0)
        return (False,)


class FakeConn:
    def __init__(self, dsn):
        self.dsn = dsn
        self.sqls = []
        self.closed = False
        self.fetch_queue = []

    def cursor(self):
        return FakeCursor(self.sqls, self)

    def set_session(self, **kw):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def bindir(tmp_path):
    """Fake PG installation directory: executable empty tool stubs (pass _tool's existence/permission checks)."""
    d = tmp_path / "bin"
    d.mkdir()
    for tool in BINDIR_TOOLS:
        p = d / tool
        p.write_text("#!/bin/sh\n", encoding="utf-8")
        p.chmod(0o755)
    return str(d)


@pytest.fixture
def fake_pg(monkeypatch):
    """Mocked psycopg2.connect: records the dsn and executed SQL (pg.connect_rw/execute run through the real code path)."""
    import psycopg2
    conns = []

    def fake_connect(dsn, **kw):
        conn = FakeConn(dsn)
        conns.append(conn)
        return conn

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    return conns


def make_fake_run(version="PostgreSQL 19devel", configure="'--prefix=/x' '--enable-debug'",
                  initdb_rc=0, ctl_rc=0, isready_rc=0, calls=None):
    """Fake subprocess.run dispatching by tool name; the initdb stub really creates the directory and writes a minimal postgresql.conf."""
    def fake_run(cmd, env=None, capture_output=None, text=None, timeout=None):
        if calls is not None:
            calls.append(cmd)
        tool = Path(cmd[0]).name
        if tool == "pg_config":
            return FakeProc(stdout=version if "--version" in cmd else configure)
        if tool == "initdb":
            data_dir = Path(cmd[cmd.index("-D") + 1])
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "postgresql.conf").write_text("# initdb stub\n", encoding="utf-8")
            return FakeProc(returncode=initdb_rc,
                            stderr="initdb: error: cannot create directory" if initdb_rc else "")
        if tool == "pg_ctl":
            if cmd[-1] == "status":               # status: non-zero = not running (normal semantics)
                return FakeProc(returncode=ctl_rc)
            return FakeProc()                     # start/stop succeed
        if tool == "pg_isready":
            return FakeProc(returncode=isready_rc)
        return FakeProc()

    return fake_run


def inst_dict(name="lab", bindir="/nonexistent", port=5599, **over):
    """Build the registry's instance dict directly (bypassing create, no PG dependency)."""
    d = dict(name=name, bindir=bindir, pg_version="19devel", build_type="release",
             data_dir="", port=port, socket_dir="/tmp", status="stopped", managed=True)
    d.update(over)
    return d


def put_inst(reg_file, insts, clones=None):
    instance._save({"instances": insts, "clones": clones or {}}, reg_file)


# ---------------------------- pure functions: version/build-type parsing ----------------------------

def test_parse_pg_version():
    assert instance.parse_pg_version("PostgreSQL 19devel") == "19devel"
    assert instance.parse_pg_version("PostgreSQL 16.4\n") == "16.4"
    assert instance.parse_pg_version("17.2") == "17.2"          # also handles input without the product-name prefix
    with pytest.raises(ValueError):
        instance.parse_pg_version("   ")


def test_build_type_from_configure():
    assert instance.build_type_from_configure("'--prefix=/x' '--enable-debug'") == "debug"
    assert instance.build_type_from_configure("'--enable-cassert' '--with-openssl'") == "debug"
    assert instance.build_type_from_configure("'--prefix=/usr' '--with-icu'") == "release"
    assert instance.build_type_from_configure("") == "release"


# ---------------------------- registry read/write round-trip ----------------------------

def test_registry_save_load_roundtrip(tmp_path):
    reg = tmp_path / "r.json"
    inst = instance.Instance(name="a", bindir="/b", pg_version="19devel",
                             build_type="debug", data_dir="/d/a", port=5433)
    instance._save({"instances": [inst.to_dict()], "clones": {"a": ["x_c1"]}}, reg)
    assert reg.exists()
    loaded = instance._load(reg)
    assert loaded == {"instances": [inst.to_dict()], "clones": {"a": ["x_c1"]}}
    assert instance.Instance.from_dict(loaded["instances"][0]) == inst   # dict → instance round-trip is identical
    assert instance._load(tmp_path / "none.json") == {"instances": [], "clones": {}}  # no file → skeleton


def test_register_and_get_roundtrip(bindir, tmp_path, monkeypatch):
    reg = tmp_path / "r.json"
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run())
    inst = instance.register("dev19", bindir, port=5555, registry_file=reg)
    assert (inst.pg_version, inst.build_type, inst.port, inst.data_dir, inst.managed, inst.status) \
        == ("19devel", "debug", 5555, "", False, "unknown")
    instance.register("aaa17", bindir, port=5433, registry_file=reg)
    # on-disk JSON structure matches the contract; get/list_instances round-trips the registered values
    data = json.loads(reg.read_text(encoding="utf-8"))
    assert set(data) == {"instances", "clones"} and data["clones"] == {}
    assert {d["name"] for d in data["instances"]} == {"dev19", "aaa17"}
    assert instance.get("dev19", reg).to_dict() == inst.to_dict()
    assert [i.name for i in instance.list_instances(reg)] == ["aaa17", "dev19"]   # sorted by name


def test_register_missing_pg_config(tmp_path):
    with pytest.raises(ValueError, match="pg_config"):
        instance.register("x", str(tmp_path / "nope"), port=5433,
                          registry_file=tmp_path / "r.json")


def test_register_pg_config_not_executable(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    (d / "pg_config").write_text("", encoding="utf-8")            # default 0644, not executable
    with pytest.raises(ValueError, match="pg_config"):
        instance.register("x", str(d), port=5433, registry_file=tmp_path / "r.json")


def test_get_unknown_raises(tmp_path):
    with pytest.raises(ValueError, match="未注册"):
        instance.get("ghost", tmp_path / "r.json")


# ---------------------------- port allocation ----------------------------

def test_find_free_port_first_free(bindir, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(instance.subprocess, "run",
                        lambda cmd, **kw: (calls.append(cmd), FakeProc(returncode=1))[1])
    assert instance.find_free_port(bindir, socket_dir=str(tmp_path)) == 5433
    assert Path(calls[0][0]).name == "pg_isready"                  # absolute-path tool
    assert calls[0][-2:] == ["-p", "5433"]


def test_find_free_port_skips_socket_file(bindir, tmp_path, monkeypatch):
    (tmp_path / ".s.PGSQL.5433").write_text("", encoding="utf-8")  # socket file present → occupied
    monkeypatch.setattr(instance.subprocess, "run", lambda cmd, **kw: FakeProc(returncode=1))
    assert instance.find_free_port(bindir, socket_dir=str(tmp_path)) == 5434


def test_find_free_port_skips_accepting_server(bindir, tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        return FakeProc(returncode=0 if cmd[-1] == "5433" else 1)  # a server is accepting connections on 5433
    monkeypatch.setattr(instance.subprocess, "run", fake_run)
    assert instance.find_free_port(bindir, socket_dir=str(tmp_path)) == 5434


# ---------------------------- create flow (mocked subprocess) ----------------------------

def test_create_flow(bindir, tmp_path, monkeypatch):
    reg, root = tmp_path / "r.json", tmp_path / "data"
    calls = []
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run(calls=calls))
    inst = instance.create("lab", bindir, port=5599, registry_file=reg, data_root=root)
    assert (inst.pg_version, inst.build_type, inst.port, inst.status, inst.managed) \
        == ("19devel", "debug", 5599, "running", True)
    assert inst.data_dir == str(root / "lab")
    by_tool = {}
    for cmd in calls:
        by_tool.setdefault(Path(cmd[0]).name, []).append(cmd)
    data_dir = str(root / "lab")
    assert by_tool["initdb"][0] == [f"{bindir}/initdb", "-D", data_dir,
                                    "--auth-local=trust", "--encoding=UTF8"]
    conf = (root / "lab" / "postgresql.conf").read_text(encoding="utf-8")
    assert "listen_addresses = ''" in conf
    assert "port = 5599" in conf
    assert "unix_socket_directories = '/tmp'" in conf
    assert by_tool["pg_ctl"] == [[f"{bindir}/pg_ctl", "-D", data_dir,
                                  "-w", "-t", "60", "-l", f"{data_dir}/logfile", "start"]]
    assert by_tool["pg_isready"]                                  # readiness polling did run
    assert instance.get("lab", reg).port == 5599                  # registered


def test_create_auto_port(bindir, tmp_path, monkeypatch):
    monkeypatch.setattr(instance, "find_free_port", lambda *a, **k: 6100)
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run())
    inst = instance.create("lab", bindir, registry_file=tmp_path / "r.json",
                           data_root=tmp_path / "d")
    assert inst.port == 6100
    assert "port = 6100" in (tmp_path / "d" / "lab" / "postgresql.conf").read_text(encoding="utf-8")


def test_register_rejects_unsafe_names(bindir, tmp_path, monkeypatch):
    """Adversarial review R1-D-m2: instance names become data-directory path components —
    path traversal ('../x', absolute paths, separators) and exotic chars are refused up
    front, before any registry write or initdb."""
    reg = tmp_path / "r.json"
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run())
    for bad in ("../escape", "a/b", "a b", "..", "", "x" * 65, "-leading", "测试库"):
        with pytest.raises(ValueError, match="不合法"):
            instance.register(bad, bindir, port=5433, registry_file=reg)
        with pytest.raises(ValueError, match="不合法"):
            instance.create(bad, bindir, port=5433, registry_file=reg)
    assert not reg.exists()                     # nothing ever landed on disk


def test_create_duplicate_name(bindir, tmp_path, monkeypatch):
    reg = tmp_path / "r.json"
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run())
    instance.create("lab", bindir, port=5500, registry_file=reg, data_root=tmp_path / "d")
    with pytest.raises(ValueError, match="已注册"):
        instance.create("lab", bindir, port=5501, registry_file=reg, data_root=tmp_path / "d2")


def test_create_nonempty_data_dir(bindir, tmp_path, monkeypatch):
    d = tmp_path / "d" / "lab"
    d.mkdir(parents=True)
    (d / "PG_VERSION").write_text("19\n", encoding="utf-8")
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run())
    with pytest.raises(ValueError, match="非空"):
        instance.create("lab", bindir, port=5500, registry_file=tmp_path / "r.json",
                        data_root=tmp_path / "d")


def test_create_failure_raises_runtime_error(bindir, tmp_path, monkeypatch):
    reg = tmp_path / "r.json"
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run(initdb_rc=1))
    with pytest.raises(RuntimeError, match="initdb"):
        instance.create("bad", bindir, port=5500, registry_file=reg, data_root=tmp_path / "d")
    assert instance.list_instances(reg) == []                     # failure leaves no registration


# ---------------------------- start/stop idempotence ----------------------------

def test_start_idempotent_when_ready(bindir, tmp_path, monkeypatch):
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir)])
    calls = []
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run(isready_rc=0, calls=calls))
    instance.start("lab", reg)
    assert not any(Path(c[0]).name == "pg_ctl" for c in calls)    # already running → no more pg_ctl
    assert instance.get("lab", reg).status == "running"


def test_stop_issues_fast_mode(bindir, tmp_path, monkeypatch):
    reg = tmp_path / "r.json"
    data_dir = str(tmp_path / "d")
    put_inst(reg, [inst_dict(bindir=bindir, data_dir=data_dir, status="running")])
    calls = []
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run(ctl_rc=0, calls=calls))
    instance.stop("lab", registry_file=reg)
    stop_cmds = [c for c in calls if Path(c[0]).name == "pg_ctl" and c[-1] == "stop"]
    assert stop_cmds == [[f"{bindir}/pg_ctl", "-D", data_dir,
                          "-m", "fast", "-w", "-t", "60", "stop"]]
    assert instance.get("lab", reg).status == "stopped"


def test_stop_idempotent_when_stopped(bindir, tmp_path, monkeypatch):
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir, data_dir=str(tmp_path / "d"))])
    calls = []
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run(ctl_rc=3, calls=calls))
    instance.stop("lab", registry_file=reg)
    assert not any(Path(c[0]).name == "pg_ctl" and c[-1] == "stop" for c in calls)
    assert instance.get("lab", reg).status == "stopped"


def test_stop_invalid_mode(bindir, tmp_path):
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir)])
    with pytest.raises(ValueError, match="模式"):
        instance.stop("lab", mode="kill", registry_file=reg)


def test_ensure_running_already_ready(bindir, tmp_path, monkeypatch):
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir)])
    calls = []
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run(isready_rc=0, calls=calls))
    inst = instance.ensure_running("lab", reg)
    assert inst.name == "lab"
    assert not any(Path(c[0]).name == "pg_ctl" for c in calls)


def test_ensure_running_starts_and_waits(bindir, tmp_path, monkeypatch):
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir, data_dir=str(tmp_path / "d"))])
    state = {"ready": False}

    def fake_run(cmd, **kw):
        if Path(cmd[0]).name == "pg_ctl" and cmd[-1] == "start":
            state["ready"] = True                                  # ready only after start
            return FakeProc()
        if Path(cmd[0]).name == "pg_isready":
            return FakeProc(returncode=0 if state["ready"] else 1)
        return FakeProc()

    monkeypatch.setattr(instance.subprocess, "run", fake_run)
    inst = instance.ensure_running("lab", reg)
    assert inst.status == "running"
    assert instance.is_ready("lab", reg)


# ---------------------------- dsn / destroy guards ----------------------------

def test_dsn_of(bindir, tmp_path):
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir, port=5599)])
    assert instance.dsn_of("lab", "bench_new", reg) == "host=/tmp port=5599 dbname='bench_new'"


def test_dsn_of_escapes_dbname_as_one_conninfo_value(bindir, tmp_path):
    """D5: dbname is caller-supplied (CLI --db) and is embedded as ONE quoted conninfo
    value — single quotes + backslash escaping, libpq's own keyword/value rules (exactly
    what psycopg2.extensions.make_dsn emits). Spaces, 'host='/'port=' fragments, quotes
    and backslashes cannot inject conninfo keys: parsing the DSN back with psycopg2's own
    conninfo parser yields the hostile string intact as dbname and the instance's
    host/port untouched."""
    pytest.importorskip("psycopg2")
    from psycopg2.extensions import parse_dsn
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir, port=5599)])
    for hostile in ("x host=/tmp/evil port=1", "my db", 'we"ird', "it's",
                    "back\\slash 'q' end", ""):
        dsn = instance.dsn_of("lab", hostile, reg)
        parsed = parse_dsn(dsn)
        assert parsed["dbname"] == hostile          # one single value; injected keys dead
        assert parsed["host"] == "/tmp" and parsed["port"] == "5599"


def test_destroy_managed_removes_dir_and_registry(bindir, tmp_path, monkeypatch):
    reg = tmp_path / "r.json"
    data = tmp_path / "d" / "lab"
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("19\n", encoding="utf-8")
    put_inst(reg, [inst_dict(bindir=bindir, data_dir=str(data), status="stopped")],
             clones={"lab": ["tpub_cab12cd34"]})
    monkeypatch.setattr(instance.subprocess, "run", make_fake_run(ctl_rc=3))
    instance.destroy("lab", registry_file=reg)
    assert not data.exists()                                       # data directory deleted
    assert instance.list_instances(reg) == []                      # deregistered
    assert instance.pool_stats(reg) == {}                          # clone records cleared along with the instance


def test_destroy_unmanaged_requires_force(bindir, tmp_path, monkeypatch):
    reg = tmp_path / "r.json"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "PG_VERSION").write_text("17\n", encoding="utf-8")
    put_inst(reg, [inst_dict(name="ext", bindir=bindir,
                             data_dir=str(foreign), managed=False)])

    def no_run(cmd, **kw):
        raise AssertionError("destroy 外部实例不应跑任何 subprocess")

    monkeypatch.setattr(instance.subprocess, "run", no_run)
    with pytest.raises(ValueError, match="force"):
        instance.destroy("ext", registry_file=reg)                 # without force → refused
    assert instance.get("ext", reg).managed is False               # still registered
    instance.destroy("ext", force=True, registry_file=reg)
    assert foreign.exists()                                        # never deletes someone else's data directory
    assert instance.list_instances(reg) == []


# ---------------------------- templates and the clone pool (mocked psycopg2.connect) ----------------------------

def test_make_template_sql_sequence(bindir, tmp_path, fake_pg):
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir, port=5599)])
    setup = tmp_path / "setup.sql"
    setup.write_text("CREATE TABLE t(i int);\nINSERT INTO t SELECT g FROM generate_series(1,10) g;\n",
                     encoding="utf-8")
    instance.make_template("lab", "tpub", str(setup), registry_file=reg)
    assert [c.dsn for c in fake_pg] == [              # postgres → template database → postgres → postgres (seal verification)
        "host=/tmp port=5599 dbname='postgres'",
        "host=/tmp port=5599 dbname='tpub'",
        "host=/tmp port=5599 dbname='postgres'",
        "host=/tmp port=5599 dbname='postgres'",
    ]
    sqls = [s for c in fake_pg for s in c.sqls]
    assert sqls[0] == 'CREATE DATABASE "tpub"'
    assert sqls[1] == setup.read_text(encoding="utf-8")            # the whole setup_sql in a single execute
    assert sqls[2] == "UPDATE pg_database SET datallowconn = false WHERE datname = 'tpub'"
    assert sqls[3] == "SELECT datallowconn FROM pg_database WHERE datname = 'tpub'"
    assert all(c.closed for c in fake_pg)


def test_make_template_empty_sql_still_seals(bindir, tmp_path, fake_pg):
    """B1 regression: an empty setup_sql must not block sealing (the old implementation errored on execute("") → the template stayed unsealed yet cloneable)."""
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir, port=5599)])
    setup = tmp_path / "empty.sql"
    setup.write_text("", encoding="utf-8")
    instance.make_template("lab", "tpub", str(setup), registry_file=reg)
    dsns = [c.dsn for c in fake_pg]
    assert "host=/tmp port=5599 dbname='tpub'" not in dsns         # an empty script does not connect to the template database
    sqls = [s for c in fake_pg for s in c.sqls]
    assert any("datallowconn = false" in s for s in sqls)          # sealing still executes
    assert any("SELECT datallowconn" in s for s in sqls)           # and the read-back verification follows


def test_make_template_seal_failure_cleans_up(bindir, tmp_path, fake_pg, monkeypatch):
    """B1 regression: seal verification fails → raises and cleans up the half-built template, leaving no unsealed database."""
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir, port=5599)])
    setup = tmp_path / "setup.sql"
    setup.write_text("CREATE TABLE t(i int);", encoding="utf-8")
    import psycopg2
    inner = psycopg2.connect                       # the fake_connect already installed by the fixture

    def bad_seal_connect(dsn, **kw):
        conn = inner(dsn, **kw)
        conn.fetch_queue = [(True,)]               # datallowconn=true → simulates the seal not taking effect
        return conn

    monkeypatch.setattr(psycopg2, "connect", bad_seal_connect)
    with pytest.raises(RuntimeError, match="密封校验失败"):
        instance.make_template("lab", "tpub", str(setup), registry_file=reg)
    sqls = [s for c in fake_pg for s in c.sqls]
    assert 'DROP DATABASE IF EXISTS "tpub"' in sqls                # the half-built one is cleaned up


def test_make_template_missing_sql(bindir, tmp_path):
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir)])
    with pytest.raises(ValueError, match="setup_sql"):
        instance.make_template("lab", "tpub", str(tmp_path / "nope.sql"), registry_file=reg)


def test_clone_default_name_and_registry(bindir, tmp_path, fake_pg):
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir, port=5599)])
    new_db = instance.clone("lab", "tpub", registry_file=reg)
    assert re.fullmatch(r"tpub_c[0-9a-f]{8}", new_db)              # default name f"{tpl}_c{8 random hex chars}"
    assert fake_pg[0].dsn == "host=/tmp port=5599 dbname='postgres'"  # TEMPLATE executed via the postgres database
    assert fake_pg[0].sqls == [f'CREATE DATABASE "{new_db}" TEMPLATE "tpub"']
    assert instance.pool_stats(reg) == {"lab": [new_db]}           # registered in the clone pool


def test_clone_explicit_name(bindir, tmp_path, fake_pg):
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir)])
    assert instance.clone("lab", "tpub", new_db="mydb", registry_file=reg) == "mydb"
    assert fake_pg[0].sqls == ['CREATE DATABASE "mydb" TEMPLATE "tpub"']
    assert instance.pool_stats(reg) == {"lab": ["mydb"]}


def test_drop_db_terminates_then_drops(bindir, tmp_path, fake_pg):
    reg = tmp_path / "r.json"
    put_inst(reg, [inst_dict(bindir=bindir)], clones={"lab": ["tpub_c1", "tpub_c2"]})
    instance.drop_db("lab", "tpub_c1", registry_file=reg)
    assert [c.dsn for c in fake_pg] == ["host=/tmp port=5599 dbname='postgres'"] * 2
    sqls = [s for c in fake_pg for s in c.sqls]                    # one connection opened per SQL statement
    assert "pg_terminate_backend(pid)" in sqls[0] and "'tpub_c1'" in sqls[0]
    assert sqls[1] == 'DROP DATABASE IF EXISTS "tpub_c1"'
    assert instance.pool_stats(reg) == {"lab": ["tpub_c2"]}        # only the dropped one is removed


def test_pool_stats_empty(tmp_path):
    assert instance.pool_stats(tmp_path / "none.json") == {}


def test_tool_accepts_prefix_form(tmp_path):
    """The --prefix form (no bin suffix) automatically falls back to <prefix>/bin/<tool> (regression of an integration fix)."""
    from pgtuner import instance as inst_mod
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "pg_config"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert inst_mod._tool(tmp_path, "pg_config") == str(exe)      # prefix form
    assert inst_mod._tool(bin_dir, "pg_config") == str(exe)       # bin form
    missing = tmp_path / "nowhere"
    with pytest.raises(ValueError):
        inst_mod._tool(missing, "pg_config")


# ---------------------------- registry atomic writes + concurrency mutual exclusion (tool-w3) ----------------------------

def test_upsert_concurrent_threads_no_lost_update(tmp_path):
    """tool-w3 regression: with multithreaded concurrent _upsert (read-modify-write) and no locking, last-writer-wins loses registrations.

    8 threads × 20 distinct instance names each (default _upsert signature + registry_file pointing at tmp_path);
    afterwards all 160 names must be visible and the registry JSON intact and parseable. flock counts per
    open file description, so threads in one process that each open the lock file are still mutually exclusive."""
    import threading

    reg = tmp_path / "reg.json"
    n_threads, rounds = 8, 20

    def worker(t: int) -> None:
        for i in range(rounds):
            instance._upsert(
                instance.Instance(name=f"i{t}_{i}", bindir="/b", pg_version="19devel",
                                  build_type="release", data_dir="", port=5599),
                reg)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert not any(th.is_alive() for th in threads), "worker 线程未退出(疑似死锁)"

    names = {i.name for i in instance.list_instances(reg)}
    assert names == {f"i{t}_{i}" for t in range(n_threads) for i in range(rounds)}
    data = json.loads(reg.read_text(encoding="utf-8"))            # JSON intact
    assert len(data["instances"]) == n_threads * rounds           # not one lost, none duplicated


def test_save_atomic_no_tmp_leftovers(tmp_path):
    """tool-w3 regression: after _save's atomic write the same directory has no *.tmp.* leftovers; the content is fully parseable and the format unchanged
    (ensure_ascii=False, indent=2, trailing newline)."""
    reg = tmp_path / "reg.json"
    payload = {"instances": [{"name": "x", "bindir": "/b", "managed": True}],
               "clones": {"x": ["tpl_c1a2b3c4"]}}
    for _ in range(20):
        instance._save(payload, reg)
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == [], f"临时文件残留: {leftovers}"
    text = reg.read_text(encoding="utf-8")
    assert json.loads(text) == payload                            # fully parseable
    assert text == json.dumps(payload, ensure_ascii=False, indent=2) + "\n"   # format locked
