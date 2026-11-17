"""restartbench — slow channel: the change-config -> restart -> warmup -> measure ->
restore loop for postmaster/sighup-level GUCs.

The bench fast channel injects via PGOPTIONS at session level and can only touch
context='user' parameters; this module goes through ALTER SYSTEM + a real restart,
dedicated to postmaster/sighup-level GUCs (e.g. shared_buffers / max_connections).
Each candidate gets one full cycle: ALTER SYSTEM SET -> stop/start -> readiness
wait -> warmup (pgbench -T or two passes of custom_queries) -> measure -> restore.
Measurement and parsing directly reuse pgtuner.bench's run_pgbench /
parse_pgbench_output.

The restore guarantee is this module's reputation: run_restart_bench wraps every
candidate cycle in try/finally; exceptions on any path (apply failure / warmup
failure / measurement error) must still run restore_and_verify in the finally —
per touched GUC, ALTER SYSTEM RESET -> restart -> re-fetch the full pg_settings
and compare against the baseline (**all parameters**, not just the touched ones,
guarding against tampering). The only degraded path: the snapshot itself failed
(nothing has been touched yet, the exception propagates directly, no restore
obligation). A restore_and_verify failure (differences remain after restore) is
treated as a serious incident: RuntimeError with the difference list attached,
raised directly, never silently continuing.

Crash safety (v0.12.0 hardening, adversarial review R2) — the in-memory
_TOUCHED bookkeeping alone cannot survive a kill:
- On-disk restore journal: before the first ALTER SYSTEM of every GUC cycle,
  run_restart_bench persists <PGTUNER_DIR>/restore/<instance>.json (derived from
  pgtuner.paths, re-resolved per call) holding the instance name, the full
  pre-experiment pg_settings baseline and a timestamp. restore_and_verify
  removes the journal only after the post-restore full comparison passed, so a
  run killed mid-cycle (signal, closed terminal) leaves a recovery recipe on
  disk instead of a silently drifted postgresql.auto.conf. The next
  run_restart_bench detects the leftover journal, restores from it first
  (one-line notice on stdout), and only then takes the new baseline — a fresh
  snapshot can no longer launder the drift. A journal write failure aborts the
  run before any config is touched; a failed recovery raises (the new run
  refuses to build on an unrestorable leftover), and a journal that exists but
  cannot be parsed gets the same treatment (C-F7): RuntimeError naming the
  path — never an automatic delete, which would launder the drift into the
  next run's fresh baseline; a human inspects and removes it.
- Signal-delayed restore: the restore's critical section (RESET -> stop/start ->
  verify) runs with SIGINT/SIGTERM blocked via signal.pthread_sigmask (no-op on
  platforms without it) — a second Ctrl-C during the up-to-~120s restart window
  can no longer abort the restore halfway. Blocked signals stay pending and are
  delivered after the section completes (a delayed Ctrl-C is acceptable).
- Restore result contract: restore_and_verify returns
  {"restored": True, "mismatches": []} ONLY on the verified-success path (the
  full re-fetch matched the baseline AND the journal was removed) and raises
  RuntimeError carrying the difference list otherwise. Callers distinguish
  "已验证还原成功 / 还原失败" by the presence of the return value vs the raised
  exception; run_restart_bench additionally mirrors each cycle's real receipt
  into CycleResult.restore (None when measure_only ran no restore), so upper
  layers need not hardcode "restored": True.

DEVIATION (backward-compatible extensions relative to contract C, no existing
signature changed):
- CycleResult gains an optional field payload (default None): records per-query
  details when workload="custom";
- CycleResult gains an optional field restore (default None): that cycle's real
  restore_and_verify receipt (see the crash-safety section above);
- run_restart_bench gains an optional parameter on_cycle (the optional progress
  callback mentioned in the contract body notes);
- during parallel development pgtuner.instance may not be merged yet: on import
  failure a placeholder module is installed (all contract functions raise
  RuntimeError) so this module can still be imported and tested standalone; once
  instance.py lands, the real module is used automatically.
"""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import json
import os
import re
import signal
import statistics
import sys
import time
import types
from dataclasses import dataclass, field
from pathlib import Path

from . import bench
from . import paths
from . import pg

try:
    from . import instance as instance_mod              # contract A: the single entry point for instance lifecycle
except ImportError:                                     # parallel development period: instance.py not yet merged
    if importlib.util.find_spec(f"{__package__}.instance") is not None:
        raise                                           # file exists but its own import failed: propagate honestly, do not mask

    def _missing(fn: str):
        def _unavailable(*_args, **_kwargs):
            raise RuntimeError(f"pgtuner.instance 尚未合入,契约函数 {fn}() 不可用")
        return _unavailable

    instance_mod = types.ModuleType(f"{__package__}.instance")
    for _fn in ("get", "ensure_running", "start", "stop", "is_ready", "dsn_of"):
        setattr(instance_mod, _fn, _missing(_fn))

READY_TIMEOUT_S = 60.0          # upper bound (seconds) of the is_ready poll after restart
READY_POLL_INTERVAL_S = 0.5
SNAPSHOT_SQL = "SELECT name, setting FROM pg_settings"

# GUCs this module touched via ALTER SYSTEM (bookkept per instance name): one source
# of restore_and_verify's restore list; the other is drift detection on "current
# value differs from baseline" (backstopping leftover external ALTER SYSTEM
# changes).
_TOUCHED: dict[str, set[str]] = {}

# ---------------------------- crash safety: restore journal / signal mask ----------------------------

_JOURNAL_VERSION = 1
_INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")     # journal filenames must not escape the restore dir
_BLOCKED_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_RESTORE_NOTICE = "正在还原配置(约含一次重启,期间 Ctrl-C 将被延迟到还原完成)"
_LEFTOVER_NOTICE = "发现上次未完成实验的残留配置,已自动还原"


def _restore_journal_path(instance_name: str) -> Path:
    """<PGTUNER_DIR>/restore/<instance>.json, anchored on pgtuner.paths' project root.
    Re-resolved per call via paths.resolve_project_root() rather than reading the
    import-time PGTUNER_DIR constant, so a redirected PGTUNER_PROJECT_ROOT (test
    isolation, future multi-project daemons) is honored."""
    if not _INSTANCE_NAME_RE.match(instance_name):
        raise ValueError(f"实例名不能用作 journal 文件名(仅字母数字点下划线连字符): {instance_name!r}")
    return paths.resolve_project_root() / ".pgtuner" / "restore" / f"{instance_name}.json"


def _journal_write(instance_name: str, snapshot: dict) -> Path:
    """Persist the restore journal BEFORE the first ALTER SYSTEM of a cycle: instance
    name + full pre-experiment GUC baseline + timestamp (+ pid for forensics).
    Atomic (tmp file + os.replace) so a crash mid-write never leaves half a journal.
    Failures propagate: touching the config without a journal on disk is forbidden."""
    path = _restore_journal_path(instance_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": _JOURNAL_VERSION, "instance": instance_name,
               "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "pid": os.getpid(), "snapshot": dict(snapshot)}
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _journal_read(instance_name: str) -> "dict | None":
    """Load the leftover journal for the instance, None when absent. A journal that
    exists but cannot be parsed carries no recovery recipe — same treatment as a
    failed recovery (C-F7): raise RuntimeError naming the on-disk path and let a
    human decide. Auto-deleting it here is forbidden: the next run_restart_bench
    would then take the drifted pg_settings as its fresh baseline — the exact
    drift-laundering this module's journal exists to prevent."""
    path = _restore_journal_path(instance_name)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        if not (isinstance(data, dict) and isinstance(data.get("snapshot"), dict)
                and data.get("instance") == instance_name):
            raise ValueError("journal 结构不合法(缺少 instance/snapshot)")
        return data
    except ValueError as exc:                    # json.JSONDecodeError subclasses ValueError
        raise RuntimeError(
            f"还原 journal 已损坏,无法解析({path}: {exc});"
            f"该文件未被删除——人工核对后删除该文件再重试") from exc


def _journal_remove(instance_name: str) -> None:
    """Delete the journal (idempotent); called only on the verified-success path of
    restore_and_verify — a journal that survives is evidence of an unfinished restore."""
    try:
        _restore_journal_path(instance_name).unlink()
    except FileNotFoundError:
        pass


@contextlib.contextmanager
def _instance_run_lock(instance_name: str):
    """Cross-process rebench mutex per instance (R2-D finding): two concurrent
    rebench runs on one instance would interleave ALTER SYSTEM cycles and clobber
    each other's restore journal. Acquired non-blocking at run start; a busy lock
    means another rebench owns the instance — refuse instead of corrupting.

    Contract (C-F9): NOT reentrant within one process — nesting this context
    manager self-refuses, because the inner acquire opens a second file
    description whose LOCK_EX|LOCK_NB fails against the outer one and surfaces
    as the "另一个 rebench 正在实例 … 上运行" RuntimeError. No depth counter is
    installed (unlike instance._registry_lock, whose _held_locks thread-local
    counting exists because locked flows re-enter it via _save): no current call
    chain nests this lock — the rebench CLI holds it once per run, diagnose
    --verify's _run_measure calls run strictly sequentially, selftest acquires
    it once — callers guarantee non-nesting, and an accidental nesting fails
    loudly (refusal error) rather than corrupting anything."""
    path = _restore_journal_path(instance_name).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as lf:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(
                f"另一个 rebench 正在实例 {instance_name} 上运行(还原 journal 锁被持有);"
                "请等它结束后再跑——并发同实例会互相踩 ALTER SYSTEM 与还原") from None
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def recover_leftover_journal(inst: "instance_mod.Instance") -> "dict | None":
    """Rebench-startup recovery: a leftover on-disk journal means a previous run was
    killed between ALTER SYSTEM and the verified restore — restore from the journal's
    baseline FIRST (the instance must already be running; run_restart_bench ensures
    that before calling), then let the caller proceed with a clean state.

    Returns restore_and_verify's receipt, or None when no journal was found (the
    normal path: silent, zero SQL, zero restarts). A recovery that cannot verify
    raises RuntimeError — the new run refuses to start on top of an unrestorable
    leftover instead of laundering it into a fresh baseline snapshot."""
    data = _journal_read(inst.name)
    if data is None:
        return None
    snapshot = {str(k): str(v) for k, v in data["snapshot"].items()}
    try:
        result = restore_and_verify(inst, snapshot)     # removes the journal on verified success
    except Exception as exc:
        # name the journal's on-disk path: otherwise the only way out of a stale
        # journal is knowing where to look (R2-D finding)
        raise RuntimeError(
            f"残留 journal 还原失败(实例 {inst.name}): {exc};journal 保留在 "
            f"{_restore_journal_path(inst.name)},人工核对后删除该文件再重试") from exc
    print(f"{_LEFTOVER_NOTICE}(实例 {inst.name},journal 建于 {data.get('created', '?')})",
          flush=True, file=sys.stderr)
    return result


@contextlib.contextmanager
def _delayed_signals() -> "Iterator[bool]":
    """Delay SIGINT/SIGTERM delivery for the restore's critical section (see the
    module docstring). The blocked signals stay pending and are delivered on unblock
    (a delayed Ctrl-C after the restore completes is acceptable). POSIX-only:
    platforms without signal.pthread_sigmask get a no-op. Yields whether masking is
    actually active, and restores the exact previous mask on exit (no leakage)."""
    if not hasattr(signal, "pthread_sigmask"):
        yield False
        return
    try:
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, _BLOCKED_SIGNALS)
    except (OSError, ValueError):                # e.g. exotic thread/signal table states
        yield False
        return
    try:
        yield True
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


@dataclass
class RestartCandidate:
    """One restart candidate. An empty gucs dict = baseline: restart without setting
    anything, eliminating the restart's own effect on measurement."""
    name: str
    gucs: dict = field(default_factory=dict)   # {"shared_buffers": "512MB"}; values are conf literals


@dataclass
class CycleResult:
    """Result of one candidate's cycle. A failed candidate has tps/latency None and error recording the summary; later candidates are not interrupted."""
    candidate: RestartCandidate
    tps: float | None = None
    latency_ms: float | None = None
    warmup_s: int = 0
    error: str | None = None
    payload: dict | None = None                # extension field: per-query details of the custom workload
    restore: dict | None = None                # crash-safety v0.12.0: this cycle's real restore_and_verify
                                               # receipt ({"restored": True, "mismatches": []} on the verified
                                               # path); None when measure_only ran no restore


# ---------------------------- primitives: snapshot / apply / restore ----------------------------

def snapshot_settings(dsn: str) -> dict[str, str]:
    """Full pg_settings snapshot {name: setting} — the restore-comparison baseline (must be taken before any change; only a successful snapshot creates the restore obligation)."""
    conn = pg.connect_rw(dsn)
    try:
        rows = pg.fetch_all(conn, SNAPSHOT_SQL)
    finally:
        conn.close()
    return {name: setting for name, setting in rows}


def _restart_and_wait(inst: "instance_mod.Instance") -> None:
    """stop -> start (both via instance_mod) -> poll is_ready (<= READY_TIMEOUT_S); raises RuntimeError on timeout."""
    instance_mod.stop(inst.name)
    instance_mod.start(inst.name)
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if instance_mod.is_ready(inst.name):
            return
        time.sleep(READY_POLL_INTERVAL_S)
    raise RuntimeError(
        f"实例 {inst.name!r} 重启后 {READY_TIMEOUT_S:g}s 内未就绪(is_ready 持续返回 False)")


_GUC_NAME_RE = re.compile(r"^[A-Za-z0-9_.]+$")


def apply_and_restart(inst: "instance_mod.Instance", gucs: dict) -> None:
    """Per-GUC ALTER SYSTEM SET -> stop -> start -> readiness wait.

    B1 acceptance defect D1 fix: values are quoted automatically via psycopg2
    parameter binding — unit-carrying values like `128MB` without quotes would
    raise "trailing junk after numeric literal"; GUC names pass a whitelist regex
    first, then double quotes — no injection. An empty gucs dict still restarts:
    the baseline candidate uses the same flow, aligning the effect of "the restart
    itself" across candidates. Every successful ALTER is recorded into _TOUCHED
    immediately (the restore-list source; mid-way failures have already been
    recorded, nothing is lost).
    """
    if gucs:
        conn = pg.connect_rw(instance_mod.dsn_of(inst.name, "postgres"))
        try:
            for name, value in gucs.items():
                if not _GUC_NAME_RE.match(name):
                    raise ValueError(f"GUC 名不合法(仅字母数字点下划线): {name!r}")
                with conn.cursor() as cur:
                    cur.execute(f'ALTER SYSTEM SET "{name}" = %s', (str(value),))
                _TOUCHED.setdefault(inst.name, set()).add(name)
        finally:
            conn.close()
    _restart_and_wait(inst)


def _diff_settings(snapshot: dict, current: dict) -> list[str]:
    """Full comparison (not just the touched ones). Returns the difference list, e.g. "work_mem: 基准='4096' 实际='64MB'"."""
    mismatches = []
    for name in sorted(set(snapshot) | set(current)):
        expected, actual = snapshot.get(name), current.get(name)
        if expected != actual:
            mismatches.append(f"{name}: 基准={expected!r} 实际={actual!r}")
    return mismatches


def restore_and_verify(inst: "instance_mod.Instance", snapshot: dict) -> dict:
    """Restore and fully verify — where this module's reputation lives (the restore
    guarantee is in the module docstring).

    Restore list = GUCs this module touched (_TOUCHED bookkeeping) ∪ drifted GUCs
    whose current value differs from the baseline (backstopping leftover external
    ALTER SYSTEM changes); per GUC, ALTER SYSTEM RESET -> restart -> re-fetch the
    full pg_settings and compare against the snapshot on **all parameters** (not
    just the touched ones, guarding against tampering). With nothing to RESET the
    restart is skipped (config untouched) but the full comparison still runs.

    Result contract (crash-safety v0.12.0): returns {"restored": True,
    "mismatches": []} ONLY after the post-restore full comparison matched AND the
    on-disk restore journal was removed; any restore that cannot verify raises
    RuntimeError with the difference list (the journal stays on disk as evidence
    and recovery recipe). When a RESET-triggered restart is due, the whole
    critical section runs with SIGINT/SIGTERM delayed (one-line notice printed
    before blocking) so a second Ctrl-C cannot abort the restore halfway.
    """
    dsn = instance_mod.dsn_of(inst.name, "postgres")

    def _verify_and_close_journal() -> dict:
        mismatches = _diff_settings(snapshot, snapshot_settings(dsn))
        if mismatches:
            raise RuntimeError("还原后 pg_settings 与基准不一致(疑似被串改): " + "; ".join(mismatches))
        _journal_remove(inst.name)      # journal removed only on the verified-success path
        return {"restored": True, "mismatches": []}

    current = snapshot_settings(dsn)
    tracked = _TOUCHED.pop(inst.name, set())
    drifted = {name for name in set(snapshot) | set(current)   # union: names absent from the
               if current.get(name) != snapshot.get(name)}     # baseline but appearing mid-run also get backstopped
    to_reset = tracked | drifted
    if to_reset:
        # B1 acceptance defect D3 fix: derived read-only parameters with
        # context=internal such as shared_memory_size drift with their primary
        # parameter but cannot be RESET (CantChangeRuntimeParam) — filtered by
        # pg_settings.context; they are recomputed automatically once the primary is
        # RESET and the server restarted, and the full comparison still covers them.
        conn = pg.connect_rw(dsn)
        try:
            contexts = {n: c for n, c in
                        pg.fetch_all(conn, "SELECT name, context FROM pg_settings")}
        finally:
            conn.close()
        resettable = sorted(n for n in to_reset
                            if contexts.get(n, "user") != "internal")
        if resettable:
            # crash-safety R2-B: the stop/start inside the restore takes up to
            # ~120s; block SIGINT/SIGTERM for the whole critical section so a
            # second Ctrl-C is delayed until after the verified restore.
            # stderr on purpose: `rebench --format json` owns stdout (R2-C finding).
            print(_RESTORE_NOTICE, flush=True, file=sys.stderr)
            with _delayed_signals():
                conn = pg.connect_rw(dsn)
                try:
                    for name in resettable:
                        # names come from pg_settings / the on-disk journal snapshot —
                        # quoted identifier + charset guard keeps journal tampering from
                        # becoming an SQL injection surface (R2-D finding)
                        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name):
                            raise RuntimeError(f"拒绝 RESET 非法 GUC 名: {name!r}")
                        pg.execute(conn, f'ALTER SYSTEM RESET "{name}"')
                finally:
                    conn.close()
                _restart_and_wait(inst)
                return _verify_and_close_journal()
    return _verify_and_close_journal()


# ---------------------------- custom workload ----------------------------

def _strip_query(sql: str) -> str:
    """Strip surrounding whitespace and the trailing semicolon (EXPLAIN cannot take a semicolon; custom_queries may be written with one)."""
    return sql.strip().rstrip(";").strip()


def _explain_execution_ms(conn, query: str) -> float:
    """EXPLAIN (ANALYZE, FORMAT JSON), take the top-level Execution Time (milliseconds)."""
    rows = pg.fetch_all(conn, f"EXPLAIN (ANALYZE, FORMAT JSON) {query}")
    doc = rows[0][0]                     # psycopg2 auto-deserializes json columns
    if isinstance(doc, str):             # manual parse fallback when the driver did not deserialize
        doc = json.loads(doc)
    entry = doc[0] if isinstance(doc, list) else doc   # B1 defect D2: the deserialized form is [ {..} ]
    return float(entry["Execution Time"])


# first words of statements EXPLAIN can wrap; the rest (ALTER/CREATE/ANALYZE/VACUUM/…) are utility statements
_RE_EXPLAINABLE = re.compile(r"\s*\(*\s*(select|with|values|insert|update|delete|table|merge)\b",
                             re.I)


def _is_utility(stmt: str) -> bool:
    """A statement not wrappable by EXPLAIN (DDL/ANALYZE/VACUUM etc.) -> script mode (see _run_custom_cycle)."""
    return not _RE_EXPLAINABLE.match(stmt)


def _run_custom_cycle(dsn: str, queries: list[str], *, passes: int = 2,
                      runs: int = 3) -> tuple[float, dict]:
    """One cycle of the custom workload: warmup (each statement run passes times
    over an rw connection) + measurement (per statement EXPLAIN ANALYZE **repeated
    runs times, take the median** — B1R2 re-review of the R1 fix: a single
    measurement violates the repetition rule). Returns (the median of per-query
    median times — the summary metric, per-query details with the runs raw values).

    Any utility statement in the file (DDL/ANALYZE/…) -> **script mode**: the whole
    file runs once in written order, each statement wall-clock timed, no warmup, no
    re-runs — non-idempotent statements necessarily error on a second execution
    (duplicate ADD COLUMN / a wasted ALTER TYPE rewrite), and EXPLAIN does not
    support utility statements either; migration-script/ANALYZE timings use the
    wall-clock metric, recorded separately from the query Execution Time metric
    (payload["script"]=True).
    """
    queries = [_strip_query(q) for q in queries]
    if any(_is_utility(q) for q in queries):
        conn = pg.connect_rw(dsn)
        try:
            per_query = []
            for q in queries:
                t0 = time.perf_counter()
                pg.execute(conn, q)
                ms = (time.perf_counter() - t0) * 1000
                per_query.append({"query": q, "execution_ms": ms, "times_ms": [ms]})
            return (float(statistics.median(p["execution_ms"] for p in per_query)),
                    {"queries": per_query, "script": True})
        finally:
            conn.close()
    for _ in range(passes):              # warmup: one rw connection per pass, executing each statement
        conn = pg.connect_rw(dsn)
        try:
            for q in queries:
                pg.execute(conn, q)
        finally:
            conn.close()
    per_query = []
    conn = pg.connect_rw(dsn)
    try:
        for q in queries:
            times = [_explain_execution_ms(conn, q) for _ in range(max(1, runs))]
            per_query.append({"query": q, "execution_ms": float(statistics.median(times)),
                              "times_ms": times})
    finally:
        conn.close()
    tps_equivalent = float(statistics.median(item["execution_ms"] for item in per_query))
    return tps_equivalent, {"queries": per_query}


def _run_setup_statements(dsn: str, statements: list[str]) -> tuple[float, dict]:
    """Run the intervention statements (ANALYZE/index creation etc.) in order, once,
    each wall-clock timed — the same metric as script mode. The first failure stops
    and propagates (later statements' effects would be undefinable); the caller
    records the error; after runs regardless."""
    stmts = [_strip_query(s) for s in statements]
    conn = pg.connect_rw(dsn)
    try:
        per_query = []
        for s in stmts:
            t0 = time.perf_counter()
            pg.execute(conn, s)
            ms = (time.perf_counter() - t0) * 1000
            per_query.append({"query": s, "execution_ms": ms, "times_ms": [ms]})
        return (float(statistics.median(p["execution_ms"] for p in per_query)),
                {"queries": per_query, "script": True})
    finally:
        conn.close()


# ---------------------------- main flow ----------------------------

def run_restart_bench(instance_name: str, candidates: list[RestartCandidate], *,
                      dbname: str, workload: str = "pgbench",
                      duration: int = 20, warmup: int = 10, clients: int = 4,
                      pgbench_path: str | None = None, registry_file: str | Path | None = None,
                      custom_queries: list[str] | None = None, runs: int = 3,
                      measure_only: bool = False, setup_statements: list[str] | None = None,
                      on_cycle=None) -> list[CycleResult]:
    """Contract C full flow: snapshot -> ensure_running -> [change config -> restart
    -> warmup -> measure -> restore] x N.

    - candidates gets a baseline auto-prepended (name="baseline", gucs={},
      restart-only, eliminating the restart's own effect); not duplicated when the
      user already supplied a candidate named baseline;
    - measure_only=True (patch-trial F-D): measure-only mode — no config change,
      **no restart**, no restore needed afterwards. Only empty candidates are
      allowed (the auto-prepended baseline round), for comparisons that need no
      parameter change such as cross-build A/B; GUC comparisons still go through
      the default restart path;
    - workload="pgbench": per candidate, first run_pgbench(duration=warmup,
      gucs=None) as warmup with the result discarded, then
      run_pgbench(duration=duration, clients=clients) to measure; parsing reuses
      bench.parse_pgbench_output for TPS/latency (the GUC is already effective at
      the server level, PGOPTIONS no longer injected);
    - workload="custom": custom_queries each run twice over connect_rw (warmup);
      measurement = per statement EXPLAIN ANALYZE **repeated runs times, take the
      median** (R1 fix); the median of per-query medians is the summary metric;
      per-query details (with the runs raw values) go into
      result.payload["queries"];
    - setup_statements (v0.11.0): before/setup/after three phases in one command —
      measure the workload first, run the intervention statements in order once
      (per-statement timing, script metric), then measure the workload again;
      returns three CycleResults (candidate names before/setup/after). Only
      mounted on the measure-only channel (validated: custom workload + pure
      SELECT + measure_only); the intervention statements really execute on the
      target db — aim them at a clone (the CLI layer warns when the target is not
      a pool-registered clone); a failed setup statement records that cycle's
      error and after still runs.
    - pgbench_path, when not given, resolves via instance bindir > PATH
      (bench.resolve_pgbench, only needed for workload=pgbench);
      returns list[CycleResult] (baseline included);
    - a single candidate failure records error and continues; **the restore runs
      unconditionally** (per-cycle try/finally, see the module docstring) and its
      real receipt is recorded into each CycleResult.restore (None when
      measure_only ran no restore); a restore_and_verify error (differences
      remain after restore) propagates directly and aborts the whole flow;
    - crash safety: a leftover on-disk restore journal from a killed previous run
      is recovered (restore + one-line notice) right after ensure_running, BEFORE
      the new baseline snapshot is taken; before each GUC cycle's first ALTER
      SYSTEM the journal (instance + full baseline + timestamp) is persisted —
      a journal write failure aborts the run before any config is touched.
    """
    if workload not in ("pgbench", "custom"):
        raise ValueError(f"workload 必须是 'pgbench' 或 'custom': {workload!r}")
    if workload == "custom" and not custom_queries:
        raise ValueError("workload='custom' 需要非空 custom_queries")
    if measure_only and any(c.gucs for c in candidates):
        raise ValueError("measure_only 纯测量模式不接受带 GUC 的候选(不改参就无所谓候选对比)")
    if setup_statements:
        if workload != "custom" or not custom_queries:
            raise ValueError("setup_statements 需要纯 SELECT 测量负载(--queries-file):"
                             "干预语句夹在前后两轮测量之间,没有负载就无前后可言")
        if not measure_only:
            raise ValueError("setup_statements 只挂纯测量(免确认)通道,不与 GUC 重启对比混用;"
                             "GUC 实验请去掉 setup")
        if any(_is_utility(_strip_query(q)) for q in custom_queries):
            raise ValueError("setup_statements 要求测量负载纯 SELECT(EXPLAIN 可包装):"
                             "utility 语句(DDL/ANALYZE)要么整文件 script 模式单遍跑,"
                             "要么作为 setup 干预语句")

    cands = list(candidates)
    if not any(c.name == "baseline" for c in cands):
        cands.insert(0, RestartCandidate("baseline", {}))

    with _instance_run_lock(instance_name):
        return _run_locked(instance_name, cands, dbname=dbname, workload=workload,
                           duration=duration, warmup=warmup, clients=clients,
                           pgbench_path=pgbench_path, registry_file=registry_file,
                           custom_queries=custom_queries, runs=runs,
                           measure_only=measure_only, setup_statements=setup_statements,
                           on_cycle=on_cycle)


def _run_locked(instance_name: str, cands: list[RestartCandidate], *,
                dbname: str, workload: str = "pgbench",
                duration: int = 20, warmup: int = 10, clients: int = 4,
                pgbench_path: str | None = None,
                registry_file: str | Path | None = None,
                custom_queries: list[str] | None = None, runs: int = 3,
                measure_only: bool = False, setup_statements: list[str] | None = None,
                on_cycle=None) -> list[CycleResult]:
    """Body of run_restart_bench, executed under the per-instance cross-process lock."""
    inst = instance_mod.get(instance_name, registry_file=registry_file)
    instance_mod.ensure_running(inst.name)
    recover_leftover_journal(inst)   # crash-safety: a killed previous run's leftover is restored before the new baseline
    if workload == "pgbench":
        # v0.9.0: the machine-specific hardcoded DEFAULT_PGBENCH default was removed; resolve via instance bindir > PATH instead
        pgbench_path = pgbench_path or bench.resolve_pgbench(bindir=inst.bindir)
        if not pgbench_path:
            raise ValueError(
                "pgbench 未找到:显式传 pgbench_path,或实例 bindir/PATH 提供"
                "(编译安装: bash scripts/setup.sh --pg <prefix>)")
    dsn_bench = instance_mod.dsn_of(inst.name, dbname)
    host, port, _ = pg.target(dsn_bench)
    snapshot = snapshot_settings(instance_mod.dsn_of(inst.name, "postgres"))

    if setup_statements:
        # the before/setup/after three phases: the candidate loop steps aside entirely (validation already guarantees measure-only, no GUCs, no restart)
        out: list[CycleResult] = []
        for name in ("before", "setup", "after"):
            tps: float | None = None
            payload: dict | None = None
            error: str | None = None
            try:
                if name == "setup":
                    tps, payload = _run_setup_statements(dsn_bench, setup_statements)
                else:
                    tps, payload = _run_custom_cycle(dsn_bench, custom_queries,
                                                     passes=2, runs=runs)
            except Exception as exc:          # a setup failure records error; after runs regardless
                error = f"{type(exc).__name__}: {exc}"[:200]
            out.append(CycleResult(RestartCandidate(name, {}), tps=tps,
                                   warmup_s=warmup, error=error, payload=payload))
            if on_cycle is not None:
                on_cycle(out[-1])
        return out

    results: list[CycleResult] = []
    for cand in cands:
        tps: float | None = None
        latency_ms: float | None = None
        payload: dict | None = None
        error: str | None = None
        restore_receipt: dict | None = None
        if not measure_only and cand.gucs:
            # crash-safety: the journal hits the disk BEFORE the first ALTER SYSTEM
            # of this cycle (outside the try: a failure here must abort the run
            # before any config is touched, not be recorded as a candidate error).
            _journal_write(inst.name, snapshot)
        try:
            if not measure_only:
                apply_and_restart(inst, cand.gucs)
            if workload == "pgbench":
                bench.run_pgbench(pgbench_path, host, port, dbname,      # warmup, result discarded
                                  duration=warmup, clients=clients, gucs=None)
                proc = bench.run_pgbench(pgbench_path, host, port, dbname,
                                         duration=duration, clients=clients)
                if proc.returncode != 0:
                    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
                    error = lines[-1][:200] if lines else f"exit {proc.returncode}"
                else:
                    parsed = bench.parse_pgbench_output(proc.stdout)
                    tps, latency_ms = parsed["tps"], parsed["latency_ms"]
                    if tps is None:
                        error = "未能从 pgbench 输出解析 TPS"
            else:
                tps, payload = _run_custom_cycle(dsn_bench, custom_queries, passes=2, runs=runs)
        except Exception as exc:                   # single candidate failure: record error, continue with the next candidate
            error = f"{type(exc).__name__}: {exc}"[:200]
        finally:
            if not measure_only:
                # unconditional restore; a restore failure propagates directly —
                # on success the real receipt rides on this cycle's result
                restore_receipt = restore_and_verify(inst, snapshot)
        results.append(CycleResult(cand, tps=tps, latency_ms=latency_ms,
                                   warmup_s=warmup, error=error, payload=payload,
                                   restore=restore_receipt))
        if on_cycle is not None:
            on_cycle(results[-1])
    return results
