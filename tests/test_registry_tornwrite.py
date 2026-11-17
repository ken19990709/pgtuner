"""Registry torn-write regression (tool-registry-torn-write): _save used to be open('w') truncate + a single
write (no lock, no tmp+rename). When concurrent clone/release interleave, readers between the truncation and the finished
write saw an empty/half file — the regrace2 sampler measured 2 torn reads out of 2580 (len=1749 half JSON, len=0 empty file);
when that reader happened to be `lab release`'s DSN resolution (drop_db → _run_sql → dsn_of → get → _load, unlocked
read), release crashed rc=1 before the DROP executed → the clone database leaked server-side with the registration left
behind (incident: scratch_tpl_c3af9ae3f leaked on p3arbiter, returned afterwards by a manual lab release).

Fix (same root and mechanism as tool-w3-flock): _save now does same-directory tmp + fsync + os.replace atomic swap-in
— readers at any moment see only the complete old or new JSON, and release's read path can no longer hit the truncation
window. This file regresses along the two paths specific to this defect (the fork context is isomorphic to the real
multi-process `pgtuner lab clone/release` storm; stubbing happens at the pg connection layer rather than _run_sql,
preserving drop_db/clone's real bodies — the crash point dsn_of → get → _load executes as-is; no running PG needed):
  - release side: seed clones returned one by one amid a write storm, the releaser exits with zero exceptions (in the
    old implementation any _load that read truncated JSON raised RuntimeError("实例注册表损坏") → release rc=1, DROP
    not executed → leak + leftover registration);
    and the final clone pool must be empty (a leftover registration is the ledger half of the leak incident).
  - sampler side: a raw-bytes reader isomorphic to the evidence sampler (bypassing _load, reading the file body directly
    at 5ms intervals) sees 0 empty files / 0 broken JSON during the write storm (old measurement: 2/2580).
"""
import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from pgtuner import instance

CTX = mp.get_context("fork")            # child processes inherit the monkeypatches and already-imported modules

WRITE_ROUNDS = 50                        # clone+release rounds per writer (2 atomic persists per round)
SEEDS_N = 40                             # number of seed clones for the releaser to return one by one


class _FakeConn:
    """_run_sql only uses close() again (execute is already stubbed; the cursor is never reached)."""
    def close(self):
        pass


def _fake_connect_rw(dsn=None, autocommit=True):
    return _FakeConn()


def _fake_execute(conn, sql):
    return None


def _seed(reg, clones=None):
    """Put a lab instance in the registry (with the pg connection layer stubbed, clone/drop_db no longer touch real PG)."""
    entry = dict(name="lab", bindir="/nonexistent", pg_version="20devel", build_type="release",
                 data_dir="", port=5599, socket_dir="/tmp", status="running", managed=True)
    instance._save({"instances": [entry], "clones": clones or {}}, reg)


def _churn_worker(reg_str, rounds):
    """Writer: repeatedly clone → release (two in-lock read-modify-write + _save persists per iteration)."""
    for _ in range(rounds):
        db = instance.clone("lab", "tpl", registry_file=reg_str)
        instance.drop_db("lab", db, registry_file=reg_str)


def _release_worker(reg_str, dbnames):
    """Releaser: drop_db one by one; each call internally goes through dsn_of → get → _load three times (the evidence crash point).

    In the old implementation, any read of truncated/half JSON → RuntimeError("实例注册表损坏") → the child process exits non-zero
    (real world = release rc=1, DROP not executed, the clone database leaked server-side with the registration left behind)."""
    for db in dbnames:
        instance.drop_db("lab", db, registry_file=reg_str)


def _sampler_worker(reg_str, stop_path, out_path):
    """Isomorphic to the evidence sampler: during the storm, read the registry's raw bytes at 5ms intervals (bypassing _load),
    record torn events (empty file/broken JSON) with their lengths into out_path, and write {"reads", "torn"} before exiting."""
    torn, reads = [], 0
    reg, stop = Path(reg_str), Path(stop_path)
    while not stop.exists():
        raw = ""
        try:
            raw = reg.read_text(encoding="utf-8")
            json.loads(raw)
            reads += 1
        except FileNotFoundError:
            pass                           # not yet written for the first time
        except Exception as e:             # JSONDecodeError (len=0 / half-written) = torn window
            torn.append({"len": len(raw), "err": f"{type(e).__name__}: {e}"})
        time.sleep(0.005)
    Path(out_path).write_text(json.dumps({"reads": reads, "torn": torn}), encoding="utf-8")


def _run_all(procs):
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0, f"worker 异常退出: exitcode={p.exitcode}（读者撞上截断空窗）"


@pytest.fixture
def reg(tmp_path, monkeypatch):
    path = tmp_path / "r.json"
    _seed(path)
    # stub the connection layer rather than _run_sql: drop_db/clone's real bodies (including the crash point dsn_of) execute as-is
    monkeypatch.setattr(instance.pg, "connect_rw", _fake_connect_rw)
    monkeypatch.setattr(instance.pg, "execute", _fake_execute)
    return path


# ---------------------------- release side: returns amid the storm with zero crashes, zero leftovers ----------------------------

def test_release_survives_write_storm_and_leaks_nothing(reg):
    """Amid a storm of 4 writers × 50 rounds of clone+release, the releaser returns 40 seed clones one by one:

    The releaser must exit with zero exceptions — the old implementation's _save truncated first via open('w') then wrote;
    drop_db's DSN resolution (dsn_of → get → _load) hitting the window meant "实例注册表损坏", DROP not executed →
    the clone database leaked server-side + leftover registration; the final clone pool must be empty (a leftover registration is the ledger half of the leak incident)."""
    seeds = [f"tpl_c{i:08x}" for i in range(SEEDS_N)]
    _seed(reg, clones={"lab": list(seeds)})
    writers = [CTX.Process(target=_churn_worker, args=(str(reg), WRITE_ROUNDS))
               for _ in range(4)]
    releaser = CTX.Process(target=_release_worker, args=(str(reg), seeds))
    _run_all(writers + [releaser])
    assert instance.pool_stats(reg) == {"lab": []}     # no ghost registrations (= no on-ledger leak)


# ---------------------------- sampler side: zero torn reads for the raw-bytes reader ----------------------------

def test_raw_reader_never_sees_torn_registry(reg, tmp_path):
    """A raw-bytes reader isomorphic to the evidence sampler (5ms intervals, bypassing _load) sees
    0 empty files / 0 broken JSON during the write storm (old measurement: 2 of 2580 reads: len=1749 half-written, len=0 empty).

    After the tmp + os.replace atomic swap-in, a reader at any moment sees only the complete old or new JSON."""
    stop = tmp_path / "sampler.stop"
    out = tmp_path / "sampler.json"
    writers = [CTX.Process(target=_churn_worker, args=(str(reg), WRITE_ROUNDS))
               for _ in range(4)]
    # daemon: even if an assertion fails early, interpreter exit will not hang on the never-stopping sampler
    sampler = CTX.Process(target=_sampler_worker, args=(str(reg), str(stop), str(out)),
                          daemon=True)
    for p in writers + [sampler]:
        p.start()
    try:
        for p in writers:
            p.join(timeout=120)
            assert p.exitcode == 0, f"写者异常退出: exitcode={p.exitcode}"
    finally:
        stop.touch()                                   # writers done (same as storm.sh's sampler.stop)
        sampler.join(timeout=30)
    assert sampler.exitcode == 0, "采样器异常退出"
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["reads"] > 0, "采样器一次完整读都没有（风暴没跑起来？）"
    assert report["torn"] == [], f"读到撕裂注册表: {report['torn'][:3]}"
