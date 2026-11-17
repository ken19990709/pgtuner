"""Bench layer: the pgbench candidate loop — candidate-set generation → PGOPTIONS-injected
execution → TPS/latency parsing → comparison.

The server configuration is never modified: PGOPTIONS affects only this pgbench session
(following the injection mode of scripts/hw_perf/run.sh).
Injection is limited to parameters with pg_settings.context='user' (runtime SET-able);
sighup/postmaster ones are skipped with the reason recorded (per the live database's
pg_settings.context, not the knowledge base's context field).

Candidate sets (--candidates):
  simple (default)  baseline + recommended + work_mem×{0.5,2} (single-variable perturbation,
                    isolating a single GUC's effect)
  work_mem/rpc/parallel/all  stack the corresponding family variants on top of the
    recommended combination (fine-tuning of the recommended combo):
    work_mem×{0.5,2} (multiples of the recommended value), random_page_cost∈{1.1,4.0}
    (SSD low end / PG default), max_parallel_workers_per_gather∈{0,2,4}
    (parallelism off / PG default / aggressive)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .engine import to_kb
from .report import conf_value
from .signals import setting_kb

CANDIDATE_SETS = ("simple", "work_mem", "rpc", "parallel", "all")
WORK_MEM_FACTORS = (0.5, 2.0)
RPC_VALUES = (1.1, 4.0)
PARALLEL_VALUES = (0, 2, 4)


def resolve_pgbench(explicit: str | None = None, bindir: str | None = None) -> str | None:
    """Resolve the pgbench path (machine binding removed, tool-w3; in v0.9.0 gathered from
    cli._fallback_pgbench into this module, the old machine-hardcoded DEFAULT_PGBENCH
    default deleted): an explicit path is respected as-is (existence/executability checking
    is the caller's job); the default tries, in order, the instance bindir (version-matched,
    preferred; both <bindir>/pgbench and <bindir>/bin/pgbench forms) > PATH lookup
    (shutil.which). All miss → None; the caller reports repair guidance
    (scripts/setup.sh --pg / --pgbench)."""
    if explicit:
        return explicit
    if bindir:
        for cand in (Path(bindir) / "pgbench", Path(bindir) / "bin" / "pgbench"):
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
    return shutil.which("pgbench")


@dataclass
class Candidate:
    name: str
    gucs: dict = field(default_factory=dict)     # GUC -> conf literal (context=user only)
    skipped: list = field(default_factory=list)  # [(guc, context)] recommended but not runtime SET-able


@dataclass
class BenchResult:
    candidate: Candidate
    tps: float | None = None
    latency_ms: float | None = None
    error: str | None = None


def recommended_injection(recs: list, contexts: dict[str, str]) -> tuple[dict, list]:
    """recommend output → injectable GUC set. Only parameters with live context='user' are
    kept; the rest go into skipped.

    conf_value's single quotes are postgresql.conf syntax; PGOPTIONS -c passes the value
    as-is (booleans/enums reject quotes), so they are stripped."""
    gucs: dict = {}
    skipped: list = []
    for rec in recs:
        p = rec.primary
        if p is None:
            continue
        ctx = contexts.get(rec.name)
        if ctx == "user":
            gucs[rec.name] = conf_value(p.value, p.unit).strip("'")
        else:
            skipped.append((rec.name, ctx or "未知"))
    return gucs, skipped


def _work_mem_base_kb(recs: list, settings: dict) -> int | None:
    """Base value for work_mem multiples: the recommended value first, falling back to the current runtime value."""
    for rec in recs:
        if rec.name == "work_mem" and rec.primary is not None:
            return int(to_kb(rec.primary.value, rec.primary.unit))
    return setting_kb(settings, "work_mem")


def _variant(cands: list, name: str, rec_gucs: dict, guc: str, literal: str) -> None:
    cands.append(Candidate(name, {**rec_gucs, guc: literal}))


def build_candidates(recs: list, settings: dict, choice: str = "simple") -> list[Candidate]:
    """Candidate sets (see module docstring). Duplicate injection combos are deduplicated (e.g. rec+rpc=1.1 when the recommendation already has rpc=1.1)."""
    if choice not in CANDIDATE_SETS:
        raise ValueError(f"candidates 必须是 {CANDIDATE_SETS} 之一: {choice!r}")
    contexts = {n: s["context"] for n, s in settings.items()}
    rec_gucs, skipped = recommended_injection(recs, contexts)
    base_kb = _work_mem_base_kb(recs, settings)
    cands = [Candidate("baseline"), Candidate("recommended", dict(rec_gucs), list(skipped))]

    if choice == "simple":
        # single-variable perturbation: touch only work_mem, keep the rest at current server settings
        for f in WORK_MEM_FACTORS:
            if base_kb:
                cands.append(Candidate(f"work_mem×{f:g}",
                                       {"work_mem": conf_value(int(base_kb * f), "kB")}))
    else:
        # fine-tuning variants stacked on the recommended combination
        if choice in ("work_mem", "all"):
            for f in WORK_MEM_FACTORS:
                if base_kb:
                    _variant(cands, f"rec+work_mem×{f:g}", rec_gucs, "work_mem",
                             conf_value(int(base_kb * f), "kB"))
        if choice in ("rpc", "all"):
            for v in RPC_VALUES:
                _variant(cands, f"rec+random_page_cost={v:g}", rec_gucs, "random_page_cost", f"{v:g}")
        if choice in ("parallel", "all"):
            for v in PARALLEL_VALUES:
                _variant(cands, f"rec+max_parallel_workers_per_gather={v}", rec_gucs,
                         "max_parallel_workers_per_gather", str(v))

    seen: list[dict] = []
    unique = []
    for c in cands:
        if c.gucs in seen:
            continue
        seen.append(c.gucs)
        unique.append(c)
    return unique


# ---------------------------- Execution & parsing ----------------------------

def build_pgoptions(gucs: dict | None) -> str:
    """Injection dict → PGOPTIONS string. An empty set returns '' (clears leftover PGOPTIONS in the environment, keeping the baseline clean)."""
    return " ".join(f"-c {k}={v}" for k, v in (gucs or {}).items())


def run_pgbench(pgbench_path: str, host: str, port: str, dbname: str, *,
                duration: int = 30, clients: int = 4, jobs: int = 4,
                gucs: dict | None = None) -> subprocess.CompletedProcess:
    """Run a single candidate (following the scripts/hw_perf/run.sh mode: -n -M prepared -T -c -j)."""
    cmd = [pgbench_path, "-h", host, "-p", str(port), "-d", dbname,
           "-n", "-M", "prepared", "-T", str(duration), "-c", str(clients), "-j", str(jobs)]
    env = {**os.environ, "PGOPTIONS": build_pgoptions(gucs)}
    return subprocess.run(cmd, env=env, capture_output=True, text=True,
                          timeout=duration * 2 + 120)


def run_init(pgbench_path: str, host: str, port: str, dbname: str, scale: int = 10
             ) -> subprocess.CompletedProcess:
    """pgbench -i -s <scale> initialization (--init)."""
    cmd = [pgbench_path, "-h", host, "-p", str(port), "-d", dbname, "-i", "-s", str(scale)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=1800)


_TPS_RE = re.compile(r"tps = ([0-9]+(?:\.[0-9]+)?) \(([^)]*)\)")
_LATENCY_RE = re.compile(r"latency average = ([0-9]+(?:\.[0-9]+)?)")


def parse_pgbench_output(text: str) -> dict:
    """Parse tps and latency average from pgbench stdout.

    Two formats are accepted: the old two-line `tps = X (including connections
    establishing)` / `tps = X (excluding connections establishing)` (the excluding line
    is taken); and the new single-line `tps = X (without initial connection time)`
    (same metric).
    """
    tps = None
    matches = _TPS_RE.findall(text)
    if matches:
        preferred = [m for m in matches if "excluding" in m[1] or "without initial" in m[1]]
        tps = float((preferred or matches)[0][0])
    m = _LATENCY_RE.search(text)
    latency_ms = float(m.group(1)) if m else None
    return {"tps": tps, "latency_ms": latency_ms}


def run_candidates(candidates: list[Candidate], *, pgbench_path: str, host: str, port: str,
                   dbname: str, duration: int, clients: int, jobs: int = 4,
                   on_result=None) -> list[BenchResult]:
    """Execute and parse candidates sequentially. A failed candidate records an error summary without interrupting the ones after it. on_result(result) drives progress display."""
    results = []
    for cand in candidates:
        proc = run_pgbench(pgbench_path, host, port, dbname, duration=duration,
                           clients=clients, jobs=jobs, gucs=cand.gucs)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            result = BenchResult(cand, error=err[-1][:200] if err else f"exit {proc.returncode}")
        else:
            parsed = parse_pgbench_output(proc.stdout)
            error = None if parsed["tps"] is not None else "未能从 pgbench 输出解析 TPS"
            result = BenchResult(cand, parsed["tps"], parsed["latency_ms"], error)
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results
