"""Input assembly: probe values + user parameters + database observations → a flat
variable-name dictionary.

Three sources:
  1. Probe values: HardwareInfo from hardware.probe()
  2. User parameters (with defaults): workload_type=mixed, max_connections=100,
     db_size_gb=defaults to total_ram_gb, risk_appetite=balanced; --disk-type
     overrides the probe
  3. Database observations: an empty dict at this layer; the db_observations
     parameter is left for the later check layer to inject (e.g. wal_rate_mbps,
     buffer_cache_hit_ratio, n_live_tup; variable names match the knowledge base inputs)
"""
from __future__ import annotations

from .hardware import HardwareInfo

WORKLOAD_TYPES = ("web", "oltp", "dw", "mixed", "desktop")
DISK_TYPES = ("hdd", "ssd", "nvme", "cloud")
OS_TYPES = ("linux", "windows", "mac")
RISK_APPETITES = ("conservative", "balanced")   # the random_page_cost[3] formula dict only has these two levels
PG_VERSION_CAP = 18   # upper bound of the knowledge base pg_versions="13-18"; defaults to the cap when probing fails


def is_extrapolated(inputs: dict) -> bool:
    """pg_version exceeds the knowledge base cap (19+): formulas still evaluate against the newest branch, but the report must carry an "extrapolated" marker."""
    return inputs.get("pg_version", PG_VERSION_CAP) > PG_VERSION_CAP


def build_inputs(
    hw: HardwareInfo,
    *,
    workload_type: str = "mixed",
    max_connections: int = 100,
    db_size_gb: float | None = None,
    risk_appetite: str = "balanced",
    pg_version: int | None = None,
    disk_type: str | None = None,
    db_observations: dict | None = None,
) -> dict:
    """Assemble the variable dictionary for the evaluation namespace. Whatever is missing stays missing — rules lacking an input are skipped by the engine with the reason recorded."""
    if workload_type not in WORKLOAD_TYPES:
        raise ValueError(f"workload_type 必须是 {WORKLOAD_TYPES} 之一: {workload_type!r}")
    if risk_appetite not in RISK_APPETITES:
        raise ValueError(f"risk_appetite 必须是 {RISK_APPETITES} 之一: {risk_appetite!r}")
    if hw.os_type not in OS_TYPES:
        raise ValueError(f"os_type 必须是 {OS_TYPES} 之一: {hw.os_type!r}")
    if max_connections < 1:
        raise ValueError(f"max_connections 必须 >= 1: {max_connections}")
    if hw.total_ram_gb is None:
        raise ValueError("内存探测失败（/proc/meminfo 不可用），无法装配内存相关输入")

    eff_disk = disk_type or hw.disk_type   # --disk-type overrides the probe
    if eff_disk not in DISK_TYPES:
        raise ValueError(f"disk_type 必须是 {DISK_TYPES} 之一: {eff_disk!r}")

    inputs: dict = {
        "total_ram_gb": hw.total_ram_gb,
        "cpu_cores": hw.cpu_cores,
        "os_type": hw.os_type,
        "disk_type": eff_disk,
        "workload_type": workload_type,
        "max_connections": max_connections,
        "db_size_gb": float(db_size_gb) if db_size_gb is not None else hw.total_ram_gb,
        "risk_appetite": risk_appetite,
        # pg_version: defaults to the knowledge base cap 18 when probing fails, overridable via --pg-version; 19+ is flagged by is_extrapolated
        "pg_version": int(pg_version) if pg_version is not None else PG_VERSION_CAP,
    }
    # Hardware-aware clamping inputs (heavy-load verification fix): not injected when probing fails, and the corresponding clamp is skipped (backward compatible)
    if hw.mem_available_gb is not None:
        inputs["mem_available_gb"] = hw.mem_available_gb
    if hw.disk_free_gb is not None:
        inputs["disk_free_gb"] = hw.disk_free_gb
    if db_observations:
        inputs.update(db_observations)
    return inputs


__all__ = ["build_inputs", "is_extrapolated", "WORKLOAD_TYPES", "DISK_TYPES",
           "OS_TYPES", "RISK_APPETITES", "PG_VERSION_CAP", "HardwareInfo"]
