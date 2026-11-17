"""Check-layer signal collection: read-only SQL + /proc sampling → knowledge-base observation variable mapping.

Variable names match formula_conventions / each rule.inputs of pgtuner/data/guc_knowledge.json;
variables that cannot be collected are not injected (the engine skips the corresponding
rules as usual; values are never guessed).

Mapping basis (§5 "acquisition paths for non-hardware inputs" of the historical internal
knowledge-base research report):
  - pg_settings            all current GUC values (diff baseline) + the context column
                           (for the bench layer's injection filtering)
  - pg_database_size       → db_size_gb
  - pg_stat_database       blks_hit/(blks_hit+blks_read) → buffer_cache_hit_ratio (not injected
                           when the denominator is 0); temp_files/temp_bytes → work_mem pressure
                           signals (no corresponding formula input in the knowledge base, noted only)
  - pg_stat_user_tables    sum(n_live_tup)→n_live_tup, max→biggest_table_live_tuples,
                           sum(n_dead_tup) as an autovacuum advisory note
  - target_dead_tuples     documented heuristic = biggest_table_live_tuples × 0.01: tightens the
                           trigger point to 1%, aligned with the direction of Percona's advice for
                           large tables (the tolerable dead-tuple count is inherently a business
                           questionnaire item; the default tightens it this way)
  - wal_rate_mbps          differential of two pg_current_wal_lsn() samples (≈0 with no write
                           workload, injected as-is)
  - os_free_and_cached_gb / freeable_mem_kb  /proc/meminfo (MemAvailable+Cached; NoCommit,
                           committed not counted — matching the Percona free+cached and
                           pganalyze freeable conventions)
  - cpu_saturated          /proc/loadavg 1min > cpu_cores × 0.8
Variables whose values must not be guessed (concurrent_commits, tolerable_loss_ms and other
questionnaire items) stay missing.

pg_version / max_connections / db_size_gb are named parameters of build_inputs and are not put
into observations (the runtime value takes precedence over the user default and is passed in as
a parameter by the cli layer; this way observations contains only pure observation variables,
keeping the diff clean).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import pg
from .hardware import disk_free_gb as _disk_free_gb

GB_KB = 1048576          # 1GB = 1048576 kB (consistent with the knowledge-base convention)
WAL_SAMPLE_SECONDS = 6.0   # a 3s short window under mixed load easily lands exactly on an analytic
                          # blocking segment (wal_rate=0 distortion; heavy-load validation improvement item 2)


# ---------------------------- /proc sampling (pure functions, easy to test) ----------------------------

def parse_meminfo(text: str) -> dict[str, int]:
    """Parse /proc/meminfo text → {metric name: kB value}."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            out[parts[0].rstrip(":")] = int(parts[1])
    return out


def meminfo_values(mi: dict[str, int]) -> tuple[float | None, int | None]:
    """(os_free_and_cached_gb, freeable_mem_kb). Not injected if either MemAvailable or Cached is missing."""
    avail, cached = mi.get("MemAvailable"), mi.get("Cached")
    if avail is None or cached is None:
        return None, None
    return round((avail + cached) / GB_KB, 2), avail + cached


def parse_loadavg(text: str) -> float | None:
    """The 1min load average (first field)."""
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return None


def cpu_is_saturated(load1: float | None, cpu_cores: int) -> bool | None:
    """loadavg 1min > cpu_cores × 0.8 → True (not injected when there is no data)."""
    if load1 is None:
        return None
    return load1 > cpu_cores * 0.8


def lsn_to_int(lsn: str) -> int:
    """'0/15D2648' → byte offset (high 32 bits << 32 | low 32 bits)."""
    hi, lo = lsn.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


def collect_os(cpu_cores: int, meminfo_text: str | None = None,
               loadavg_text: str | None = None) -> dict:
    """OS observations: os_free_and_cached_gb / freeable_mem_kb / cpu_saturated (+ loadavg_1m for the dashboard)."""
    def _read(path: str, text: str | None) -> str:
        if text is not None:
            return text
        try:
            with open(path, encoding="ascii") as f:
                return f.read()
        except OSError:
            return ""

    gb, kb = meminfo_values(parse_meminfo(_read("/proc/meminfo", meminfo_text)))
    load1 = parse_loadavg(_read("/proc/loadavg", loadavg_text))
    return {"os_free_and_cached_gb": gb, "freeable_mem_kb": kb,
            "loadavg_1m": load1, "cpu_saturated": cpu_is_saturated(load1, cpu_cores)}


# ---------------------------- Live database observation ----------------------------

@dataclass
class Signals:
    settings: dict = field(default_factory=dict)      # pg_settings: name -> {setting, unit, context}
    enumvals: dict = field(default_factory=dict)      # pg_settings: name -> list of legal enum values (rows with non-empty enumvals)
    observations: dict = field(default_factory=dict)  # pure observation variables injectable into the knowledge base (no key when not collectable)
    # —— below are for the dashboard/notes (not fed into formulas directly) ——
    db_size_gb: float | None = None
    cache_hit_ratio: float | None = None
    temp_files: int | None = None
    temp_bytes: int | None = None
    n_live_tup: int | None = None
    n_dead_tup: int | None = None
    biggest_table_live_tuples: int | None = None
    wal_rate_mbps: float | None = None
    wal_sample_seconds: float = WAL_SAMPLE_SECONDS
    os_free_and_cached_gb: float | None = None
    freeable_mem_kb: int | None = None
    loadavg_1m: float | None = None
    cpu_saturated: bool | None = None
    notes: list = field(default_factory=list)

    @property
    def pg_major(self) -> int | None:
        """server_version_num → major version number (200000 → 20)."""
        s = self.settings.get("server_version_num", {}).get("setting")
        return int(s) // 10000 if s and s.isdigit() else None

    @property
    def runtime_max_connections(self) -> int | None:
        """The runtime value of max_connections (backfilled in preference to the user default of 100)."""
        s = self.settings.get("max_connections", {}).get("setting")
        return int(s) if s and s.isdigit() else None

    @property
    def debug_build(self) -> bool:
        return self.settings.get("debug_assertions", {}).get("setting") == "on"


def sample_wal_rate(conn, seconds: float = WAL_SAMPLE_SECONDS) -> float | None:
    """Differential of two pg_current_wal_lsn samples → MB/s. Returns None on insufficient privileges / standby and similar errors."""
    try:
        first = pg.fetch_one(conn, "SELECT pg_current_wal_lsn()")[0]
        t0 = time.perf_counter()
        if seconds > 0:
            time.sleep(seconds)
        second = pg.fetch_one(conn, "SELECT pg_current_wal_lsn()")[0]
        elapsed = time.perf_counter() - t0
    except Exception:   # psycopg2.Error and others; signal collection is not interrupted by a single failure
        return None
    if elapsed <= 0:
        elapsed = 1e-6
    return (lsn_to_int(second) - lsn_to_int(first)) / elapsed / (1024 * 1024)


def collect(conn, cpu_cores: int, wal_sample_seconds: float = WAL_SAMPLE_SECONDS,
            meminfo_text: str | None = None, loadavg_text: str | None = None) -> Signals:
    """Collect all observations from the live database + /proc (read-only SQL). The *_text parameters let tests inject /proc contents."""
    sig = Signals(wal_sample_seconds=wal_sample_seconds)

    # 1) full pg_settings: current GUC values (diff baseline) + context (bench injection
    #    filtering) + the source for runtime-value backfill
    for name, setting, unit, context in pg.fetch_all(
            conn, "SELECT name, setting, unit, context FROM pg_settings"):
        sig.settings[name] = {"setting": setting, "unit": unit, "context": context}

    # 1a) data disk free space (heavy-load validation fix): statvfs measured on the disk
    #     holding the live database's data_directory, taking precedence over the
    #     hardware.probe value for the default path (/tmp) (overrides ns via observations)
    data_dir = sig.settings.get("data_directory", {}).get("setting")
    if data_dir:
        gb = _disk_free_gb(data_dir)
        if gb is not None:
            sig.observations["disk_free_gb"] = gb

    # 1b) legal values of enum-type GUCs (for post-validation: a recommended string value
    #     must be in this list, otherwise the target build hits FATAL)
    for name, enumvals in pg.fetch_all(
            conn, "SELECT name, enumvals FROM pg_settings WHERE enumvals IS NOT NULL"):
        sig.enumvals[name] = list(enumvals or [])

    # 2) database size → db_size_gb
    size_b = pg.fetch_one(conn, "SELECT pg_database_size(current_database())")[0]
    sig.db_size_gb = round(size_b / GB_KB / 1024, 2)

    # 3) pg_stat_database: hit ratio + temp spill signals
    hit, read, temp_files, temp_bytes = pg.fetch_one(
        conn, "SELECT COALESCE(blks_hit,0), COALESCE(blks_read,0), "
              "COALESCE(temp_files,0), COALESCE(temp_bytes,0) "
              "FROM pg_stat_database WHERE datname = current_database()")
    if hit + read > 0:
        sig.cache_hit_ratio = round(hit / (hit + read), 4)
        sig.observations["buffer_cache_hit_ratio"] = sig.cache_hit_ratio
    sig.temp_files, sig.temp_bytes = temp_files, temp_bytes
    if temp_files:
        sig.notes.append(
            f"temp_files={temp_files} 个 / {temp_bytes / GB_KB:.1f}MB（统计累计值）：sort/hash 溢盘信号，"
            f"可考虑增大 work_mem；知识库无对应公式输入，仅作注记")

    # 4) pg_stat_user_tables: row-count observations → autovacuum-family formulas
    live, biggest, dead = pg.fetch_one(
        conn, "SELECT COALESCE(sum(n_live_tup),0), COALESCE(max(n_live_tup),0), "
              "COALESCE(sum(n_dead_tup),0) FROM pg_stat_user_tables")
    sig.n_live_tup, sig.biggest_table_live_tuples, sig.n_dead_tup = live, biggest, dead
    if live > 0:
        sig.observations["n_live_tup"] = int(live)
    if biggest > 0:
        sig.observations["biggest_table_live_tuples"] = int(biggest)
        # documented heuristic: target_dead_tuples = 1% of the biggest table (tightens the
        # trigger point to 1%, aligned with Percona's advice direction for large tables)
        sig.observations["target_dead_tuples"] = max(1, int(biggest * 0.01))

    # 5) WAL rate: differential of two samples (≈0 with no write workload, injected as-is)
    rate = sample_wal_rate(conn, wal_sample_seconds)
    if rate is not None:
        sig.wal_rate_mbps = round(rate, 2)
        sig.observations["wal_rate_mbps"] = sig.wal_rate_mbps

    # 6) OS: /proc/meminfo + /proc/loadavg
    os_obs = collect_os(cpu_cores, meminfo_text=meminfo_text, loadavg_text=loadavg_text)
    sig.os_free_and_cached_gb = os_obs["os_free_and_cached_gb"]
    sig.freeable_mem_kb = os_obs["freeable_mem_kb"]
    sig.loadavg_1m = os_obs["loadavg_1m"]
    sig.cpu_saturated = os_obs["cpu_saturated"]
    for key in ("os_free_and_cached_gb", "freeable_mem_kb", "cpu_saturated"):
        if os_obs[key] is not None:
            sig.observations[key] = os_obs[key]

    return sig


# ---------------------------- Check-layer post-validation (2026-11 application validation fix) ----------------------------

def validate_against_instance(recs, sig: Signals) -> None:
    """Mark recommendation results in place against the live database's pg_settings
    (check-layer only; the offline recommend mode has no live database and skips this).

    Background: end-to-end application validation exposed two classes of startup FATAL —
    io_workers, deleted in PG20 (unrecognized configuration parameter), and
    wal_compression='lz4' on builds not compiled with lz4 (invalid value).
    Rules:
    1. A recommended GUC not found in pg_settings → all its ok results are marked
       unknown_guc (kept out of conf); even if the knowledge base's version gate missed
       it (e.g. a parameter already deleted in 20devel), this layer catches it.
    2. A string value not in that GUC's enumvals → marked invalid_enum; legal
       alternatives are skipped due to status!=ok and the next in line is promoted
       (engine primary selection is simply "first ok"); when no legal alternative
       exists, fall back to the current pg_settings value (note says "keep current
       value") — the runtime value is itself always legal.
    """
    from .engine import RuleResult   # local import to avoid a module-level circular dependency
    for rec in recs:
        if rec.primary is None:
            continue
        if rec.name not in sig.settings:
            for r in rec.results:
                if r.status == "ok":
                    r.status = "unknown_guc"
                    r.reason = "目标实例 pg_settings 查无此参数（该版本已删或改名），不输出"
            continue
        allowed = sig.enumvals.get(rec.name)
        if not allowed:
            continue
        for r in rec.results:
            if r.status == "ok" and isinstance(r.value, str) and r.value not in allowed:
                r.status = "invalid_enum"
                r.reason = f"目标构建不支持 {r.value}，枚举值：{'/'.join(allowed)}"
        if rec.primary is None:   # all ok results illegal: fall back to the current runtime value and note it
            current = sig.settings[rec.name]["setting"]
            rec.results.append(RuleResult(
                guc=rec.name, index=len(rec.results), formula="pg_settings 当前值",
                origin="check 后置校验回退", caveats="", unit="none",
                value=current, status="ok",
                note=f"维持现值：推荐枚举值目标构建均不支持（合法值 {'/'.join(allowed)}）"))


# ---------------------------- Display/conversion helpers ----------------------------

def _fmt_kb(kb: float) -> str:
    if kb < 1024:
        return f"{kb:g}kB"
    if kb < 1024 * 1024:
        return f"{kb / 1024:g}MB"
    return f"{kb / 1024 / 1024:g}GB"


def display_value(setting: str, unit: str | None) -> str:
    """pg_settings.setting (internal unit) → human readable (for the current-value column display)."""
    try:
        v = float(setting)
    except (TypeError, ValueError):
        return str(setting)          # on/off, enums, and strings as-is
    if unit == "8kB":
        return _fmt_kb(v * 8)
    if unit == "kB":
        return _fmt_kb(v)
    if unit == "MB":
        return _fmt_kb(v * 1024)
    if unit in ("us", "ms", "s", "min"):
        return f"{v:g}{unit}"
    return str(setting)


def setting_kb(settings: dict, name: str) -> int | None:
    """Convert a pg_settings value to kB (used by the bench layer for the work_mem base value; returns None for non-memory units)."""
    s = settings.get(name)
    if not s:
        return None
    try:
        v = float(s["setting"])
    except (TypeError, ValueError):
        return None
    if s["unit"] == "8kB":
        return int(v * 8)
    if s["unit"] == "kB":
        return int(v)
    if s["unit"] == "MB":
        return int(v * 1024)
    return None
