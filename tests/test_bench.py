"""bench tests: pgbench stdout parsing, candidate-set generation (context filtering / variants / dedup), execution orchestration (mocked subprocess)."""
import os

import pytest

from pgtuner import bench
from pgtuner.engine import recommend_all
from pgtuner.knowledge import load_knowledge

KNOWLEDGE_PATH = os.path.join(os.path.dirname(__file__), "..", "pgtuner", "data", "guc_knowledge.json")

# new pgbench: single tps line (without initial connection time, equivalent to excluding connections establishing)
NEW_FORMAT = """transaction type: <builtin: TPC-B (Sort of)>
scaling factor: 10
query mode: prepared
number of clients: 4
number of threads: 4
duration: 10 s
number of transactions actually processed: 13384
latency average = 2.991 ms
initial connection time = 21.311 ms
tps = 1337.62 (without initial connection time)
"""

# old pgbench: two tps lines (including / excluding connections establishing)
OLD_FORMAT = """transaction type: TPC-B (Sort of)
scaling factor: 10
number of clients: 4
number of transactions actually processed: 5000
latency average = 3.200 ms
tps = 1240.50 (including connections establishing)
tps = 1238.42 (excluding connections establishing)
"""

INPUTS = {   # same source as tests/test_engine.REALISTIC (mixed workload)
    "total_ram_gb": 16, "cpu_cores": 8, "os_type": "linux", "disk_type": "ssd",
    "workload_type": "mixed", "max_connections": 100, "db_size_gb": 50,
    "risk_appetite": "balanced", "pg_version": 18,
}

# live pg_settings mock: these GUCs are recorded as context='user' (SETtable at runtime), everything else sighup
USER_GUCS = {"work_mem", "random_page_cost", "max_parallel_workers_per_gather",
             "effective_cache_size", "jit", "maintenance_work_mem", "max_parallel_workers"}


@pytest.fixture(scope="module")
def recs():
    return recommend_all(load_knowledge(KNOWLEDGE_PATH), dict(INPUTS))


def make_settings(all_names):
    return {n: {"setting": "1", "unit": None,
                "context": "user" if n in USER_GUCS else "sighup"} for n in all_names}


@pytest.fixture(scope="module")
def settings(recs):
    return make_settings([r.name for r in recs])


# ---------------------------- stdout parsing ----------------------------

def test_parse_new_format():
    parsed = bench.parse_pgbench_output(NEW_FORMAT)
    assert parsed["tps"] == 1337.62
    assert parsed["latency_ms"] == 2.991


def test_parse_old_format_prefers_excluding_line():
    parsed = bench.parse_pgbench_output(OLD_FORMAT)
    assert parsed["tps"] == 1238.42          # takes the excluding connections establishing line
    assert parsed["latency_ms"] == 3.2


def test_parse_failure_output():
    parsed = bench.parse_pgbench_output("pgbench: error: could not connect")
    assert parsed["tps"] is None and parsed["latency_ms"] is None


# ---------------------------- candidate-set generation ----------------------------

def test_build_candidates_simple(recs, settings):
    cands = bench.build_candidates(recs, settings, "simple")
    assert [c.name for c in cands] == ["baseline", "recommended", "work_mem×0.5", "work_mem×2"]
    assert cands[0].gucs == {}
    rec = cands[1]
    assert set(rec.gucs) == USER_GUCS                    # only inject recommended values with context='user'
    assert rec.gucs["work_mem"] == "17MB"                # recommended primary value (17408kB)
    assert all("'" not in v for v in rec.gucs.values())  # PGOPTIONS -c does not accept conf quote syntax
    skipped = dict(rec.skipped)
    assert skipped["shared_buffers"] == "sighup"         # in this mock everything non-user is sighup
    assert skipped["max_wal_size"] == "sighup"
    # single-variable perturbation: only work_mem, others keep current server settings; base value = recommended value
    assert cands[2].gucs == {"work_mem": "8704kB"}       # 17408×0.5 (non-integer MB falls back to kB spelling)
    assert cands[3].gucs == {"work_mem": "34MB"}         # 17408×2


def test_build_candidates_parallel_stacks_on_recommended(recs, settings):
    cands = bench.build_candidates(recs, settings, "parallel")
    # with this input the recommendation already contains max_parallel_workers_per_gather=4 → rec+...=4 equals recommended and is deduped
    assert [c.name for c in cands][2:] == [
        "rec+max_parallel_workers_per_gather=0",
        "rec+max_parallel_workers_per_gather=2"]
    m2 = cands[3]
    assert m2.gucs["max_parallel_workers_per_gather"] == "2"
    assert m2.gucs["work_mem"] == "17MB"                 # stacking: recommended combination + single-parameter override


def test_build_candidates_rpc_dedupes(recs, settings):
    cands = bench.build_candidates(recs, settings, "rpc")
    names = [c.name for c in cands]
    # recommended already contains random_page_cost=1.1 → rec+rpc=1.1 injects the same as recommended, deduped
    assert "rec+random_page_cost=1.1" not in names
    assert "rec+random_page_cost=4" in names
    rec4 = next(c for c in cands if c.name == "rec+random_page_cost=4")
    assert rec4.gucs["random_page_cost"] == "4"
    assert rec4.gucs["work_mem"] == "17MB"


def test_build_candidates_all(recs, settings):
    cands = bench.build_candidates(recs, settings, "all")
    names = [c.name for c in cands]
    assert names[0] == "baseline" and names[1] == "recommended"
    assert "rec+work_mem×0.5" in names and "rec+work_mem×2" in names
    assert "rec+random_page_cost=4" in names
    assert "rec+max_parallel_workers_per_gather=0" in names
    # 2 + 2 (wm) + 1 (rpc4; 1.1 duplicates recommended and is deduped) + 2 (mppg 0/2; 4 duplicates recommended and is deduped)
    assert len(cands) == 7


def test_build_candidates_invalid_choice(recs, settings):
    with pytest.raises(ValueError):
        bench.build_candidates(recs, settings, "everything")


def test_recommended_injection_filters_by_live_context(recs):
    """Filtering follows the live pg_settings.context (the knowledge base context field is descriptive text, unusable)."""
    ctx = {"work_mem": "user", "shared_buffers": "postmaster", "wal_compression": "superuser"}
    gucs, skipped = bench.recommended_injection(recs, ctx)
    assert "work_mem" in gucs
    assert "shared_buffers" not in gucs and "wal_compression" not in gucs
    assert ("shared_buffers", "postmaster") in skipped
    assert ("wal_compression", "superuser") in skipped


def test_work_mem_base_falls_back_to_runtime_setting(recs):
    settings = {"work_mem": {"setting": "4096", "unit": "kB", "context": "user"}}
    assert bench._work_mem_base_kb(recs, settings) == 17408     # recommended value takes precedence
    no_wm_recs = [r for r in recs if r.name != "work_mem"]
    assert bench._work_mem_base_kb(no_wm_recs, settings) == 4096  # falls back to current runtime value


# ---------------------------- execution orchestration (mocked subprocess) ----------------------------

def test_build_pgoptions():
    assert bench.build_pgoptions(None) == ""                 # baseline clears leftover PGOPTIONS from the environment
    assert bench.build_pgoptions({"a": "1", "b": "2MB"}) == "-c a=1 -c b=2MB"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_pgbench_command_and_env(monkeypatch):
    captured = {}

    def fake_run(cmd, env=None, capture_output=None, text=None, timeout=None):
        captured.update(cmd=cmd, env=env, timeout=timeout)
        return FakeProc(stdout=NEW_FORMAT)

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    bench.run_pgbench("/usr/bin/pgbench", "/tmp", "5432", "bench_new",
                      duration=10, clients=8, gucs={"work_mem": "17MB"})
    assert captured["cmd"] == ["/usr/bin/pgbench", "-h", "/tmp", "-p", "5432", "-d", "bench_new",
                               "-n", "-M", "prepared", "-T", "10", "-c", "8", "-j", "4"]
    assert captured["env"]["PGOPTIONS"] == "-c work_mem=17MB"
    assert captured["timeout"] == 10 * 2 + 120


def test_run_candidates_orchestration(monkeypatch):
    def fake_run(cmd, env=None, capture_output=None, text=None, timeout=None):
        # baseline (no injection) gets old-format output, injected candidates get new-format output; verifies both formats parse successfully
        if env.get("PGOPTIONS"):
            return FakeProc(stdout=NEW_FORMAT)
        return FakeProc(stdout=OLD_FORMAT)

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    cands = [bench.Candidate("baseline"), bench.Candidate("tuned", {"work_mem": "34MB"})]
    seen = []
    results = bench.run_candidates(cands, pgbench_path="pgbench", host="/tmp", port="5432",
                                   dbname="bench_new", duration=5, clients=4,
                                   on_result=seen.append)
    assert [r.tps for r in results] == [1238.42, 1337.62]
    assert [r.latency_ms for r in results] == [3.2, 2.991]
    assert seen == results                                  # progress callback fires per candidate


def test_run_candidates_failure_recorded_not_fatal(monkeypatch):
    calls = []

    def fake_run(cmd, env=None, capture_output=None, text=None, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            return FakeProc(returncode=1, stderr="pgbench: error: connection to server failed")
        return FakeProc(stdout=NEW_FORMAT)

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    results = bench.run_candidates(
        [bench.Candidate("bad", {"work_mem": "999GB"}), bench.Candidate("ok")],
        pgbench_path="pgbench", host="/tmp", port="5432", dbname="bench_new",
        duration=5, clients=4)
    assert results[0].tps is None
    assert "connection to server failed" in results[0].error
    assert results[1].tps == 1337.62                        # a failure does not interrupt subsequent candidates


def test_run_init_command(monkeypatch):
    captured = {}

    def fake_run(cmd, env=None, capture_output=None, text=None, timeout=None):
        captured.update(cmd=cmd, timeout=timeout)
        return FakeProc()

    monkeypatch.setattr(bench.subprocess, "run", fake_run)
    bench.run_init("/usr/bin/pgbench", "/tmp", "5432", "bench_new", scale=10)
    assert captured["cmd"] == ["/usr/bin/pgbench", "-h", "/tmp", "-p", "5432",
                               "-d", "bench_new", "-i", "-s", "10"]
