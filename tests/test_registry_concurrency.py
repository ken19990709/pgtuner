"""Registry concurrency regression (tool-w3-flock): the lab clone pool's unlocked read-modify-write + non-atomic writes once produced,
under a P=4 storm, ghost registrations (database already dropped, registration left behind) and orphan databases (database present,
registration overwritten away), making lab stats untrustworthy.

The same class of race is reproduced at the registry layer (monkeypatch away _run_sql, no running PG; multiprocessing uses the
fork context, isomorphic to the real multi-process `pgtuner lab clone/release` storm):
  - concurrent clones: every registration must survive (lost registration → orphan database);
  - concurrent clone + release: a pool exit must not be resurrected by a concurrent registration write (resurrection → ghost registration);
  - concurrent _save + reads: readers must only ever see complete JSON (no half-written files).
"""
import json
import multiprocessing as mp
import threading

import pytest

from pgtuner import instance

CTX = mp.get_context("fork")            # child processes inherit the monkeypatches and already-imported modules


def _seed(reg, clones=None):
    """Put a lab instance in the registry (clone/drop_db no longer touch PG once _run_sql is stubbed)."""
    entry = dict(name="lab", bindir="/nonexistent", pg_version="20devel", build_type="release",
                 data_dir="", port=5599, socket_dir="/tmp", status="running", managed=True)
    instance._save({"instances": [entry], "clones": clones or {}}, reg)


def _noop_run_sql(name, dbname, sql, registry_file=None):
    """Stub: skips terminate/CREATE/DROP (the subject under test is the registry read-modify-write, not the DDL)."""
    pass


def _clone_worker(reg_str, rounds):
    for _ in range(rounds):
        instance.clone("lab", "tpl", registry_file=reg_str)


def _release_worker(reg_str, dbnames):
    for db in dbnames:
        instance.drop_db("lab", db, registry_file=reg_str)


def _run_all(procs):
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker 异常退出: exitcode={p.exitcode}"


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "r.json"
    _seed(path)
    monkeypatch.setattr(instance, "_run_sql", _noop_run_sql)
    return path


# ---------------------------- concurrent clones: zero registration loss ----------------------------

def test_concurrent_clones_keep_all_registrations(reg):
    """8 processes × 6 concurrent clone registrations: the pool must hold exactly 48 (old implementation's last-writer-wins → lost registrations)."""
    procs = [CTX.Process(target=_clone_worker, args=(str(reg), 6)) for _ in range(8)]
    _run_all(procs)
    pool = instance.pool_stats(reg)["lab"]
    assert len(pool) == 48
    assert len(set(pool)) == 48                                # and no duplicate registrations


def test_concurrent_clones_do_not_clobber_other_instances(reg):
    """base2's registration storm must not overwrite other instances' registrations (the full rewrite once affected all instances)."""
    _seed(reg, clones={"lab": [], "p2sje": ["colleague_c1a2b3c4"]})
    procs = [CTX.Process(target=_clone_worker, args=(str(reg), 5)) for _ in range(8)]
    _run_all(procs)
    stats = instance.pool_stats(reg)
    assert stats["p2sje"] == ["colleague_c1a2b3c4"]            # the colleague's registration survives intact
    assert len(stats["lab"]) == 40


# ---------------------------- concurrent clone + release: no ghosts, no orphans ----------------------------

def test_concurrent_clone_and_release_no_ghost_no_loss(reg):
    """8 seed registrations: 3 processes each release 2 (6 total), 4 processes each newly register 3 → the final state is exactly 2 survivors + 12 new.

    Old implementation: a concurrent registration write would resurrect already-released seeds (ghosts), or conversely overwrite away new registrations (orphans)."""
    seeds = [f"tpl_c{i:08x}" for i in range(8)]
    _seed(reg, clones={"lab": list(seeds)})
    releasers = [CTX.Process(target=_release_worker,
                             args=(str(reg), seeds[i * 2:i * 2 + 2])) for i in range(3)]
    cloners = [CTX.Process(target=_clone_worker, args=(str(reg), 3)) for _ in range(4)]
    _run_all(releasers + cloners)
    pool = set(instance.pool_stats(reg)["lab"])
    assert len(pool) == 14                                     # 2 survivors + 12 new, not one more or less
    assert set(seeds[6:]) <= pool                              # surviving seeds still registered
    assert not (set(seeds[:6]) & pool)                         # ghost check: released seeds must not resurrect
    new_only = pool - set(seeds)
    assert len(new_only) == 12                                 # orphan check: not one new registration lost


# ---------------------------- _save atomicity: readers never see half-written files ----------------------------

def test_save_atomic_for_concurrent_readers(reg, tmp_path):
    """A writer thread repeatedly rewrites the whole file with two states while a reader keeps reading: every read must yield one complete, valid state.

    The old implementation's path.write_text truncates before writing, so readers could hit an empty file / half-written JSON (JSONDecodeError)."""
    reg = tmp_path / "atomic.json"
    _seed(reg)
    version_a = {"instances": [], "clones": {"lab": ["tpl_cdeadbeef"]}}
    version_b = {"instances": [], "clones": {"lab": ["tpl_cdeadbeef", "tpl_cfeedface"]}}
    stop = threading.Event()
    first_write = threading.Event()                            # skip the seed state: start reading only after the first write completes
    errors = []

    def writer():
        i = 0
        while not stop.is_set() and i < 400:
            instance._save(version_a if i % 2 == 0 else version_b, reg)
            first_write.set()
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    first_write.wait(timeout=10)
    try:
        for _ in range(400):
            try:
                data = json.loads(reg.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue                                       # not yet written for the first time
            except json.JSONDecodeError as e:                  # half-written file = non-atomic write
                errors.append(f"读到损坏 JSON: {e}")
                break
            if data not in (version_a, version_b):
                errors.append(f"读到中间态: {data}")
                break
    finally:
        stop.set()
        t.join(timeout=10)
    assert not errors, errors[0]


# ---------------------------- lock side effects observable ----------------------------

def test_registry_lock_sidecar_next_to_registry(reg):
    """After a clone, a <name>.lock sidecar file should appear next to the registry (the flock mutual-exclusion point, observable and debuggable)."""
    new_db = instance.clone("lab", "tpl", registry_file=reg)
    assert (reg.parent / (reg.name + ".lock")).exists()
    assert instance.pool_stats(reg) == {"lab": [new_db]}
