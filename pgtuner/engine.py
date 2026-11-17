"""Formula evaluation engine: a generic restricted evaluator with zero formula knowledge of its own.

Formulas, units, gates, and dependency ordering all come from the versioned data in
pgtuner/data/guc_knowledge.json; this module only statically registers four small tables
(UNITS / GATES / NOTES / two wave sets), each entry annotated with the JSON caveats
text it is based on. The JSON is a versioned data file; rules array indices are stable.
"""
from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field

from .knowledge import Guc, Knowledge, Rule

# ---- Convention functions injected into the eval namespace (formula_conventions, original text) ----
# "The evaluation namespace must provide min/max/abs/int/round/ceil/floor/ln(=math.log, single argument)/log(=math.log, optional second argument as base)"
FUNCS = {
    "min": min,
    "max": max,
    "abs": abs,
    "int": int,
    "round": round,
    "ceil": math.ceil,
    "floor": math.floor,
    "ln": lambda x: math.log(x),  # single argument
    "log": math.log,              # log(x) or log(x, base), e.g. min_parallel_table_scan_size[0]: log(table_mb/8, 3)
}

# ---- AST whitelist: only pure expression nodes are allowed (the 109 formulas actually use a subset) ----
_ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp,
    ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Dict, ast.List, ast.Tuple,
    ast.Set, ast.Subscript, ast.Slice, ast.operator, ast.unaryop, ast.boolop,
    ast.cmpop,
)


def eval_formula(formula: str, ns: dict):
    """Restricted evaluation: AST whitelist + identifier check first, then eval. A formula returning None = no output (handled by the caller)."""
    full = {**FUNCS, **ns}
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"公式语法错误: {formula!r}: {e}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"公式含不允许的节点 {type(node).__name__}: {formula!r}")
        if isinstance(node, ast.Name) and node.id not in full:
            raise ValueError(f"公式引用未注入的标识符 {node.id!r}: {formula!r}")
    return eval(formula, {"__builtins__": {}}, full)  # noqa: S307 restricted evaluator


# ---- Unit table: (guc_name, rules index) -> output unit ----
# Default is 'none' (dimensionless/enum/string). Only entries deviating from the default or memory-related are registered; comments quote the original caveats.
UNITS: dict[tuple[str, int], str] = {
    # -- kB (caveats: "output in kB") --
    ("work_mem", 0): "kB", ("work_mem", 1): "kB", ("work_mem", 2): "kB",
    ("work_mem", 3): "kB", ("work_mem", 4): "kB", ("work_mem", 5): "kB",
    ("work_mem", 6): "kB",
    ("shared_buffers", 0): "kB",   # "output in kB; official 25%..."
    ("shared_buffers", 1): "kB",   # "output in kB; AWS 30–35% range"
    ("effective_cache_size", 0): "kB", ("effective_cache_size", 1): "kB",
    ("effective_cache_size", 2): "kB",
    ("maintenance_work_mem", 0): "kB", ("maintenance_work_mem", 1): "kB",
    ("maintenance_work_mem", 2): "kB", ("maintenance_work_mem", 3): "kB",
    ("maintenance_work_mem", 4): "kB",
    ("wal_buffers", 0): "kB", ("wal_buffers", 1): "kB",
    ("autovacuum_work_mem", 0): "kB", ("autovacuum_work_mem", 1): "kB",
    ("temp_buffers", 0): "kB",
    ("min_parallel_index_scan_size", 0): "kB",
    ("min_parallel_table_scan_size", 1): "kB",
    ("io_combine_limit", 0): "kB",  # "output in kB; Unix typical cap 1MB, Windows 128kB"
    # -- pages_8k (8kB blocks/pages) --
    ("shared_buffers", 2): "pages_8k",  # "output as 8kB block count" (AWS RDS /32768)
    ("shared_buffers", 3): "pages_8k",  # "output as 8kB page count" (AWS Aurora /12038-50003)
    ("shared_buffers", 4): "pages_8k",  # "output as 8kB page count" (Azure 32768/16384)
    ("effective_cache_size", 3): "pages_8k",  # "output as 8kB page count" (Aurora, same formula)
    ("effective_cache_size", 4): "pages_8k",  # "output as 8kB page count" (Azure ×131072 − shared_buffers)
    # -- MB --
    ("max_wal_size", 0): "MB", ("max_wal_size", 1): "MB",
    ("max_wal_size", 2): "MB", ("max_wal_size", 3): "MB",   # all four: "output in MB"
    ("min_wal_size", 0): "MB",                             # "output in MB; pgconfig Mixed tier 2GB"
    # -- seconds --
    ("checkpoint_timeout", 0): "sec",  # "output in seconds; ÷MB/s already yields seconds, do not multiply by 60 again"
    ("autovacuum_naptime", 0): "sec",  # "output in seconds"
    # -- milliseconds --
    ("autovacuum_vacuum_cost_delay", 0): "ms",  # "output in ms"
    ("bgwriter_delay", 0): "ms",                # "output in ms"
    # -- microseconds --
    ("commit_delay", 0): "us",                  # "output in μs"
}


def to_kb(value, unit: str) -> float:
    """Convert everything to kB (both report display and dependency backfill use kB as the intermediary)."""
    if unit == "kB":
        return float(value)
    if unit == "pages_8k":
        return float(value) * 8          # 8kB page
    if unit == "MB":
        return float(value) * 1024
    raise ValueError(f"单位 {unit!r} 不能换算为 kB")


def to_mb(value, unit: str) -> float:
    return to_kb(value, unit) / 1024


# ---- Implicit gates (existing only as caveats text, statically encoded; functions return a skip-reason string, None = pass) ----
def _gate_checkpoint_completion_target(ns) -> str | None:
    # caveats: "PG13 must explicitly set 0.9 (PG14+ already defaults to 0.9); PGTune outputs it explicitly only for PG<14"
    if ns.get("pg_version", 0) >= 14:
        return "PG14+ 默认已为 0.9，无需显式输出（PGTune 仅对 PG<14 输出）"
    return None


def _gate_effective_io_concurrency(ns) -> str | None:
    # caveats: "PG<18 and os_type not linux (no posix_fadvise, e.g. macOS/Windows): no output (non-zero values are rejected)"
    if ns.get("os_type") != "linux" and ns.get("pg_version", 18) < 18:
        return f"os_type={ns.get('os_type')} 且 PG<18：无 posix_fadvise，非零值被拒绝，不输出"
    return None


def _gate_maintenance_io_concurrency(ns) -> str | None:
    # caveats: "output only for PG>=13"
    if ns.get("pg_version", 0) < 13:
        return "仅 PG>=13 输出"
    return None


GATES = {
    ("checkpoint_completion_target", 0): _gate_checkpoint_completion_target,
    ("effective_io_concurrency", 0): _gate_effective_io_concurrency,
    ("maintenance_io_concurrency", 0): _gate_maintenance_io_concurrency,
}

# The other two implicit gates are "dependency-based", statically guaranteed by the inputs declaration + missing-input skip mechanism:
#   shared_buffers[1]      inputs includes buffer_cache_hit_ratio (caveats: "increase if buffer cache hit ratio <0.9")
#   hash_mem_multiplier[0] inputs includes work_mem_mb (caveats: depends on the work_mem recommendation, see dependency backfill)
# Their observation-style notes are registered here (return a display hint, None = nothing to say):
NOTES = {
    ("shared_buffers", 1): lambda ns: (
        f"buffer_cache_hit_ratio={ns['buffer_cache_hit_ratio']}<0.9，AWS 建议在 30–35% 基础上上调"
        if ns.get("buffer_cache_hit_ratio", 1.0) < 0.9 else None),
    ("hash_mem_multiplier", 0): lambda ns: (
        "work_mem 已 ≥40MB：官方提示 2.0–8.0 或更高可能有收益"
        if ns.get("work_mem_mb", 0) >= 40 else None),
}


# ---- Dependency ordering (two passes plus a third wrap-up pass; based on chained variables appearing in rule.inputs) ----
# Pass 1: base GUCs (shared_buffers, max_parallel_workers, max_parallel_workers_per_gather,
#   max_wal_size, maintenance_work_mem, plus every GUC with no chained inputs)
# Backfill: shared_buffers_kb/_mb/_pages, max_parallel_workers, parallel_workers_per_gather,
#   max_wal_size_mb, maintenance_work_mem_kb
# Pass 2 (WAVE2):
#   work_mem[0] inputs includes shared_buffers_kb+max_parallel_workers (caveats: "must subtract shared_buffers")
#   wal_buffers[0][1] includes shared_buffers_kb; effective_cache_size[4] includes shared_buffers_pages
#   autovacuum_work_mem[0] includes maintenance_work_mem_kb; checkpoint_timeout[0] includes max_wal_size_mb
# Backfill: work_mem_kb/_mb
# Pass 3 (WAVE3): hash_mem_multiplier[0] includes work_mem_mb; max_connections[6] includes work_mem_mb+shared_buffers_mb
#   (max_connections[0] only needs the pass-1 backfill, but the GUC as a whole is evaluated in pass 3; ns grows monotonically, no side effects)
WAVE2 = {"work_mem", "wal_buffers", "effective_cache_size", "autovacuum_work_mem", "checkpoint_timeout"}
WAVE3 = {"hash_mem_multiplier", "max_connections"}


@dataclass
class RuleResult:
    guc: str
    index: int
    formula: str
    origin: str
    caveats: str
    unit: str
    value: object = None
    status: str = "ok"      # ok | missing_input | gated | none_result | error | invalid_enum | unknown_guc (the last two are marked by the check layer's post-validation)
    reason: str = ""        # skip/error reason
    note: str = ""          # gate observation-style note (produced by NOTES)


@dataclass
class Recommendation:
    name: str
    description: str
    default_value: str
    context: str
    restart_required: bool
    restart_required_note: str | None
    pain_level: str | None
    why_hard: str
    version_notes: str
    sources: list
    results: list[RuleResult] = field(default_factory=list)

    @property
    def primary(self) -> RuleResult | None:
        """Primary selection: JSON rule order is the priority (mainstream sources are curated first, e.g. work_mem[0]=PGTune);
        the first rule with status=='ok' wins; when skipped by a gate or missing input, the next in line takes over."""
        return next((r for r in self.results if r.status == "ok"), None)

    @property
    def alternatives(self) -> list[RuleResult]:
        p = self.primary
        return [r for r in self.results if r.status == "ok" and r is not p]


def _eval_rule(guc: Guc, idx: int, rule: Rule, ns: dict) -> RuleResult:
    rr = RuleResult(
        guc=guc.name, index=idx, formula=rule.formula, origin=rule.origin,
        caveats=rule.caveats, unit=UNITS.get((guc.name, idx), "none"),
    )
    missing = [v for v in rule.inputs if v not in ns]
    if missing:  # a rule with missing inputs is skipped with the reason recorded; never guess values
        rr.status = "missing_input"
        rr.reason = f"缺 {', '.join(missing)} 输入"
        return rr
    gate = GATES.get((guc.name, idx))
    if gate is not None:
        why = gate(ns)
        if why:
            rr.status = "gated"
            rr.reason = why
            return rr
    try:
        value = eval_formula(rule.formula, ns)
    except Exception as e:  # formula exceptions are recorded per rule, without aborting the whole run
        rr.status = "error"
        rr.reason = f"{type(e).__name__}: {e}"
        return rr
    if value is None:  # formula convention: None = keep default / no output
        rr.status = "none_result"
        rr.reason = "公式返回 None（保持默认/不输出）"
        return rr
    rr.value = value
    note = NOTES.get((guc.name, idx))
    if note is not None:
        rr.note = note(ns) or ""
    return rr


def _backfill(ns: dict, recs: dict[str, Recommendation]) -> None:
    """Convert the selected primary recommendation values into variants per the unit table and backfill ns for downstream formulas to reference."""
    sb = recs["shared_buffers"].primary
    if sb is not None:
        kb = to_kb(sb.value, sb.unit)
        ns["shared_buffers_kb"] = int(kb)
        ns["shared_buffers_mb"] = kb / 1024
        ns["shared_buffers_pages"] = int(kb // 8)
    for guc, var in (("max_parallel_workers", "max_parallel_workers"),
                     ("max_parallel_workers_per_gather", "parallel_workers_per_gather")):
        p = recs[guc].primary
        if p is not None:
            ns[var] = p.value
    mws = recs["max_wal_size"].primary
    if mws is not None:
        ns["max_wal_size_mb"] = to_mb(mws.value, mws.unit)
    mwm = recs["maintenance_work_mem"].primary
    if mwm is not None:
        ns["maintenance_work_mem_kb"] = int(to_kb(mwm.value, mwm.unit))
    wm = recs["work_mem"].primary
    if wm is not None:
        kb = to_kb(wm.value, wm.unit)
        ns["work_mem_kb"] = int(kb)
        ns["work_mem_mb"] = kb / 1024


def _reconcile_shared_buffers(ns: dict, recs: dict[str, Recommendation]) -> None:
    """Clamp to available memory (heavy-load validation fix): shared_buffers formulas compute a ratio of
    total_ram_gb (25% etc.) and are unaware of runtime MemAvailable — on memory-constrained hosts
    (WSL2 with dev tools resident) this can recommend a buffer pool larger than the actually available
    memory; once the dirty pool fills up, kernel writeback throttling causes a system-wide cliff
    (heavy-htap validation note §五.2, internal historical: 7.76GB total×25%=1.94GB vs MemAvailable
    of only 1.9GB, 34s cliff). When the landed value exceeds 60% of MemAvailable, clamp it to that
    threshold; skip when mem_available_gb cannot be probed (backward compatible).
    Timing: within the evaluation waves, after shared_buffers lands and before the downstream backfill
    (formulas using shared_buffers_kb such as work_mem) — ensuring downstream consumers get the clamped value."""
    avail = ns.get("mem_available_gb")
    sb = recs["shared_buffers"].primary
    if avail is None or sb is None:
        return
    cap_kb = int(avail * 1048576 * 0.6)
    kb = to_kb(sb.value, sb.unit)
    if kb <= cap_kb:
        return
    orig_kb = int(kb)
    sb.value = cap_kb if sb.unit == "kB" else int(cap_kb // 8)   # clamp in the original output unit
    sb.note = (f"可用内存钳制：原 {orig_kb}kB（{orig_kb / 1048576:.2f}GB）超过 MemAvailable 的 "
               f"60%（MemAvailable={avail:g}GB），机器实际可用内存有限")


def _reconcile_wal_sizes(ns: dict, recs: dict[str, Recommendation]) -> None:
    """WAL size post-processing (two ordered steps):
    1. Disk free-space clamp (heavy-load validation fix): the max_wal_size formula only looks at
       wal_rate×checkpoint_timeout with no disk upper bound (153GB was once derived in practice),
       risking blowing up pg_wal in production. When the output exceeds 25% of the data disk's free
       space, clamp it to that value; skip when disk_free_gb cannot be probed (backward compatible).
    2. min<=max consistency (2026-11 application validation fix): min_wal_size and max_wal_size are
       evaluated by two independent formulas unaware of each other (min uses the PGTune fixed tier,
       max uses wal_rate/tiers), which can output the contradictory combination min >= max
       (PG does not reject it and silently retains WAL per min). If both have output and
       min_wal_size >= max_wal_size, adjust min_wal_size to max(80MB, max_wal_size // 2)
       (PG's default lower bound is 80MB, aligning with the official convention that
       "min is usually a small fraction of max") and note the original contradictory value.
       Clamp max first, then enforce min consistency, so min is compared against the clamped max."""
    mx = recs["max_wal_size"].primary
    if mx is not None:
        free_gb = ns.get("disk_free_gb")
        max_mb = to_mb(mx.value, mx.unit)
        if free_gb is not None and max_mb > free_gb * 1024 * 0.25:
            cap_mb = int(free_gb * 1024 * 0.25)
            mx.value = cap_mb
            mx.unit = "MB"
            mx.note = (f"磁盘余量钳制：原 {max_mb:g}MB 超过数据盘剩余空间的 25%"
                       f"（剩余 {free_gb:g}GB），已钳到 {cap_mb}MB")
    mn, mx = recs["min_wal_size"].primary, recs["max_wal_size"].primary
    if mn is None or mx is None:
        return
    min_mb, max_mb = to_mb(mn.value, mn.unit), to_mb(mx.value, mx.unit)
    if min_mb < max_mb:
        return
    adjusted = max(80, int(max_mb // 2))
    mn.value = adjusted
    mn.unit = "MB"
    mn.note = (f"一致性调整：原 {min_mb:g}MB 与 max_wal_size={max_mb:g}MB 矛盾，"
               f"按 max/2（下限 80MB）调整为 {adjusted}MB")


def recommend_all(knowledge: Knowledge, inputs: dict) -> list[Recommendation]:
    """Evaluate all GUCs. inputs is a flat variable-name dict (see inputs.build_inputs).
    Note: the user-set value of max_connections is the authoritative input and is never overridden
    by the recommendation for the max_connections GUC (its rules[6] memory-guard check uses exactly
    the user-set value)."""
    ns = dict(inputs)
    recs = {g.name: Recommendation(
        name=g.name, description=g.description, default_value=g.default_value,
        context=g.context, restart_required=g.restart_required,
        restart_required_note=g.restart_required_note, pain_level=g.pain_level,
        why_hard=g.why_hard, version_notes=g.version_notes, sources=g.sources,
    ) for g in knowledge.gucs}

    def run_wave(gucs: list[Guc]) -> None:
        for g in gucs:
            recs[g.name].results = [
                _eval_rule(g, i, r, ns) for i, r in enumerate(g.rules)
            ]

    wave1 = [g for g in knowledge.gucs if g.name not in WAVE2 | WAVE3]
    run_wave(wave1)
    _reconcile_shared_buffers(ns, recs)   # clamp shared_buffers first, then backfill variants (downstream work_mem etc. use the clamped value)
    _backfill(ns, recs)
    run_wave([g for g in knowledge.gucs if g.name in WAVE2])
    _backfill(ns, recs)
    run_wave([g for g in knowledge.gucs if g.name in WAVE3])
    _reconcile_wal_sizes(ns, recs)
    # output keeps the knowledge base's original order (curation such as work_mem first is the display order)
    return [recs[g.name] for g in knowledge.gucs]
