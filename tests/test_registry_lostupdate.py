"""Registry lost-update regression (tool-registry-lost-update): the bare _load → modify → _save window is entirely unlocked;
the later persister overwrites with a whole-file copy that lacks the other's changes, silently erasing the earlier persister's
registration — the clone database exists server-side but is invisible to lab stats, and cleanup (sweep) misses it too.
The replica experiment reproduces this deterministically: after B contains B_db → A does a full write from the stale copy
→ B_db vanishes (LOST UPDATE: True).

tool-w3-flock only wrapped the high-level read-modify-write flows (clone/drop_db/_upsert/...) in flock; the bare _load/_save
pair remained outside the lock — when two writers' windows overlap the lock cannot help (the lock is already released between the two calls).

Fix (CAS lost-update protection): _load returns a read fingerprint (_StampedDict._read_fp, ==/json semantics fully
isomorphic to a plain dict); _save compares the current file fingerprint inside the <registry>.lock and, on mismatch (stale
copy), refuses the full write and raises RuntimeError — the silent erase becomes a loud failure; the fingerprint comparison
and os.replace complete under the same lock, so the window is atomic to other processes. _registry_lock became reentrant per
thread (flows already holding the lock take the same lock again inside _save; re-entry only counts instead of re-flocking,
otherwise self-deadlock; the count lives in threading.local, threads remain truly mutually exclusive).

Regressions in this file (fork context, isomorphic to real multi-process writers; no running PG):
  - stale full write rejected: raises and the other's registration survives intact (old implementation silently erased);
  - truly overlapping dual writers: exactly one succeeds and one fails, the persisted content = the winner's version (old implementation: last writer wins, first writer loses);
  - in-lock flows do not deadlock: clone/_upsert/_set_status/drop_db keep working after _save takes the lock;
  - compatibility: hand-built fingerprint-less dicts still write directly (seeding/rescue scenarios); the remedy path named
    by the error (reload → modify → save under the lock) really works.
"""
import multiprocessing as mp

import pytest

from pgtuner import instance
from pgtuner.instance import Instance

CTX = mp.get_context("fork")            # child processes inherit the monkeypatches and already-imported modules


def _seed(reg, clones=None):
    """Put a lab instance in the registry (clone/drop_db no longer touch PG once _run_sql is stubbed)."""
    entry = dict(name="lab", bindir="/nonexistent", pg_version="20devel", build_type="release",
                 data_dir="", port=5599, socket_dir="/tmp", status="running", managed=True)
    instance._save({"instances": [entry], "clones": clones or {"lab": ["seed_c0deadbee"]}}, reg)


def _noop_run_sql(name, dbname, sql, registry_file=None):
    """Stub: skips terminate/CREATE/DROP (the subject under test is the registry read-modify-write, not the DDL)."""
    pass


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "r.json"
    _seed(path)
    monkeypatch.setattr(instance, "_run_sql", _noop_run_sql)
    return path


# ---------------------------- stale full write: rejected instead of silently erased ----------------------------

def test_stale_full_file_save_rejected_and_keeps_other_writer(reg):
    """The defect itself (in-process replay of the replica experiment): A reads an old copy → B persists B_db → A does a full write from the stale copy.

    Old implementation: A's _save unconditionally replaces the whole file, B_db vanishes (the clone database leaks as an off-ledger orphan).
    After the fix: A's _save must raise RuntimeError (fingerprint mismatch); B_db survives intact and A_db must not slip in."""
    reg_a = instance._load(reg)                     # A reads an old copy (with fingerprint)
    fresh = instance._load(reg)                     # B: another process reads-modifies-writes and persists
    fresh["clones"]["lab"].append("B_db")
    instance._save(fresh, reg)
    assert instance.pool_stats(reg)["lab"] == ["seed_c0deadbee", "B_db"]

    reg_a["clones"]["lab"].append("A_db")           # A still modifies its stale copy
    with pytest.raises(RuntimeError, match="tool-registry-lost-update"):
        instance._save(reg_a, reg)                  # full write refused, B_db no longer silently erased
    assert instance.pool_stats(reg)["lab"] == ["seed_c0deadbee", "B_db"]   # the other's registration survives


# ---------------------------- truly overlapping dual writers: exactly one commits ----------------------------

def _overlap_worker(reg_str, tag, barrier, q):
    """Bare writers isomorphic to the defect scene: each _loads first (before the barrier), then both _save at once — truly overlapping windows."""
    reg = instance._load(reg_str)
    reg["clones"]["lab"].append(f"{tag}_db")
    barrier.wait(timeout=30)                        # persisting allowed only after both hold a v0 copy
    try:
        instance._save(reg, reg_str)
        q.put((tag, "ok", None))
    except RuntimeError as e:
        q.put((tag, "stale", str(e)))


def test_overlapping_writers_exactly_one_commits(reg):
    """Both processes finish reading before the other persists (overlapping windows): there must be exactly one ok and one stale;
    final state = the winner's version (the seed + one winner registration each; the loser must not slip in).

    Old implementation: the later persister unconditionally overwrote the whole file → the earlier persister's registration was erased (LOST UPDATE: True)."""
    barrier, q = CTX.Barrier(2), CTX.Queue()
    procs = [CTX.Process(target=_overlap_worker, args=(str(reg), tag, barrier, q))
             for tag in ("A", "B")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"写者异常退出: exitcode={p.exitcode}"
    outcomes = {q.get(timeout=5) for _ in range(2)}
    tags_ok = {t for t, outcome, _ in outcomes if outcome == "ok"}
    assert len(tags_ok) == 1, f"恰一提交成功，实得: {sorted(outcomes)}"      # double ok = the lost update is back
    assert all(outcome != "ok" or err is None for _, outcome, err in outcomes)
    winner = tags_ok.pop()
    assert instance.pool_stats(reg)["lab"] == ["seed_c0deadbee", f"{winner}_db"]


# ---------------------------- after _save takes the lock, in-lock flows do not self-deadlock ----------------------------

def _locked_flow_worker(reg_str, q):
    """The full in-lock flow family: clone (in-lock _load→modify→_save) + _upsert + _set_status + drop_db.

    If _registry_lock were not reentrant, these flows would self-deadlock where _save takes the lock a second time → this worker would never exit."""
    db = instance.clone("lab", "tpl", registry_file=reg_str)
    inst = Instance(name="lab", bindir="/nonexistent", pg_version="20devel",
                    build_type="release", data_dir="", port=5599)
    instance._upsert(inst, reg_str)
    instance._set_status("lab", "stopped", reg_str)     # the real write path that persists only when the value changes
    instance.drop_db("lab", db, registry_file=reg_str)
    q.put(instance.pool_stats(reg_str))


def test_locked_flows_survive_reentrant_save_lock(reg):
    """Each of clone/_upsert/_set_status/drop_db's "in-lock _save" must still complete after _save takes the lock itself
    (a join timeout means deadlock); in the final state the pool is emptied of the flow's own clone and the instance status is updated."""
    q = CTX.Queue()
    p = CTX.Process(target=_locked_flow_worker, args=(str(reg), q))
    p.start()
    p.join(timeout=60)
    if p.is_alive():                                    # deadlock: wait no longer, kill and fail
        p.terminate()
        p.join(timeout=10)
        pytest.fail("锁内流在 _save 取锁处自死锁（_registry_lock 重入失效）")
    assert p.exitcode == 0, f"锁内流异常退出: exitcode={p.exitcode}"
    assert q.get(timeout=5) == {"lab": ["seed_c0deadbee"]}   # the flow's own clone left the pool; the seed intact
    assert instance.get("lab", reg).status == "stopped"


# ---------------------------- compatibility and the remedy path ----------------------------

def test_unstamped_hand_built_dict_still_writes(tmp_path):
    """Hand-built plain dicts (no read fingerprint, seeding/rescue scenarios) do not trigger CAS and still write directly — including new-file creation."""
    reg = tmp_path / "hand.json"
    instance._save({"instances": [], "clones": {"lab": ["tpl_c0123abcd"]}}, reg)
    assert instance._load(reg) == {"instances": [], "clones": {"lab": ["tpl_c0123abcd"]}}
    instance._save({"instances": [], "clones": {}}, reg)
    assert instance._load(reg)["clones"] == {}


def test_remedy_path_reload_and_save_under_lock_works(reg):
    """The remedy path named by the error really works: after a conflict, reload → modify → save under the lock; both registrations coexist (rather than one-or-the-other)."""
    reg_a = instance._load(reg)
    fresh = instance._load(reg)
    fresh["clones"]["lab"].append("B_db")
    instance._save(fresh, reg)
    with pytest.raises(RuntimeError, match="重新 _load"):
        instance._save(reg_a, reg)
    with instance._registry_lock(reg):               # following the error's guidance: reload the latest under the lock
        latest = instance._load(reg)
        latest["clones"]["lab"].append("A_db")
        instance._save(latest, reg)
    assert instance.pool_stats(reg)["lab"] == ["seed_c0deadbee", "B_db", "A_db"]
