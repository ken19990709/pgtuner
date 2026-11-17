"""watch — read-only live status dashboard (rich Live) + UI-less snapshot text mode (agent/CI).

Single-frame collection collect_frame(): one connection, a fixed query list; any failed
query sets its field to None/empty container and never interrupts the whole frame
(mirroring signals.collect's per-query fault tolerance). The normal path issues
exactly 10 read-only SQL statements per frame (see the _SQL_* constants); negligible
overhead at interval=1s:
  1) version() + current_setting('max_connections')   — merged into one statement, keeping the per-frame query count <=10
  2) pg_database_size(current_database())
  3) pg_stat_activity GROUP BY state (background processes with NULL state go to "background")
  4) active sessions TOP5 (state='active' excluding our own pid, runtime descending, query truncated to 120 chars)
  5) active session wait_event counts (NULL goes to "CPU")
  6) pg_stat_database hit ratio (denominator 0 -> NULL -> None, no guessing)
  7) pg_stat_all_tables dead-tuple ratio (same)
  8) pg_current_wal_lsn() (cached within the frame; with prev passed in, differenced into wal_bytes_per_s)
  9) pg_stat_database temp_files/temp_bytes (cumulative values)
  10) checkpointer: on PG17+ try pg_stat_checkpointer (PG17 column names
      num_timed/num_requested, renamed num_timed_checkpoints/num_requested_checkpoints
      from PG18, both tried), on error fall back to pg_stat_bgwriter (the only source
      on PG<=16; on PG17+ that view no longer has checkpoint columns, so None on
      failure). For major version <17 go straight to bgwriter based on version(),
      saving a doomed probe.
Contract note (dev-contracts.md §E): the list does not include pg_stat_statements —
"skip quietly if absent" is naturally satisfied.

DEVIATION (vs the dev-contracts.md §E field notes):
  - db_size_bytes / max_connections / temp_files / temp_bytes are actually int | None
    (the contract says bare int): per the "any failure -> None" convention, a failed
    collection does not produce a misleading 0; the normal path still yields int.
  - the other public signatures (collect_frame/render_frame/snapshot/run_live,
    WatchFrame field names and order) match the contract; checkpointer_delta semantics
    = the current frame's checkpoint counter snapshot (normalized key names), with
    frame-to-frame deltas computed for display by render_frame(prev=...).

Δ coloring convention: fast-moving metrics (connections / hit ratio / dead-live / temp
bytes) get a delta column when prev is passed in; pressure metrics turn red up, green
down, buffer hit ratio the other way round (green up, red down). The WAL rate is
itself a differenced value and gets no delta on top.

Non-interactive full-frame output (fix tool-w2-watch-noninteractive-last-frame-only):
rich Live on a non-terminal stdout (redirect / pipe / CI, or a script pty recording
with TERM=dumb) is silent on refresh and only drops one final frame at stop -> the
sampling timeline is lost. Now run_live degrades to per-frame print when it detects
non-interactivity, and snapshot also outputs the text of all frames (each frame with
the delta against the previous one); the interactive terminal's Live behavior is
unchanged.
"""
from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta

from rich import box
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.table import Table

from . import pg
from .signals import lsn_to_int

KB = 1024
MB = KB * KB
GB = MB * KB


def _now() -> float:
    """Frame timestamp source (perf_counter, monotonic); extracted into a function so tests can inject a fake clock."""
    return time.perf_counter()


# ---------------------------- single-frame query list (all read-only) ----------------------------

_SQL_VERSION = ("SELECT version(), current_setting('max_connections')")
_SQL_DB_SIZE = "SELECT pg_database_size(current_database())"
_SQL_BACKENDS = "SELECT count(*),state FROM pg_stat_activity GROUP BY state"
_SQL_TOP = ("SELECT pid,state,wait_event_type,wait_event,now()-query_start AS runtime,"
            "left(query,120) FROM pg_stat_activity WHERE state='active' "
            "AND pid<>pg_backend_pid() ORDER BY runtime DESC LIMIT 5")
_SQL_WAITS = ("SELECT wait_event,count(*) FROM pg_stat_activity WHERE state='active' "
              "AND pid<>pg_backend_pid() GROUP BY wait_event")
_SQL_HIT = ("SELECT sum(blks_hit)::float/NULLIF(sum(blks_hit)+sum(blks_read),0) "
            "FROM pg_stat_database WHERE datname=current_database()")
_SQL_DEAD_LIVE = ("SELECT sum(n_dead_tup)::float/NULLIF(sum(n_dead_tup)+sum(n_live_tup),0) "
                  "FROM pg_stat_all_tables")
_SQL_WAL_LSN = "SELECT pg_current_wal_lsn()"
_SQL_TEMP = ("SELECT temp_files,temp_bytes FROM pg_stat_database "
             "WHERE datname=current_database()")
# PG17 column names / PG18+ renamed columns / PG<=16 fallback (PG17+ pg_stat_bgwriter no longer has checkpoint columns)
_SQL_CKPT_17 = ("SELECT num_timed,num_requested,write_time,sync_time,buffers_written "
                "FROM pg_stat_checkpointer")
_SQL_CKPT_18 = ("SELECT num_timed_checkpoints,num_requested_checkpoints,write_time,"
                "sync_time,buffers_written FROM pg_stat_checkpointer")
_SQL_BGWRITER = ("SELECT checkpoints_timed,checkpoints_req,checkpoint_write_time,"
                 "checkpoint_sync_time,buffers_clean,buffers_backend FROM pg_stat_bgwriter")


@dataclass
class WatchFrame:
    """One frame of read-only observation. top_queries in built-in order is runtime descending (SQL ORDER BY + Python fallback)."""
    ts: str                                    # "%H:%M:%S" collection moment
    pg_version: str                            # first line of version(); "?" on failure
    db_size_bytes: int | None                  # DEVIATION: None on failure instead of 0
    backends: dict                             # {state: count}; NULL state goes to "background"
    max_connections: int | None                # DEVIATION: None on failure
    buffer_hit_ratio: float | None             # denominator 0 -> None (no guessing)
    dead_live_ratio: float | None              # dead/(dead+live); denominator 0 -> None
    wal_bytes_per_s: float | None              # (lsn_b-lsn_a)/Δt; first frame (no prev) None
    temp_files: int | None                     # DEVIATION: None on failure
    temp_bytes: int | None                     # DEVIATION: None on failure
    top_queries: list = field(default_factory=list)   # [{pid,state,wait_event_type,wait_event,runtime_s,query}]
    waits: dict = field(default_factory=dict)  # {wait_event: count}; NULL goes to "CPU"
    checkpointer_delta: dict | None = None     # current frame's checkpoint counter snapshot (normalized key names)
    # -- private cache: previous frame's lsn/ts, for the next frame's WAL rate delta (excluded from repr/equality) --
    _lsn: str | None = field(default=None, repr=False, compare=False)
    _ts: float = field(default_factory=_now, repr=False, compare=False)


def _safe(fn, default=None):
    """Per-query fault tolerance: any exception (permissions / version differences / connection jitter) -> default, without interrupting the frame."""
    try:
        return fn()
    except Exception:
        return default


def _to_seconds(v) -> float:
    """interval (-> timedelta) / number -> seconds; None/unparseable treated as 0.0."""
    if isinstance(v, timedelta):
        return v.total_seconds()
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pg_major(version_str: str | None) -> int | None:
    """version() text -> major version ("PostgreSQL 17.2 ..." -> 17); None on parse failure."""
    if not version_str:
        return None
    m = re.match(r"\s*PostgreSQL\s+(\d+)", version_str)
    return int(m.group(1)) if m else None


def _norm_checkpointer(row) -> dict:
    """pg_stat_checkpointer row -> normalized counters (time columns ms -> s)."""
    return {"checkpoints_timed": int(row[0] or 0), "checkpoints_req": int(row[1] or 0),
            "write_time_s": float(row[2] or 0) / 1000.0,
            "sync_time_s": float(row[3] or 0) / 1000.0,
            "buffers_written": int(row[4] or 0), "source": "pg_stat_checkpointer"}


def _norm_bgwriter(row) -> dict:
    """pg_stat_bgwriter row -> the same normalized structure (buffers_written = bgwriter + backend writes)."""
    return {"checkpoints_timed": int(row[0] or 0), "checkpoints_req": int(row[1] or 0),
            "write_time_s": float(row[2] or 0) / 1000.0,
            "sync_time_s": float(row[3] or 0) / 1000.0,
            "buffers_written": int(row[4] or 0) + int(row[5] or 0),
            "source": "pg_stat_bgwriter"}


def _collect_checkpointer(conn, major: int | None) -> dict | None:
    """Checkpointer counters: pick the try order by major version, try each until one succeeds; None if all fail."""
    if major is not None and major < 17:
        chain = ((_SQL_BGWRITER, _norm_bgwriter),)
    else:
        chain = ((_SQL_CKPT_17, _norm_checkpointer), (_SQL_CKPT_18, _norm_checkpointer),
                 (_SQL_BGWRITER, _norm_bgwriter))
    for sql, norm in chain:
        row = _safe(lambda s=sql: pg.fetch_one(conn, s))
        if row:
            return norm(row)
    return None


def collect_frame(conn, *, prev: WatchFrame | None = None) -> WatchFrame:
    """Collect one frame over a single connection (all read-only). With prev as the
    previous frame, difference the WAL rate; any single query failure only sets its
    own field to None/empty container, leaving the rest of the frame intact."""
    now = _now()

    # 1) version + max_connections (one merged query, part of the frame's 10-query budget)
    pg_version, max_conn = "?", None
    row = _safe(lambda: pg.fetch_one(conn, _SQL_VERSION))
    if row:
        pg_version = str(row[0]) if row[0] is not None else "?"
        try:
            max_conn = int(row[1])
        except (TypeError, ValueError):
            max_conn = None

    # 2) database size
    row = _safe(lambda: pg.fetch_one(conn, _SQL_DB_SIZE))
    db_size = int(row[0]) if row and row[0] is not None else None

    # 3) session state counts (background processes have NULL state -> "background")
    backends: dict = {}
    for r in _safe(lambda: pg.fetch_all(conn, _SQL_BACKENDS), default=[]) or []:
        if r and len(r) >= 2:
            state = r[1] if r[1] is not None else "background"
            backends[state] = backends.get(state, 0) + int(r[0] or 0)

    # 4) active sessions TOP (SQL already ORDER BY runtime DESC; Python re-sorts as a fallback + truncates to 5)
    top: list = []
    for r in _safe(lambda: pg.fetch_all(conn, _SQL_TOP), default=[]) or []:
        if not r or len(r) < 6:
            continue
        top.append({"pid": r[0], "state": r[1], "wait_event_type": r[2],
                    "wait_event": r[3], "runtime_s": _to_seconds(r[4]),
                    "query": (str(r[5]) if r[5] is not None else "")[:120]})
    top = sorted(top, key=lambda q: -(q["runtime_s"] or 0.0))[:5]

    # 5) active session wait counts (wait_event NULL = not waiting -> "CPU")
    waits: dict = {}
    for r in _safe(lambda: pg.fetch_all(conn, _SQL_WAITS), default=[]) or []:
        if r and len(r) >= 2:
            ev = r[0] if r[0] is not None else "CPU"
            waits[ev] = waits.get(ev, 0) + int(r[1] or 0)

    # 6/7) ratios: with denominator 0 the SQL returns NULL -> None (no guessing)
    row = _safe(lambda: pg.fetch_one(conn, _SQL_HIT))
    hit = float(row[0]) if row and row[0] is not None else None
    row = _safe(lambda: pg.fetch_one(conn, _SQL_DEAD_LIVE))
    dead_live = float(row[0]) if row and row[0] is not None else None

    # 8) WAL: cache lsn within the frame; difference the rate when prev carries cached lsn/ts
    row = _safe(lambda: pg.fetch_one(conn, _SQL_WAL_LSN))
    lsn = str(row[0]) if row and row[0] is not None else None
    wal_rate = None
    if prev is not None and lsn and prev._lsn:
        dt = now - prev._ts
        try:
            if dt > 0:
                wal_rate = (lsn_to_int(lsn) - lsn_to_int(prev._lsn)) / dt
        except (ValueError, AttributeError):   # illegal LSN format etc.
            wal_rate = None

    # 9) temp spill (cumulative values)
    row = _safe(lambda: pg.fetch_one(conn, _SQL_TEMP))
    temp_files = int(row[0] or 0) if row else None
    temp_bytes = int(row[1] or 0) if row else None

    # 10) checkpointer (version gating + fallback chain)
    ckpt = _collect_checkpointer(conn, _pg_major(pg_version))

    return WatchFrame(
        ts=time.strftime("%H:%M:%S"), pg_version=pg_version, db_size_bytes=db_size,
        backends=backends, max_connections=max_conn, buffer_hit_ratio=hit,
        dead_live_ratio=dead_live, wal_bytes_per_s=wal_rate,
        temp_files=temp_files, temp_bytes=temp_bytes, top_queries=top, waits=waits,
        checkpointer_delta=ckpt, _lsn=lsn, _ts=now)


# ---------------------------- humanized numbers / Δ coloring ----------------------------

def _human_bytes(b) -> str:
    """Byte count -> B/kB/MB/GB (one decimal place at >=MB)."""
    if b is None:
        return "-"
    if b >= GB:
        return f"{b / GB:.1f}GB"
    if b >= MB:
        return f"{b / MB:.1f}MB"
    if b >= KB:
        return f"{b / KB:.1f}kB"
    return f"{b}B"


def _pct(r) -> str:
    """Ratio -> percentage with one decimal place."""
    return f"{r * 100:.1f}%" if r is not None else "-"


def _fmt_duration(s) -> str:
    """Seconds -> "3.2s" / "2m05s"."""
    if s is None:
        return "-"
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s // 60)}m{int(s % 60):02d}s"


def _delta_cell(diff: float, text: str, higher_is_bad: bool) -> str:
    """Δ text coloring: pressure metrics red up, green down; higher_is_bad=False (e.g. hit ratio) green up, red down."""
    up = diff > 0
    color = ("red" if up else "green") if higher_is_bad else ("green" if up else "red")
    return f"[{color}]{text}[/{color}]"


def _delta_int(cur, prev, *, higher_is_bad=True, dim=False) -> str:
    if cur is None or prev is None or cur == prev:
        return ""
    d = cur - prev
    cell = _delta_cell(d, f"{d:+g}", higher_is_bad)
    return f"[dim]{cell}[/dim]" if dim else cell


def _delta_pp(cur, prev, *, higher_is_bad=True) -> str:
    """Ratio deltas displayed in percentage points (pp)."""
    if cur is None or prev is None or cur == prev:
        return ""
    d = (cur - prev) * 100
    return _delta_cell(d, f"{d:+.1f}pp", higher_is_bad)


def _delta_bytes(cur, prev, *, higher_is_bad=True) -> str:
    if cur is None or prev is None or cur == prev:
        return ""
    d = cur - prev
    sign = "+" if d > 0 else "-"
    return _delta_cell(d, f"{sign}{_human_bytes(abs(d))}", higher_is_bad)


def _ratio_health(r) -> str:
    if r is None:
        return ""
    if r >= 0.99:
        return "[green]优秀[/green]"
    if r >= 0.95:
        return "[green]良好[/green]"
    if r >= 0.90:
        return "[yellow]一般[/yellow]"
    return "[red]偏低[/red]"


def _dead_live_health(r) -> str:
    if r is None:
        return ""
    if r < 0.1:
        return "[green]健康[/green]"
    if r < 0.2:
        return "[yellow]接近 autovacuum 触发区[/yellow]"
    return "[red]超过 autovacuum 触发区[/red]"


# ---------------------------- rendering ----------------------------

def _overview_table(frame: WatchFrame, prev: WatchFrame | None) -> Table:
    """Upper half: instance overview (metric | current | Δ | note)."""
    t = Table(box=None, show_header=False, expand=True, title="实例概况", title_style="bold")
    t.add_column("指标", style="bold cyan", no_wrap=True)
    t.add_column("当前值", no_wrap=True)
    t.add_column("Δ", justify="right", no_wrap=True)
    t.add_column("备注", max_width=46, overflow="fold")

    ver = frame.pg_version or "?"
    ver_short = ver.split(" on ")[0]
    t.add_row("实例", ver_short, "", f"[dim]{escape(ver)}[/dim]" if ver != ver_short else "")
    t.add_row("库大小", _human_bytes(frame.db_size_bytes), "", "")

    conn_now = sum(frame.backends.values())
    conn_prev = sum(prev.backends.values()) if prev is not None else None
    breakdown = " · ".join(f"{k} {v}" for k, v in
                           sorted(frame.backends.items(), key=lambda kv: -kv[1]))
    t.add_row("连接",
              f"{conn_now}/{frame.max_connections if frame.max_connections is not None else '?'}",
              _delta_int(conn_now, conn_prev),
              f"[dim]{escape(breakdown)}[/dim]" if breakdown else "")

    t.add_row("buffer 命中率", _pct(frame.buffer_hit_ratio),
              _delta_pp(frame.buffer_hit_ratio,
                        prev.buffer_hit_ratio if prev is not None else None,
                        higher_is_bad=False),
              _ratio_health(frame.buffer_hit_ratio))
    t.add_row("dead/live", _pct(frame.dead_live_ratio),
              _delta_pp(frame.dead_live_ratio,
                        prev.dead_live_ratio if prev is not None else None),
              _dead_live_health(frame.dead_live_ratio))

    wal = f"{_human_bytes(frame.wal_bytes_per_s)}/s" if frame.wal_bytes_per_s is not None else "-"
    wal_note = "" if frame.wal_bytes_per_s is not None else "[dim]首帧无 prev 基准[/dim]"
    t.add_row("WAL 速率", wal, "", wal_note)

    if frame.temp_files is not None or frame.temp_bytes is not None:
        temp_val = f"{frame.temp_files or 0} 个 / {_human_bytes(frame.temp_bytes or 0)}"
    else:
        temp_val = "-"
    t.add_row("temp 溢盘", temp_val,
              _delta_bytes(frame.temp_bytes, prev.temp_bytes if prev is not None else None),
              "[dim]sort/hash 溢盘信号（累计值）[/dim]" if temp_val != "-" else "")

    ck = frame.checkpointer_delta
    if ck:
        ck_prev = prev.checkpointer_delta if prev is not None else None
        cur_total = ck["checkpoints_timed"] + ck["checkpoints_req"]
        prev_total = (ck_prev["checkpoints_timed"] + ck_prev["checkpoints_req"]) if ck_prev else None
        t.add_row("checkpoints", f"{ck['checkpoints_timed']} 定时 / {ck['checkpoints_req']} 请求",
                  _delta_int(cur_total, prev_total, dim=True),
                  f"[dim]write {ck['write_time_s']:.1f}s · sync {ck['sync_time_s']:.1f}s"
                  f" · {ck['source']}[/dim]")

    if frame.waits:
        top_waits = sorted(frame.waits.items(), key=lambda kv: -kv[1])[:3]
        t.add_row("活动等待", escape(" · ".join(f"{k} {v}" for k, v in top_waits)), "",
                  "[dim]active 会话 wait_event 计数[/dim]")
    return t


def _top_queries_table(frame: WatchFrame) -> Table:
    """Lower half: active sessions TOP (pid/state/wait/runtime/query)."""
    t = Table(box=None, title="活动会话 TOP（runtime 降序）", title_style="bold")
    t.add_column("PID", justify="right", no_wrap=True)
    t.add_column("状态", no_wrap=True)
    t.add_column("等待", no_wrap=True)
    t.add_column("运行", justify="right", no_wrap=True)
    t.add_column("查询", overflow="fold", max_width=64)
    if not frame.top_queries:
        t.add_row("", "", "", "", "[dim]无活动会话（state='active' 为空）[/dim]")
        return t
    for q in frame.top_queries:
        wet, we = q.get("wait_event_type"), q.get("wait_event")
        wait = f"{wet}/{we}" if wet else (we or "CPU")   # not waiting (both columns NULL) -> CPU
        t.add_row(str(q.get("pid")), str(q.get("state") or "-"), wait,
                  _fmt_duration(q.get("runtime_s")), escape(str(q.get("query") or "")))
    return t


def render_frame(frame: WatchFrame, *, prev: WatchFrame | None = None) -> "rich.table.Table":
    """One frame -> one combined panel Table (top: instance overview with Δ coloring;
    bottom: active sessions TOP). Returns a rich Table; the caller (Live /
    snapshot's StringIO Console) decides where the output goes."""
    outer = Table(box=box.SIMPLE_HEAVY, title=f"pgtuner watch · {frame.ts}",
                  title_style="bold", show_header=False, pad_edge=False)
    outer.add_column()
    outer.add_row(_overview_table(frame, prev))
    outer.add_row(_top_queries_table(frame))
    return outer


# ---------------------------- two drivers: UI-less / Live ----------------------------

def snapshot(dsn, *, interval: float = 1.0, frames: int = 3) -> str:
    """Sampling mode (CI/agent): collect frames frames (interval apart), render each
    frame to text (each with the delta against the previous frame; the first has no
    prev) -> returns plain text (ANSI-free automatically on non-tty). Full-frame
    output guarantees sampling continuity / a reviewable timeline — non-interactive
    consumers no longer see only the last frame."""
    frames = int(frames)
    if frames < 1:
        raise ValueError(f"frames 至少 1 帧，得到 {frames}")
    if interval < 0:
        raise ValueError(f"interval 不能为负，得到 {interval}")
    conn = pg.connect(dsn)
    collected: list[WatchFrame] = []
    try:
        for i in range(frames):
            prev = collected[-1] if collected else None
            collected.append(collect_frame(conn, prev=prev))
            if interval > 0 and i < frames - 1:
                time.sleep(interval)
    finally:
        conn.close()
    buf = io.StringIO()
    console = Console(file=buf, width=100)
    for i, frame in enumerate(collected):
        console.print(render_frame(frame, prev=collected[i - 1] if i else None))
    return buf.getvalue()


def run_live(dsn, *, interval: float = 1.0, duration: float | None = None) -> None:
    """Live dashboard: collect+render one frame every interval seconds; exits when
    duration is reached; Ctrl-C (KeyboardInterrupt) exits cleanly keeping the last
    frame. Read-only connection throughout. When stdout is non-interactive
    (redirect / pipe / CI / a script pty recording with TERM=dumb — Live only drops
    the last frame at exit on non-terminals) it automatically degrades to per-frame
    print: the frame sequence is fully preserved in stdout, the sampling timeline is
    reviewable; interactive terminal behavior is unchanged."""
    if interval < 0:
        raise ValueError(f"interval 不能为负，得到 {interval}")
    conn = pg.connect(dsn)
    console = Console()
    # Live is usable only on an interactive terminal (console.is_interactive =
    # is_terminal and not TERM=dumb, the same criterion rich Live uses to decide
    # in-place redraws): non-interactive Live is silent on file refresh and only
    # drops one final frame at stop -> degrade to per-frame print, the frame
    # sequence fully preserved in stdout.
    live: Live | None = (Live(console=console, auto_refresh=False, refresh_per_second=4)
                         if console.is_interactive else None)
    prev: WatchFrame | None = None
    start = _now()

    def _loop(emit) -> None:
        nonlocal prev
        while True:
            frame = collect_frame(conn, prev=prev)
            emit(render_frame(frame, prev=prev))
            prev = frame
            if duration is not None and _now() - start >= duration:
                break
            time.sleep(interval)

    try:
        if live is not None:
            with live:

                def emit(table) -> None:
                    live.update(table)
                    live.refresh()

                _loop(emit)
        else:
            _loop(console.print)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()
