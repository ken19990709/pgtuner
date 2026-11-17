"""inputs / hardware tests: IOPS tier classification (mocked), input assembly, missing-input skipping, enum validation."""
import os

import pytest

from pgtuner.engine import recommend_all
from pgtuner.hardware import HardwareInfo, _mem_available_gb, classify_disk_by_iops, disk_free_gb
from pgtuner.inputs import PG_VERSION_CAP, build_inputs, is_extrapolated
from pgtuner.knowledge import load_knowledge

KNOWLEDGE_PATH = os.path.join(os.path.dirname(__file__), "..", "pgtuner", "data", "guc_knowledge.json")


def hw(**kw) -> HardwareInfo:
    base = dict(total_ram_gb=16.0, cpu_cores=8, os_type="linux", is_wsl2=False,
                disk_type="ssd", disk_confidence="medium", rotational={"sda": 0},
                bench_iops=5000.0, bench_path="/tmp", bench_error=None, disk_notes=[])
    base.update(kw)
    return HardwareInfo(**base)


@pytest.mark.parametrize("iops,expected", [
    (150, "hdd"),        # <200 → hdd
    (5000, "ssd"),       # 200–20000 → ssd/cloud
    (50000, "nvme"),     # >20000 → nvme
    (200, "ssd"),        # lower boundary included in the ssd tier
    (20000, "ssd"),      # upper boundary included in the ssd tier
    (20001, "nvme"),
    (199.9, "hdd"),
])
def test_classify_disk_by_iops(iops, expected):
    assert classify_disk_by_iops(iops) == expected


def test_build_inputs_defaults():
    inputs = build_inputs(hw())
    assert inputs["total_ram_gb"] == 16.0
    assert inputs["db_size_gb"] == 16.0            # default = total_ram_gb
    assert inputs["pg_version"] == PG_VERSION_CAP  # not probed → knowledge cap 18
    assert inputs["workload_type"] == "mixed"
    assert inputs["max_connections"] == 100
    assert inputs["risk_appetite"] == "balanced"
    assert inputs["disk_type"] == "ssd"            # from the probe
    assert is_extrapolated(inputs) is False


def test_build_inputs_overrides_and_observations():
    inputs = build_inputs(hw(), workload_type="dw", db_size_gb=500, pg_version=19,
                          disk_type="cloud", db_observations={"wal_rate_mbps": 25.0})
    assert inputs["workload_type"] == "dw"
    assert inputs["db_size_gb"] == 500.0
    assert inputs["disk_type"] == "cloud"          # --disk-type overrides the probe
    assert inputs["wal_rate_mbps"] == 25.0         # observation injected by the check layer
    assert is_extrapolated(inputs)                 # 19+ → extrapolation flagged
    # when extrapolated, formulas still evaluate on the newest branch: checkpoint_timeout yields a value using max_wal_size_mb + the wal_rate observation
    recs = {r.name: r for r in recommend_all(load_knowledge(KNOWLEDGE_PATH), inputs)}
    ct = recs["checkpoint_timeout"].primary
    assert ct is not None and ct.status == "ok"


def test_build_inputs_enum_validation():
    with pytest.raises(ValueError):
        build_inputs(hw(), workload_type="analytics")
    with pytest.raises(ValueError):
        build_inputs(hw(), risk_appetite="aggressive")   # the formula dictionary only has the conservative/balanced tiers
    with pytest.raises(ValueError):
        build_inputs(hw(), disk_type="tape")
    with pytest.raises(ValueError):
        build_inputs(hw(), max_connections=0)
    with pytest.raises(ValueError):
        build_inputs(hw(total_ram_gb=None))              # memory probe failed → refuse to assemble
    with pytest.raises(ValueError):
        build_inputs(hw(os_type="sunos"))


def test_missing_input_rules_skipped_not_guessed():
    """Without any observations: rules depending on observations are skipped as missing input; base formulas still produce values."""
    knowledge = load_knowledge(KNOWLEDGE_PATH)
    inputs = build_inputs(hw())
    recs = {r.name: r for r in recommend_all(knowledge, inputs)}
    assert recs["work_mem"].primary is not None
    skipped = [r for r in recs["checkpoint_timeout"].results if r.status == "missing_input"]
    assert skipped and "wal_rate_mbps" in skipped[0].reason


# ------------------- hardware-aware clamping inputs (heavy-load verification fix) -------------------

def test_mem_available_and_disk_free_probes():
    """Available-memory parsing (injected /proc/meminfo text) and the disk-free statvfs probe."""
    assert _mem_available_gb("MemTotal: 8132520 kB\nMemAvailable: 2097152 kB\n") == 2.0
    assert _mem_available_gb("MemTotal: 8132520 kB\n") is None      # no MemAvailable line
    assert _mem_available_gb("") is None
    assert disk_free_gb("/tmp") > 0
    assert disk_free_gb("/nonexistent-path-xyz") is None            # statvfs failure → None


def test_build_inputs_injects_clamp_inputs_conditionally():
    """Injected when probed (for engine clamping); not injected when the probe is missing (clamping skipped accordingly, backward compatible)."""
    inputs = build_inputs(hw(mem_available_gb=1.88, disk_free_gb=852.0))
    assert inputs["mem_available_gb"] == 1.88
    assert inputs["disk_free_gb"] == 852.0
    inputs2 = build_inputs(hw())                                    # new fields default to None
    assert "mem_available_gb" not in inputs2
    assert "disk_free_gb" not in inputs2
