"""Registry torn-read regression (tool-w3-tornread): during a clone storm, after `lab clone` had created the database and
printed its name, printing the usage hint re-read the registry (cli.py dsn_of → get → _load) and could land exactly on
another process's _save write_text truncation window (open('w') zeroes first, write not yet flushed) → json.loads raises
"实例注册表损坏（非 JSON）": the database was created yet rc=1, a set -e script skips release → leaked database; if the
process happened to be killed inside that window, the 0-byte registry is permanent and every subsequent pgtuner command breaks.

tool-w3-flock's tmp + os.replace atomic persistence eliminated that window (readers only ever see the complete old or new JSON).
This file regresses from the torn-read-specific angle (monkeypatch away _run_sql, no running PG; the fork context is
isomorphic to the real multi-process `pgtuner lab clone/release/stats` storm):
  - cross-process readers vs writer storm: all _load read paths (pool_stats / get / dsn_of) with zero exceptions (the CLI symptom);
  - write-mechanism contract: _save never opens the registry itself in write mode; it must write a same-directory tmp then os.replace it in atomically;
  - killed before the replace: the registry is still the complete old JSON (hazard (3)'s 0-byte permanence path does not exist).
"""
import builtins
import json
import multiprocessing as mp
import os
from pathlib import Path

import pytest

from pgtuner import instance

CTX = mp.get_context("fork")            # child processes inherit the monkeypatches and already-imported modules

READ_ROUNDS = 250
WRITE_ROUNDS = 30


def _seed(reg, clones=None):
    """Put a lab instance in the registry (clone/drop_db no longer touch PG once _run_sql is stubbed)."""
    entry = dict(name="lab", bindir="/nonexistent", pg_version="20devel", build_type="release",
                 data_dir="", port=5599, socket_dir="/tmp", status="running", managed=True)
    instance._save({"instances": [entry], "clones": clones or {}}, reg)


def _noop_run_sql(name, dbname, sql, registry_file=None):
    """Stub: skips terminate/CREATE/DROP (the subject under test is the registry read/write, not the DDL)."""
    pass


def _churn_worker(reg_str, rounds):
    """Writer: repeatedly clone → release (each iteration is an in-lock read-modify-write + _save persist)."""
    for _ in range(rounds):
        db = instance.clone("lab", "tpl", registry_file=reg_str)
        instance.drop_db("lab", db, registry_file=reg_str)


def _reader_worker(reg_str, rounds):
    """Reader: continuously exercises the three _load read paths; any exception (e.g. "registry corrupt") → non-zero exit."""
    for _ in range(rounds):
        stats = instance.pool_stats(reg_str)          # lab stats path
        assert isinstance(stats, dict)
        inst = instance.get("lab", reg_str)           # lab clone usage-hint path
        assert inst.name == "lab"
        assert "dbname=" in instance.dsn_of("lab", "x", reg_str)


def _run_all(procs):
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker 异常退出: exitcode={p.exitcode}（读者撞上截断空窗）"


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "r.json"
    _seed(path)
    monkeypatch.setattr(instance, "_run_sql", _noop_run_sql)
    return path


# ---------------------------- cross-process readers vs writers: zero crashes ----------------------------

def test_concurrent_readers_never_crash_on_registry(reg):
    """Under a storm of 4 writers × 30 rounds of clone+release, 4 readers × 250 rounds read the registry with zero exceptions.

    The old implementation's _save truncated first via open('w') then wrote, so readers could hit a 0-byte / half-written JSON,
    _load raising RuntimeError("实例注册表损坏") — the r15 phenomenon: database created, name printed, process rc=1."""
    writers = [CTX.Process(target=_churn_worker, args=(str(reg), WRITE_ROUNDS))
               for _ in range(4)]
    readers = [CTX.Process(target=_reader_worker, args=(str(reg), READ_ROUNDS))
               for _ in range(4)]
    _run_all(writers + readers)
    # each writer returned its own clones → the pool is back to the seed state (bonus regression: no orphan registrations left)
    assert instance.pool_stats(reg) == {"lab": []}


# ---------------------------- _save write mechanism: never truncates the registry itself ----------------------------

def test_save_never_truncates_live_registry(reg, monkeypatch):
    """_save must not open the registry itself in write mode (the old implementation's open('w') truncated first → 0-byte window);
    it must write a same-directory tmp then os.replace it in atomically (same directory = same filesystem, only then is rename atomic)."""
    reg.write_text("SENTINEL-OLD", encoding="utf-8")     # an old version differing from what will be written
    opened, replaced = [], []
    real_open, real_replace = builtins.open, os.replace

    def spy_open(file, mode="r", *args, **kwargs):
        if Path(file) == reg and any(c in mode for c in "wa+"):
            opened.append((str(file), mode))
        return real_open(file, mode, *args, **kwargs)

    def spy_replace(src, dst):
        replaced.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "replace", spy_replace)
    instance._save({"instances": [], "clones": {"lab": ["tpl_c0123abcd"]}}, reg)

    assert not opened, f"注册表本体被以写模式打开（截断空窗复活）: {opened}"
    assert len(replaced) == 1
    src, dst = replaced[0]
    assert dst == reg                                     # the atomic swap-in target is the registry itself
    assert src.parent == reg.parent                       # the tmp is in the target's directory (same filesystem)
    assert src.name.startswith(reg.name + ".tmp.")
    assert not src.exists()                               # the tmp was consumed by replace
    assert instance._load(reg) == {"instances": [], "clones": {"lab": ["tpl_c0123abcd"]}}


def test_save_killed_before_replace_keeps_old_registry(reg, monkeypatch):
    """Simulate a process killed after writing the tmp but before os.replace: the registry must still be the complete old JSON.

    Under the old implementation the registry would already be 0 bytes at this moment and never recover (all subsequent commands report "registry corrupt") —
    the same-root risk this kill round did not hit is reproduced here deterministically."""
    old = instance._load(reg)
    monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(
        KeyboardInterrupt("模拟 kill：替换前进程死亡")))

    with pytest.raises(KeyboardInterrupt):
        instance._save({"instances": [], "clones": {}}, reg)

    assert instance._load(reg) == old                     # the old version intact, service can continue at any time
    leftovers = [p for p in reg.parent.iterdir()
                 if p.name.startswith(reg.name + ".tmp.")]
    assert leftovers == []                                # the mid-flight tmp is cleaned up too, no debris
