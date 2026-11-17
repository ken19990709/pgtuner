"""watch tests: a FakeConn (dispatching fixture result sets by SQL keyword) drives collect_frame /
render_frame / snapshot / run_live; no running PG needed. Mock style follows test_signals."""
import io
import re
from datetime import timedelta

import pytest
from rich.console import Console
from rich.table import Table

from pgtuner import watch
from pgtuner.signals import lsn_to_int


# ---------------------------- FakeConn: execute records SQL, dispatches fixture rows by keyword ----------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append(sql)
        for key, exc in self._conn.errors.items():
            if key in sql:
                raise exc
        for key, queue in self._conn.responses.items():
            if key in sql:
                assert queue, f"fixture 结果集耗尽: {key}"
                self._rows = queue.pop(0)      # each execute pops one result set → multiple frames consumed in order
                return
        raise AssertionError(f"未 mock 的 SQL: {sql}")

    def fetchone(self):
        assert self._rows, "fixture 行耗尽（fetchone）"
        return self._rows.pop(0)

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows


class FakeConn:
    def __init__(self, responses=None, errors=None):
        self.responses = responses or {}
        self.errors = errors or {}
        self.executed = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


LONG_QUERY = "SELECT * FROM big_table WHERE pad = '" + "x" * 160 + "'"   # >120 chars, verifies truncation


def make_responses(frames=1, **over):
    """Each key = a queue of result sets in frame order (one result set per frame).
    Note: a comprehension must generate fresh inner lists — fetchone pops in place, and `[[row]] * n` sharing
    the same result set would be drained empty at the second frame (which once needlessly triggered the fallback chain)."""
    r = {
        "version()": [[("PostgreSQL 17.2 on x86_64-pc-linux-gnu, compiled by gcc", "100")]
                      for _ in range(frames)],
        "pg_database_size": [[(1610612736,)] for _ in range(frames)],    # 1.5GB
        "GROUP BY state": [[(3, "active"), (10, "idle"), (2, None)] for _ in range(frames)],
        "ORDER BY runtime": [[
            (4242, "active", "Lock", "relation", timedelta(seconds=62.5),
             "UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE aid = 42"),
            (4243, "active", None, None, timedelta(seconds=3.25), LONG_QUERY),
        ] for _ in range(frames)],
        "GROUP BY wait_event": [[("Lock", 2), (None, 1)] for _ in range(frames)],  # NULL counts as CPU
        "blks_hit": [[(0.9524,)] for _ in range(frames)],
        "n_dead_tup": [[(0.087,)] for _ in range(frames)],
        "pg_current_wal_lsn": [[("0/100000",)], [("0/200000",)], [("0/300000",)]][:frames],
        "temp_files": [[(7, 31457280)] for _ in range(frames)],          # 7 files / 30.0MB
        "num_timed,": [[(12, 1, 3400.5, 210.25, 1024)] for _ in range(frames)],    # PG17 column names
    }
    r.update(over)
    return r


def collect_one(responses=None, errors=None, **kw):
    conn = FakeConn(responses or make_responses(), errors)
    return conn, watch.collect_frame(conn, **kw)


# ---------------------------- collect_frame: field parsing ----------------------------

def test_collect_frame_fields():
    conn, f = collect_one()
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", f.ts)
    assert f.pg_version.startswith("PostgreSQL 17.2")
    assert f.max_connections == 100
    assert f.db_size_bytes == 1610612736
    assert f.backends == {"active": 3, "idle": 10, "background": 2}      # NULL state → background
    assert f.buffer_hit_ratio == 0.9524
    assert f.dead_live_ratio == 0.087
    assert f.temp_files == 7 and f.temp_bytes == 31457280
    assert f.waits == {"Lock": 2, "CPU": 1}                              # NULL wait_event → CPU
    ck = f.checkpointer_delta
    assert ck["checkpoints_timed"] == 12 and ck["checkpoints_req"] == 1
    assert ck["write_time_s"] == pytest.approx(3.4005)                    # ms → s
    assert ck["sync_time_s"] == pytest.approx(0.21025)
    assert ck["buffers_written"] == 1024
    assert ck["source"] == "pg_stat_checkpointer"
    # top queries: runtime descending, all fields present, query truncated at 120 chars
    assert [q["pid"] for q in f.top_queries] == [4242, 4243]
    q0 = f.top_queries[0]
    assert q0["state"] == "active" and q0["runtime_s"] == 62.5
    assert q0["wait_event_type"] == "Lock" and q0["wait_event"] == "relation"
    assert q0["query"].startswith("UPDATE pgbench_accounts")
    assert len(f.top_queries[1]["query"]) == 120
    # first frame (no prev): no WAL rate yet, but the lsn is cached for the next frame's differencing
    assert f.wal_bytes_per_s is None and f._lsn == "0/100000"


def test_collect_frame_query_budget():
    """A single frame is exactly 10 read-only SELECTs, no pg_stat_statements (premise: negligible overhead at interval=1s)."""
    conn, _ = collect_one()
    assert len(conn.executed) == 10
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in conn.executed)
    assert not any("pg_stat_statements" in sql for sql in conn.executed)


# ---------------------------- WAL rate: two-frame lsn delta / time delta ----------------------------

def test_collect_frame_wal_rate_from_prev(monkeypatch):
    conn = FakeConn(make_responses(frames=2))
    clock = iter([100.0, 102.0])                       # Δt = 2s (fake clock injected, precise assertion)
    monkeypatch.setattr(watch, "_now", lambda: next(clock))
    f1 = watch.collect_frame(conn)
    f2 = watch.collect_frame(conn, prev=f1)
    assert f1.wal_bytes_per_s is None                  # first frame warmup: no prev
    expected = (lsn_to_int("0/200000") - lsn_to_int("0/100000")) / 2.0
    assert f2.wal_bytes_per_s == pytest.approx(expected)          # 1MB / 2s = 524288 B/s
    assert f2._lsn == "0/200000"


def test_collect_frame_bad_prev_lsn_no_crash(monkeypatch):
    """prev's lsn is invalid (empty string from standby/permission scenarios) → rate None, no crash."""
    conn = FakeConn(make_responses(frames=2))
    clock = iter([100.0, 101.0])
    monkeypatch.setattr(watch, "_now", lambda: next(clock))
    f1 = watch.collect_frame(conn)
    f1._lsn = "not-a-lsn"
    f2 = watch.collect_frame(conn, prev=f1)
    assert f2.wal_bytes_per_s is None


# ---------------------------- single-query fault tolerance: fields set to None without breaking the frame ----------------------------

def test_collect_frame_single_query_failures_tolerated():
    conn, f = collect_one(errors={"blks_hit": RuntimeError("permission denied")})
    assert f.buffer_hit_ratio is None
    assert f.dead_live_ratio == 0.087                  # the rest as usual

    conn, f = collect_one(errors={"ORDER BY runtime": RuntimeError("boom")})
    assert f.top_queries == [] and f.waits == {"Lock": 2, "CPU": 1}

    conn, f = collect_one(errors={"version()": RuntimeError("boom")})
    assert f.pg_version == "?" and f.max_connections is None
    assert f.db_size_bytes == 1610612736               # the version failure does not affect other queries


def test_collect_frame_total_failure_frame_survives():
    keys = ("version()", "pg_database_size", "GROUP BY state", "ORDER BY runtime",
            "GROUP BY wait_event", "blks_hit", "n_dead_tup", "pg_current_wal_lsn",
            "temp_files", "num_timed,", "num_timed_checkpoints", "pg_stat_bgwriter")
    conn = FakeConn(make_responses(), errors={k: RuntimeError("boom") for k in keys})
    f = watch.collect_frame(conn)                      # passes if nothing raises
    assert (f.pg_version, f.db_size_bytes, f.max_connections) == ("?", None, None)
    assert f.backends == {} and f.top_queries == [] and f.waits == {}
    assert f.buffer_hit_ratio is None and f.dead_live_ratio is None
    assert f.wal_bytes_per_s is None and f._lsn is None
    assert f.temp_files is None and f.temp_bytes is None
    assert f.checkpointer_delta is None


# ---------------------------- checkpointer: version gating + fallback chain ----------------------------

def test_collect_frame_checkpointer_pg16_goes_straight_to_bgwriter():
    resp = make_responses()
    resp["version()"] = [[("PostgreSQL 16.4 on x86_64-pc-linux-gnu", "80")]]
    resp["pg_stat_bgwriter"] = [[(5, 2, 1200.0, 300.0, 40, 60)]]
    conn = FakeConn(resp)
    f = watch.collect_frame(conn)
    ck = f.checkpointer_delta
    assert ck["source"] == "pg_stat_bgwriter"
    assert ck["checkpoints_timed"] == 5 and ck["checkpoints_req"] == 2
    assert ck["write_time_s"] == pytest.approx(1.2) and ck["sync_time_s"] == pytest.approx(0.3)
    assert ck["buffers_written"] == 100                # buffers_clean 40 + buffers_backend 60
    assert not any("pg_stat_checkpointer" in sql for sql in conn.executed)
    assert len(conn.executed) == 10                    # gating skips the doomed probe


def test_collect_frame_checkpointer_pg18_rename_fallback():
    """PG17 column names fail → PG18 renamed column (num_timed_checkpoints) → still normalized."""
    resp = make_responses()
    resp["num_timed_checkpoints"] = [[(20, 3, 1000.0, 100.0, 2048)]]
    conn = FakeConn(resp, errors={"num_timed,": RuntimeError('column "num_timed" does not exist')})
    f = watch.collect_frame(conn)
    ck = f.checkpointer_delta
    assert ck["source"] == "pg_stat_checkpointer"
    assert ck["checkpoints_timed"] == 20 and ck["checkpoints_req"] == 3
    assert ck["buffers_written"] == 2048


def test_collect_frame_checkpointer_all_variants_fail():
    resp = make_responses()
    resp["pg_stat_bgwriter"] = [[(5, 2, 1200.0, 300.0, 40, 60)]]
    conn = FakeConn(resp, errors={
        "num_timed,": RuntimeError("boom"), "num_timed_checkpoints": RuntimeError("boom"),
        "pg_stat_bgwriter": RuntimeError("boom")})
    f = watch.collect_frame(conn)
    assert f.checkpointer_delta is None
    assert f.buffer_hit_ratio == 0.9524                # a checkpointer failure does not drag down the whole frame


# ---------------------------- number humanization / Δ coloring helpers ----------------------------

def test_humanize_and_duration_helpers():
    assert watch._human_bytes(None) == "-"
    assert watch._human_bytes(512) == "512B"
    assert watch._human_bytes(2048) == "2.0kB"
    assert watch._human_bytes(5 * 1024 * 1024) == "5.0MB"
    assert watch._human_bytes(3 * 1024 ** 3) == "3.0GB"
    assert watch._pct(None) == "-" and watch._pct(0.087) == "8.7%"
    assert watch._fmt_duration(None) == "-"
    assert watch._fmt_duration(3.25) == "3.2s"
    assert watch._fmt_duration(62.5) == "1m02s"


def test_delta_helpers_direction_colors():
    assert watch._delta_int(15, 13) == "[red]+2[/red]"          # pressure metric up → red
    assert watch._delta_int(13, 15) == "[green]-2[/green]"      # down → green
    assert watch._delta_pp(0.95, 0.90) == "[red]+5.0pp[/red]"
    assert watch._delta_pp(0.95, 0.90, higher_is_bad=False) == "[green]+5.0pp[/green]"  # hit ratio up → green
    assert watch._delta_bytes(31457280, 20971520) == "[red]+10.0MB[/red]"
    assert watch._delta_int(5, 5) == ""                          # no change, not shown
    assert watch._delta_int(None, 3) == ""                       # either side missing, not shown


# ---------------------------- render_frame: one combined panel ----------------------------

def make_frame(**over):
    base = dict(
        ts="12:34:56",
        pg_version="PostgreSQL 17.2 on x86_64-pc-linux-gnu, compiled by gcc",
        db_size_bytes=1610612736, backends={"active": 3, "idle": 10, "background": 2},
        max_connections=100, buffer_hit_ratio=0.9524, dead_live_ratio=0.087,
        wal_bytes_per_s=1048576.0, temp_files=7, temp_bytes=31457280,
        top_queries=[
            {"pid": 4242, "state": "active", "wait_event_type": "Lock", "wait_event": "relation",
             "runtime_s": 62.5,
             "query": "UPDATE pgbench_accounts SET abalance = abalance + 1 WHERE aid = 42"},
            {"pid": 4243, "state": "active", "wait_event_type": None, "wait_event": None,
             "runtime_s": 3.25, "query": "SELECT arr @> ARRAY[1, 2] FROM t"},
        ],
        waits={"Lock": 2, "CPU": 1},
        checkpointer_delta={"checkpoints_timed": 12, "checkpoints_req": 1, "write_time_s": 3.4,
                            "sync_time_s": 0.2, "buffers_written": 1024,
                            "source": "pg_stat_checkpointer"},
    )
    base.update(over)
    return watch.WatchFrame(**base)


def render_text(frame, prev=None, width=110):
    buf = io.StringIO()
    Console(file=buf, width=width).print(watch.render_frame(frame, prev=prev))
    return buf.getvalue()


def test_render_frame_returns_table():
    assert isinstance(watch.render_frame(make_frame()), Table)


def test_render_frame_overview_and_top_rows():
    text = render_text(make_frame())
    for expect in (
            "pgtuner watch · 12:34:56",
            "实例概况", "活动会话 TOP",                       # upper and lower halves
            "PostgreSQL 17.2", "1.5GB",                      # version + humanized database size
            "15/100",                                         # connections x/max
            "95.2%", "8.7%",                                  # ratios with one decimal
            "1.0MB/s",                                        # WAL B/s humanized
            "7 个 / 30.0MB",                                  # temp
            "12 定时 / 1 请求",                                # checkpointer
            "Lock 2 · CPU 1",                                 # active waits summary
            "4242", "Lock/relation", "CPU",                   # top rows: pid / wait
            "1m02s", "3.2s",                                   # runtime humanized
            "UPDATE pgbench_accounts", "ARRAY[1, 2]",          # query text (markup characters not swallowed)
    ):
        assert expect in text, f"渲染文本缺少: {expect}"


def test_render_frame_delta_column():
    prev = make_frame(
        backends={"active": 2, "idle": 11, "background": 0},          # 13 → 15
        buffer_hit_ratio=0.90, dead_live_ratio=0.05,
        temp_bytes=20971520,                                           # 20MB → +10.0MB
        checkpointer_delta={"checkpoints_timed": 10, "checkpoints_req": 1,
                            "write_time_s": 3.0, "sync_time_s": 0.1,
                            "buffers_written": 900, "source": "pg_stat_checkpointer"})
    text = render_text(make_frame(), prev=prev)
    assert "+2" in text            # connections +2 (up → red)
    assert "+5.2pp" in text        # hit ratio 0.90→0.9524 (up → green)
    assert "+3.7pp" in text        # dead/live 0.05→0.087 (up → red)
    assert "+10.0MB" in text       # temp bytes +10MB (up → red)


def test_render_frame_first_frame_placeholders():
    text = render_text(make_frame(wal_bytes_per_s=None, top_queries=[], waits={},
                                  buffer_hit_ratio=None, checkpointer_delta=None))
    assert "首帧无 prev 基准" in text        # WAL has no prev hint
    assert "无活动会话" in text               # top empty
    assert "-" in text                       # hit-ratio missing placeholder


def test_watch_frame_private_cache_not_in_repr_or_eq():
    a, b = make_frame(), make_frame()
    a._lsn, a._ts = "0/AAAAAA", 1.0
    b._lsn, b._ts = "0/BBBBBB", 2.0
    assert a == b                              # compare=False: private caches do not participate in equality
    assert "_lsn" not in repr(a) and "_ts" not in repr(a)


# ---------------------------- snapshot: UI-less text mode ----------------------------

def test_snapshot_renders_last_frame_with_wal_rate(monkeypatch):
    conn = FakeConn(make_responses(frames=3))
    monkeypatch.setattr(watch.pg, "connect", lambda dsn: conn)
    clock = iter([100.0, 101.0, 102.0])               # last two frames Δt = 1s
    monkeypatch.setattr(watch, "_now", lambda: next(clock))
    text = watch.snapshot("dsn-unused", interval=0, frames=3)
    assert conn.closed
    assert len(conn.executed) == 30                   # 3 frames × 10 queries
    assert "实例概况" in text and "活动会话 TOP" in text
    # last frame's WAL rate = (lsn3 - lsn2)/Δt = 1048576 B/s → 1.0MB/s
    assert "1.0MB/s" in text


def test_snapshot_single_frame_no_prev(monkeypatch):
    conn = FakeConn(make_responses(frames=1))
    monkeypatch.setattr(watch.pg, "connect", lambda dsn: conn)
    text = watch.snapshot("dsn-unused", interval=0, frames=1)
    assert conn.closed
    assert "首帧无 prev 基准" in text
    assert "\x1b[" not in text                        # non-tty Console: plain text without ANSI


def test_snapshot_validates_args():
    with pytest.raises(ValueError):
        watch.snapshot("dsn", frames=0)
    with pytest.raises(ValueError):
        watch.snapshot("dsn", interval=-0.5)


def test_snapshot_renders_all_frames(monkeypatch):
    """Regression for tool-w2-watch-noninteractive-last-frame-only: snapshot previously sampled N frames but
    rendered only the last → now every frame is printed, so agent/CI can verify sampling continuity / review the timeline."""
    conn = FakeConn(make_responses(frames=3))
    monkeypatch.setattr(watch.pg, "connect", lambda dsn: conn)
    clock = iter([100.0, 101.0, 102.0])                       # each frame Δt = 1s
    monkeypatch.setattr(watch, "_now", lambda: next(clock))
    text = watch.snapshot("dsn-unused", interval=0, frames=3)
    assert conn.closed
    assert len(conn.executed) == 30                           # 3 frames × 10 queries collected as usual
    assert text.count("pgtuner watch · ") == 3                # all 3 frames output (no longer only the last)
    assert text.count("实例概况") == 3
    assert text.count("首帧无 prev 基准") == 1                  # only the first frame lacks a WAL baseline
    assert text.count("1.0MB/s") == 2                         # the last two frames render WAL diffs as usual
    assert "\x1b[" not in text                                # plain text without ANSI


# ---------------------------- run_live: duration expiry / clean Ctrl-C exit ----------------------------

class _LiveConsole(watch.Console):
    """A Console forced to be treated as an interactive terminal — stably takes the Live path (regardless of whether pytest captures stdout)."""

    @property
    def is_terminal(self):
        return True

    @property
    def is_dumb_terminal(self):
        return False


class _PlainConsole(watch.Console):
    """A Console forced to be treated as non-interactive output — takes the per-frame print path (redirection/CI/script recording)."""

    @property
    def is_terminal(self):
        return False


def test_run_live_exits_on_duration(monkeypatch):
    conn = FakeConn(make_responses(frames=1))
    monkeypatch.setattr(watch.pg, "connect", lambda dsn: conn)
    monkeypatch.setattr(watch, "Console", _LiveConsole)       # pin the Live (interactive) path
    sleeps = []
    monkeypatch.setattr(watch.time, "sleep", lambda s: sleeps.append(s))
    watch.run_live("dsn-unused", interval=0, duration=0)      # duration expires after the first frame
    assert conn.closed
    assert len(conn.executed) == 10
    assert sleeps == []                                       # exit happens before sleep


def test_run_live_keyboard_interrupt_clean(monkeypatch):
    conn = FakeConn(make_responses(frames=1))
    monkeypatch.setattr(watch.pg, "connect", lambda dsn: conn)
    monkeypatch.setattr(watch, "Console", _LiveConsole)       # pin the Live (interactive) path

    def raise_ki(_s):
        raise KeyboardInterrupt

    monkeypatch.setattr(watch.time, "sleep", raise_ki)
    watch.run_live("dsn-unused", interval=0.01, duration=None)   # clean exit as long as KI is not propagated outward
    assert conn.closed
    assert len(conn.executed) == 10


# ---------------------------- non-interactive (redirection/CI/pty recording): per-frame print keeps the full frame sequence ----------------------------

def test_run_live_noninteractive_prints_every_frame(monkeypatch, capsys):
    """Regression for tool-w2-watch-noninteractive-last-frame-only: rich Live refreshes silently on non-terminal stdout
    and leaves only the last frame on exit → when non-interactive, degrade to per-frame print with the full frame sequence reviewable."""
    conn = FakeConn(make_responses(frames=3))
    monkeypatch.setattr(watch.pg, "connect", lambda dsn: conn)
    monkeypatch.setattr(watch, "Console", _PlainConsole)      # redirection/CI/script recording path
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    # _now call order: start → (collect₁, chk) → (collect₂, chk) → (collect₃, chk→duration expires)
    clock = iter([0.0, 0.0, 0.0, 1.0, 1.0, 2.0, 5.0])
    monkeypatch.setattr(watch, "_now", lambda: next(clock))
    watch.run_live("dsn-unused", interval=0, duration=3)      # duration expires after the 3rd frame
    assert conn.closed
    out = capsys.readouterr().out
    assert out.count("pgtuner watch · ") == 3                 # 3 frames printed one by one, no longer only the last
    assert out.count("实例概况") == 3
    assert "1.0MB/s" in out                                   # inter-frame WAL diffs render as usual
    assert "\x1b[" not in out                                 # non-tty Console → plain text without ANSI


def test_run_live_interactive_uses_live_not_print(monkeypatch, capsys):
    """Interactive terminals still take the Live in-place redraw (not a per-frame print flood), behavior unchanged."""
    conn = FakeConn(make_responses(frames=3))
    monkeypatch.setattr(watch.pg, "connect", lambda dsn: conn)
    monkeypatch.setattr(watch, "Console", _LiveConsole)
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    clock = iter([0.0, 0.0, 0.0, 1.0, 1.0, 2.0, 5.0])
    monkeypatch.setattr(watch, "_now", lambda: next(clock))
    watch.run_live("dsn-unused", interval=0, duration=3)      # duration expires after sampling 3 frames
    out = capsys.readouterr().out
    # Live in-place redraw markers: cursor hide/restore control codes + carriage-return repositioning (never seen on the print path)
    assert "\x1b[?25l" in out and "\x1b[?25h" in out          # start/stop cursor control
    assert "\r" in out                                        # in-place redraw (no per-frame page-feed print)
