"""Unit tests for lab clone session-hold anchoring (tool-huntA-clone-wipe regression).

Defect: two parallel tracks under the same OS user (another terminal / another agent process tree) share the same owner string
(both fall back to USER when PGTUNER_AGENT is not explicitly exported), so tool-w1's ownership check cannot stop them — a single
`lab release` by someone else (or a sweep batch cleanup via drop_db) could delete an active clone in use, interrupting the experiment.

Security invariants of the fix (every test guards one):
  - clone() records a "claim" (the cloning process's ancestor chain [(pid, starttime), ...], excluding init) into clone_meta;
  - live processes still on the holding chain, and not inside the releasing process tree → refused: zero SQL sent, the clone stays in the pool,
    and the release-denied audit entry carries held_by (attributed to the holding process);
  - release from the same session (shared chain, including rebench-style same-process clone→release) is unaffected;
  - holding session already exited (all ancestors dead) / PID reuse (same pid, different starttime) → the leftover clone is returned as usual;
  - --force lets the trespasser through but still audits (forced flag); old registries without claim / non-Linux without /proc
    → protection degrades to the ownership check only, nothing blocked (backward compatible);
  - sweep --yes via the drop_db single point: clones held by others go into errors and are not deleted; those of one's own session are cleaned as usual.

fake_pg mode (cf. tests/test_instance.py / test_lab_ownership.py): mock psycopg2.connect;
the process tree is simulated with a programmable_proc (monkeypatch instance._proc_stat / os.getppid).
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


@pytest.fixture
def proc_tree(monkeypatch):
    """Programmable /proc: add(pid, ppid, comm, starttime) builds a process tree; as_session(root) points
    os.getppid at root (= attaching "the current process" into that tree). Pointing clone and release at different roots simulates the two tracks."""

    class ProcTable:
        def __init__(self):
            self.procs: dict[int, dict] = {}

        def add(self, pid, ppid, comm, starttime):
            self.procs[pid] = {"comm": comm, "ppid": ppid, "starttime": starttime}

        def as_session(self, root):
            monkeypatch.setattr(instance.os, "getppid", lambda: root)

    table = ProcTable()
    monkeypatch.setattr(instance, "_proc_stat", lambda pid: table.procs.get(pid))
    return table


def build_tracks(tree):
    """Two parallel tracks A/B sharing the common root node(505) (same OS user, different sessions), plus each one's
    transient per-command bash. Returns (trackA_roots, trackB_roots) — referenced by root pid."""
    tree.add(505, 1, "node-root", 50_000)                    # common root (ancestor of both)
    tree.add(595, 505, "zcli-A", 60_000)                     # track A's resident parent (agent/terminal)
    tree.add(596, 505, "zcli-B", 60_001)                     # track B's resident parent
    tree.add(900, 595, "bash", 70_000)                       # a command shell of track A
    tree.add(901, 595, "bash", 70_100)                       # a later command shell of track A
    tree.add(910, 596, "bash", 70_200)                       # a command shell of track B
    return 900, 901, 910


def audit_rows(reg):
    path = instance._audit_log_path(reg)
    return [json.loads(line) for line
            in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sqls_of(conns):
    return [s for c in conns for s in c.sqls]


# ---------------------------- clone registers claim ----------------------------

def test_clone_records_claim_chain(lab_env, proc_tree, fake_pg):
    build_tracks(proc_tree)
    proc_tree.as_session(900)                                # clone from within one command of track A
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    meta = json.loads(lab_env.read_text(encoding="utf-8"))["clone_meta"]["lab"][db]
    # ancestor chain: upward from ppid, excluding pid 1; [pid, starttime] pairs (starttime is used for liveness/PID-reuse checks)
    assert meta["claim"]["ancestors"] == [[900, 70_000], [595, 60_000], [505, 50_000]]
    assert meta["owner"] and meta["created_at"]              # tool-w1 fields kept as-is
    assert audit_rows(lab_env)[-1]["event"] == "clone"       # the clone audit entry carries no held_by
    assert "held_by" not in audit_rows(lab_env)[-1]


def test_no_proc_claims_empty_and_never_blocks(lab_env, proc_tree, fake_pg):
    """Non-Linux / /proc unavailable: claim is an empty chain, protection degrades to the ownership check only (nothing blocked)."""
    proc_tree.as_session(900)
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    meta = json.loads(lab_env.read_text(encoding="utf-8"))["clone_meta"]["lab"][db]
    assert meta["claim"]["ancestors"] == []                  # empty tree → empty chain
    build_tracks(proc_tree)
    proc_tree.as_session(910)                                # any track can return it
    instance.drop_db("lab", db, registry_file=lab_env)
    assert instance.pool_stats(lab_env) == {"lab": []}


# ---------------------------- same session: zero friction ----------------------------

def test_same_session_release_allowed(lab_env, proc_tree, fake_pg):
    """A later command on the same track (different transient bash, same resident parent): returned as usual."""
    _, conns = fake_pg
    build_tracks(proc_tree)
    proc_tree.as_session(900)
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    proc_tree.procs.pop(900)                                 # the shell of the clone command has exited
    proc_tree.as_session(901)                                # same track, a new command
    instance.drop_db("lab", db, registry_file=lab_env)
    assert 'DROP DATABASE IF EXISTS "scratch_tpl_c' in "".join(sqls_of(conns))
    assert instance.pool_stats(lab_env) == {"lab": []}


def test_single_process_clone_then_release(lab_env, proc_tree, fake_pg):
    """rebench-style same-process lifecycle (cli.py clone → use → finally drop): chains fully identical, allowed."""
    _, conns = fake_pg
    build_tracks(proc_tree)
    proc_tree.as_session(900)
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    instance.drop_db("lab", db, registry_file=lab_env)       # session unchanged
    assert instance.pool_stats(lab_env) == {"lab": []}
    assert audit_rows(lab_env)[-1]["event"] == "release"


# ---------------------------- held by someone else: denial + attribution audit (core regression) ----------------------------

def test_foreign_live_session_denied(lab_env, proc_tree, fake_pg):
    """Another active track of the same user releases a clone in use: denied, zero SQL, audit carries held_by."""
    _, conns = fake_pg
    build_tracks(proc_tree)
    proc_tree.as_session(900)                                # the victim track clones
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    before = len(conns)

    proc_tree.as_session(910)                                # the parallel track attempts a batch return
    with pytest.raises(RuntimeError, match="其他活动会话持有") as ei:
        instance.drop_db("lab", db, registry_file=lab_env)

    assert len(conns) == before                              # denial comes first: zero SQL sent
    assert instance.pool_stats(lab_env) == {"lab": [db]}     # clone still in the pool
    assert "tool-huntA-clone-wipe" in str(ei.value)          # the denial reason points to the defect and the way out
    assert "--force" in str(ei.value)
    denied = audit_rows(lab_env)[-1]
    assert (denied["event"], denied["db"], denied["instance"]) == \
        ("release-denied", db, "lab")
    assert {"pid": 595, "comm": "zcli-A"} in denied["held_by"]   # attributed to the holding process
    assert not [s for s in sqls_of(conns) if s.startswith("DROP DATABASE")]


def test_owner_check_still_first_when_agent_differs(lab_env, proc_tree, fake_pg, monkeypatch):
    """An explicitly different PGTUNER_AGENT (ownership check) still applies — the two protections complement rather than replace each other."""
    build_tracks(proc_tree)
    monkeypatch.setenv("PGTUNER_AGENT", "agentA")
    proc_tree.as_session(900)
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    proc_tree.as_session(900)                                # same session but a different identity
    monkeypatch.setenv("PGTUNER_AGENT", "agentB")
    with pytest.raises(RuntimeError, match="无权释放"):
        instance.drop_db("lab", db, registry_file=lab_env)
    assert instance.pool_stats(lab_env) == {"lab": [db]}


def test_force_overrides_hold_and_audits(lab_env, proc_tree, fake_pg):
    """Emergency channel --force: the trespass is allowed but the audit carries forced (not silent)."""
    build_tracks(proc_tree)
    proc_tree.as_session(900)
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    proc_tree.as_session(910)
    instance.drop_db("lab", db, registry_file=lab_env, force=True)
    assert instance.pool_stats(lab_env) == {"lab": []}
    last = audit_rows(lab_env)[-1]
    assert (last["event"], last["forced"]) == ("release", True)


# ---------------------------- holding session died: leftover clone returned as usual ----------------------------

def test_holder_session_ended_allows_release(lab_env, proc_tree, fake_pg):
    _, conns = fake_pg
    build_tracks(proc_tree)
    proc_tree.as_session(900)
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    for pid in (900, 595):                                   # the victim track exits entirely
        proc_tree.procs.pop(pid)
    proc_tree.as_session(910)                                # someone else comes to clean up the leftover
    instance.drop_db("lab", db, registry_file=lab_env)
    assert 'DROP DATABASE IF EXISTS "scratch_tpl_c' in "".join(sqls_of(conns))
    assert instance.pool_stats(lab_env) == {"lab": []}


def test_pid_reuse_not_treated_as_alive(lab_env, proc_tree, fake_pg):
    """The holding pid got reused (starttime changed) ≠ the holding session is alive: returned as usual."""
    build_tracks(proc_tree)
    proc_tree.as_session(900)
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    proc_tree.procs.pop(900)                                 # the clone command's shell has exited
    proc_tree.add(595, 505, "reused-worker", 99_999)         # same pid revived, different starttime
    proc_tree.as_session(910)
    instance.drop_db("lab", db, registry_file=lab_env)
    assert instance.pool_stats(lab_env) == {"lab": []}


def test_legacy_meta_without_claim_not_blocked(lab_env, proc_tree, fake_pg):
    """Old registries (tool-w1 era, no claim field): behavior unchanged, not blocked by the new protection."""
    build_tracks(proc_tree)
    proc_tree.as_session(900)
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    reg = instance._load(lab_env)
    del reg["clone_meta"]["lab"][db]["claim"]                # erase claim to simulate a legacy row
    instance._save(reg, lab_env)
    proc_tree.as_session(910)
    instance.drop_db("lab", db, registry_file=lab_env)
    assert instance.pool_stats(lab_env) == {"lab": []}


# ---------------------------- sweep batch cleanup: only cleans one's own session ----------------------------

def test_sweep_yes_spares_foreign_held_drops_own(lab_env, proc_tree, fake_pg, monkeypatch):
    """Batch-cleanup storm regression: actively held by others → errors, no wrongful deletion; one's own session's clones + orphans cleaned as usual."""
    servers, conns = fake_pg
    monkeypatch.setattr(instance, "ensure_running", lambda n, registry_file=None: None)
    build_tracks(proc_tree)

    proc_tree.as_session(900)                                # track A: two clones
    held1 = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    held2 = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    proc_tree.as_session(910)                                # track B: one of its own
    own = instance.clone("lab", "scratch_tpl", registry_file=lab_env)
    for dbname in (held1, held2, own):
        servers["5599"].rows.append((dbname, 16390, True, False))

    rep = instance.sweep("lab", yes=True, registry_file=lab_env)
    assert rep["dropped"] == [f"lab/{own}"]                  # only its own session's is cleaned
    assert sorted(e["dbname"] for e in rep["errors"]) == sorted([held1, held2])
    assert all("其他活动会话持有" in e["error"] for e in rep["errors"])
    assert not [s for s in sqls_of(conns)
                if s.startswith("DROP DATABASE") and (held1 in s or held2 in s)]
    assert instance.pool_stats(lab_env)["lab"] == [held1, held2]   # others' clones preserved
    events = [r["event"] for r in audit_rows(lab_env)]       # processing order is nondeterministic, assert by event counts
    assert events.count("release-denied") == 2 and events.count("release") == 1


# ---------------------------- CLI: actionable denial message ----------------------------

def test_cli_release_denied_then_force(monkeypatch, lab_env, proc_tree, fake_pg):
    from click.testing import CliRunner
    from pgtuner import cli
    monkeypatch.setattr(instance, "INSTANCE_REGISTRY", lab_env)
    build_tracks(proc_tree)
    proc_tree.as_session(900)
    db = instance.clone("lab", "scratch_tpl", registry_file=lab_env)

    proc_tree.as_session(910)
    denied = CliRunner().invoke(cli.cli, ["lab", "release", "lab", db])
    assert denied.exit_code != 0
    out = denied.output
    assert "其他活动会话持有" in out and "zcli-A" in out and "--force" in out
    assert instance.pool_stats(lab_env) == {"lab": [db]}     # clone preserved

    forced = CliRunner().invoke(cli.cli, ["lab", "release", "lab", db, "--force"])
    assert forced.exit_code == 0, forced.output
    assert instance.pool_stats(lab_env) == {"lab": []}
    assert audit_rows(lab_env)[-1]["forced"] is True
