"""restartbench tests: fully mocked, no dependency on a running PG.

- pg layer: FakePG (in-memory ALTER SYSTEM / pg_settings / EXPLAIN) + FakeConn/FakeCursor recording statements;
- instance layer: FakeInstance stand-in + monkeypatched instance_mod contract functions (no dependency on the real instance.py;
  the placeholder module wired into restartbench during parallel development is equally stubbable);
- bench layer: monkeypatched bench.run_pgbench / parse_pgbench_output (asserting that measurement and parsing are reused, not rewritten).
"""
from __future__ import annotations          # B2: `X | None` annotations must not evaluate on Python 3.9

import json
import os
import signal
from types import SimpleNamespace

import pytest
import psycopg2

from pgtuner import bench, restartbench
from pgtuner.restartbench import RestartCandidate

_EXPLAIN_PREFIX = "EXPLAIN (ANALYZE, FORMAT JSON) "    # kept consistent with restartbench._explain_execution_ms


# ---------------------------- in-memory PG ----------------------------

class FakePG:
    """ALTER SYSTEM SET writes overrides (postgresql.auto.conf equivalent); RESET falls back to base (conf equivalent).

    pg_settings always returns the full (name, setting) rows of base∪overrides — enough to drive the full logic of snapshot /
    drift detection / restore verification; base can also be edited directly to simulate "tampering at the conf layer" (RESET cannot bring it back).
    """

    def __init__(self, settings: dict, explain_times: dict | None = None, events: list | None = None,
                 contexts: dict | None = None, derive: dict | None = None):
        self.base = dict(settings)
        self.overrides: dict = {}
        self.explain_times = explain_times or {}      # query → Execution Time(ms)
        self.events = events if events is not None else []
        self.contexts = contexts or {}                # name → context (e.g. internal, B1 D3)
        self.derive = derive or {}                    # internal derived parameters: live → value (recomputed with the primary parameter)
        self.connects: list[str] = []
        self.statements: list[str] = []
        self.closed = 0

    def live(self) -> dict:
        out = {**self.base, **self.overrides}
        for name, fn in self.derive.items():          # simulate derived-parameter recomputation after restart
            out[name] = fn(out)
        return out

    def execute(self, sql: str) -> None:              # called by FakeCursor (params already rendered)
        self.statements.append(sql)
        self.events.append(("sql", sql))
        if sql.startswith("ALTER SYSTEM SET "):
            name, _, value = sql[len("ALTER SYSTEM SET "):].partition(" = ")
            name = name.strip('"')
            if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
                value = value[1:-1]
            elif not value.lstrip("-").replace(".", "", 1).isdigit():
                # mimic real PG: B1 defect D1 — unquoted values with units report trailing junk
                raise psycopg2.ProgrammingError(
                    f'trailing junk after numeric literal at or near "{value}"')
            self.overrides[name] = value
        elif sql.startswith("ALTER SYSTEM RESET "):
            self.overrides.pop(sql[len("ALTER SYSTEM RESET "):].strip('"'), None)

    def fetchall(self, sql: str) -> list:             # called by FakeCursor
        if "SELECT name, context FROM pg_settings" in sql:
            return [(n, self.contexts.get(n, "user")) for n in self.live()]
        if "pg_settings" in sql:
            return list(self.live().items())
        if sql.startswith(_EXPLAIN_PREFIX):
            query = sql[len(_EXPLAIN_PREFIX):]
            assert query in self.explain_times, f"未 mock 的 EXPLAIN 查询: {query}"
            # psycopg2 auto-deserializes json columns: list form (B1 defect D2)
            return [([{"Execution Time": self.explain_times[query], "Plan": {}}],)]
        raise AssertionError(f"未 mock 的 SQL: {sql}")

    def connect(self, dsn) -> "FakeConn":             # replaces pg.connect_rw
        self.connects.append(dsn)
        self.events.append(("connect", dsn))
        return FakeConn(self, dsn)


class FakeCursor:
    def __init__(self, state: FakePG):
        self._state = state
        self._sql: str | None = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None):
        if params:                                    # parameter binding rendering (B1 D1: values auto-quoted)
            for p in params:
                sql = sql.replace("%s", f"'{p}'", 1)
        self._sql = sql
        self._state.execute(sql)

    def fetchall(self):
        assert self._sql is not None
        return self._state.fetchall(self._sql)


class FakeConn:
    def __init__(self, state: FakePG, dsn):
        self.state = state
        self.dsn = dsn
        self.is_closed = False

    def cursor(self):
        return FakeCursor(self.state)

    def close(self):
        self.is_closed = True
        self.state.closed += 1
        self.state.events.append(("close", self.dsn))


class FakeInstance:
    """Minimal stand-in for the contract-A Instance: restartbench only consumes .name/.bindir."""

    def __init__(self, name: str = "t9"):
        self.name = name
        self.bindir = "/fake-pg-prefix/bin"


# ---------------------------- stubbing tools ----------------------------

@pytest.fixture(autouse=True)
def _clean_touched(monkeypatch, tmp_path):
    """The module-level _TOUCHED bookkeeping is isolated between test cases; the
    on-disk restore journal is redirected into a per-test tmp project root
    (PGTUNER_PROJECT_ROOT env — the same anchor pgtuner.paths resolves first), so
    journal round-trips never touch the repository's own .pgtuner/."""
    restartbench._TOUCHED.clear()
    monkeypatch.setenv("PGTUNER_PROJECT_ROOT", str(tmp_path / "proj"))
    yield
    restartbench._TOUCHED.clear()


def install_fake_instance(monkeypatch, events: list) -> SimpleNamespace:
    """Replace all six instance_mod contract functions with fake implementations that record calls."""
    inst = FakeInstance()
    calls = {"get": [], "ensure": [], "start": [], "stop": []}

    def fake_get(name, registry_file=None):
        calls["get"].append((name, registry_file))
        events.append(("get", name))
        return inst

    def fake_ensure(name):
        calls["ensure"].append(name)
        events.append(("ensure", name))
        return inst

    def fake_start(name):
        calls["start"].append(name)
        events.append(("start", name))

    def fake_stop(name):
        calls["stop"].append(name)
        events.append(("stop", name))

    monkeypatch.setattr(restartbench.instance_mod, "get", fake_get)
    monkeypatch.setattr(restartbench.instance_mod, "ensure_running", fake_ensure)
    monkeypatch.setattr(restartbench.instance_mod, "start", fake_start)
    monkeypatch.setattr(restartbench.instance_mod, "stop", fake_stop)
    monkeypatch.setattr(restartbench.instance_mod, "is_ready", lambda name: True)
    monkeypatch.setattr(restartbench.instance_mod, "dsn_of",
                        lambda name, dbname: f"host=/tmp port=5555 dbname={dbname}")
    return SimpleNamespace(inst=inst, calls=calls)


def install_harness(monkeypatch, settings: dict | None = None, explain_times: dict | None = None,
                    pgbench=None, parse=None, events: list | None = None) -> SimpleNamespace:
    """One-shot stubbing: instance_mod + pg.connect_rw/pg.target + bench.run_pgbench/parse.

    Returns state (FakePG)/inst/calls/events/pg_calls/parse_calls for use in assertions."""
    events = events if events is not None else []
    state = FakePG(settings if settings is not None
                   else {"shared_buffers": "16384", "work_mem": "4096"},
                   explain_times=explain_times, events=events)
    fake_inst = install_fake_instance(monkeypatch, events)
    monkeypatch.setattr(restartbench.pg, "connect_rw", state.connect)
    monkeypatch.setattr(restartbench.pg, "target", lambda dsn: ("/tmp", "5555", "bench_new"))

    pg_calls: list[dict] = []

    def default_pgbench(pgbench_path, host, port, dbname, *, duration, clients, gucs=None):
        pg_calls.append({"path": pgbench_path, "host": host, "port": port, "dbname": dbname,
                         "duration": duration, "clients": clients, "gucs": gucs})
        return SimpleNamespace(returncode=0, stdout="tps-text", stderr="")

    parse_calls: list[str] = []

    def default_parse(text):
        parse_calls.append(text)
        return {"tps": 123.4, "latency_ms": 3.2}

    monkeypatch.setattr(restartbench.bench, "run_pgbench", pgbench or default_pgbench)
    monkeypatch.setattr(restartbench.bench, "parse_pgbench_output", parse or default_parse)
    # v0.9.0: without an explicit pgbench_path, resolve via the instance bindir (bench.resolve_pgbench)
    monkeypatch.setattr(restartbench.bench, "resolve_pgbench",
                        lambda explicit=None, bindir=None: "/fake-pg-prefix/bin/pgbench")
    return SimpleNamespace(state=state, inst=fake_inst.inst, calls=fake_inst.calls,
                           events=events, pg_calls=pg_calls, parse_calls=parse_calls)


# ---------------------------- snapshot / comparison ----------------------------

def test_snapshot_settings_full_dict(monkeypatch):
    state = FakePG({"shared_buffers": "16384", "work_mem": "4096", "fsync": "on"})
    monkeypatch.setattr(restartbench.pg, "connect_rw", state.connect)
    snap = restartbench.snapshot_settings("host=/tmp port=5 dbname=postgres")
    assert snap == {"shared_buffers": "16384", "work_mem": "4096", "fsync": "on"}
    assert restartbench.SNAPSHOT_SQL in state.statements      # full SELECT name, setting
    assert state.connects == ["host=/tmp port=5 dbname=postgres"]
    assert state.closed == 1                                   # closed when done


def test_diff_settings_lists_all_mismatches():
    snap = {"a": "1", "b": "2", "c": "3"}
    cur = {"a": "1", "b": "9", "d": "4"}                       # b changed, c gone, d added
    diffs = restartbench._diff_settings(snap, cur)
    assert len(diffs) == 3
    assert "b: 基准='2' 实际='9'" in diffs
    assert "c: 基准='3' 实际=None" in diffs
    assert "d: 基准=None 实际='4'" in diffs


def test_restore_and_verify_detects_tampering(monkeypatch):
    """Mismatches between two different settings must be detected — compare all parameters, not only the touched ones (anti-tampering)."""
    h = install_harness(monkeypatch)
    snap = restartbench.snapshot_settings("host=/tmp port=5555 dbname=postgres")
    restartbench.apply_and_restart(h.inst, {"shared_buffers": "512MB"})
    h.state.base["work_mem"] = "999999kB"     # simulate conf-layer tampering after the baseline was taken (RESET cannot bring it back)
    with pytest.raises(RuntimeError) as ei:
        restartbench.restore_and_verify(h.inst, snap)
    msg = str(ei.value)
    assert "work_mem" in msg and "4096" in msg and "999999kB" in msg   # the diff list carries baseline/actual
    assert "shared_buffers" not in msg       # the touched GUC was restored normally; no false alarm


# ---------------------------- apply_and_restart ----------------------------

def test_apply_and_restart_alter_then_restart(monkeypatch):
    events: list = []
    h = install_harness(monkeypatch, events=events)
    restartbench.apply_and_restart(h.inst, {"shared_buffers": "512MB", "max_wal_size": "2GB"})
    # ALTERs one by one with parameterized quoting (B1 D1: a bare 512MB would be rejected by PG); stop→start only after all ALTERs complete
    assert events == [
        ("connect", "host=/tmp port=5555 dbname=postgres"),
        ("sql", 'ALTER SYSTEM SET "shared_buffers" = \'512MB\''),
        ("sql", 'ALTER SYSTEM SET "max_wal_size" = \'2GB\''),
        ("close", "host=/tmp port=5555 dbname=postgres"),
        ("stop", "t9"),
        ("start", "t9"),
    ]
    assert restartbench._TOUCHED["t9"] == {"shared_buffers", "max_wal_size"}


def test_apply_and_restart_empty_gucs_still_restarts(monkeypatch):
    h = install_harness(monkeypatch)
    restartbench.apply_and_restart(h.inst, {})
    assert h.state.statements == []                             # no ALTER
    assert h.calls["stop"] == ["t9"] and h.calls["start"] == ["t9"]   # but still restarts (baseline)
    assert "t9" not in restartbench._TOUCHED


def test_apply_and_restart_ready_timeout(monkeypatch):
    h = install_harness(monkeypatch)
    monkeypatch.setattr(restartbench.instance_mod, "is_ready", lambda name: False)
    clock = {"t": 0.0}                                          # fake clock: the poll loop burns through 60s instantly
    monkeypatch.setattr(restartbench.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(restartbench.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + s))
    with pytest.raises(RuntimeError, match="未就绪"):
        restartbench.apply_and_restart(h.inst, {"shared_buffers": "512MB"})
    assert h.calls["stop"] == ["t9"]                            # the restart was issued; stuck at the readiness wait


# ---------------------------- restore_and_verify ----------------------------

def test_restore_and_verify_resets_and_restarts(monkeypatch):
    h = install_harness(monkeypatch)
    snap = restartbench.snapshot_settings("host=/tmp port=5555 dbname=postgres")
    restartbench.apply_and_restart(h.inst, {"shared_buffers": "512MB"})
    assert h.state.overrides == {"shared_buffers": "512MB"}
    out = restartbench.restore_and_verify(h.inst, snap)
    assert out == {"restored": True, "mismatches": []}
    assert h.state.overrides == {}                             # RESET cleaned up
    assert "ALTER SYSTEM RESET \"shared_buffers\"" in h.state.statements
    assert h.calls["stop"] == ["t9", "t9"]                      # 1 apply restart + 1 restore restart


def test_restore_and_verify_resets_drifted_gucs(monkeypatch):
    """GUCs this module never touched but whose current value differs from the baseline (leftover external ALTER SYSTEM) must also be RESET as a safety net."""
    h = install_harness(monkeypatch)
    snap = restartbench.snapshot_settings("host=/tmp port=5555 dbname=postgres")
    h.state.overrides["fsync"] = "off"
    out = restartbench.restore_and_verify(h.inst, snap)
    assert out == {"restored": True, "mismatches": []}
    assert "ALTER SYSTEM RESET \"fsync\"" in h.state.statements
    assert h.calls["stop"] == ["t9"]                            # the RESET triggers one restart


def test_restore_and_verify_noop_skips_restart(monkeypatch):
    h = install_harness(monkeypatch)
    snap = restartbench.snapshot_settings("host=/tmp port=5555 dbname=postgres")
    out = restartbench.restore_and_verify(h.inst, snap)         # nothing was touched
    assert out == {"restored": True, "mismatches": []}
    assert h.calls["stop"] == [] and h.calls["start"] == []      # no RESET → no restart needed
    assert h.state.statements == [restartbench.SNAPSHOT_SQL] * 3  # baseline shot + pre-restore + post-restore


# ---------------------------- run_restart_bench full flow ----------------------------

def test_run_restart_bench_pgbench_full_flow(monkeypatch):
    h = install_harness(monkeypatch)
    seen = []
    results = restartbench.run_restart_bench(
        "t9", [RestartCandidate("sb512", {"shared_buffers": "512MB"})],
        dbname="bench_new", duration=30, warmup=5, clients=8,
        registry_file="/tmp/reg.json", on_cycle=seen.append)

    assert [r.candidate.name for r in results] == ["baseline", "sb512"]   # baseline auto-filled in first position
    assert results[0].candidate.gucs == {}
    base, tuned = results
    assert (base.tps, base.latency_ms, base.warmup_s, base.error, base.payload) \
        == (123.4, 3.2, 5, None, None)
    assert (tuned.tps, tuned.latency_ms, tuned.error) == (123.4, 3.2, None)
    assert seen == results                                       # the progress callback fires per cycle
    assert h.calls["get"] == [("t9", "/tmp/reg.json")]           # registry_file passed through to instance.get

    # per candidate: warmup (gucs=None, result discarded) + measurement; default pgbench_path resolved via the instance bindir
    assert len(h.pg_calls) == 4
    assert all(c["path"] == "/fake-pg-prefix/bin/pgbench" for c in h.pg_calls)
    assert all(c["gucs"] is None for c in h.pg_calls)            # already effective at the server layer; no PGOPTIONS injection
    assert [c["duration"] for c in h.pg_calls] == [5, 30, 5, 30]
    assert all(c["clients"] == 8 and c["host"] == "/tmp" and c["port"] == "5555"
               and c["dbname"] == "bench_new" for c in h.pg_calls)
    assert h.parse_calls == ["tps-text", "tps-text"]             # parsing reuses bench.parse_pgbench_output

    # restart count: baseline apply 1 + sb512 apply 1 + sb512 restore 1 (baseline restore has no change, no restart)
    assert h.calls["stop"] == ["t9"] * 3
    assert h.state.overrides == {}                               # final restore is clean


def test_run_restart_bench_failed_candidate_continues(monkeypatch):
    h = install_harness(monkeypatch)
    measure_calls = {"n": 0}

    def flaky_pgbench(pgbench_path, host, port, dbname, *, duration, clients, gucs=None):
        h.pg_calls.append({"duration": duration, "gucs": gucs})
        if duration == 30:                                       # measurement phase (warmup is 5)
            measure_calls["n"] += 1
            if measure_calls["n"] == 2:                          # the 2nd candidate (sb512) fails measurement
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr="pgbench: error: fatal\nconnection to server failed")
        return SimpleNamespace(returncode=0, stdout="tps-text", stderr="")

    monkeypatch.setattr(restartbench.bench, "run_pgbench", flaky_pgbench)
    results = restartbench.run_restart_bench(
        "t9", [RestartCandidate("sb512", {"shared_buffers": "512MB"}),
               RestartCandidate("wm", {"work_mem": "64MB"})],
        dbname="bench_new", duration=30, warmup=5)
    assert [r.candidate.name for r in results] == ["baseline", "sb512", "wm"]
    assert results[1].tps is None and results[1].latency_ms is None
    assert results[1].error == "connection to server failed"     # last-line summary
    assert results[0].tps == 123.4 and results[2].tps == 123.4   # a failure does not interrupt subsequent candidates
    assert len(h.parse_calls) == 2
    assert h.state.overrides == {}                               # the failed candidate is still restored
    assert h.calls["stop"] == ["t9"] * 5   # 3 applies + 1 restore each for sb512/wm (baseline restore is a no-op)


def test_run_restart_bench_restores_even_when_measure_raises(monkeypatch):
    """Even if measurement raises, the finally block must restore — the restore guarantee is this module's credibility."""
    h = install_harness(monkeypatch)

    def exploding_pgbench(pgbench_path, host, port, dbname, *, duration, clients, gucs=None):
        raise RuntimeError("pgbench 子进程超时")

    monkeypatch.setattr(restartbench.bench, "run_pgbench", exploding_pgbench)
    restore_calls = []
    real_restore = restartbench.restore_and_verify

    def spy_restore(inst, snapshot):
        restore_calls.append(snapshot)
        return real_restore(inst, snapshot)                      # counts while running the real restore

    monkeypatch.setattr(restartbench, "restore_and_verify", spy_restore)
    results = restartbench.run_restart_bench(
        "t9", [RestartCandidate("sb512", {"shared_buffers": "512MB"})], dbname="bench_new")
    assert len(restore_calls) == 2                               # every cycle's finally restores
    assert restore_calls[0] is restore_calls[1]                  # both are the same baseline snapshot
    assert all(r.tps is None for r in results)
    assert all("pgbench 子进程超时" in r.error for r in results)
    assert h.state.overrides == {}                               # the real restore did execute


def test_run_restart_bench_keeps_user_supplied_baseline(monkeypatch):
    h = install_harness(monkeypatch)
    results = restartbench.run_restart_bench(
        "t9", [RestartCandidate("mine", {"shared_buffers": "1GB"}),
               RestartCandidate("baseline")], dbname="bench_new")
    assert [r.candidate.name for r in results] == ["mine", "baseline"]   # baseline already supplied → not re-added
    assert len(h.pg_calls) == 4


def test_run_restart_bench_snapshot_failure_propagates_without_restore(monkeypatch):
    """Snapshot failed: config untouched, the exception propagates directly, no restore obligation (and indeed no restart)."""
    h = install_harness(monkeypatch)

    def dead_connect(dsn=None, autocommit=True):
        raise ConnectionError("连接失败(rw): 服务没起")

    monkeypatch.setattr(restartbench.pg, "connect_rw", dead_connect)
    with pytest.raises(ConnectionError):
        restartbench.run_restart_bench("t9", [RestartCandidate("x", {"fsync": "off"})],
                                       dbname="bench_new")
    assert h.calls["stop"] == [] and h.calls["start"] == []


# ---------------------------- custom workload ----------------------------

def test_run_restart_bench_custom_workload(monkeypatch):
    h = install_harness(
        monkeypatch,
        explain_times={"SELECT count(*) FROM t": 10.0, "SELECT * FROM t WHERE id = 1": 30.0})
    results = restartbench.run_restart_bench(
        "t9", [], dbname="bench_new", workload="custom",
        custom_queries=["SELECT count(*) FROM t;", "  SELECT * FROM t WHERE id = 1 "],
        runs=3)
    assert [r.candidate.name for r in results] == ["baseline"]   # empty candidate list still gets a baseline
    r = results[0]
    assert r.tps == pytest.approx(20.0)                          # median of per-query medians = the summary metric
    assert r.latency_ms is None
    assert r.payload["queries"] == [                             # R1 fix: runs repetitions + median + raw values
        {"query": "SELECT count(*) FROM t", "execution_ms": 10.0,
         "times_ms": [10.0, 10.0, 10.0]},
        {"query": "SELECT * FROM t WHERE id = 1", "execution_ms": 30.0,
         "times_ms": [30.0, 30.0, 30.0]},
    ]
    # warmup: each statement executes exactly twice via the rw connection
    warm = [s for s in h.state.statements
            if s.startswith("SELECT") and "pg_settings" not in s]
    assert warm.count("SELECT count(*) FROM t") == 2
    assert warm.count("SELECT * FROM t WHERE id = 1") == 2
    # measurement goes through EXPLAIN ANALYZE, runs times per query; pgbench plays no part at all
    explains = [s for s in h.state.statements if s.startswith(_EXPLAIN_PREFIX)]
    assert len(explains) == 2 * 3                                # 2 queries × runs=3
    assert h.pg_calls == []
    # business connections go to the bench database, config snapshots to the postgres database
    assert h.state.connects.count("host=/tmp port=5555 dbname=bench_new") == 3   # 2 warmups + 1 measurement
    assert h.state.connects.count("host=/tmp port=5555 dbname=postgres") == 3    # snapshot + pre/post restore


def test_run_restart_bench_measure_only_never_restarts(monkeypatch):
    """F-D (patch-trial): measure-only mode — no apply/no restart/no restore, just measures the current state."""
    h = install_harness(monkeypatch, explain_times={"SELECT 1": 5.0})
    results = restartbench.run_restart_bench(
        "t9", [], dbname="bench_new", workload="custom",
        custom_queries=["SELECT 1"], runs=3, measure_only=True,
        registry_file="/tmp/reg.json")
    assert [r.candidate.name for r in results] == ["baseline"]
    assert results[0].tps == pytest.approx(5.0) and results[0].error is None
    assert h.calls["stop"] == []                    # zero restarts (the old path restarted once even for baseline)
    assert h.state.overrides == {}                  # config untouched
    assert h.pg_calls == []                         # pgbench does not participate in custom workloads


def test_run_restart_bench_measure_only_rejects_guc_candidates():
    """F-D: measure-only mode is mutually exclusive with GUC candidates (with no parameter changes there is no candidate comparison to speak of)."""
    with pytest.raises(ValueError, match="measure_only"):
        restartbench.run_restart_bench(
            "t9", [RestartCandidate("sb", {"shared_buffers": "512MB"})],
            dbname="db", workload="custom", custom_queries=["SELECT 1"],
            measure_only=True)


def test_run_restart_bench_rejects_unknown_workload():
    with pytest.raises(ValueError, match="workload"):
        restartbench.run_restart_bench("t9", [RestartCandidate("x")],
                                       dbname="db", workload="tpcc")


def test_run_restart_bench_custom_requires_queries():
    with pytest.raises(ValueError, match="custom_queries"):
        restartbench.run_restart_bench("t9", [], dbname="db", workload="custom")


def test_restore_skips_internal_derived(monkeypatch):
    """B1 D3 regression: derived parameters with context=internal (shared_memory_size) drift with the primary parameter,
    but cannot be RESET (CantChangeRuntimeParam) — after filtering by context, the restore is still fully consistent."""
    h = install_harness(monkeypatch)
    h.state.contexts = {"shared_memory_size": "internal"}
    h.state.derive = {"shared_memory_size":
                      lambda live: "536MB" if live.get("shared_buffers") == "512MB" else "134MB"}
    h.state.base.setdefault("shared_memory_size", "134MB")
    snap = restartbench.snapshot_settings("host=/tmp port=5555 dbname=postgres")
    restartbench.apply_and_restart(h.inst, {"shared_buffers": "512MB"})
    cur = restartbench.snapshot_settings("host=/tmp port=5555 dbname=postgres")
    assert cur["shared_memory_size"] == "536MB"          # the derived parameter indeed drifted
    out = restartbench.restore_and_verify(h.inst, snap)
    assert out == {"restored": True, "mismatches": []}
    assert "ALTER SYSTEM RESET shared_memory_size" not in h.state.statements   # filtered out
    assert "ALTER SYSTEM RESET \"shared_buffers\"" in h.state.statements           # the primary parameter restored as usual


def test_run_custom_cycle_script_mode_for_utility_statements(monkeypatch):
    """v0.9.0 script mode: the file contains DDL/ANALYZE → execute once in order + wall-clock timing, no warmup, no re-runs
    (non-idempotent statements would surely fail on a second execution), no EXPLAIN (utility not supported)."""
    h = install_harness(monkeypatch, explain_times={"SELECT 1": 5.0})
    tps, payload = restartbench._run_custom_cycle(
        "host=/tmp port=5555 dbname=bench_new",
        ["ALTER TABLE orders ADD COLUMN note text;",
         "ANALYZE orders",
         "   SELECT 1 "])
    assert payload["script"] is True
    assert [p["query"] for p in payload["queries"]] == [
        "ALTER TABLE orders ADD COLUMN note text", "ANALYZE orders", "SELECT 1"]
    for p in payload["queries"]:
        assert p["execution_ms"] >= 0.0 and len(p["times_ms"]) == 1   # one pass, one value
    # each statement executes exactly once (no warmup pass) and no EXPLAIN wrapping
    stmts = [s for s in h.state.statements if "pg_settings" not in s]
    assert stmts.count("ALTER TABLE orders ADD COLUMN note text") == 1
    assert stmts.count("ANALYZE orders") == 1
    assert not any(s.startswith(_EXPLAIN_PREFIX) for s in h.state.statements)


def test_run_restart_bench_setup_three_phase(monkeypatch):
    """v0.11.0 --setup: one experiment with before/setup/after segments — setup statements execute exactly once
    (per-statement wall-clock timing, no EXPLAIN wrapping); the before/after phases each take the SELECT path of
    warmup×2 + EXPLAIN×runs, with zero restarts and zero GUCs (the pure-measurement channel)."""
    h = install_harness(
        monkeypatch, explain_times={"SELECT count(*) FROM events": 10.0})
    results = restartbench.run_restart_bench(
        "t9", [], dbname="bench_new", workload="custom",
        custom_queries=["SELECT count(*) FROM events;"], runs=3,
        measure_only=True, setup_statements=["ANALYZE events"])
    assert [r.candidate.name for r in results] == ["before", "setup", "after"]
    assert all(r.error is None for r in results)
    setup = results[1]
    assert setup.payload["script"] is True                       # script criterion: one pass, one value
    assert setup.payload["queries"][0]["query"] == "ANALYZE events"
    assert len(setup.payload["queries"][0]["times_ms"]) == 1
    assert h.state.statements.count("ANALYZE events") == 1      # executed exactly once
    assert not any("EXPLAIN" in s and "ANALYZE events" in s
                   for s in h.state.statements)                  # no EXPLAIN wrapping
    # before/after phases each: warmup ×2 (bare SELECT) + EXPLAIN × runs=3
    assert h.state.statements.count("SELECT count(*) FROM events") == 4
    explains = [s for s in h.state.statements if s.startswith(_EXPLAIN_PREFIX)]
    assert len(explains) == 6
    assert [r.tps for r in (results[0], results[2])] == [pytest.approx(10.0), pytest.approx(10.0)]
    assert h.calls["stop"] == [] and h.state.overrides == {}     # zero restarts, zero config changes


def test_run_restart_bench_setup_error_recorded_after_phase_runs(monkeypatch):
    """A setup statement fails → that cycle records the error and the after phase still runs (a measurement of the unchanged state remains valid evidence)."""
    h = install_harness(monkeypatch, explain_times={"SELECT 1": 5.0})
    real_execute = restartbench.pg.execute

    def selective(conn, sql, *a, **kw):
        if sql == "CREATE INDEX boom":
            raise RuntimeError("boom")
        return real_execute(conn, sql, *a, **kw)
    monkeypatch.setattr(restartbench.pg, "execute", selective)
    results = restartbench.run_restart_bench(
        "t9", [], dbname="bench_new", workload="custom",
        custom_queries=["SELECT 1"], runs=2,
        measure_only=True, setup_statements=["CREATE INDEX boom"])
    assert [r.candidate.name for r in results] == ["before", "setup", "after"]
    assert results[1].error is not None and results[1].error.startswith("RuntimeError: boom")
    assert results[0].error is None and results[0].tps == pytest.approx(5.0)
    assert results[2].error is None and results[2].tps == pytest.approx(5.0)   # after still runs


def test_run_restart_bench_setup_validations():
    """Three precondition rejections: no measurement workload / not pure measurement (GUC candidates present) / workload contains utility statements."""
    with pytest.raises(ValueError, match="纯 SELECT 测量负载"):
        restartbench.run_restart_bench("t9", [], dbname="db", workload="pgbench",
                                       measure_only=True, setup_statements=["ANALYZE t"])
    with pytest.raises(ValueError, match="只挂纯测量"):
        restartbench.run_restart_bench(
            "t9", [RestartCandidate("sb", {"shared_buffers": "512MB"})],
            dbname="db", workload="custom", custom_queries=["SELECT 1"],
            setup_statements=["ANALYZE t"])
    with pytest.raises(ValueError, match="纯 SELECT"):
        restartbench.run_restart_bench(
            "t9", [], dbname="db", workload="custom",
            custom_queries=["ALTER TABLE t ADD COLUMN a int"],
            measure_only=True, setup_statements=["ANALYZE t"])


def test_is_utility_matches_runner_classification():
    from pgtuner.benchmark import _is_utility_stmt
    for q in ("SELECT 1", "  (SELECT 1)", "WITH x AS (SELECT 1) SELECT * FROM x",
              "UPDATE t SET a=1", "TABLE t", "MERGE INTO t USING s ON 1=1 THEN NOTHING"):
        assert not restartbench._is_utility(q), q
        assert not _is_utility_stmt(q), q                          # same criterion on the grading side
    for q in ("ALTER TABLE t ADD COLUMN a int", "CREATE INDEX ON t(k)", "ANALYZE t",
              "VACUUM t", "DROP TABLE t", "GRANT SELECT ON t TO u"):
        assert restartbench._is_utility(q), q
        assert _is_utility_stmt(q), q


# ---------------------------- crash safety: restore journal / signal mask / receipt ----------------------------

def _journal_path(proj, name: str = "t9"):
    return proj / ".pgtuner" / "restore" / f"{name}.json"


def test_journal_on_disk_during_cycle_and_removed_after_verified_restore(monkeypatch, tmp_path):
    """R2-A: the journal (instance + full pre-experiment baseline + timestamp) is on disk
    BEFORE the first ALTER SYSTEM of the GUC cycle and is removed once the final restore
    verified — a passing run leaves no journal behind."""
    proj = tmp_path / "proj"
    monkeypatch.setenv("PGTUNER_PROJECT_ROOT", str(proj))
    h = install_harness(monkeypatch)
    jp = _journal_path(proj)
    seen: list[dict] = []
    real_pgbench = restartbench.bench.run_pgbench

    def spy_pgbench(pgbench_path, host, port, dbname, *, duration, clients, gucs=None):
        if jp.exists():                                             # probe the disk at measurement time
            seen.append(json.loads(jp.read_text(encoding="utf-8")))
        return real_pgbench(pgbench_path, host, port, dbname,
                            duration=duration, clients=clients, gucs=gucs)

    monkeypatch.setattr(restartbench.bench, "run_pgbench", spy_pgbench)
    results = restartbench.run_restart_bench(
        "t9", [RestartCandidate("sb512", {"shared_buffers": "512MB"})], dbname="bench_new")

    assert len(seen) == 2                        # the sb512 cycle's warmup + measurement both ran
                                                 # under the on-disk journal; baseline (its 2 calls) carried none
    doc = seen[0]
    assert seen[1] == seen[0]                    # one stable journal per GUC cycle, not rewrites mid-flight
    assert doc["instance"] == "t9"
    assert doc["snapshot"] == {"shared_buffers": "16384", "work_mem": "4096"}   # the full baseline
    assert doc["created"] and doc["pid"] == os.getpid()
    assert not jp.exists()                       # the verified final restore removed the journal
    # R2-C: every cycle carries the real restore receipt, not a hardcoded literal
    assert all(r.restore == {"restored": True, "mismatches": []} for r in results)


def test_leftover_journal_auto_restored_before_new_run(monkeypatch, tmp_path, capsys):
    """R2-A: simulate a run killed after ALTER SYSTEM (override left in the "auto.conf",
    journal on disk, _TOUCHED empty as in a fresh process) — the next run_restart_bench
    restores from the journal FIRST (RESET + restart + verify), prints the one-line
    notice, and only then takes the new baseline: the drift cannot be laundered."""
    proj = tmp_path / "proj"
    monkeypatch.setenv("PGTUNER_PROJECT_ROOT", str(proj))
    h = install_harness(monkeypatch)
    jp = _journal_path(proj)
    restartbench._journal_write("t9", {"shared_buffers": "16384", "work_mem": "4096"})
    h.state.overrides["shared_buffers"] = "1GB"  # the killed experiment's leftover GUC

    results = restartbench.run_restart_bench("t9", [], dbname="bench_new")

    assert "ALTER SYSTEM RESET \"shared_buffers\"" in h.state.statements   # the recovery did the RESET
    assert h.state.overrides == {}                   # the drift was washed away before the new baseline
    assert not jp.exists()
    assert "发现上次未完成实验的残留配置,已自动还原" in capsys.readouterr().err
    # restarts: 1 recovery (RESET-triggered) + 1 auto-prepended baseline cycle; the
    # post-recovery baseline restore is a no-op (nothing drifted anymore)
    assert h.calls["stop"] == ["t9", "t9"]


def test_leftover_journal_absent_is_a_silent_noop(monkeypatch, capsys):
    """Normal path unchanged: without a leftover journal the startup recovery does no
    SQL, no restart, prints no leftover notice."""
    h = install_harness(monkeypatch)
    restartbench.run_restart_bench(
        "t9", [RestartCandidate("wm", {"work_mem": "64MB"})], dbname="db")
    assert "残留" not in capsys.readouterr().err


def test_corrupted_journal_refuses_run_and_is_kept(monkeypatch, tmp_path):
    """C-F7: a journal that exists but cannot be parsed gets the same treatment as a
    failed recovery — run_restart_bench refuses to start, the RuntimeError names the
    on-disk path and points at manual deletion, and the file itself is NOT
    auto-deleted: deleting it would let the next run take the drifted pg_settings as
    its fresh baseline (laundering the drift the journal exists to expose)."""
    proj = tmp_path / "proj"
    monkeypatch.setenv("PGTUNER_PROJECT_ROOT", str(proj))
    h = install_harness(monkeypatch)
    jp = _journal_path(proj)
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text("{not valid json!!", encoding="utf-8")       # unparseable bytes
    with pytest.raises(RuntimeError) as ei:
        restartbench.run_restart_bench(
            "t9", [RestartCandidate("sb512", {"shared_buffers": "512MB"})],
            dbname="bench_new")
    assert str(jp) in str(ei.value) and "人工核对后删除" in str(ei.value)
    assert jp.exists()                                          # kept: no silent deletion
    assert h.state.statements == [] and h.calls["stop"] == []    # refused before touching config

    jp.write_text(json.dumps({"instance": "t9", "no_snapshot": True}),
                  encoding="utf-8")                              # parseable but structurally invalid
    with pytest.raises(RuntimeError, match="人工核对后删除"):
        restartbench.run_restart_bench("t9", [], dbname="bench_new")
    assert jp.exists()


def test_journal_kept_as_evidence_when_restore_fails(monkeypatch, tmp_path):
    """R2-A: a restore that cannot verify leaves the journal on disk (evidence + recovery
    recipe for the next run); deletion happens only on the verified-success path."""
    proj = tmp_path / "proj"
    monkeypatch.setenv("PGTUNER_PROJECT_ROOT", str(proj))
    h = install_harness(monkeypatch)
    jp = _journal_path(proj)
    restartbench._journal_write("t9", {"shared_buffers": "16384", "work_mem": "4096"})
    restartbench.apply_and_restart(h.inst, {"shared_buffers": "512MB"})
    h.state.base["work_mem"] = "999999kB"        # conf-layer tampering: RESET cannot bring it back
    with pytest.raises(RuntimeError, match="不一致"):
        restartbench.restore_and_verify(h.inst, {"shared_buffers": "16384", "work_mem": "4096"})
    assert jp.exists()                           # kept: the next run still has a recovery recipe


def test_restore_blocks_sigint_sigterm_during_restart(monkeypatch, capsys):
    """R2-B: while the restore's stop/start is in flight the process must have
    SIGINT/SIGTERM blocked (a second Ctrl-C cannot abort the restore halfway); after
    the restore completes the previous mask is exactly restored; the warning line is
    printed before the blocking starts."""
    h = install_harness(monkeypatch)
    snap = restartbench.snapshot_settings("host=/tmp port=5555 dbname=postgres")
    restartbench.apply_and_restart(h.inst, {"shared_buffers": "512MB"})

    masks_at_stop: list[set] = []
    real_stop = restartbench.instance_mod.stop

    def spy_stop(name):
        masks_at_stop.append(set(signal.pthread_sigmask(signal.SIG_BLOCK, [])))   # probe the current mask
        return real_stop(name)

    monkeypatch.setattr(restartbench.instance_mod, "stop", spy_stop)
    before = set(signal.pthread_sigmask(signal.SIG_BLOCK, []))

    out = restartbench.restore_and_verify(h.inst, snap)

    assert out == {"restored": True, "mismatches": []}
    assert masks_at_stop and signal.SIGINT in masks_at_stop[0] and signal.SIGTERM in masks_at_stop[0]
    assert set(signal.pthread_sigmask(signal.SIG_BLOCK, [])) == before            # no mask leakage afterwards
    assert "正在还原配置" in capsys.readouterr().err


def test_delayed_signals_helper_roundtrip():
    """R2-B (unit): the helper blocks SIGINT/SIGTERM on entry and restores the exact
    previous mask on exit; skipped on platforms without pthread_sigmask."""
    if not hasattr(signal, "pthread_sigmask"):
        pytest.skip("signal.pthread_sigmask 不可用(非 POSIX)")
    before = set(signal.pthread_sigmask(signal.SIG_BLOCK, []))
    with restartbench._delayed_signals() as blocked:
        assert blocked is True
        cur = set(signal.pthread_sigmask(signal.SIG_BLOCK, []))
        assert {signal.SIGINT, signal.SIGTERM} <= cur
    assert set(signal.pthread_sigmask(signal.SIG_BLOCK, [])) == before


def test_cycle_restore_receipt_none_in_measure_only(monkeypatch):
    """R2-C: measure_only runs no restore → the receipt field is None (distinct from a
    failed restore, which raises, and a verified restore, which carries the dict)."""
    h = install_harness(monkeypatch, explain_times={"SELECT 1": 5.0})
    results = restartbench.run_restart_bench(
        "t9", [], dbname="db", workload="custom", custom_queries=["SELECT 1"],
        measure_only=True)
    assert results[0].restore is None


def test_run_restart_bench_refuses_concurrent_same_instance(monkeypatch):
    """R2-D M4: two concurrent rebench runs on one instance would interleave ALTER
    SYSTEM cycles and clobber each other's restore journal — the second must refuse."""
    h = install_harness(monkeypatch)
    with restartbench._instance_run_lock(h.inst.name):
        with pytest.raises(RuntimeError, match="另一个 rebench"):
            restartbench.run_restart_bench(
                h.inst.name, [], dbname="postgres", workload="custom",
                custom_queries=["SELECT 1"], measure_only=True)


# ---------------------------- R4 C-NIT-3: estimate-only stop exits nonzero ----------------------------

def test_rebench_estimate_only_stop_exits_nonzero(tmp_path, monkeypatch):
    """R4 C-NIT-3: without --yes, rebench prints the slow-channel estimate and
    stops — that stop used to leave rc=0, so agent chains
    (`rebench ... && results diff ...`) read "refused, never ran" as success.
    The exit is now 1 with the friendly message intact (no Error prefix:
    ctx.exit is click control flow). No instance is touched on this path."""
    from click.testing import CliRunner
    from pgtuner import cli
    r = CliRunner().invoke(
        cli.cli, ["rebench", "--instance", "t9", "--guc", "work_mem=64MB"])
    assert r.exit_code == 1, (r.exit_code, r.output)
    assert "慢通道确认" in r.output and "确认执行请加 --yes" in r.output
    assert "Error" not in r.output                       # friendly refusal, not an exception render


def test_rebench_setup_gate_stop_exits_nonzero(tmp_path, monkeypatch):
    """Same contract on the --setup consent gate (writes onto a non-pool db
    without --template): the "add --yes" stop must not masquerade as success."""
    import sys
    from click.testing import CliRunner
    from pgtuner import cli
    qf = tmp_path / "q.sql"
    qf.write_text("SELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr(cli.instance_mod, "pool_stats", lambda registry_file=None: {})
    argv = ["pgtuner", "rebench", "--instance", "t9", "--queries-file", str(qf),
            "--db=d1", "--setup", "ANALYZE events"]
    monkeypatch.setattr(sys, "argv", argv)               # --db given (guard reads sys.argv)
    r = CliRunner().invoke(cli.cli, argv[1:])
    assert r.exit_code == 1, (r.exit_code, r.output)
    assert "确认执行请加 --yes" in r.output and "Error" not in r.output
