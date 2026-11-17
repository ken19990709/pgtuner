"""Dual-anchor paths (v0.10.0):

- REPO_ROOT (the tool repository, where packages live): anchor for **repo-only
  assets** — benchmark task templates (benchmarks/) and the hygiene scan root.
  Shipped assets (knowledge base, init-distributed AGENTS.md/skills) live inside
  the package (pgtuner/data, pgtuner/assets) so wheel/sdist installs work too.
- PROJECT_ROOT (the current project root): anchor for **runtime state** —
  .pgtuner/ (ledger / instance registry / deployment config / plan archives /
  bench_runs). Resolution chain: env PGTUNER_PROJECT_ROOT > walking up from cwd,
  the first directory containing .pgtuner/ or .git > cwd as the fallback; system
  temp roots (TEMP_ROOTS, e.g. /tmp) are never anchors — a stray /tmp/.pgtuner
  must not capture every bare directory under /tmp.
  Inside the pgtuner repository (including subdirectories) the result is always
  REPO_ROOT, byte-for-byte compatible with v0.9.0 behavior; once `pgtuner init`
  has landed in any project, that project's data never mixes ledgers with the
  tool repository.

PG instance data directories still live in /tmp/pgtuner-instances/ (for
experiments, disposable, machine-wide shared).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Shipped data lives inside the package (v0.11.0 packaging fix): works identically
# for editable and wheel/sdist installs; REPO_ROOT keeps anchoring repo-only assets
# (benchmark task templates, hygiene scan root).
KNOWLEDGE_FILE = Path(__file__).resolve().parent / "data" / "guc_knowledge.json"


def _system_temp_roots() -> frozenset:
    """Candidate system temp roots (resolved). tempfile.gettempdir() honors
    TMPDIR/TMP/TEMP; the literal paths keep the conventional locations covered
    even when the env override points somewhere private."""
    candidates = ("/tmp", "/var/tmp", "/usr/tmp", "/dev/shm", tempfile.gettempdir())
    return frozenset(Path(c).resolve() for c in candidates if c)


# Directories that never act as project anchors: merely running a pgtuner command
# in a temp root plants <root>/.pgtuner (ensure_dirs), which would otherwise
# silently capture every bare directory under it. Module-level constant so tests
# can inject their own root set (monkeypatch) instead of touching the real /tmp.
TEMP_ROOTS = _system_temp_roots()


def resolve_project_root() -> Path:
    """Project root for runtime state: env PGTUNER_PROJECT_ROOT > the first directory
    up from cwd containing .pgtuner/ or .git > cwd. Temp roots (TEMP_ROOTS) are
    skipped by the upward walk — a system temp root is never a project anchor.
    Called once at process startup (the PROJECT_ROOT constant); stable for the
    CLI's single-process lifetime."""
    env = os.environ.get("PGTUNER_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if d in TEMP_ROOTS:
            continue          # a system temp root never anchors (its markers are skipped)
        if (d / ".pgtuner").exists() or (d / ".git").exists():
            return d
    return cur


PROJECT_ROOT = resolve_project_root()
PGTUNER_DIR = PROJECT_ROOT / ".pgtuner"
LEDGER_DIR = PGTUNER_DIR / "ledger"
LEDGER_FILE = LEDGER_DIR / "ledger.jsonl"
PLANS_DIR = PGTUNER_DIR / "plans"
INSTANCE_REGISTRY = PGTUNER_DIR / "instances.json"
BENCH_RUNS_DIR = PGTUNER_DIR / "bench_runs"      # benchmark task run state + dispatched materials (v0.7.0)
# Instance data directory: machine-wide shared (/tmp, disposable, for experiments); overridable via env (v0.11.0, portability: no non-configurable path)
INSTANCE_DATA_ROOT = Path(os.environ.get("PGTUNER_INSTANCE_DATA_ROOT", "/tmp/pgtuner-instances"))


def ensure_dirs() -> None:
    for d in (PGTUNER_DIR, LEDGER_DIR, PLANS_DIR):
        d.mkdir(parents=True, exist_ok=True)
