"""ledger tests: JSONL round-trips, prefix fuzzy matching, update deep merge, the three diff rules, calibration lookup.
Pure file operations (tmp_path), no dependency on a running PG."""
import json
import os
import re
import statistics
from dataclasses import asdict

import pytest

from pgtuner import ledger


# ---------------------------- builder helpers ----------------------------

def fp(pg_major=17, build_type="release", gucs=None):
    """Standard fingerprint fixture."""
    return {"pg_major": pg_major, "build_type": build_type, "nondefault_gucs": gucs or {}}


def add_bench(ledger_file, label, *, metrics=None, fingerprint=None, noise_floor=None,
              kind="bench", conclusion=None):
    """Build the fixture in two contract steps: new_experiment creates the row → update adds noise_floor/conclusion."""
    exp = ledger.new_experiment(kind, label, payload={"metrics": metrics or {}},
                                fingerprint=fingerprint, ledger_file=ledger_file)
    if noise_floor is not None or conclusion is not None:
        exp = ledger.update(exp.id, noise_floor=noise_floor, conclusion=conclusion,
                            ledger_file=ledger_file)
    return exp


# ---------------------------- append / read round-trip ----------------------------

def test_append_roundtrip(tmp_path):
    lf = tmp_path / "ledger.jsonl"
    exp = ledger.new_experiment(
        "bench", "共享内存对照",
        hypothesis="shared_buffers 提升缓存命中",
        instance={"name": "i1", "pg_version": "17beta", "build_type": "release", "port": 5433},
        fingerprint=fp(gucs={"shared_buffers": "512MB"}),
        payload={"metrics": {"tps": 100.0}},
        ledger_file=lf)
    assert re.fullmatch(r"exp-\d{6}-\d{6}-[0-9a-f]{8}", exp.id)
    raw = lf.read_text(encoding="utf-8")
    assert "共享内存对照" in raw and "\\u" not in raw   # ensure_ascii=False: Chinese characters kept as-is
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 1 and rows[0]["id"] == exp.id
    assert ledger.get(exp.id, lf) == exp                     # dataclass equality: lossless round-trip
    assert ledger.list_experiments(ledger_file=lf) == [exp]


def test_list_filters_and_last(tmp_path):
    lf = tmp_path / "l.jsonl"
    a = add_bench(lf, "alpha", metrics={"tps": 1.0})
    b = add_bench(lf, "beta-共享", metrics={"tps": 2.0})
    c = ledger.new_experiment("planbench", "beta-plan", ledger_file=lf)
    assert [e.id for e in ledger.list_experiments(kind="bench", ledger_file=lf)] == [a.id, b.id]
    assert [e.label for e in ledger.list_experiments(label_contains="beta", ledger_file=lf)] \
        == ["beta-共享", "beta-plan"]
    assert [e.id for e in ledger.list_experiments(last=2, ledger_file=lf)] == [b.id, c.id]
    assert ledger.list_experiments(last=0, ledger_file=lf) == []


def test_empty_or_missing_ledger(tmp_path):
    lf = tmp_path / "none.jsonl"                     # missing file → empty ledger
    assert ledger.list_experiments(ledger_file=lf) == []
    with pytest.raises(ValueError):
        ledger.get("exp-999999-000000-ffff", lf)


# ---------------------------- prefix fuzzy matching ----------------------------

def test_prefix_unique_match(tmp_path):
    lf = tmp_path / "l.jsonl"
    a = add_bench(lf, "a", metrics={})
    assert ledger.get(a.id[:-2], lf).id == a.id      # dropping the last 2 chars is still a unique prefix
    with pytest.raises(ValueError):
        ledger.get("exp-000000-000000-0000", lf)


def test_prefix_ambiguous(tmp_path):
    lf = tmp_path / "l.jsonl"
    a = add_bench(lf, "a", metrics={})
    b = add_bench(lf, "b", metrics={})
    assert a.id != b.id
    p = os.path.commonprefix([a.id, b.id])           # ids are fixed-length → the common prefix is necessarily shorter than a full id
    assert p not in (a.id, b.id)
    with pytest.raises(ValueError) as ei:
        ledger.get(p, lf)
    msg = str(ei.value)
    assert "歧义" in msg and a.id in msg and b.id in msg


# ---------------------------- update deep merge + full rewrite ----------------------------

def test_update_deep_merge_and_rewrite(tmp_path):
    lf = tmp_path / "l.jsonl"
    a = ledger.new_experiment(
        "bench", "a", payload={"metrics": {"tps": 100.0}, "env": {"clients": 4}}, ledger_file=lf)
    other = ledger.new_experiment("bench", "other", ledger_file=lf)
    upd = ledger.update(
        a.id,
        payload_merge={"metrics": {"latency_ms": 10.0}, "env": {"clients": 8, "duration": 20},
                       "note": "x"},
        conclusion="shared_buffers=512MB 更稳", artifacts=["plans/a.json"],
        noise_floor={"tps_cv_pct": 2.5}, ledger_file=lf)
    assert upd.payload == {"metrics": {"tps": 100.0, "latency_ms": 10.0},
                           "env": {"clients": 8, "duration": 20}, "note": "x"}
    assert upd.conclusion == "shared_buffers=512MB 更稳"
    assert upd.artifacts == ["plans/a.json"]
    assert upd.noise_floor == {"tps_cv_pct": 2.5}
    # fetched back via prefix match + line count unchanged after the full rewrite, other rows intact
    assert ledger.get(a.id[:-2], lf) == upd
    assert len(lf.read_text(encoding="utf-8").splitlines()) == 2
    assert ledger.get(other.id, lf).label == "other"
    with pytest.raises(ValueError):
        ledger.update("exp-000000-000000-0000", payload_merge={"x": 1}, ledger_file=lf)


def test_update_artifacts_append(tmp_path):
    lf = tmp_path / "l.jsonl"
    a = ledger.new_experiment("bench", "a", ledger_file=lf)
    a = ledger.update(a.id, artifacts=["r1.json"], ledger_file=lf)
    upd = ledger.update(a.id, artifacts=["r2.json"], ledger_file=lf)
    assert upd.artifacts == ["r1.json", "r2.json"]   # appended, not overwritten


# ---------------------------- crash resilience (adversarial-review hardening) ----------------------------

def _raw_line(exp) -> str:
    """One ledger line exactly as ledger.py would write it."""
    return json.dumps(asdict(exp), ensure_ascii=False)


def test_tail_halfline_quarantined_ledger_still_usable(tmp_path, capsys):
    """A crash-truncated tail line must not paralyze the ledger: it is quarantined into
    <ledger>.corrupt (timestamp comment line + raw bytes) with a Chinese stderr warning;
    list/get/diff keep working on the preceding rows, and the next append self-heals the
    file (the corrupt tail is dropped, later loads no longer warn)."""
    lf = tmp_path / "l.jsonl"
    nf = {"tps_cv_pct": 5.0}
    a = add_bench(lf, "a", metrics={"tps": 100.0}, noise_floor=nf)
    b = add_bench(lf, "b", metrics={"tps": 110.0}, noise_floor=nf)
    frag = _raw_line(ledger.Experiment(id="exp-261116-000000-deadbeef",
                                       ts="2026-11-16T00:00:00", kind="bench",
                                       label="truncated"))[:29]
    with lf.open("a", encoding="utf-8") as f:
        f.write(frag)                                # no trailing newline: truncated tail

    rows = ledger.list_experiments(ledger_file=lf)   # loads despite the corrupt tail
    assert [e.id for e in rows] == [a.id, b.id]
    assert ledger.get(a.id, lf).label == "a"         # get still resolves evidence
    assert ledger.diff(a.id, b.id, ledger_file=lf)["verdict"] == "improved"

    corrupt = tmp_path / "l.jsonl.corrupt"
    assert corrupt.exists()                          # quarantine landed on disk
    content = corrupt.read_text(encoding="utf-8")
    first, raw = content.splitlines()[:2]
    assert re.match(r"# \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", first)   # timestamp comment line
    assert raw == frag                               # the raw half-line preserved verbatim
    err = capsys.readouterr().err
    assert "l.jsonl.corrupt" in err and "2 条正常加载" in err

    c = ledger.new_experiment("bench", "c", ledger_file=lf)   # append self-heals the tail
    capsys.readouterr()
    rows = ledger.list_experiments(ledger_file=lf)
    assert [e.id for e in rows] == [a.id, b.id, c.id]
    assert "deadbeef" not in lf.read_text(encoding="utf-8")    # tail really dropped
    assert capsys.readouterr().err == ""                       # no repeated quarantine


def test_tail_split_multibyte_char_quarantined(tmp_path, capsys):
    """A crash can split a multi-byte UTF-8 character at the tail (Chinese labels are
    written verbatim); loading must quarantine the line instead of dying on
    UnicodeDecodeError before _parse ever runs."""
    lf = tmp_path / "mb.jsonl"
    a = ledger.new_experiment("bench", "a", ledger_file=lf)
    full = _raw_line(ledger.Experiment(id="exp-261116-000002-abcdef01",
                                       ts="2026-11-16T00:00:02", kind="bench",
                                       label="中文标签结尾")).encode("utf-8")
    with lf.open("ab") as f:
        f.write(full[:-2])                           # cut mid-character: invalid UTF-8 tail
    rows = ledger.list_experiments(ledger_file=lf)
    assert [e.id for e in rows] == [a.id]
    corrupt = tmp_path / "mb.jsonl.corrupt"
    assert corrupt.exists() and "abcdef01" in corrupt.read_text(encoding="utf-8")
    assert "mb.jsonl.corrupt" in capsys.readouterr().err


def test_middle_corrupt_line_still_raises(tmp_path, capsys):
    """Anti-tamper semantics preserved: a corrupt line that is NOT the last one cannot
    be explained by crash truncation, so loading still raises ValueError — now with the
    line number and a remediation hint — and nothing is quarantined or warned."""
    lf = tmp_path / "m.jsonl"
    a = ledger.new_experiment("bench", "a", ledger_file=lf)
    good_last = ledger.Experiment(id="exp-261116-000001-cccccccc",
                                  ts="2026-11-16T00:00:01", kind="bench", label="last")
    lf.write_text("\n".join([_raw_line(a), "{坏行-not-json", _raw_line(good_last)]) + "\n",
                  encoding="utf-8")

    with pytest.raises(ValueError) as ei:
        ledger.list_experiments(ledger_file=lf)
    msg = str(ei.value)
    assert "第 2 行" in msg                       # line number indicated
    assert "m.jsonl.corrupt" in msg               # remediation hint given
    assert not (tmp_path / "m.jsonl.corrupt").exists()   # nothing silently quarantined
    assert capsys.readouterr().err == ""          # no quarantine warning
    with pytest.raises(ValueError):
        ledger.update(a.id, conclusion="x", ledger_file=lf)   # update refuses too
    with pytest.raises(ValueError):
        ledger.new_experiment("bench", "n", ledger_file=lf)   # append refuses too
    assert len(lf.read_text(encoding="utf-8").splitlines()) == 3   # no partial write


def test_update_rewrites_atomically_via_replace(tmp_path, monkeypatch):
    """update must not rewrite the ledger in place: the new content goes to a tmp file
    (fsynced) and is moved into place with os.replace — asserted by spying on
    os.replace; no *.tmp.* leftovers remain after a successful update."""
    lf = tmp_path / "u.jsonl"
    a = ledger.new_experiment("bench", "a", ledger_file=lf)
    b = ledger.new_experiment("bench", "b", ledger_file=lf)
    real_replace = os.replace
    calls = []

    def spy(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(ledger.os, "replace", spy)
    upd = ledger.update(a.id, conclusion="结论", ledger_file=lf)
    assert len(calls) == 1 and str(calls[0][1]) == str(lf)     # replace used, target is the ledger
    assert ledger.get(a.id, lf) == upd
    assert [e.id for e in ledger.list_experiments(ledger_file=lf)] == [a.id, b.id]
    assert not [p for p in tmp_path.iterdir() if ".tmp." in p.name]   # no tmp leftovers


def test_update_crash_before_replace_leaves_ledger_intact(tmp_path, monkeypatch):
    """Simulated crash between the tmp write and the rename: the original ledger must
    survive byte-for-byte (the old in-place seek/truncate rewrite would already have
    destroyed it), the tmp file is cleaned up, and the error propagates."""
    lf = tmp_path / "u.jsonl"
    a = ledger.new_experiment("bench", "a", ledger_file=lf)
    b = ledger.new_experiment("bench", "b", ledger_file=lf)
    before = lf.read_bytes()

    def boom(src, dst):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(ledger.os, "replace", boom)
    with pytest.raises(OSError, match="simulated crash"):
        ledger.update(a.id, conclusion="x", ledger_file=lf)
    monkeypatch.undo()
    assert lf.read_bytes() == before                          # no half-written ledger
    assert not [p for p in tmp_path.iterdir() if ".tmp." in p.name]   # tmp cleaned up
    assert [e.id for e in ledger.list_experiments(ledger_file=lf)] == [a.id, b.id]
    assert ledger.update(a.id, conclusion="ok", ledger_file=lf).conclusion == "ok"  # retry works


def test_append_fsyncs_before_close(tmp_path, monkeypatch):
    """Fix C: every append flushes + fsyncs the ledger file before closing it, so a
    killed process cannot leave an unsynced half line (spy on os.fsync)."""
    lf = tmp_path / "f.jsonl"
    real_fsync = os.fsync
    seen = []

    def spy(fd):
        seen.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(ledger.os, "fsync", spy)
    ledger.new_experiment("bench", "a", ledger_file=lf)
    ledger.record_calibration(None, None, [100.0, 102.0], ledger_file=lf)
    assert seen                                                # fsync called on append
    rows = ledger.list_experiments(ledger_file=lf)             # and the lines made it to disk
    assert len(rows) == 2


# ---------------------------- fingerprint_of ----------------------------

def test_fingerprint_of_with_source():
    settings = {
        "shared_buffers": {"setting": "65536", "unit": "8kB", "source": "configuration file"},
        "work_mem": {"setting": "4096", "unit": "kB", "source": "default"},
        "client_min_messages": {"setting": "log", "source": "override"},
    }
    out = ledger.fingerprint_of(settings, {"name": "i1", "pg_version": "19devel",
                                           "build_type": "release", "port": 5433})
    assert out["nondefault_gucs"] == {"shared_buffers": "65536", "client_min_messages": "log"}
    assert out["pg_major"] == 19 and out["build_type"] == "release"


def test_fingerprint_of_signals_form_without_source():
    # current shape of signals.collect().settings: {setting, unit, context}, no source → undecidable, skipped
    settings = {"work_mem": {"setting": "4096", "unit": "kB", "context": "user"}}
    out = ledger.fingerprint_of(settings, {"pg_version": "17beta", "build_type": "debug"})
    assert out["nondefault_gucs"] == {}
    assert out["pg_major"] == 17 and out["build_type"] == "debug"


def test_fingerprint_of_plain_str_form():
    # dict[str, str] passed directly: treated as already-filtered non-default GUCs, all counted
    out = ledger.fingerprint_of({"shared_buffers": "512MB", "random_page_cost": "1.1"})
    assert out["nondefault_gucs"] == {"shared_buffers": "512MB", "random_page_cost": "1.1"}
    assert "pg_major" not in out                     # not injected without instance_meta


# ---------------------------- the three diff rules ----------------------------

def test_diff_build_type_incompatible(tmp_path):
    lf = tmp_path / "l.jsonl"
    a = add_bench(lf, "release 组", metrics={"tps": 100.0}, fingerprint=fp(build_type="release"))
    b = add_bench(lf, "debug 组", metrics={"tps": 95.0}, fingerprint=fp(build_type="debug"))
    d = ledger.diff(a.id, b.id, ledger_file=lf)
    assert d["compatible"] is False
    assert d["verdict"] == "incomparable"
    assert any("build_type" in w for w in d["warnings"])
    assert d["metrics"][0]["name"] == "tps"          # metrics still given (informative)


def test_diff_cross_version_warning_only_for_bench(tmp_path):
    lf = tmp_path / "l.jsonl"
    a = add_bench(lf, "v17", metrics={"tps": 100.0}, fingerprint=fp(pg_major=17), kind="bench")
    b = add_bench(lf, "v19", metrics={"tps": 120.0}, fingerprint=fp(pg_major=19), kind="rebench")
    d = ledger.diff(a.id, b.id, ledger_file=lf)
    assert d["compatible"] is True
    assert any("跨大版本" in w for w in d["warnings"])
    # non-bench kinds (custom) do not warn on cross-version (so the "no noise floor" warning stays isolated)
    nf = {"tps_cv_pct": 5.0}
    c = add_bench(lf, "v17c", metrics={"x": 1.0}, fingerprint=fp(pg_major=17),
                  kind="custom", noise_floor=nf)
    e = add_bench(lf, "v19c", metrics={"x": 2.0}, fingerprint=fp(pg_major=19),
                  kind="custom", noise_floor=nf)
    d2 = ledger.diff(c.id, e.id, ledger_file=lf)
    assert d2["warnings"] == []


def test_diff_significance_with_noise_floor(tmp_path):
    lf = tmp_path / "l.jsonl"
    nf = {"tps_cv_pct": 5.0}
    # tps +10% ≥ 5 → significant positive gain → improved; latency_ms is a duration measure, not judged by the TPS CV (None)
    a = add_bench(lf, "基线", metrics={"tps": 100.0, "latency_ms": 10.0}, noise_floor=nf)
    b = add_bench(lf, "候选", metrics={"tps": 110.0, "latency_ms": 10.1}, noise_floor=nf)
    d = ledger.diff(a.id, b.id, ledger_file=lf)
    assert d["compatible"] is True and d["verdict"] == "improved"
    by_name = {m["name"]: m for m in d["metrics"]}
    assert by_name["tps"]["delta_pct"] == pytest.approx(10.0)
    assert by_name["tps"]["significant"] is True
    assert by_name["latency_ms"]["delta_pct"] == pytest.approx(1.0)
    assert by_name["latency_ms"]["significant"] is None   # duration metrics do not use the TPS-criterion noise floor


def test_diff_verdict_matrix(tmp_path):
    lf = tmp_path / "l.jsonl"
    nf = {"tps_cv_pct": 5.0}

    def run(ma, mb):
        a = add_bench(lf, "a", metrics=ma, noise_floor=nf)
        b = add_bench(lf, "b", metrics=mb, noise_floor=nf)
        return ledger.diff(a.id, b.id, ledger_file=lf)

    assert run({"tps": 100.0}, {"tps": 103.0})["verdict"] == "not significant"  # +3% < 5
    assert run({"tps": 100.0}, {"tps": 90.0})["verdict"] == "regressed"        # -10% significantly negative
    # significant improvement and worsening together → mixed (two higher_better metrics; latency-type metrics no longer use the TPS CV, so mixed cannot be built with them)
    d = run({"tps": 100.0, "qps": 50.0}, {"tps": 110.0, "qps": 40.0})
    assert d["verdict"] == "mixed"
    # no common metrics → no metrics
    assert run({"tps": 100.0}, {"qps": 10.0})["verdict"] == "no metrics"


def test_diff_lower_better_direction(tmp_path):
    """F-G (patch-trial): time-type metrics are lower-better — duration decrease = improved, increase = regressed.
    Significance accepts only the duration-criterion noise floor lat_cv_pct (tool-rpx-diff-sig-no-floor)."""
    lf = tmp_path / "l.jsonl"
    nf = {"lat_cv_pct": 5.0}        # duration criterion (e.g. dummy rebuild comparison / repeated-experiment drift)
    a = add_bench(lf, "基线", metrics={"base|Q0_ms": 100.0}, noise_floor=nf)
    b = add_bench(lf, "候选", metrics={"base|Q0_ms": 70.0}, noise_floor=nf)   # -30% → faster
    d = ledger.diff(a.id, b.id, ledger_file=lf)
    assert d["verdict"] == "improved"                       # the old semantics would misjudge it as regressed
    assert d["metrics"][0]["lower_better"] is True
    assert d["metrics"][0]["significant"] is True
    c = add_bench(lf, "变慢", metrics={"base|Q0_ms": 130.0}, noise_floor=nf)  # +30% → slower
    d2 = ledger.diff(a.id, c.id, ledger_file=lf)
    assert d2["verdict"] == "regressed"


def test_diff_custom_ms_metrics_not_judged_by_tps_cv(tmp_path):
    """tool-rpx-diff-sig-no-floor regression: custom-workload duration metrics (_ms) have no noise floor available under the TPS criterion
    — pure layout noise from a dummy rebuild (+1.43%) must not be judged "significant", even when both sides carry a tps_cv_pct noise floor.
    tps_cv_pct is a TPS criterion, an inaccurate proxy for durations; missing criterion → significant=None, verdict=undetermined."""
    lf = tmp_path / "l.jsonl"
    nf = {"tps_cv_pct": 1.11}       # bench --calibrate produces only the TPS CV (rebench stamps it onto the experiment)
    a = add_bench(lf, "dummy-a", kind="rebench",
                  metrics={"baseline|Q0_ms": 152.905, "baseline|Q2_ms": 1.021}, noise_floor=nf)
    b = add_bench(lf, "dummy-b", kind="rebench",
                  metrics={"baseline|Q0_ms": 155.092, "baseline|Q2_ms": 1.085}, noise_floor=nf)
    d = ledger.diff(a.id, b.id, ledger_file=lf)
    assert all(m["significant"] is None for m in d["metrics"])   # old implementation: +1.43% ≥ 1.11 → "yes"
    assert d["metrics"][0]["delta_pct"] == pytest.approx(1.43)   # Δ% still given (informative)
    assert d["verdict"] == "undetermined"                        # no adjudicable metric
    assert any("耗时口径" in w and "lat_cv_pct" in w for w in d["warnings"])
    # adding the duration-criterion noise floor to the same data (lat_cv_pct=7.0; +1.43%/+6.27% both below it) makes it adjudicable: not significant
    ledger.update(a.id, noise_floor={"tps_cv_pct": 1.11, "lat_cv_pct": 7.0}, ledger_file=lf)
    d2 = ledger.diff(a.id, b.id, ledger_file=lf)
    assert all(m["significant"] is False for m in d2["metrics"])
    assert d2["verdict"] == "not significant"


def test_diff_cross_instance_binary_provenance_warning(tmp_path):
    """F2 (patch-trial): cross-instance (different binary provenance) → warning, to prevent build differences from being taken for experiment effects."""
    lf = tmp_path / "l.jsonl"
    nf = {"tps_cv_pct": 5.0}
    fa = fp(pg_major=20, build_type="release")
    fa["instance"] = "base2"
    fa["server_version"] = "20devel"
    fb = fp(pg_major=20, build_type="release")
    fb["instance"] = "p1uuid"
    fb["server_version"] = "20devel"
    a = add_bench(lf, "a", metrics={"x_ms": 100.0}, fingerprint=fa, noise_floor=nf)
    b = add_bench(lf, "b", metrics={"x_ms": 30.0}, fingerprint=fb, noise_floor=nf)
    d = ledger.diff(a.id, b.id, ledger_file=lf)
    assert d["compatible"] is True                          # compatibility is left to human judgment, but the provenance must be surfaced
    assert any("跨实例" in w and "二进制出处" in w for w in d["warnings"])
    # same instance: no warning
    fc = fp(pg_major=20, build_type="release")
    fc["instance"] = "base2"
    c = add_bench(lf, "c", metrics={"x_ms": 90.0}, fingerprint=fc, noise_floor=nf)
    d2 = ledger.diff(a.id, c.id, ledger_file=lf)
    assert not any("跨实例" in w for w in d2["warnings"])


def test_diff_noise_floor_falls_back_to_latest_calibration(tmp_path):
    lf = tmp_path / "l.jsonl"
    f = fp(pg_major=17, build_type="release")
    ledger.record_calibration({"name": "i1", "pg_version": "17beta", "build_type": "release"},
                              f, [100.0, 103.0], ledger_file=lf)      # cv ≈ 2.79%
    ledger.record_calibration({"name": "i1", "pg_version": "17beta", "build_type": "release"},
                              f, [100.0, 108.0], ledger_file=lf)      # cv ≈ 7.69% (latest)
    a = add_bench(lf, "基线", metrics={"tps": 100.0}, fingerprint=f)   # neither side carries its own noise_floor
    b = add_bench(lf, "候选", metrics={"tps": 105.0}, fingerprint=f)
    d = ledger.diff(a.id, b.id, ledger_file=lf)
    # +5%: with the old calibration (2.79) it would be significant; with the latest (7.69) → not significant ⇒ proves the latest is used
    assert d["metrics"][0]["significant"] is False
    assert d["verdict"] == "not significant"


def test_diff_no_noise_floor_undetermined(tmp_path):
    lf = tmp_path / "l.jsonl"
    a = add_bench(lf, "a", metrics={"tps": 100.0})
    b = add_bench(lf, "b", metrics={"tps": 200.0})
    d = ledger.diff(a.id, b.id, ledger_file=lf)
    assert d["verdict"] == "undetermined"
    assert d["metrics"][0]["significant"] is None
    assert any("噪声底" in w for w in d["warnings"])


# ---------------------------- calibration ----------------------------

def test_record_calibration_and_latest_noise_floor(tmp_path):
    lf = tmp_path / "l.jsonl"
    f = fp(pg_major=17, build_type="release")
    exp = ledger.record_calibration(
        {"name": "i1", "pg_version": "17beta", "build_type": "release", "port": 5433},
        f, [100.0, 102.0, 98.0], ledger_file=lf)
    assert exp.kind == "calibration"
    assert "i1" in exp.label
    expected_cv = round(statistics.stdev([100.0, 102.0, 98.0]) / 100.0 * 100, 2)   # = 2.0
    assert exp.payload["tps_list"] == [100.0, 102.0, 98.0]
    assert exp.payload["cv_pct"] == pytest.approx(expected_cv)
    assert exp.noise_floor == {"tps_cv_pct": pytest.approx(expected_cv)}
    assert ledger.latest_noise_floor(f, ledger_file=lf) == pytest.approx(expected_cv)
    # build_type / pg_major mismatch → not found
    assert ledger.latest_noise_floor(fp(pg_major=17, build_type="debug"), ledger_file=lf) is None
    assert ledger.latest_noise_floor(fp(pg_major=19), ledger_file=lf) is None
    # None → no filtering, takes the latest calibration
    assert ledger.latest_noise_floor(None, ledger_file=lf) == pytest.approx(expected_cv)
    assert ledger.latest_noise_floor(f, ledger_file=tmp_path / "none.jsonl") is None


def test_record_calibration_short_list(tmp_path):
    exp = ledger.record_calibration(None, None, [42.0], ledger_file=tmp_path / "l.jsonl")
    assert exp.payload["cv_pct"] == 0.0 and exp.noise_floor == {"tps_cv_pct": 0.0}


# ---------------------------- citation ----------------------------

def test_citation(tmp_path):
    lf = tmp_path / "l.jsonl"
    long_text = "这是一个足够长的实验结论" * 10        # 120 chars
    a = add_bench(lf, "共享内存对照", metrics={}, conclusion=long_text)
    cite = ledger.citation(a.id[:-4], lf)            # prefix matching applies here too
    assert cite == f"[{a.id}] bench:共享内存对照({long_text[:40]})"
    b = add_bench(lf, "无结论", metrics={})
    assert ledger.citation(b.id, lf) == f"[{b.id}] bench:无结论"


def test_concurrent_append_update_no_loss(tmp_path):
    """D4 regression: concurrent append and update (full rewrite) no longer swallow each other's lines (read-modify-write inside the flock)."""
    import threading
    from pgtuner import ledger
    lf = tmp_path / "c.jsonl"
    a = ledger.new_experiment("bench", "a", payload={"metrics": {"x": 1}}, ledger_file=lf)

    def appender():
        for i in range(20):
            ledger.new_experiment("bench", f"t{i}", ledger_file=lf)

    t = threading.Thread(target=appender)
    t.start()
    for _ in range(20):
        ledger.update(a.id, conclusion="c", ledger_file=lf)
    t.join()
    rows = ledger.list_experiments(ledger_file=lf)
    assert len(rows) == 21, f"并发丢失:{len(rows)}/21"


# ---------------------------- id uniqueness (tool-w3-ledgerdup) ----------------------------

def test_append_rejects_duplicate_id(tmp_path):
    """tool-w3-ledgerdup regression: new_experiment already records automatically; manually _append-ing the same object again is rejected,
    no longer producing double-written rows that make show/diff/conclude permanently "prefix-ambiguous" for that id."""
    lf = tmp_path / "l.jsonl"
    e = ledger.new_experiment("bench", "dup", ledger_file=lf)
    with pytest.raises(ledger.DuplicateIdError, match="已自动落账"):
        ledger._append(e, lf)                              # old implementation: silent double write
    assert len(lf.read_text(encoding="utf-8").splitlines()) == 1   # the double write did not happen
    assert ledger.get(e.id, lf) == e                       # the original id still resolves normally
    assert isinstance(ledger.DuplicateIdError("x"), ValueError)    # same family as the ambiguity error


def test_new_id_random_segment_8hex(tmp_path):
    """tool-w3-ledgerdup regression: random segment 4→8 hex (same-second collision rate 1/65536 → 1/2^32);
    bulk generation (very likely spanning the same second) yields well-formed and all-unique ids."""
    lf = tmp_path / "l.jsonl"
    ids = [ledger.new_experiment("bench", f"u{i}", ledger_file=lf).id for i in range(200)]
    assert all(re.fullmatch(r"exp-\d{6}-\d{6}-[0-9a-f]{8}", i) for i in ids)
    assert len(set(ids)) == len(ids)


def test_append_rejects_prefix_ambiguous_id(tmp_path):
    """tool-w3-ledgerdup regression: a new id and an existing id that are prefixes of each other (old-ledger 4-hex row vs a new 8-hex row)
    are likewise rejected — such rows would make prefix matching permanently ambiguous for the shorter side."""
    lf = tmp_path / "l.jsonl"
    legacy = ledger.Experiment(id="exp-261112-213354-41ac", ts="2026-11-12T21:33:54",
                               kind="bench", label="legacy-4hex")
    ledger._append(legacy, lf)

    def mk(eid, label):
        return ledger.Experiment(id=eid, ts=legacy.ts, kind="bench", label=label)

    # the existing short id is a prefix of the new long id → prefix matching for the old id would become ambiguous
    with pytest.raises(ledger.DuplicateIdError, match="冲突"):
        ledger._append(mk(legacy.id + "ab12cd34", "extends-legacy"), lf)
    # reverse: a new short id is a prefix of an existing long id → prefix matching for the new short id would become ambiguous
    ledger._append(mk("exp-261112-213355-00000000", "long"), lf)
    with pytest.raises(ledger.DuplicateIdError):
        ledger._append(mk("exp-261112-213355-0000", "prefix-of-long"), lf)
    # unrelated ids append as usual; both rejections left nothing on disk
    ledger._append(mk("exp-261112-213356-ffffffff", "unrelated"), lf)
    assert len(lf.read_text(encoding="utf-8").splitlines()) == 3


def test_new_experiment_retries_on_id_collision(tmp_path, monkeypatch):
    """tool-w3-ledgerdup regression: after a duplicate check rejects, new_experiment automatically retries with a new id;
    same-second random collisions no longer fail experiment creation (and no ambiguous row is written)."""
    lf = tmp_path / "l.jsonl"
    a = ledger.new_experiment("bench", "a", ledger_file=lf)        # occupies an id
    real = ledger._new_id
    seq = iter([a.id, a.id, real()])                               # first two collide, the third is unique
    monkeypatch.setattr(ledger, "_new_id", lambda: next(seq))
    b = ledger.new_experiment("bench", "b", ledger_file=lf)
    assert b.id != a.id and re.fullmatch(r"exp-\d{6}-\d{6}-[0-9a-f]{8}", b.id)
    assert len(ledger.list_experiments(ledger_file=lf)) == 2       # both colliding attempts left nothing on disk
    assert ledger.get(a.id, lf).label == "a"                       # no ambiguity


def test_record_calibration_retries_on_id_collision(tmp_path, monkeypatch):
    """tool-w3-ledgerdup regression: record_calibration shares the collision retry with new_experiment."""
    lf = tmp_path / "l.jsonl"
    c = ledger.record_calibration(None, None, [100.0], ledger_file=lf)   # occupies an id
    real = ledger._new_id
    seq = iter([c.id, real()])
    monkeypatch.setattr(ledger, "_new_id", lambda: next(seq))
    c2 = ledger.record_calibration(None, None, [102.0], ledger_file=lf)
    assert c2.id != c.id
    assert len(ledger.list_experiments(kind="calibration", ledger_file=lf)) == 2


def test_concurrent_new_experiment_ids_unique(tmp_path):
    """tool-w3-ledgerdup regression: multithreaded parallel bookkeeping (dedup inside the lock) all succeeds with zero duplicate ids."""
    import threading
    lf = tmp_path / "c.jsonl"
    errs: list[Exception] = []

    def worker(t):
        try:
            for i in range(25):
                ledger.new_experiment("bench", f"t{t}-{i}", ledger_file=lf)
        except Exception as e:                                      # pragma: no cover
            errs.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = ledger.list_experiments(ledger_file=lf)
    ids = [e.id for e in rows]
    assert not errs, errs
    assert len(rows) == 200 and len(set(ids)) == 200


def test_quarantine_does_not_grow_on_repeated_reads(tmp_path):
    """R2-C: every read of the same broken tail used to append another copy to
    <ledger>.corrupt — three list calls tripled the file."""
    import pgtuner.ledger as L
    led = tmp_path / "ledger.jsonl"
    L.new_experiment("bench", "t", ledger_file=led)          # one valid row
    broken = '{"id": "e2", "ts": "2026'                    # crash-truncated tail
    with open(led, "a", encoding="utf-8") as f:
        f.write(broken + "\n")
    for _ in range(3):
        L.list_experiments(ledger_file=led)
    content = (tmp_path / "ledger.jsonl.corrupt").read_text(encoding="utf-8")
    assert content.count(broken) == 1
