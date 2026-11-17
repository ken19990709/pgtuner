"""engine tests: full-eval smoke over all 109 formulas + units/gating/primary selection/dependency order."""
import os

import pytest

from pgtuner.engine import (FUNCS, UNITS, eval_formula, recommend_all, to_kb, to_mb)
from pgtuner.knowledge import load_knowledge

KNOWLEDGE_PATH = os.path.join(os.path.dirname(__file__), "..", "pgtuner", "data", "guc_knowledge.json")

# synthetic namespace required by the task (total_ram_gb=16, cpu_cores=8, disk_type='nvme', workload_type='oltp',
# max_connections=100, db_size_gb=50, pg_version=18, os_type='linux' + chained variables after dependency backfill)
REALISTIC = {
    "total_ram_gb": 16, "cpu_cores": 8, "disk_type": "nvme", "workload_type": "oltp",
    "max_connections": 100, "db_size_gb": 50, "pg_version": 18, "os_type": "linux",
    "risk_appetite": "balanced",
    # chained variables after dependency backfill (converted results such as shared_buffers=25%RAM=4GB)
    "shared_buffers_kb": 4194304, "shared_buffers_mb": 4096.0, "shared_buffers_pages": 524288,
    "max_parallel_workers": 8, "parallel_workers_per_gather": 4,
    "max_wal_size_mb": 8192.0, "maintenance_work_mem_kb": 1048576,
    "work_mem_kb": 34816, "work_mem_mb": 34.0,
}

# namespace covering all 56 input variables (database observation variables get plausible synthetic values), for the full smoke run
FULL = {
    **REALISTIC,
    "wal_rate_mbps": 10.0, "autovacuum_max_workers": 3, "avg_join_count": 8,
    "hash_nodes": 2, "sort_nodes": 2, "hash_mem_multiplier": 2.0,
    "freeable_mem_kb": 8 * 1024 * 1024, "buffer_cache_hit_ratio": 0.99,
    "effective_spindle_count": 1, "mem_per_conn_mb": 5.0, "avg_ops_per_connection": 2.0,
    "target_dead_tuples": 100_000, "biggest_table_live_tuples": 10_000_000,
    "vacuum_max_threshold": 1_000_000, "autovacuum_vacuum_threshold": 50,
    "scale_factor": 0.05, "n_live_tup": 1_000_000, "cost_delay_ms": 2.0,
    "per_worker_cost_limit": 2000, "page_cost": 1.0, "rto_seconds": 1800,
    "high_concurrency_oltp": False, "tolerable_loss_ms": 1000, "wal_writer_delay_ms": 200,
    "os_free_and_cached_gb": 8.0, "shared_memory_bytes": 4 * 1024 ** 3,
    "huge_page_size_bytes": 2 * 1024 ** 2, "table_mb": 1000.0,
    "cpu_saturated": False, "cpu_headroom_high": True, "wal_bottleneck": False,
    "n_databases": 3, "avg_db_vacuum_seconds": 120, "liburing_available": True,
    "avg_txn_ms": 5.0, "wal_flush_iops_near_limit": False, "concurrent_commits": 20,
    "cursor_usage": "interactive",
}


@pytest.fixture(scope="module")
def knowledge():
    return load_knowledge(KNOWLEDGE_PATH)


def test_all_109_formulas_eval(knowledge):
    """Full smoke: each of the 109 formulas is evaluated in the restricted evaluator, yielding a value or None, never raising."""
    count = 0
    for g in knowledge.gucs:
        for r in g.rules:
            value = eval_formula(r.formula, FULL)
            assert value is None or isinstance(value, (int, float, str, bool)), (g.name, r.formula)
            count += 1
    assert count == 109


def test_units_table(knowledge):
    """Units table: non-none entries agree with the caveats annotations, and conversions are correct."""
    assert UNITS[("shared_buffers", 2)] == "pages_8k"   # caveats: "输出 8kB 块数"
    assert UNITS[("shared_buffers", 4)] == "pages_8k"   # caveats: "输出 8kB 页数"
    assert UNITS[("effective_cache_size", 4)] == "pages_8k"
    assert UNITS[("max_wal_size", 0)] == "MB" and UNITS[("max_wal_size", 3)] == "MB"
    assert UNITS[("min_wal_size", 0)] == "MB"
    assert UNITS[("checkpoint_timeout", 0)] == "sec"
    assert UNITS[("autovacuum_vacuum_cost_delay", 0)] == "ms"
    assert UNITS[("commit_delay", 0)] == "us"
    assert UNITS[("work_mem", 0)] == "kB"
    assert UNITS.get(("random_page_cost", 0), "none") == "none"
    assert to_kb(2048, "MB") == 2048 * 1024
    assert to_kb(3, "pages_8k") == 24            # 8kB pages ×3
    assert to_mb(2, "pages_8k") == 16 / 1024     # 2 pages = 16kB
    with pytest.raises(ValueError):
        to_kb(1, "sec")


def test_eval_formula_rejects_unknown_or_unsafe(knowledge):
    assert eval_formula("min(4, 2) + 1", {}) == 3
    with pytest.raises(ValueError):   # identifier not injected
        eval_formula("total_ram_gb + nope", {"total_ram_gb": 16})
    with pytest.raises(ValueError):   # non-whitelisted node (attribute access)
        eval_formula("x.__class__", {"x": 1})
    with pytest.raises(ValueError):   # non-whitelisted node (lambda)
        eval_formula("(lambda: 1)()", {})


def test_recommend_dependency_order(knowledge):
    """Dependency order: work_mem sees the backfilled shared_buffers_kb; hash_mem_multiplier sees work_mem_mb."""
    recs = {r.name: r for r in recommend_all(knowledge, dict(REALISTIC))}
    wm = recs["work_mem"].primary
    assert wm is not None and wm.index == 0            # primary selection: first successful rule = PGTune (uses shared_buffers_kb)
    # hand calculation: int((16*1048576-4194304)/((100+8)*3))*1*0.9//1024*1024 → 34816 kB
    assert wm.value == 34816 and wm.unit == "kB"
    hm = recs["hash_mem_multiplier"].primary           # wave3 depends on work_mem_mb=34
    assert hm is not None and hm.value == 2.0
    assert hm.status == "ok" and not hm.note           # 34MB < 40MB, no uplift annotation
    wb = recs["wal_buffers"].primary                   # min(16384, max(64, 4194304//256*8)) = 16384
    assert wb is not None and wb.value == 16384 and wb.unit == "kB"
    # checkpoint_timeout depends on max_wal_size_mb (backfilled) but wal_rate_mbps is an observation input → skipped as missing input
    ct = recs["checkpoint_timeout"].results[0]
    assert ct.status == "missing_input" and "wal_rate_mbps" in ct.reason
    # effective_cache_size[4] (Azure) uses the backfilled shared_buffers_pages: 16*131072-524288
    ecs4 = recs["effective_cache_size"].results[4]
    assert ecs4.status == "ok" and ecs4.value == 16 * 131072 - 524288 and ecs4.unit == "pages_8k"


def test_gate_effective_io_concurrency(knowledge):
    """Gating: the PGTune rule does not emit on non-Linux with PG<18; PG18 lets it through."""
    ns = dict(REALISTIC, os_type="mac", pg_version=17)
    recs = {r.name: r for r in recommend_all(knowledge, ns)}
    rule0 = recs["effective_io_concurrency"].results[0]
    assert rule0.status == "gated"
    assert "posix_fadvise" in rule0.reason
    # from PG18 unified across platforms → allowed
    recs18 = {r.name: r for r in recommend_all(knowledge, dict(REALISTIC, os_type="mac", pg_version=18))}
    assert recs18["effective_io_concurrency"].results[0].status == "ok"
    assert recs18["effective_io_concurrency"].results[0].value == 1000


def test_gate_checkpoint_completion_target(knowledge):
    recs = {r.name: r for r in recommend_all(knowledge, REALISTIC)}       # pg_version=18
    rec = recs["checkpoint_completion_target"]
    assert rec.primary is None and rec.results[0].status == "gated"
    recs13 = {r.name: r for r in recommend_all(knowledge, dict(REALISTIC, pg_version=13))}
    assert recs13["checkpoint_completion_target"].primary.value == 0.9    # PG13 needs it emitted explicitly


def test_gate_maintenance_io_concurrency(knowledge):
    recs = {r.name: r for r in recommend_all(knowledge, dict(REALISTIC, pg_version=12))}
    assert recs["maintenance_io_concurrency"].results[0].status == "gated"
    recs18 = {r.name: r for r in recommend_all(knowledge, REALISTIC)}
    assert recs18["maintenance_io_concurrency"].primary is not None       # emitted for PG>=13


def test_missing_input_recorded(knowledge):
    """A rule with missing input is skipped with the reason recorded; never guesses a value."""
    recs = {r.name: r for r in recommend_all(knowledge, {"total_ram_gb": 16, "cpu_cores": 8})}
    sb0 = recs["shared_buffers"].results[0]
    assert sb0.status == "missing_input" and "workload_type" in sb0.reason
    # shared_buffers[1] implicit gate: depends on the buffer_cache_hit_ratio observation (caveats), skipped when absent
    assert recs["shared_buffers"].results[1].status == "missing_input"
    assert "buffer_cache_hit_ratio" in recs["shared_buffers"].results[1].reason


def test_observation_unlocks_and_annotates(knowledge):
    """After the check layer injects observations: rules depending on observations are allowed through; shared_buffers[1] with hit ratio <0.9 triggers the uplift annotation."""
    ns = dict(REALISTIC, buffer_cache_hit_ratio=0.85)
    recs = {r.name: r for r in recommend_all(knowledge, ns)}
    sb1 = recs["shared_buffers"].results[1]
    assert sb1.status == "ok" and sb1.value == int(16 * 1048576 * 0.35)
    assert "上调" in sb1.note
    # no annotation when the hit ratio is healthy
    recs_ok = {r.name: r for r in recommend_all(knowledge, dict(REALISTIC, buffer_cache_hit_ratio=0.95))}
    assert recs_ok["shared_buffers"].results[1].note == ""


def test_primary_selection_order(knowledge):
    """Primary selection: the first successful rule wins, the rest are alternatives (competing formulas shown side by side)."""
    recs = {r.name: r for r in recommend_all(knowledge, REALISTIC)}
    wm = recs["work_mem"]
    assert wm.primary.index == 0
    alts = [r.index for r in wm.alternatives]
    assert 1 in alts and 2 in alts          # competing formulas such as EDB/Greg Smith become alternatives
    mws = recs["max_wal_size"]
    assert mws.primary.index == 1           # [0] lacks wal_rate_mbps → falls through to [1] (PGTune tiering)
    assert mws.primary.value == 8192 and mws.primary.unit == "MB"


def test_io_workers_only_pg18(knowledge):
    """2026-11 application-verification fix: io_workers is emitted only for PG18 (replaced by io_max_workers from PG19;
    applying it verbatim on PG20 would FATAL with unrecognized configuration parameter)."""
    for ver in (19, 20):
        recs = {r.name: r for r in recommend_all(knowledge, dict(REALISTIC, pg_version=ver))}
        io = recs["io_workers"]
        assert io.primary is None, f"pg_version={ver} 不应输出 io_workers"
        assert all(r.status == "none_result" for r in io.results)
    # PG18 (the newest version within the extrapolation cap) still emits normally
    recs18 = {r.name: r for r in recommend_all(knowledge, dict(REALISTIC))}
    assert recs18["io_workers"].primary is not None


def test_min_wal_size_consistency_adjustment(knowledge):
    """2026-11 application-verification fix: when min_wal_size ≥ max_wal_size (two independent formulas unaware of each other),
    min is adjusted to max(80MB, max_wal_size//2) with the contradiction annotated."""
    # wal_rate 0.55 → max_wal_size[0]=max(1024, 1980)=1980MB < min_wal_size[0] (oltp tier 2048MB)
    ns = dict(REALISTIC, wal_rate_mbps=0.55)
    recs = {r.name: r for r in recommend_all(knowledge, ns)}
    assert recs["max_wal_size"].primary.value == 1980
    mn = recs["min_wal_size"].primary
    assert mn.value == 990 and mn.unit == "MB"     # max(80, 1980//2)
    assert "一致性调整" in mn.note and "2048" in mn.note and "矛盾" in mn.note
    # without a contradiction (REALISTIC: max=8192 > min=2048) the original value is kept, no annotation
    recs_ok = {r.name: r for r in recommend_all(knowledge, dict(REALISTIC))}
    mn_ok = recs_ok["min_wal_size"].primary
    assert mn_ok.value == 2048 and not mn_ok.note


# ------------------- hardware-aware clamping (heavy-load verification fix) -------------------

def test_shared_buffers_available_mem_clamp(knowledge):
    """Clamp triggers: with total_ram=16GB the formula gives 4GB, but MemAvailable=2GB → clamped to 2×1048576×0.6=1258291kB;
    ordering correctness: the clamp happens before backfill, so the downstream work_mem[0] (total−shared_buffers_kb budget formula)
    consumes the clamped value — hand calculation (16777216−1258291)/324=47903 → ×0.9//1024×1024 = 43008kB."""
    ns = dict(REALISTIC, mem_available_gb=2.0)
    recs = {r.name: r for r in recommend_all(knowledge, ns)}
    sb = recs["shared_buffers"].primary
    assert sb.value == 1258291 and sb.unit == "kB"
    assert "可用内存钳制" in sb.note and "60%" in sb.note and "MemAvailable=2GB" in sb.note
    wm = recs["work_mem"].primary
    assert wm.value == 43008 and wm.unit == "kB"   # evaluated with the clamped shared_buffers_kb (not the unclamped 34816)


def test_shared_buffers_no_clamp_when_mem_plenty(knowledge):
    """Clamp does not trigger: MemAvailable is plentiful (16GB×60%=9.6GB > the 4GB formula value) → original value kept, no annotation."""
    recs = {r.name: r for r in recommend_all(knowledge, dict(REALISTIC, mem_available_gb=16.0))}
    sb = recs["shared_buffers"].primary
    assert sb.value == 4194304 and not sb.note


def test_shared_buffers_clamp_skipped_when_unavailable(knowledge):
    """Skipped when the probe is missing (backward compatible): without mem_available_gb in the ns there is no clamping, behavior identical to before the fix."""
    recs = {r.name: r for r in recommend_all(knowledge, dict(REALISTIC))}   # no mem_available_gb
    sb = recs["shared_buffers"].primary
    assert sb.value == 4194304 and not sb.note
    assert recs["work_mem"].primary.value == 34816


def test_max_wal_size_disk_free_clamp(knowledge):
    """Disk clamp triggers: wal_rate 42.63 → max_wal_size[0]=153468MB > the free space 10GB×1024×0.25=2560MB;
    after clamping, min_wal_size consistency still holds (oltp tier 2048 < 2560, no further adjustment needed)."""
    ns = dict(REALISTIC, wal_rate_mbps=42.63, disk_free_gb=10.0)
    recs = {r.name: r for r in recommend_all(knowledge, ns)}
    mx = recs["max_wal_size"].primary
    assert mx.value == 2560 and mx.unit == "MB"
    assert "磁盘余量钳制" in mx.note and "25%" in mx.note
    mn = recs["min_wal_size"].primary
    assert mn.value == 2048 and not mn.note        # min < the clamped max


def test_max_wal_size_clamp_then_min_consistency(knowledge):
    """Ordering correctness: disk clamp first, then min≤max consistency — with a tight disk (4GB→cap=1024MB)
    max clamps to 1024 and min (2048) is then adjusted to max/2 = 512, compared against the clamped max."""
    ns = dict(REALISTIC, wal_rate_mbps=42.63, disk_free_gb=4.0)
    recs = {r.name: r for r in recommend_all(knowledge, ns)}
    assert recs["max_wal_size"].primary.value == 1024
    mn = recs["min_wal_size"].primary
    assert mn.value == 512 and mn.unit == "MB"     # max(80, 1024//2)
    assert "一致性调整" in mn.note


def test_max_wal_size_no_disk_clamp(knowledge):
    """Clamp not triggered / probe missing: with a roomy disk (852GB→cap=218112MB > 153468MB) no clamping;
    without disk_free_gb no clamping either (backward compatible, 153468MB emitted verbatim)."""
    for free_gb in (852.0, None):
        ns = dict(REALISTIC, wal_rate_mbps=42.63)
        if free_gb is not None:
            ns["disk_free_gb"] = free_gb
        recs = {r.name: r for r in recommend_all(knowledge, ns)}
        mx = recs["max_wal_size"].primary
        assert mx.value == 153468 and not mx.note
