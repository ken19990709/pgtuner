"""Instance registry and clone pool: PG lifecycle management (initdb / pg_ctl /
pg_isready) + fast template-database cloning (eliminates repeated initdb/init).

Registry (INSTANCE_REGISTRY; all reads/writes go through _load/_save; the registry_file
parameter can be overridden for test injection of tmp_path):
    {"instances": [Instance.to_dict() ...], "clones": {instance_name: [clone db names...]}}

Concurrency essentials (tool-w3-flock / tool-registry-lost-update fixes):
  - _save is atomic on disk (tmp file + os.replace): readers only ever see the complete
    old or new JSON, never a half-written file;
  - every "read-modify-write" (clone registration / drop_db pool exit / _upsert /
    _set_status / destroy) holds an flock on <registry>.lock for cross-process mutual
    exclusion throughout, and re-_loads inside the lock: concurrent multi-process
    clone/release no longer loses registrations (orphan databases) or resurrects
    returned entries (ghost registrations). The DDL itself (CREATE/DROP DATABASE)
    stays outside the lock — the registry is a single file shared by all instances;
    locking the DDL would serialize other instances' clones too, while registry
    consistency only requires "re-read inside the lock before modifying".
  - bare intervals that bypass the high-level functions and do a direct _load ->
    modify -> _save cannot be covered by the lock (the lock is already released
    between the two calls); they are backstopped by _save's lost-update protection:
    _load carries back the "file fingerprint at read time", and _save compares the
    current file fingerprint inside the lock, refusing to write and raising when it
    differs (the copy is stale) — the later writer prefers a loud failure over
    silently wiping the other party's registrations with a whole-file copy that lacks
    their changes (tool-registry-lost-update).

Clone ownership and audit (tool-w1-clone-vanished fix):
  - clone() records ownership in the registry's top-level "clone_meta"
    ({instance: {db: {owner, created_at}}}, parallel to the "clones" db-name list — the
    latter's shape is pinned by pool_stats/sweep/existing tests, so it stays); owner
    defaults to the current operator identity (PGTUNER_AGENT > USER > LOGNAME, see
    _actor).
  - drop_db() validates ownership for clones with a recorded owner: a non-owner release
    is refused outright (loud failure + release-denied audit); only force=True lets the
    override through (still audited). Legacy entries without ownership records /
    databases outside the pool are not intercepted (backward compatible).
  - session-holding anchor (tool-huntA-clone-wipe, same-user multi-session protection):
    parallel sessions under the same OS user (another terminal / another agent track)
    share the same owner string, which the ownership check cannot stop — clone()
    additionally records a "claim" (the cloning process's ancestor chain
    [(pid, starttime), ...], see _ancestor_chain); at release time, if any process in
    the chain is still alive and not inside the current releasing process tree (the
    holding session still running), the release is refused (--force lets the override
    through). Holding sessions all exited (ancestors dead / PID reuse) -> the leftover
    clone is returned as usual; bulk cleanup such as sweep goes through the drop_db
    single point and likewise can only clean its own session's clones.
  - every clone lifecycle event (clone/release/release-denied/destroy) is appended to
    <registry>.audit.jsonl (single-line JSON, always written inside _registry_lock, so
    lines never interleave) — who touched whose clone and when is one lookup away;
    attribution no longer depends on guesswork.

Design essentials:
  - build_type is decided by <bindir>/pg_config --configure (containing --enable-debug
    / --enable-cassert -> debug, otherwise release); pg_version comes from the
    --version output ("PostgreSQL 19devel" -> "19devel")
  - subprocess commands always use absolute paths (<bindir>/<tool>) with bounded
    timeouts; failures uniformly raise RuntimeError with the last few lines of output
  - register only registers externally installed instances (managed=False, data_dir
    left empty): such instances raise RuntimeError on start/stop for lack of a data
    directory; destroy requires force=True to unregister, and never deletes someone
    else's (external) data directory regardless of force
  - template databases finish with datallowconn=false (connections disabled) -> no
    active connections, usable as a CREATE DATABASE ... TEMPLATE source at any time;
    a clone is a single DDL (target: hundreds of milliseconds)

Public functions follow the "A. instance.py" section of the historical dev
contracts; per the global
convention on overridable parameters, lifecycle/clone functions each add an optional
registry_file=None (defaulting to the paths constant, leaving contract call sites
untouched).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

from . import pg
from .paths import INSTANCE_DATA_ROOT, INSTANCE_REGISTRY

SOCKET_DIR = os.environ.get("PGTUNER_SOCKET_DIR", "/tmp")   # same assumption as pg.py; overridable via env (v0.11.0)
PORT_START = 5433              # start searching here when port is unspecified (5432 is left for the system PG)
PORT_ATTEMPTS = 100
READY_TIMEOUT_S = 30.0         # upper bound of the pg_isready readiness wait after create/start
POLL_INTERVAL_S = 0.5
CTL_WAIT_S = 60                # pg_ctl -w -t argument: seconds to wait for start/stop to complete
STOP_MODES = ("smart", "fast", "immediate")


# ---------------------------- data model ----------------------------

@dataclass
class Instance:
    """One registered PG instance (persisted in the registry in to_dict() form)."""
    name: str
    bindir: str
    pg_version: str                       # e.g. "19devel"
    build_type: str                       # "release"|"debug" (decided by pg_config --configure)
    data_dir: str                         # left empty for externally registered instances (their data directory is never touched)
    port: int
    socket_dir: str = "/tmp"
    status: str = "unknown"               # running|stopped|unknown
    managed: bool = True                  # created by this tool via initdb; False for registered ones

    def to_dict(self) -> dict:
        return {
            "name": self.name, "bindir": self.bindir, "pg_version": self.pg_version,
            "build_type": self.build_type, "data_dir": self.data_dir, "port": self.port,
            "socket_dir": self.socket_dir, "status": self.status, "managed": self.managed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Instance":
        """Restore from a registry dict (unknown keys ignored, forward compatible)."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------- registry read/write ----------------------------

_ABSENT_FP = "<absent>"                 # fingerprint sentinel for "registry file does not exist yet" at _load time


class _StampedDict(dict):
    """Return value of _load: content is fully isomorphic to a plain dict (== / json
    serialization / item access all unaffected; the existing
    `assert _load(...) == {...}` contract is unchanged), only carrying one extra
    attribute _read_fp — the registry file's content fingerprint at the moment of the
    read, used by _save for lost-update detection (tool-registry-lost-update)."""

    __slots__ = ("_read_fp",)


def _fingerprint(path: Path) -> str:
    """Current content fingerprint of the registry file (sha256 hex, same criterion as the one _load stamps); file missing -> the sentinel."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _ABSENT_FP
    return hashlib.sha256(raw).hexdigest()


def _load(registry_file: str | Path | None = None) -> dict:
    """Read the registry; a missing file yields the skeleton {"instances": [], "clones": {}}.

    The return value carries the read fingerprint _read_fp (see _StampedDict): _save
    uses it to tell whether "this in-memory copy has been overtaken by someone else's
    write" and refuses the full write when the fingerprint mismatches (lost-update
    protection)."""
    path = Path(registry_file) if registry_file is not None else INSTANCE_REGISTRY
    reg = _StampedDict()
    if not path.exists():
        reg.update(instances=[], clones={})
        reg._read_fp = _ABSENT_FP
        return reg
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"实例注册表损坏（非 JSON）: {path}: {e}") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"实例注册表格式错误（顶层应为 object）: {path}")
    reg.update(instances=list(data.get("instances", [])),
               clones=dict(data.get("clones", {})))
    if "clone_meta" in data:            # clone ownership (tool-w1): carried out only if present in the file; old registries never grow the key out of thin air
        reg["clone_meta"] = dict(data.get("clone_meta") or {})
    reg._read_fp = hashlib.sha256(raw).hexdigest()
    return reg


def _save(reg: dict, registry_file: str | Path | None = None) -> None:
    """Write the registry (parent dirs created automatically): tmp file + fsync +
    os.replace atomic replacement.

    A reader (_load) only ever sees the previous or this complete JSON, never a
    half-written file; the tmp name carries a random suffix so concurrent writers do
    not stomp each other's tmp files.

    Lost-update protection (tool-registry-lost-update): if reg comes from _load (with
    a read fingerprint), the current file fingerprint is first compared inside the
    <registry>.lock — a mismatch means someone else wrote after this copy was read,
    and a full write of this copy would silently wipe their registrations, so it is
    refused with a RuntimeError (loud failure beats silent loss; reload-modify-save
    must happen inside _registry_lock). A hand-built plain dict has no fingerprint and
    is written directly per the original semantics. Fingerprint comparison and
    os.replace complete inside the same lock; the CAS window is atomic to other
    processes."""
    path = Path(registry_file) if registry_file is not None else INSTANCE_REGISTRY
    read_fp = getattr(reg, "_read_fp", None)
    with _registry_lock(registry_file):
        if read_fp is not None and read_fp != _fingerprint(path):
            raise RuntimeError(
                f"注册表在本副本读取之后已被其他进程更新，拒绝整文件覆写"
                f"（否则会静默抹掉对方变更，tool-registry-lost-update）: {path}"
                f"；请重新 _load 最新内容后再改再存，读改写全程置于 _registry_lock 内")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(reg, ensure_ascii=False, indent=2) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)             # atomic: either the old file or the new file, no intermediate state
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise


_held_locks = threading.local()         # per-thread {lock absolute path: hold depth} — same-thread re-entry only counts


@contextlib.contextmanager
def _registry_lock(registry_file: str | Path | None = None):
    """Cross-process mutual exclusion for registry read-modify-write: flock
    <registry path>.lock (exclusive, blocking wait).

    Inside the lock you must re-_load (only then do you hold the latest state
    committed by others), and _save before releasing; slow operations such as DDL /
    probing stay outside the lock to avoid turning the all-instance shared registry
    into a long serialization point. Reentrant within a thread: _save itself also
    takes this lock (mutual exclusion for the CAS compare+replace window), so locked
    flows (clone/_upsert/... -> _save) only count depth instead of flocking again on
    re-entry, otherwise self-deadlock; the counter lives in threading.local and is not
    shared between threads — multiple threads in the same process still get true
    mutual exclusion via flock (flock conflicts are per open file description,
    independent of process)."""
    path = Path(registry_file) if registry_file is not None else INSTANCE_REGISTRY
    lock_path = path.with_name(path.name + ".lock")
    key = os.path.abspath(os.fspath(lock_path))
    held = getattr(_held_locks, "d", None)
    if held is None:
        held = {}
        _held_locks.d = held
    if held.get(key, 0):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        held[key] = 1
        try:
            yield
        finally:
            held[key] = 0
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _upsert(inst: Instance, registry_file: str | Path | None = None) -> None:
    """Insert/replace one instance record by name (lock held for the whole read-modify-write)."""
    with _registry_lock(registry_file):
        reg = _load(registry_file)
        entry = inst.to_dict()
        for i, old in enumerate(reg["instances"]):
            if old.get("name") == inst.name:
                reg["instances"][i] = entry
                break
        else:
            reg["instances"].append(entry)
        _save(reg, registry_file)


# Instance names become path components (INSTANCE_DATA_ROOT/<name>) and SQL
# identifiers, so a conservative charset is enforced up front (adversarial review
# R1-D: a name like "../../home/x" escaped the data root and destroy would rmtree it).
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _validate_name(name: str) -> None:
    """Reject names that are not safe as path components / printable identifiers."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"实例名 {name!r} 不合法:须以字母/数字开头,仅含 [A-Za-z0-9_.-],长度≤64"
            "(名字会成为数据目录路径的一部分,拒绝路径穿越)")


def _require_free_name(name: str, registry_file: str | Path | None = None) -> None:
    _validate_name(name)
    if any(d.get("name") == name for d in _load(registry_file)["instances"]):
        raise ValueError(f"实例名已注册: {name}")


def _set_status(name: str, status: str, registry_file: str | Path | None = None) -> None:
    """Sync the status field into the registry (no write when the value is unchanged; lock held for the whole read-modify-write)."""
    with _registry_lock(registry_file):
        reg = _load(registry_file)
        for d in reg["instances"]:
            if d.get("name") == name:
                if d.get("status") != status:
                    d["status"] = status
                    _save(reg, registry_file)
                return


# ---------------------------- install probing (pure functions + pg_config) ----------------------------

def parse_pg_version(text: str) -> str:
    """'PostgreSQL 19devel' -> '19devel' (takes the version field after the product name)."""
    parts = text.strip().split()
    if not parts:
        raise ValueError(f"无法解析 pg_config --version 输出: {text!r}")
    if parts[0].lower() == "postgresql" and len(parts) > 1:
        return parts[1]
    return parts[0]


def build_type_from_configure(text: str) -> str:
    """--configure output containing --enable-debug or --enable-cassert -> debug, otherwise release."""
    return "debug" if ("--enable-debug" in text or "--enable-cassert" in text) else "release"


def probe_install(bindir: str | Path) -> tuple[str, str]:
    """Probe the install via <bindir>/pg_config -> (pg_version, build_type). Raises ValueError if pg_config is missing/not executable."""
    pg_config = _tool(bindir, "pg_config")
    version = parse_pg_version(_run([pg_config, "--version"], timeout=15).stdout)
    configure = _run([pg_config, "--configure"], timeout=15).stdout
    return version, build_type_from_configure(configure)


# ---------------------------- subprocess wrappers ----------------------------

def _tool(bindir: str | Path, tool: str) -> str:
    """Absolute path of <bindir>/<tool>; tolerates a --prefix style argument (falls
    back to <bindir>/bin/<tool> automatically); raises ValueError if neither exists
    or is not executable."""
    for cand in (Path(bindir) / tool, Path(bindir) / "bin" / tool):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    raise ValueError(f"{bindir} 下缺少可执行的 {tool}"
                     f"（期望 {Path(bindir) / tool} 或 {Path(bindir) / 'bin' / tool}）")


def _run(cmd: list[str], *, timeout: float = 60,
         ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    """Run a command (absolute path, bounded timeout); on failure raises RuntimeError with the last few lines of output."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"命令超时（>{timeout}s）: {' '.join(cmd)}") from None
    except FileNotFoundError:
        raise ValueError(f"可执行文件不存在: {' '.join(cmd)}") from None
    if proc.returncode not in ok_codes:
        tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-5:])
        raise RuntimeError(f"命令失败（rc={proc.returncode}）: {' '.join(cmd)}\n{tail}")
    return proc


def _isready_proc(bindir: str, socket_dir: str, port: int) -> bool:
    """pg_isready probe: rc==0 means accepting connections (non-zero is normal semantics, not a failure)."""
    try:
        proc = subprocess.run([_tool(bindir, "pg_isready"), "-h", socket_dir, "-p", str(port)],
                              capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def _ctl_running(data_dir: str, bindir: str) -> bool:
    """pg_ctl status: rc==0 means a server process is running (non-zero is normal semantics)."""
    try:
        proc = subprocess.run([_tool(bindir, "pg_ctl"), "-D", data_dir, "status"],
                              capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"pg_ctl status 超时: {data_dir}") from None
    return proc.returncode == 0


def _wait_ready(bindir: str, socket_dir: str, port: int, timeout: float = READY_TIMEOUT_S) -> None:
    """Poll pg_isready (0.5s interval) until the instance accepts connections; raises RuntimeError on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        if _isready_proc(bindir, socket_dir, port):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"实例 {timeout}s 内未就绪（socket={socket_dir} port={port}）")
        time.sleep(POLL_INTERVAL_S)


def find_free_port(bindir: str, socket_dir: str = SOCKET_DIR, start: int = PORT_START,
                   attempts: int = PORT_ATTEMPTS) -> int:
    """Find an unused port starting from start: no socket file present + pg_isready not accepting counts as free."""
    for port in range(start, start + attempts):
        if (Path(socket_dir) / f".s.PGSQL.{port}").exists():
            continue                          # socket file present -> already in use
        if _isready_proc(bindir, socket_dir, port):
            continue                          # a server is accepting connections -> already in use
        return port
    raise RuntimeError(f"自 {start} 起尝试 {attempts} 个端口均被占用")


def _append_conf(conf_path: Path, *, port: int, socket_dir: str = SOCKET_DIR) -> None:
    """Append three lines to postgresql.conf: disable TCP listening / pin the port / pin the socket directory."""
    lines = [f"listen_addresses = ''",
             f"port = {port}",
             f"unix_socket_directories = '{socket_dir}'"]
    with open(conf_path, "a", encoding="utf-8") as f:
        f.write("\n# --- pgtuner instance.py 追加 ---\n" + "\n".join(lines) + "\n")


# ---------------------------- lifecycle (contract A) ----------------------------

def register(name: str, bindir: str | Path, *, port: int,
             registry_file: str | Path | None = None) -> Instance:
    """Register an existing installation (no initdb, never touches the data directory, managed=False). Validates that pg_config exists and is executable."""
    _require_free_name(name, registry_file)
    pg_version, build_type = probe_install(bindir)
    inst = Instance(name=name, bindir=str(bindir), pg_version=pg_version, build_type=build_type,
                    data_dir="", port=int(port), socket_dir=SOCKET_DIR,
                    status="unknown", managed=False)
    _upsert(inst, registry_file)
    return inst


def create(name: str, bindir: str | Path, *, port: int | None = None,
           registry_file: str | Path | None = None,
           data_root: str | Path | None = None) -> Instance:
    """Create a new instance via initdb (managed=True): initdb -> append the three conf lines -> pg_ctl start -> wait ready -> register."""
    _require_free_name(name, registry_file)
    root = Path(data_root) if data_root is not None else INSTANCE_DATA_ROOT
    data_dir = root / name
    if data_dir.exists() and any(data_dir.iterdir()):
        raise ValueError(f"数据目录非空，拒绝 initdb: {data_dir}")
    pg_version, build_type = probe_install(bindir)
    if port is None:
        port = find_free_port(str(bindir), SOCKET_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    _run([_tool(bindir, "initdb"), "-D", str(data_dir),
          "--auth-local=trust", "--encoding=UTF8"], timeout=300)
    _append_conf(data_dir / "postgresql.conf", port=port)
    _run([_tool(bindir, "pg_ctl"), "-D", str(data_dir), "-w", "-t", str(CTL_WAIT_S),
          "-l", str(data_dir / "logfile"), "start"], timeout=CTL_WAIT_S + 30)
    _wait_ready(str(bindir), SOCKET_DIR, port, READY_TIMEOUT_S)
    inst = Instance(name=name, bindir=str(bindir), pg_version=pg_version, build_type=build_type,
                    data_dir=str(data_dir), port=port, socket_dir=SOCKET_DIR,
                    status="running", managed=True)
    _upsert(inst, registry_file)
    return inst


def get(name: str, registry_file: str | Path | None = None) -> Instance:
    """Get an instance; raises ValueError if absent."""
    for d in _load(registry_file)["instances"]:
        if d.get("name") == name:
            return Instance.from_dict(d)
    raise ValueError(f"实例未注册: {name}")


def list_instances(registry_file: str | Path | None = None) -> list[Instance]:
    """All instances (sorted by name, deterministic output)."""
    insts = (Instance.from_dict(d) for d in _load(registry_file)["instances"])
    return sorted(insts, key=lambda i: i.name)


def start(name: str, registry_file: str | Path | None = None) -> None:
    """pg_ctl start wrapper. Idempotent: returns directly if already accepting connections (pg_isready passes)."""
    inst = get(name, registry_file)
    if _isready_proc(inst.bindir, inst.socket_dir, inst.port):
        _set_status(name, "running", registry_file)
        return
    if not inst.data_dir:
        raise RuntimeError(f"实例 {name} 为外部注册（无 data_dir），无法 pg_ctl start")
    data_dir = Path(inst.data_dir)
    _run([_tool(inst.bindir, "pg_ctl"), "-D", str(data_dir), "-w", "-t", str(CTL_WAIT_S),
          "-l", str(data_dir / "logfile"), "start"], timeout=CTL_WAIT_S + 30)
    _wait_ready(inst.bindir, inst.socket_dir, inst.port, READY_TIMEOUT_S)
    _set_status(name, "running", registry_file)


def stop(name: str, *, mode: str = "fast", registry_file: str | Path | None = None) -> None:
    """pg_ctl stop wrapper (default fast). Idempotent: returns directly if already stopped."""
    if mode not in STOP_MODES:
        raise ValueError(f"无效的 stop 模式: {mode}（应为 smart|fast|immediate）")
    inst = get(name, registry_file)
    running = (_ctl_running(inst.data_dir, inst.bindir) if inst.data_dir
               else _isready_proc(inst.bindir, inst.socket_dir, inst.port))
    if not running:
        _set_status(name, "stopped", registry_file)
        return
    if not inst.data_dir:
        raise RuntimeError(f"实例 {name} 在运行但为外部注册（无 data_dir），无法 pg_ctl stop")
    _run([_tool(inst.bindir, "pg_ctl"), "-D", inst.data_dir,
          "-m", mode, "-w", "-t", str(CTL_WAIT_S), "stop"], timeout=CTL_WAIT_S + 30)
    _set_status(name, "stopped", registry_file)


def is_ready(name: str, registry_file: str | Path | None = None) -> bool:
    """pg_isready: whether the instance is accepting connections."""
    inst = get(name, registry_file)
    return _isready_proc(inst.bindir, inst.socket_dir, inst.port)


def ensure_running(name: str, registry_file: str | Path | None = None) -> Instance:
    """If stopped, start (including the readiness wait) then return the instance; if already running, return directly."""
    inst = get(name, registry_file)
    if _isready_proc(inst.bindir, inst.socket_dir, inst.port):
        return inst
    start(name, registry_file)
    return get(name, registry_file)


def _conninfo_quote(value: str) -> str:
    """Embed a value as ONE libpq conninfo keyword/value token: wrap in single
    quotes, backslash-escaping ' and \\ inside — semantically equivalent to
    psycopg2.extensions.make_dsn output (round-trips through psycopg2
    parse_dsn for the full test corpus; not byte-identical in general —
    make_dsn leaves a plain dbname unquoted — and not imported here because
    psycopg2 is an optional dependency of this module). Deliberately NOT
    double-quote doubling: libpq gives double quotes no grouping power in
    keyword/value strings, so a "..."-wrapped value with spaces still splits and
    injected host=/port= keys would go through."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def dsn_of(name: str, dbname: str, registry_file: str | Path | None = None) -> str:
    """Connection string of the instance: host=<socket_dir> port=<port> dbname=<dbname>.

    D5: dbname is caller-supplied (CLI --db) and is embedded as one quoted conninfo
    value (see _conninfo_quote) — a dbname such as 'x host=/tmp/evil port=1' can no
    longer inject conninfo keys: it stays a single dbname value."""
    inst = get(name, registry_file)
    return f"host={inst.socket_dir} port={inst.port} dbname={_conninfo_quote(dbname)}"


def destroy(name: str, *, force: bool = False, registry_file: str | Path | None = None) -> None:
    """stop -> delete the data directory -> unregister (along with its clone records).

    managed=False (externally registered): force=True is required to unregister, and
    its data directory is never deleted regardless of force.
    """
    inst = get(name, registry_file)
    if not inst.managed and not force:
        raise ValueError(f"实例 {name} 为外部注册（managed=False），注销需 destroy(force=True) 确认")
    if inst.managed:
        stop(name, registry_file=registry_file)                  # idempotent: passes directly if already stopped; a stop failure aborts the destroy
        shutil.rmtree(inst.data_dir, ignore_errors=True)
    with _registry_lock(registry_file):
        reg = _load(registry_file)
        meta_map = reg.get("clone_meta", {}).get(name, {})
        for dbname in reg["clones"].get(name, []):  # every clone removed along with the unregistration gets its own audit trail (tool-w1)
            _audit("destroy", name, dbname, owner=meta_map.get(dbname, {}).get("owner"),
                   registry_file=registry_file)
        reg["instances"] = [d for d in reg["instances"] if d.get("name") != name]
        reg["clones"].pop(name, None)
        if "clone_meta" in reg:
            reg["clone_meta"].pop(name, None)
        _save(reg, registry_file)


# ---------------------------- templates and clone pool ----------------------------

def _qid(name: str) -> str:
    """Double-quote escaping for an SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def _qstr(value: str) -> str:
    """Single-quote escaping for an SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _last_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1] if lines else "unknown error"


def _run_sql(name: str, dbname: str, sql: str,
             registry_file: str | Path | None = None) -> None:
    """Connect read-write to database dbname on instance name and execute a single SQL statement (autocommit); on failure re-raises as RuntimeError with the last error line."""
    conn = pg.connect_rw(dsn_of(name, dbname, registry_file))
    try:
        pg.execute(conn, sql)
    except Exception as e:
        raise RuntimeError(f"SQL 执行失败（{dbname}@{name}）: {_last_line(str(e))}\nSQL: {sql[:200]}") from None
    finally:
        conn.close()


def templates(name: str, registry_file: str | Path | None = None) -> list[str]:
    """List of template databases on the instance (datallowconn=false ones, ascending) —
    the single definition shared by lab templates / bootstrap / doctor (before v0.9.0
    this SQL was scattered in the CLI)."""
    rows = _db_inventory_rows(name, registry_file)
    return sorted(r["datname"] for r in rows if not r["allowconn"])


def _build_sealed_db(name: str, db: str, populate, registry_file=None) -> None:
    """Create the database -> populate (fill data) -> seal -> verify by reading back ->
    clean up the half-built product on failure.

    Common skeleton extracted from make_template in v0.9.0 (its public signature is
    unchanged): bootstrap needs to insert steps before sealing that cannot be
    expressed as a single SQL file, such as pgbench -i / CREATE EXTENSION; the
    callback populate(name, db, registry_file) carries them. Never leaves an unsealed
    template."""
    _run_sql(name, "postgres", f"CREATE DATABASE {_qid(db)}", registry_file)
    try:
        populate(name, db, registry_file)
        # templates disallow connections: only with no active connections can a database serve as a TEMPLATE source; takes effect immediately under autocommit
        _run_sql(name, "postgres",
                 f"UPDATE pg_database SET datallowconn = false WHERE datname = {_qstr(db)}",
                 registry_file)
        # seal verification by reading back: an unsealed template must not silently enter the clone pool
        conn = pg.connect_rw(dsn_of(name, "postgres", registry_file))
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT datallowconn FROM pg_database WHERE datname = {_qstr(db)}")
                row = cur.fetchone()
        finally:
            conn.close()
        if row is None or row[0]:
            raise RuntimeError(
                f"模板 {db} 密封校验失败: datallowconn 未变 false，模板不可靠")
    except Exception:
        with contextlib.suppress(Exception):
            # allow_any: this db was created by this very call seconds ago (a
            # half-built template), not a user database — cleanup must not be
            # blocked by the non-clone drop guard
            drop_db(name, db, registry_file, allow_any=True)
        raise


def make_template(name: str, template_db: str, setup_sql_file: str | Path,
                  registry_file: str | Path | None = None) -> None:
    """Build a clone template database: CREATE DATABASE -> connect to it and run
    setup_sql (table creation/pgbench data/trigger scenarios etc.) -> back in the
    postgres database UPDATE pg_database SET datallowconn=false (connections disabled,
    ensuring it can be cloned at any time).

    B1 fix (patch-trial): an empty/whitespace-only setup_sql is never sent to the
    server (psycopg2 execute("") errors; the old implementation therefore skipped
    sealing, leaving a fake "cloneable but unsealed" template); after sealing,
    datallowconn is read back and verified to be false; any failed step cleans up the
    half-built template before re-raising — never leaves an unsealed template (the
    v0.9.0 skeleton moved into _build_sealed_db; this function keeps only the
    read-the-SQL-file part)."""
    sql_path = Path(setup_sql_file)
    if not sql_path.is_file():
        raise ValueError(f"setup_sql 文件不存在: {setup_sql_file}")
    setup_sql = sql_path.read_text(encoding="utf-8")

    def _exec_sql(n: str, db: str, rf) -> None:
        # a single psycopg2 execute can run multiple statements (the whole setup_sql is sent in one shot)
        if setup_sql.strip():
            _run_sql(n, db, setup_sql, rf)

    _build_sealed_db(name, template_db, _exec_sql, registry_file)


def clone(name: str, template_db: str, new_db: str | None = None,
          registry_file: str | Path | None = None, *, owner: str | None = None) -> str:
    """Clone from a template database: connect to the postgres database and CREATE
    DATABASE ... TEMPLATE; register into the clone pool and return the new database
    name.

    Ownership record (tool-w1-clone-vanished): the clone records owner/created_at
    into the registry's "clone_meta" (owner defaults to the current operator identity,
    see _actor) — at release time non-owners are refused, so other agents' experiment
    subjects no longer vanish silently under parallel agents.

    Session-holding anchor (tool-huntA-clone-wipe): additionally records a "claim"
    (the current process's ancestor chain, see _ancestor_chain) — another parallel
    session under the same OS user (identical owner string) is refused at release
    because "the holding process is still running", so experiment subjects are no
    longer wiped out as collateral by a neighboring track's bulk cleanup."""
    if new_db is None:
        new_db = f"{template_db}_c{uuid.uuid4().hex[:8]}"
    if owner is None:
        owner = _actor()
    _run_sql(name, "postgres",
             f"CREATE DATABASE {_qid(new_db)} TEMPLATE {_qid(template_db)}", registry_file)
    # One clock read reused by both clone_meta.created_at and the audit ts
    # (determinism): two independent _now_iso() calls can straddle a whole-second
    # boundary, making the audit ts drift from created_at while tests assert exact
    # equality — a single captured timestamp keeps the pair always identical.
    created_ts = _now_iso()
    with _registry_lock(registry_file):        # lock held for the whole registration read-modify-write (re-read inside the lock, preventing concurrent lost registrations)
        reg = _load(registry_file)
        reg["clones"].setdefault(name, []).append(new_db)
        reg.setdefault("clone_meta", {}).setdefault(name, {})[new_db] = {
            "owner": owner, "created_at": created_ts,
            "claim": {"ancestors": _ancestor_chain()}}
        _save(reg, registry_file)
        _audit("clone", name, new_db, owner=owner, actor=owner,
               registry_file=registry_file, ts=created_ts)
    return new_db


def drop_db(name: str, dbname: str, registry_file: str | Path | None = None,
            *, actor: str | None = None, force: bool = False,
            allow_any: bool = False) -> None:
    """Disconnect all connections to the database -> DROP DATABASE IF EXISTS -> remove
    from the clone pool.

    Non-clone guard (adversarial review R1-D, tightened in R2: `lab release` could
    DROP any real database): unless force=True or allow_any=True, only databases
    REGISTERED in the clone pool may be dropped. Clone-name shape alone
    (<prefix>_c<8hex>) no longer suffices — real business databases named like
    snapshot_c20241116 would slip through the pattern (R2-C/R2-D); orphan cleanup
    goes through lab sweep (which validates the prefix against the instance's
    sealed templates) or --force. System databases (postgres/template0/template1)
    are refused even with force. allow_any is the internal-cleanup channel
    (make_template half-product rollback, sweep's template-validated orphans),
    never exposed on the CLI.

    Ownership guard (tool-w1-clone-vanished): for databases with a recorded owner in
    the clone pool, a non-owner release (actor defaults to the current operator
    identity, see _actor) is refused outright with a release-denied audit entry;
    force=True lets the override through (still audited, with a forced marker).
    Legacy entries without ownership records are not intercepted (backward
    compatible), but their releases are audited all the same.

    Session-holding guard (tool-huntA-clone-wipe): parallel sessions under the same OS
    user (another terminal / another agent track) share the same owner string and slip
    past the ownership check — a process-tree anchor is added on top: as long as the
    holding chain recorded at clone time (claim.ancestors) still has a live process
    that is not inside the current releasing process tree (the holding session still
    runs), the release is refused (--force lets the override through, still audited
    with held_by attribution). All holding sessions exited (ancestors all dead / PID
    reuse, see _foreign_holders) -> treated as a leftover clone and returned as
    usual."""
    if dbname in ("postgres", "template0", "template1"):
        raise RuntimeError(
            f"拒绝 DROP 系统库 {dbname}(任何旗标下都不放行——这不是克隆归还该碰的库)")
    if actor is None:
        actor = _actor()
    with _registry_lock(registry_file):        # ownership is decided on a re-read inside the lock, intercepted before the DROP
        reg = _load(registry_file)
        recorded = _clone_owner(reg, name, dbname)
        registered = dbname in reg["clones"].get(name, [])
        if not force and not allow_any and not registered:
            _audit("release-denied", name, dbname, owner=recorded, actor=actor,
                   registry_file=registry_file)
            raise RuntimeError(
                f"{dbname}@{name} 不在克隆池登记"
                f"——拒绝 DROP(防误删真实库;克隆命名但未登记的孤儿走 lab sweep,"
                f"确属误删清理加 --force,操作将落审计)")
        claim = (reg.get("clone_meta", {}).get(name, {}).get(dbname) or {}).get("claim")
        holders = _foreign_holders(claim) if not force else []
        if not force and ((recorded is not None and recorded != actor) or holders):
            _audit("release-denied", name, dbname, owner=recorded, actor=actor,
                   held_by=holders, registry_file=registry_file)
            if recorded is not None and recorded != actor:
                raise RuntimeError(
                    f"克隆 {dbname}@{name} 属于 {recorded}，当前操作者 {actor} 无权释放"
                    f"（tool-w1-clone-vanished，多 agent 并行克隆归属防护）；"
                    f"确属误占需要强制清理时加 --force（操作将落审计）")
            held = "、".join(f"{h['comm']}(pid {h['pid']})" for h in holders)
            raise RuntimeError(
                f"克隆 {dbname}@{name} 仍被其他活动会话持有（owner={recorded or actor}，"
                f"持有方进程 {held} 仍在运行），当前会话无权释放"
                f"（tool-huntA-clone-wipe，同用户多会话克隆防误删）；"
                f"持有会话退出后重试即可，确需强制清理时加 --force（操作将落审计）")
    _run_sql(name, "postgres",
             f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
             f"WHERE datname = {_qstr(dbname)} AND pid <> pg_backend_pid()", registry_file)
    _run_sql(name, "postgres", f"DROP DATABASE IF EXISTS {_qid(dbname)}", registry_file)
    _pool_remove(name, dbname, registry_file, actor=actor, forced=force)


def _clone_owner(reg: dict, name: str, dbname: str) -> str | None:
    """Take the clone owner from the in-memory registry copy; not registered / no ownership recorded (old registry) -> None (no interception)."""
    return reg.get("clone_meta", {}).get(name, {}).get(dbname, {}).get("owner") or None


def _pool_remove(name: str, dbname: str, registry_file: str | Path | None = None,
                 *, actor: str | None = None, forced: bool = False) -> None:
    """Remove one entry from the clone pool (together with its clone_meta ownership
    record; the registry is untouched if not in the pool) and append a release audit
    entry — who released whose clone and when is one audit lookup away. Lock held for
    the whole read-modify-write."""
    with _registry_lock(registry_file):
        reg = _load(registry_file)
        clones = reg["clones"].get(name, [])
        meta_map = reg.get("clone_meta", {}).get(name, {})
        meta = meta_map.pop(dbname, None)
        changed = False
        if dbname in clones:
            clones.remove(dbname)
            changed = True
        if meta is not None:
            if not meta_map:
                reg["clone_meta"].pop(name, None)      # no empty-map husk left behind
            changed = True
        if changed:
            _save(reg, registry_file)
        _audit("release", name, dbname, owner=(meta or {}).get("owner"),
               actor=actor, forced=forced, registry_file=registry_file)


def pool_stats(registry_file: str | Path | None = None) -> dict:
    """Active clones recorded in the registry as {instance_name: [dbnames]} (returns a copy)."""
    return {name: list(dbs) for name, dbs in _load(registry_file)["clones"].items()}


def pool_details(registry_file: str | Path | None = None) -> dict:
    """Active clone details as {instance: {dbname: {"owner","created_at"}}} — the
    ownership-aware version of pool_stats (tool-w1); legacy entries without ownership
    -> owner/created_at are None. Returns a copy."""
    reg = _load(registry_file)
    meta_all = reg.get("clone_meta", {})
    return {iname: {db: dict(meta_all.get(iname, {}).get(db) or {"owner": None, "created_at": None})
                    for db in dbs}
            for iname, dbs in reg["clones"].items()}


# ---------------------------- clone ownership and audit (tool-w1-clone-vanished) ----------------------------

ACTOR_ENV_VARS = ("PGTUNER_AGENT", "USER", "LOGNAME")   # operator identity sources, first non-empty wins in order


def _actor() -> str:
    """Current operator identity (the actor for clone ownership checks and audit):
    PGTUNER_AGENT > USER > LOGNAME > "unknown".

    With parallel agents, each agent process exports PGTUNER_AGENT=<its own name>;
    releasing another's clone is refused with a release-denied audit entry, while
    clone->release within the same process is naturally the same identity, zero
    friction."""
    for var in ACTOR_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return "unknown"


def _proc_stat(pid: int) -> dict | None:
    """{"comm", "ppid", "starttime"} of a pid (reads /proc/<pid>/stat); process gone
    or /proc unavailable (non-Linux) -> None. comm may contain spaces/parentheses ->
    parsed from after the last ')' (man proc_pid_stat); starttime (field 22, clock
    ticks) is used to detect PID reuse."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    head, _, tail = raw.rpartition(")")
    tail = tail.split()
    if "(" not in head or len(tail) < 20:
        return None
    return {"comm": head.split("(", 1)[1], "ppid": int(tail[1]),
            "starttime": int(tail[19])}


def _ancestor_chain(limit: int = 32) -> list[list[int]]:
    """Ancestor chain of the current process [[pid, starttime], ...] (walking up from
    ppid, excluding self and pid 1; capped at limit levels, stopping when a process
    disappears or a cycle appears) — the "session-holding anchor"
    (tool-huntA-clone-wipe).

    Intuition: later commands of the same session have an ancestor chain that is a
    superset of this one (shared shell / agent resident parent); a parallel session
    (another terminal / another agent track, same OS user) shares no intermediate
    ancestors, only common roots like init — and common roots are in both chains, so
    they naturally do not count as "outsiders". /proc unavailable -> [] (the guard
    degrades to the ownership check only, see _foreign_holders)."""
    chain: list[list[int]] = []
    pid, seen = os.getppid(), set()
    while pid > 1 and pid not in seen and len(chain) < limit:
        seen.add(pid)
        st = _proc_stat(pid)
        if st is None:
            break
        chain.append([pid, st["starttime"]])
        pid = st["ppid"]
    return chain


def _foreign_holders(claim: dict | None) -> list[dict]:
    """Processes in the clone's holding anchor (claim.ancestors) that are "still alive
    and not in the current process's ancestor chain" — i.e. the clone is still held
    by another active session. Returns [{"pid", "comm"}]; an empty list = nobody
    holds it (or the anchor is missing / non-Linux).

    Liveness is checked with the starttime comparison: a reused PID (same pid,
    different starttime) does not count as alive — after the holding session
    crashes/exits, the leftover clone can be returned by anyone as usual (orphan
    cleanup is unaffected)."""
    if not isinstance(claim, dict):
        return []
    ancestors = claim.get("ancestors")
    if not isinstance(ancestors, list):
        return []
    mine = {(p, t) for p, t in _ancestor_chain()}
    me = _proc_stat(os.getpid())     # self also belongs to "this session": bash exec-optimizes
    if me is not None:               # the subshell's last command, so the releasing process
        mine.add((os.getpid(), me["starttime"]))   # inherits the subshell's pid+starttime (that pid is in the claim chain) — the self anchor is not an outsider (v0.6.3 smoke-test false-refusal review)
    holders: list[dict] = []
    try:
        for pid, starttime in ancestors:
            if (pid, starttime) in mine:
                continue
            st = _proc_stat(pid)
            if st is not None and st["starttime"] == starttime:
                holders.append({"pid": pid, "comm": st["comm"]})
    except (TypeError, ValueError):               # malformed anchor in the registry: treat as unheld, do not block the return
        return []
    return holders


def _now_iso() -> str:
    """Local time, second-precision ISO (same style as ledger.ts)."""
    return datetime.now().isoformat(timespec="seconds")


def _audit_log_path(registry_file: str | Path | None = None) -> Path:
    """Audit log path: <registry path>.audit.jsonl (same directory as the registry, overridable via registry_file)."""
    path = Path(registry_file) if registry_file is not None else INSTANCE_REGISTRY
    return path.with_name(path.name + ".audit.jsonl")


def _audit(event: str, instance_name: str, dbname: str, *, owner: str | None = None,
           actor: str | None = None, forced: bool = False, held_by: list | None = None,
           registry_file: str | Path | None = None, ts: str | None = None) -> None:
    """Per-clone audit: single-line JSON appended to <registry>.audit.jsonl
    (append-only, never overwrites).

    Events: clone / release / release-denied / destroy. Who touched whose clone and
    when is one lookup away (defect attribution no longer depends on guesswork);
    release-denied may carry held_by (tool-huntA: the live holding processes at
    refusal, attributed to processes). Always called inside _registry_lock — sharing
    the same cross-process lock as registry writes, so audit lines never interleave;
    write failures (disk full etc.) are not swallowed: OSError interrupts the
    caller. ts optionally injects a pre-captured timestamp so a caller can reuse one
    clock read across meta and audit (clone's created_at == audit ts determinism);
    default None takes the time at write."""
    entry = {"ts": ts if ts is not None else _now_iso(), "event": event,
             "instance": instance_name, "db": dbname,
             "owner": owner, "actor": actor if actor is not None else _actor()}
    if forced:
        entry["forced"] = True
    if held_by:
        entry["held_by"] = held_by
    path = _audit_log_path(registry_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def audit_tail(*, instance_name: str | None = None, limit: int = 50,
               registry_file: str | Path | None = None) -> list[dict]:
    """Last limit entries of the audit log (chronological); instance_name restricts to that instance; missing log/bad lines are skipped."""
    path = _audit_log_path(registry_file)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue                                   # a bad line does not block viewing
    if instance_name is not None:
        rows = [r for r in rows if r.get("instance") == instance_name]
    return rows[-limit:] if limit and limit > 0 else rows


# ---------------------------- clone leftover cleanup (lab sweep) ----------------------------

CLONE_HEX_LEN = 8                                   # default clone-name suffix _c<8 lowercase hex chars>
_ANY_CLONE_NAME_RE = re.compile(rf"^.+_c[0-9a-f]{{{CLONE_HEX_LEN}}}$")

# Database inventory: prefer the single query with pg_stat_file (oid directory mtime ≈
# clone time, see the sweep docstring); fall back to the no-time variant without
# permissions (non-superuser).
_DB_INVENTORY_SQL = (
    "SELECT d.datname, d.oid, d.datallowconn, d.datistemplate,\n"
    "       (pg_stat_file('base/' || d.oid)).modification AS created_at,\n"
    "       EXTRACT(EPOCH FROM (now() - (pg_stat_file('base/' || d.oid)).modification))"
    " / 60 AS age_minutes\n"
    "  FROM pg_database d ORDER BY d.datname")
_DB_INVENTORY_NO_TIME_SQL = (
    "SELECT datname, oid, datallowconn, datistemplate FROM pg_database ORDER BY 1")


def _clone_template_of(dbname: str, templates: set) -> str | None:
    """Whether dbname is exactly <some sealed template>_c<8 lowercase hex chars> (the
    default clone naming, see clone()); if yes -> that template name, else -> None.
    fullmatch per template, eliminating prefix ambiguity."""
    for tpl in templates:
        if re.fullmatch(rf"{re.escape(tpl)}_c[0-9a-f]{{{CLONE_HEX_LEN}}}", dbname):
            return tpl
    return None


def _db_inventory_rows(name: str, registry_file: str | Path | None = None) -> list[dict]:
    """Connect to the postgres database and pull the database inventory ->
    [ {datname, oid, allowconn, istemplate, created_at, age_minutes} ]. Under
    autocommit, if the first query fails (no permission for pg_stat_file) it can fall
    back to the second query (time fields set to None)."""
    conn = pg.connect_rw(dsn_of(name, "postgres", registry_file))
    try:
        try:
            raw = pg.fetch_all(conn, _DB_INVENTORY_SQL)
            has_time = True
        except Exception:
            raw = pg.fetch_all(conn, _DB_INVENTORY_NO_TIME_SQL)
            has_time = False
    finally:
        conn.close()
    return [dict(datname=r[0], oid=r[1], allowconn=r[2], istemplate=r[3],
                 created_at=(r[4] if has_time else None),
                 age_minutes=(float(r[5]) if has_time and r[5] is not None else None))
            for r in raw]


def sweep(instance: str | None = None, *, older_than: float | None = None,
          yes: bool = False, registry_file: str | Path | None = None) -> dict:
    """Scan and clean up clone leftovers (dry-run by default, zero writes). Returns a
    report dict:

        {"scanned": [...], "skipped_instances": [{instance, reason}],
         "targets": [{instance, dbname, kind, template, created_at, age_minutes}],
         "ignored": [{instance, dbname, reason}], "yes": bool,
         "dropped": ["inst/db", ...], "stale_removed": ["inst/db", ...],
         "errors": [{instance, dbname, error}]}

    Three target kinds:
      registered   databases registered in the clone pool (lab clone allows explicit
                   names; registered means owned by the tool, any name is fine)
      orphan       unregistered databases whose name is exactly <a sealed template of
                   this instance>_c<8 hex chars> (the default clone naming; sealed
                   template = datallowconn=false, same criterion as lab templates)
      stale-entry  registered in the pool but no longer present on the instance
                   (--yes only cleans the registry, sends no DROP; excluded from age
                   filtering)

    --older-than N (minutes): only clean databases whose "creation time has come of
    age (age >= N)". Rationale: pg_database has no creation-time column (neither does
    19/20devel, see pg_database.h), and the registry stores only names; the
    pg_stat_file('base/<oid>') directory mtime is taken as an approximation —
    CREATE DATABASE ... TEMPLATE copies the files into the new directory, so mtime ≈
    clone time and an idle leftover database's mtime stays untouched; subsequent
    DDL/VACUUM FULL on the database only pushes the mtime up -> understates the age ->
    more conservative, no wrongful deletes. Databases whose mtime is unavailable
    (non-superuser) when --older-than is given: skipped with the reason noted; never
    blindly deleted.

    Never touched: datistemplate system databases (template1), datallowconn=false
    sealed templates (template0 and make_template products), ordinary databases not
    named like clones; databases shaped like _c<8hex> whose prefix is not a template
    of this instance only enter the ignored report. --yes cleans via drop_db
    (disconnect + DROP IF EXISTS + pool exit); a single instance's failure does not
    block the rest (recorded in skipped_instances / errors).
    """
    reg = _load(registry_file)
    if instance is not None:
        get(instance, registry_file)               # absent -> ValueError
        names = [instance]
    else:
        names = [i.name for i in list_instances(registry_file)]
    report = {"scanned": [], "skipped_instances": [], "targets": [], "ignored": [],
              "yes": yes, "dropped": [], "stale_removed": [], "errors": []}

    for iname in names:
        try:
            ensure_running(iname, registry_file)
            rows = _db_inventory_rows(iname, registry_file)
        except Exception as e:
            report["skipped_instances"].append(
                {"instance": iname, "reason": _last_line(str(e))})
            continue
        report["scanned"].append(iname)
        by_name = {d["datname"]: d for d in rows}
        templates = {d["datname"] for d in rows if not d["allowconn"]}
        registered = set(reg["clones"].get(iname, []))

        for d in rows:                             # candidates present on the server
            dbname = d["datname"]
            if not d["allowconn"] or d["istemplate"]:
                continue                           # sealed template / template0 / template1
            kind, tpl = None, None
            if dbname in registered:
                kind = "registered"
            elif _clone_template_of(dbname, templates) is not None:
                kind, tpl = "orphan", _clone_template_of(dbname, templates)
            elif _ANY_CLONE_NAME_RE.fullmatch(dbname):
                report["ignored"].append({
                    "instance": iname, "dbname": dbname,
                    "reason": f"形似克隆名，但前缀不是本实例模板（{', '.join(sorted(templates))}）"})
            if kind is None:
                continue
            age = d["age_minutes"]
            if older_than is not None and age is None:
                report["ignored"].append({
                    "instance": iname, "dbname": dbname,
                    "reason": "创建时间未知（pg_stat_file 不可用），--older-than 下跳过"})
                continue
            if older_than is not None and age < older_than:
                continue                           # not yet of age
            report["targets"].append({
                "instance": iname, "dbname": dbname, "kind": kind,
                "template": tpl, "created_at": d["created_at"], "age_minutes": age})
        for dbname in sorted(registered - set(by_name)):
            report["targets"].append({              # registered in the pool, gone from the server
                "instance": iname, "dbname": dbname, "kind": "stale-entry",
                "template": None, "created_at": None, "age_minutes": None})

    if yes:
        for t in report["targets"]:
            iname, dbname, kind = t["instance"], t["dbname"], t["kind"]
            try:
                if kind == "stale-entry":
                    _pool_remove(iname, dbname, registry_file)
                    report["stale_removed"].append(f"{iname}/{dbname}")
                else:
                    # orphans are pre-validated against this instance's sealed
                    # templates above, so they pass the internal-cleanup channel
                    drop_db(iname, dbname, registry_file,
                            allow_any=(kind != "registered"))
                    report["dropped"].append(f"{iname}/{dbname}")
            except Exception as e:
                report["errors"].append(
                    {"instance": iname, "dbname": dbname, "error": _last_line(str(e))})
    return report
