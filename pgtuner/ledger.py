"""Experiment ledger: JSONL storage + experiment fingerprints + diff significance
judging (contract: the "B. ledger.py" section of the historical dev contracts).

Storage form: one Experiment's asdict() JSON per line (ensure_ascii=False, Chinese is
written verbatim); defaults to pgtuner.paths.LEDGER_FILE (.pgtuner/ledger/ledger.jsonl),
the ledger_file parameter can override it (tests pass tmp_path).
Write policy: **append-only except update** — new_experiment / record_calibration only
append one line; update must modify old lines, so it rewrites the whole file (the
ledger line count is on the order of the experiment count, the rewrite cost is
negligible).
Durability / crash safety (adversarial-review hardening):
  - every append flushes + os.fsyncs before closing, so a killed process or a full
    disk cannot leave an unsynced half line;
  - update rewrites via same-dir tmp + fsync + os.replace (crash-atomic, same
    pattern as instance._save) instead of an in-place seek/truncate, so a crash
    mid-update can no longer destroy the whole ledger;
  - loading tolerates exactly one corrupt line when it is the LAST line of the file
    (the classic crash-truncation signature): the raw line is quarantined into
    <ledger>.corrupt (append, behind a timestamp comment line) with a Chinese
    stderr warning, and the preceding rows load normally — a truncated tail must
    not paralyze results list/show/diff/conclude. A corrupt line in the MIDDLE
    still raises ValueError with the line number and a remediation hint
    (anti-tamper: evidence is never silently dropped).

Notes on the settings form of fingerprint_of (important):
signals.collect().settings entries are currently {setting, unit, context} and **do
not include the pg_settings.source column**, so factory defaults and manual changes
cannot be distinguished. Hence fingerprint_of accepts two forms:
  1) dict[str, dict]: the signals form, or a caller-supplied query result that
     brings its own source (SELECT name, setting, source FROM pg_settings). An entry
     counts toward nondefault_gucs only if it has source and != 'default'
     (configuration file / command line / override / client etc. all count as
     non-default); an entry without source -> undecidable, skipped (better to
     under-report than to flood the fingerprint with hundreds of defaults diluting
     real differences). For an exact fingerprint, the integration layer may query
     with the source column itself and pass that in.
  2) dict[str, str]: name -> value passed directly, treated as non-default GUCs
     already filtered by the caller, all counted.

diff's verdict takes fixed enum values (consistent with the contract examples, easy
for programs to judge; Chinese explanations go into warnings):
  incomparable      build_type differs, not comparable
  improved          a significant positive gain exists (and no significant negative one)
  regressed         a significant negative gain exists (and no significant positive one)
  mixed             both significant positive and negative changes exist
  not significant   judgeable metrics exist and all are insignificant
  no metrics        the two payload["metrics"] have no common keys
  undetermined      common numeric metrics exist but the applicable noise floor is
                    missing (or the baseline is 0), significance undecidable
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import statistics
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .paths import LEDGER_FILE

CALIBRATION_KIND = "calibration"


class DuplicateIdError(ValueError):
    """The experiment id to append clashes with an existing ledger id (same id written
    twice, or the new id and an existing id are prefixes of each other).
    Inherits ValueError: same family as the prefix-ambiguity errors of get/update, so
    callers can catch uniformly. (tool-w3-ledgerdup: once a duplicate id lands on
    disk, that id is permanently unusable due to "prefix ambiguity" in
    get/diff/conclude; it can only be rejected at write time.)"""


@dataclass
class Experiment:
    """One experiment record (one line of the ledger JSONL)."""
    id: str                          # exp-YYMMDD-HHMMSS-xxxxxxxx (8-hex random segment)
    ts: str                          # local-time ISO stamp
    kind: str                        # bench|planbench|rebench|calibration|custom
    label: str
    hypothesis: str | None = None
    instance: dict | None = None     # {"name","pg_version","build_type","port"}
    fingerprint: dict | None = None  # {"pg_major","build_type","nondefault_gucs":{...}}
    payload: dict = field(default_factory=dict)   # arbitrary structure: candidates/results/metrics etc.
    noise_floor: dict | None = None  # {"tps_cv_pct": float, "based_on": id}
    conclusion: str | None = None
    artifacts: list[str] = field(default_factory=list)


# ---------------------------- internals: read/write primitives ----------------------------

def _resolve(ledger_file) -> Path:
    """ledger_file=None -> paths.LEDGER_FILE; otherwise the given path (tests' tmp_path)."""
    return Path(ledger_file) if ledger_file is not None else Path(LEDGER_FILE)


def _read_text(path: Path) -> str:
    """Read the ledger as text; invalid byte sequences decode as U+FFFD instead of
    raising. A crash mid-append can split a multi-byte UTF-8 character at the tail
    (Chinese is written verbatim), and a strict decode would blow up with
    UnicodeDecodeError before _parse ever gets the chance to quarantine the tail
    half-line. Undamaged lines are valid UTF-8 and decode unchanged."""
    return path.read_text(encoding="utf-8", errors="replace")


def _quarantine_tail(path: Path, line: str) -> None:
    """Append the crash-truncated tail line to <ledger>.corrupt, preceded by a
    timestamp comment line: the raw evidence is preserved for manual inspection while
    the ledger itself stays loadable. Content-deduplicated (R2-C finding): every
    read of the same broken tail used to append another copy — the file grew
    unboundedly while the ledger itself stayed broken until the next write."""
    corrupt = path.with_name(path.name + ".corrupt")
    if corrupt.exists():
        prev = corrupt.read_text(encoding="utf-8", errors="replace")
        if line + "\n" in prev.split("# ", 1)[0] or prev.rstrip("\n").endswith(line):
            return                      # same broken tail already quarantined — no duplicates
    with corrupt.open("a", encoding="utf-8") as f:
        f.write(f"# {_now_iso()} 账本 {path.name} 尾行损坏（疑似写入中断/崩溃截断），以下为隔离的原始内容\n")
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def _parse(text: str, path) -> tuple[list[Experiment], bool]:
    """JSONL text -> (experiments, tail_quarantined).

    Corrupt-line policy (adversarial-review hardening):
      - a corrupt LAST line is the classic crash-truncation signature: quarantine it
        into <ledger>.corrupt, warn once on stderr and keep loading the preceding
        rows, so a truncated tail can no longer paralyze results list/show/diff/
        conclude;
      - a corrupt line anywhere else still raises ValueError with the line number
        and a remediation hint (anti-tamper: a hole in the middle is not explainable
        by a crash, and evidence must never be silently dropped).
    Either way nothing vanishes: quarantined bytes land in <ledger>.corrupt."""
    lines = text.splitlines()
    out: list[Experiment] = []
    tail_quarantined = False
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            out.append(Experiment(**json.loads(line)))
        except (json.JSONDecodeError, TypeError) as e:
            if lineno == len(lines):        # last line of the file: crash-truncated tail
                _quarantine_tail(path, line)
                tail_quarantined = True
                print(f"账本尾行损坏已隔离到 {path.name}.corrupt，其余 {len(out)} 条正常加载",
                      file=sys.stderr)
                break                       # only blank lines can follow the last line
            raise ValueError(
                f"账本 {path} 第 {lineno} 行损坏，无法解析为 Experiment: {e}。"
                f"非尾行损坏无法用崩溃截断解释，疑似文件被改动，为防证据被静默篡改已拒绝加载；"
                f"请人工核对该行（确认后将损坏行原样移入 {path.name}.corrupt 或从备份恢复）再试") from None
    return out, tail_quarantined


def _load(path: Path) -> list[Experiment]:
    """Read the whole ledger back (a missing file counts as an empty ledger).
    A crash-truncated tail line is quarantined instead of failing the load."""
    if not path.exists():
        return []
    exps, _quarantined = _parse(_read_text(path), path)
    return exps


@contextlib.contextmanager
def _ledger_lock(path: Path):
    """Cross-process mutation lock for append/update: flock on a stable sidecar
    <ledger>.lock file. The lock is NOT taken on the ledger file itself because
    update replaces the file atomically (os.replace): a writer that opened the
    pre-replace inode and blocked on flock would wake up after the replace on an
    unlinked inode and silently lose its write. The sidecar is never replaced, so
    the lock stays meaningful across rewrites (same pattern as instance.py's
    _registry_lock)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _atomic_write(path: Path, exps: list[Experiment]) -> None:
    """Crash-atomic full rewrite (same pattern as instance._save): write a same-dir
    tmp file with a random suffix, flush + fsync, then os.replace. A reader (or a
    crash) only ever sees the previous complete ledger or the new complete ledger,
    never a half-written file; the random suffix keeps concurrent writers from
    stomping each other's tmp files."""
    tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for exp in exps:
                f.write(json.dumps(asdict(exp), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)               # atomic: old file or new file, no intermediate state
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _append(exp: Experiment, path: Path) -> None:
    """Append one line, flush + os.fsync before close, so a killed process or a full
    disk cannot leave an unsynced half line. Duplicate check inside the lock
    (tool-w3-ledgerdup): a new id identical to an existing one, or a prefix of one
    (or vice versa, e.g. old-ledger 4-hex lines vs new 8-hex lines) raises
    DuplicateIdError — such lines make prefix matching of the shorter id permanently
    ambiguous. The duplicate check must share the same flock as the write, otherwise
    parallel multi-process bookkeeping still slips through."""
    with _ledger_lock(path):
        existing, tail_healed = _parse(_read_text(path) if path.exists() else "", path)
        clash = next((e.id for e in existing
                      if e.id == exp.id or e.id.startswith(exp.id) or exp.id.startswith(e.id)), None)
        if clash is not None:
            raise DuplicateIdError(
                f"拒绝追加：id {exp.id!r} 与账本既有 {clash!r} 冲突"
                f"（new_experiment/record_calibration 已自动落账，勿手动 _append 同一对象；"
                f"若为同秒随机碰撞，换 id 重试）")
        if tail_healed:
            # The corrupt tail was quarantined; a plain append would fuse the new
            # record onto the unterminated tail bytes. Rewrite the good rows + the
            # new row atomically — this also drops the tail, so the ledger self-heals.
            _atomic_write(path, existing + [exp])
        else:
            with path.open("a", encoding="utf-8") as f:   # O_APPEND: writes always land at the end
                f.write(json.dumps(asdict(exp), ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())


def _match_index(exps: list[Experiment], exp_id: str) -> int:
    """id prefix matching: unique -> index; multiple matches -> ValueError listing candidates; none -> ValueError."""
    matches = [i for i, e in enumerate(exps) if e.id.startswith(exp_id)]
    if not matches:
        raise ValueError(f"账本中无 id 或前缀为 {exp_id!r} 的实验")
    if len(matches) > 1:
        cands = ", ".join(sorted(exps[i].id for i in matches))
        raise ValueError(f"id 前缀 {exp_id!r} 歧义，匹配多条: {cands}")
    return matches[0]


def _deep_merge(dst: dict, src: dict) -> dict:
    """Deep-merge src into dst in place (dicts recurse, everything else is overwritten wholesale)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ---------------------------- public API ----------------------------

# Retry cap for same-second random-segment collisions (with 8 hex a single collision is 2^-32; 3 in a row is ledger-anomaly territory)
_NEW_ID_ATTEMPTS = 3


def _new_id() -> str:
    # 8-hex random segment (tool-w3-ledgerdup): with 4 hex the same-second collision rate is 1/65536, non-negligible for parallel multi-agent bookkeeping
    return f"exp-{datetime.now():%y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append_with_fresh_id(make_exp, path: Path) -> Experiment:
    """Build and append one new experiment. When the same-second random segment hits
    an existing id (DuplicateIdError), automatically retry with a fresh id; give up
    after _NEW_ID_ATTEMPTS total hits (the ledger is most likely abnormal by then —
    prefer failing over landing an ambiguous line)."""
    for _ in range(_NEW_ID_ATTEMPTS):
        exp = make_exp()
        try:
            _append(exp, path)
            return exp
        except DuplicateIdError:
            continue
    raise DuplicateIdError(
        f"连续 {_NEW_ID_ATTEMPTS} 次 id 碰撞，账本 {path} 疑似异常，请检查既有 id")


def new_experiment(kind, label, *, hypothesis=None, instance=None, fingerprint=None,
                   payload=None, ledger_file=None) -> Experiment:
    """Generate an id (exp-YYMMDD-HHMMSS-xxxxxxxx, first 8 hex of uuid4 against
    same-second collisions) and append one line. This function already books the
    entry; do not manually _append the returned Experiment again (it would raise
    DuplicateIdError)."""

    def _make() -> Experiment:
        return Experiment(
            id=_new_id(), ts=_now_iso(), kind=kind, label=label, hypothesis=hypothesis,
            instance=instance, fingerprint=fingerprint, payload=dict(payload or {}))

    return _append_with_fresh_id(_make, _resolve(ledger_file))


def get(exp_id, ledger_file=None) -> Experiment:
    """Fetch an experiment by id (or unique prefix). No match / ambiguous prefix both raise ValueError (candidates listed on ambiguity)."""
    exps = _load(_resolve(ledger_file))
    return exps[_match_index(exps, exp_id)]


def update(exp_id, *, payload_merge=None, conclusion=None, artifacts=None,
           noise_floor=None, ledger_file=None) -> Experiment:
    """Update one record in place: payload_merge deep-merges into payload;
    conclusion/noise_floor overwrite; artifacts append (existing ones kept). The id
    supports fuzzy prefix matching. Implementation is a locked read-modify-write with
    a crash-atomic full rewrite (same-dir tmp + fsync + os.replace): B2 acceptance
    defect D4 — without the lock, a concurrent append would be silently swallowed by
    this rewrite; and the old in-place seek/truncate rewrite could destroy the whole
    ledger if the process died mid-rewrite. A quarantined corrupt tail (if any) is
    dropped by the rewrite, so update also self-heals the ledger."""
    path = _resolve(ledger_file)
    with _ledger_lock(path):              # D4: read-modify-write inside the lock
        exps = _load(path)
        exp = exps[_match_index(exps, exp_id)]
        if payload_merge:
            _deep_merge(exp.payload, dict(payload_merge))
        if conclusion is not None:
            exp.conclusion = conclusion
        if artifacts:
            exp.artifacts = list(exp.artifacts) + list(artifacts)
        if noise_floor is not None:
            exp.noise_floor = dict(noise_floor)
        _atomic_write(path, exps)         # crash-atomic rewrite: tmp + fsync + os.replace
    return exp


def list_experiments(*, kind=None, label_contains=None, last=None,
                     ledger_file=None) -> list[Experiment]:
    """Filter by kind (exact) / label_contains (substring); last=N takes the newest N (the last N lines)."""
    exps = _load(_resolve(ledger_file))
    if kind is not None:
        exps = [e for e in exps if e.kind == kind]
    if label_contains is not None:
        exps = [e for e in exps if label_contains in e.label]
    if last is not None:
        exps = exps[-last:] if last > 0 else []
    return exps


def fingerprint_of(settings: dict, instance_meta: dict | None = None) -> dict:
    """settings -> experiment fingerprint {"pg_major","build_type","nondefault_gucs":{...}}.

    Two settings forms (signals.collect().settings currently **carries no source**,
    see the module docstring):
      - dict[str, dict]: an entry counts only if it has source and != 'default';
        entries without source (the current signals form) cannot be judged as
        deviating from default and are skipped;
      - dict[str, str]: name -> value passed directly, treated as already-filtered
        non-default GUCs, all counted.
    From instance_meta (the {"name","pg_version","build_type","port"} at experiment
    time), pg_major ("19devel" -> 19) and build_type are merged into the fingerprint.
    """
    nondefault: dict = {}
    for name, entry in (settings or {}).items():
        if isinstance(entry, str):
            nondefault[name] = entry
        elif isinstance(entry, dict):
            source = entry.get("source")
            if source is not None and source != "default":
                val = entry.get("setting")
                if val is not None:
                    nondefault[name] = val
    fp: dict = {}
    if instance_meta:
        pg_major = instance_meta.get("pg_major")
        if pg_major is None:
            m = re.match(r"\s*(\d+)", str(instance_meta.get("pg_version") or ""))
            pg_major = int(m.group(1)) if m else None
        if pg_major is not None:
            fp["pg_major"] = pg_major
        if instance_meta.get("build_type"):
            fp["build_type"] = instance_meta["build_type"]
    fp["nondefault_gucs"] = nondefault
    return fp


def record_calibration(instance_meta, fingerprint, tps_list, *, ledger_file=None) -> Experiment:
    """Record one noise-floor calibration: kind=calibration,
    payload={"tps_list":[...], "cv_pct": coefficient of variation},
    noise_floor={"tps_cv_pct": cv}. CV = sample standard deviation / mean x 100
    (recorded as 0.0 with <2 samples)."""
    tps = [float(t) for t in tps_list]
    cv_pct = 0.0
    if len(tps) >= 2:
        mean = statistics.fmean(tps)
        if mean != 0:
            cv_pct = round(statistics.stdev(tps) / abs(mean) * 100, 2)
    meta = instance_meta or {}
    label = f"calibration@{meta.get('name') or meta.get('pg_version') or 'instance'}"
    # Does not go through new_experiment (the contract signature has no noise_floor
    # parameter): a calibration row carries its noise floor by nature; build the row
    # in one shot via the internal append primitive, keeping "append-only except
    # update"; the id likewise goes through collision retry.
    def _make() -> Experiment:
        return Experiment(
            id=_new_id(), ts=_now_iso(), kind=CALIBRATION_KIND, label=label,
            hypothesis="重复空跑标定噪声底", instance=instance_meta, fingerprint=fingerprint,
            payload={"tps_list": tps, "cv_pct": cv_pct},
            noise_floor={"tps_cv_pct": cv_pct})

    return _append_with_fresh_id(_make, _resolve(ledger_file))


def latest_noise_floor(fingerprint, *, ledger_file=None) -> float | None:
    """Search newest-first for a kind=calibration record whose fingerprint matches
    on build_type/pg_major, and return its noise_floor["tps_cv_pct"] (CV percentage).
    When the given fingerprint is None or lacks a key, that key does not filter
    (None -> the newest among all calibrations). Returns None when nothing matches."""
    query = fingerprint or {}
    for exp in reversed(_load(_resolve(ledger_file))):
        if exp.kind != CALIBRATION_KIND:
            continue
        fp = exp.fingerprint or {}
        if any(query.get(k) is not None and fp.get(k) != query.get(k)
               for k in ("pg_major", "build_type")):
            continue
        nf = exp.noise_floor or {}
        if nf.get("tps_cv_pct") is not None:
            return float(nf["tps_cv_pct"])
    return None


def citation(exp_id, ledger_file=None) -> str:
    """Short citation: "[id] kind:label"; appends the first 40 characters when a conclusion exists."""
    exp = get(exp_id, ledger_file)
    cite = f"[{exp.id}] {exp.kind}:{exp.label}"
    if exp.conclusion:
        cite += f"({exp.conclusion[:40]})"
    return cite


def diff(exp_id_a, exp_id_b, *, ledger_file=None) -> dict:
    """Compare two experiments + judge significance; returns
    {"compatible","warnings","metrics","verdict"}.

    Rules (contract B):
      - build_type differs (fingerprint first, instance as fallback) ->
        compatible=False, verdict="incomparable";
      - pg_major differs and either kind contains "bench" (bench/planbench/rebench
        all count) -> add a cross-version warning;
      - metrics: common keys of both sides' payload["metrics"]; when a/b are both
        numeric and a!=0, compute delta_pct=(b-a)/a*100 (display keeps 2 decimals,
        judging uses the unrounded value; a=0 or non-numeric -> delta/significant
        None);
      - significance (noise floor applied per metric scope,
        tool-rpx-diff-sig-no-floor): tps_cv_pct is the TPS scope and only judges
        non-latency metrics; latency/duration metrics (lower_better, _ms/err etc.)
        accept only the latency-scoped lat_cv_pct (e.g. a same-source dummy rebuild
        control / the drift of repeated experiments on the same instance) — TPS CV
        is a miscalibrated proxy for durations; when the scope is missing,
        significant=None (no judgment), never a confident "significant".
    The verdict enum is in the module docstring (incomparable/improved/regressed/
    mixed/not significant/no metrics/undetermined); Chinese explanations go into
    warnings.
    """
    path = _resolve(ledger_file)
    a = get(exp_id_a, path)
    b = get(exp_id_b, path)

    def _field(fp: dict, inst: dict, key: str):
        return fp.get(key) if fp.get(key) is not None else inst.get(key)

    fa, fb = a.fingerprint or {}, b.fingerprint or {}
    ia, ib = a.instance or {}, b.instance or {}
    warnings: list[str] = []

    # Rule 1: build_type differs -> incomparable
    compatible = True
    bt_a, bt_b = _field(fa, ia, "build_type"), _field(fb, ib, "build_type")
    if bt_a is not None and bt_b is not None and bt_a != bt_b:
        compatible = False
        warnings.append(f"build_type 不同（{bt_a} vs {bt_b}），debug/release 行为与性能不可比")

    # Rule 2: cross major version + bench-kind experiments -> warning
    pg_a, pg_b = _field(fa, ia, "pg_major"), _field(fb, ib, "pg_major")
    if pg_a is not None and pg_b is not None and pg_a != pg_b:
        if "bench" in a.kind or "bench" in b.kind:
            warnings.append(f"跨大版本对比（PG {pg_a} vs PG {pg_b}）：TPS 不可直接比较，仅供方向参考")

    # Rule 2.5 (patch-trial F2): cross-instance / cross server_version -> warn of a
    # different binary origin. Experiments with the same pg_major and build_type may
    # still come from different binaries (one instance built before the patch, one
    # after); the fingerprint already records instance/server_version, and diff must
    # surface "different origin" to prevent build differences from being taken for
    # the experimental effect.
    inst_a = _field(fa, ia, "instance") or (ia.get("name") if ia else None)
    inst_b = _field(fb, ib, "instance") or (ib.get("name") if ib else None)
    sv_a, sv_b = _field(fa, ia, "server_version"), _field(fb, ib, "server_version")
    if inst_a and inst_b and inst_a != inst_b:
        tail = f"（server_version: {sv_a} vs {sv_b}）" if (sv_a and sv_b and sv_a != sv_b) else ""
        warnings.append(
            f"跨实例对比（{inst_a} vs {inst_b}）{tail}：二进制出处不同，差异可能来自构建差异而非实验变量，"
            f"请确认两侧 bindir/commit 可比")

    # Rule 3: delta and significance of common metrics
    # The noise floor is applied per metric scope (tool-rpx-diff-sig-no-floor):
    # tps_cv_pct is the TPS scope (bench --calibrate only produces it) and a
    # miscalibrated proxy for duration/latency metrics (lower_better) — an _ms
    # difference at pure binary-layout noise level (e.g. dummy rebuild +1.43%) would
    # also be misjudged "significant". Duration metrics accept only lat_cv_pct (the
    # latency scope, e.g. a same-source dummy rebuild control / the drift of
    # repeated experiments on the same instance, booked via
    # update(noise_floor={"lat_cv_pct": ...})); missing scope -> significant=None,
    # no judgment.
    def _tps_floor(exp: Experiment) -> float | None:
        nf = exp.noise_floor or {}
        return float(nf["tps_cv_pct"]) if nf.get("tps_cv_pct") is not None else None

    def _lat_floor(exp: Experiment) -> float | None:
        nf = exp.noise_floor or {}
        return float(nf["lat_cv_pct"]) if nf.get("lat_cv_pct") is not None else None

    tps_cv = _tps_floor(a)
    if tps_cv is None:
        tps_cv = _tps_floor(b)
    if tps_cv is None:
        tps_cv = latest_noise_floor(fa, ledger_file=path)
    if tps_cv is None:
        tps_cv = latest_noise_floor(fb, ledger_file=path)

    ma = (a.payload or {}).get("metrics") or {}
    mb = (b.payload or {}).get("metrics") or {}
    metrics = []
    for name in (k for k in ma if k in mb):
        va, vb = ma[name], mb[name]
        lower_better = _is_lower_better(name)   # F-G: time/latency metrics are lower-better
        row = {"name": name, "a": va, "b": vb, "delta_pct": None,
               "significant": None, "lower_better": lower_better}
        if _is_num(va) and _is_num(vb) and va != 0:
            delta = (vb - va) / va * 100
            row["delta_pct"] = round(delta, 2)
            if lower_better:
                cv = _lat_floor(a)
                if cv is None:
                    cv = _lat_floor(b)
            else:
                cv = tps_cv
            row["significant"] = abs(delta) >= cv if cv is not None else None
        metrics.append(row)
    if metrics and tps_cv is None:
        warnings.append("无噪声底（双方 noise_floor 缺失且账本无匹配 calibration），显著性无法判定")
    if any(r["lower_better"] and r["delta_pct"] is not None and r["significant"] is None
           for r in metrics):
        warnings.append(
            "时间/延迟类指标（_ms 等）无耗时口径噪声底（noise_floor/calibration 只有 TPS CV，"
            "对耗时是失准代理），显著性不判定——请以同源 dummy 重建对照或同实例重复实验的漂移"
            "为噪声底（noise_floor.lat_cv_pct）")
    if any(r["lower_better"] and r["significant"] for r in metrics):
        warnings.append("含时间/延迟类指标（lower_better）：verdict 已按“b 侧耗时下降=improved”方向判定")

    # Rule 4: verdict summary (metric direction aware: higher_better delta>0 or lower_better delta<0 = improved)
    def _improved(r) -> bool:
        if not r["significant"]:
            return False
        d = r["delta_pct"]
        return d > 0 if not r["lower_better"] else d < 0

    def _regressed(r) -> bool:
        if not r["significant"]:
            return False
        d = r["delta_pct"]
        return d < 0 if not r["lower_better"] else d > 0

    if not compatible:
        verdict = "incomparable"
    elif not metrics:
        verdict = "no metrics"
    else:
        judgeable = [r for r in metrics if r["significant"] is not None]
        if not judgeable:
            verdict = "undetermined"
        elif any(map(_improved, judgeable)) and any(map(_regressed, judgeable)):
            verdict = "mixed"
        elif any(map(_improved, judgeable)):
            verdict = "improved"
        elif any(map(_regressed, judgeable)):
            verdict = "regressed"
        else:
            verdict = "not significant"
    return {"compatible": compatible, "warnings": warnings, "metrics": metrics, "verdict": verdict}


def _is_lower_better(metric_name: str) -> bool:
    """Time/latency/error metric names -> lower is better (patch-trial F-G: the
    verdict direction must not reuse TPS's "higher is better").

    Convention: names ending with _ms/_us/_s/_sec/_pct_iowait, or containing
    ms/time/latency/err/log2/abslog2 -> True. rebench's per-query metrics look like
    "<candidate>|Q<i>" (no match) while accuracy's Q*_abslog2err matches.
    """
    n = metric_name.lower()
    return (n.endswith(("_ms", "_us", "_s", "_sec"))
            or any(k in n for k in ("ms", "time", "latency", "err", "log2")))
