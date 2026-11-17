"""statements — pg_stat_statements top queries: find the statements most worth optimizing
in the real workload.

Counterpart of crystaldba's get_top_queries; metrics and boundaries (v0.5 design note §3.1, historical):
  - Only the current database is looked at (dbid filter); this command's own queries are
    also recorded by pgss (honest metric, no blacklist)
  - Column metric uses the PG13+ stable columns (the total_exec_time family); temp block
    writes are a "sort/hash spill to disk" signal
  - pgss needs shared_preload_libraries preloading + per-database CREATE EXTENSION;
    self-built instances don't have it on by default → setup() provides a one-shot enable
    flow (refuses to restart with active backends; --force overrides; external instances
    only get guidance)
  - Enabling is an instance-level persistent change (statistics collection itself has a
    slight cost): performance experiments spanning the enable point need care with the metric

Read-only exit: top_queries only reads counters — it executes no user statements and emits
no timings — performance conclusions still go through bench/rebench for ledger evidence.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import instance as instance_mod
from . import pg

PGSS_NAME = "pg_stat_statements"

# sort key → expression (whitelisted; user input is never concatenated)
SORTS = {
    "total": "s.total_exec_time",
    "mean": "s.mean_exec_time",
    "calls": "s.calls",
    "temp": "s.temp_blks_written",
}

_SQL_TOP = (
    "SELECT s.queryid, s.calls, s.rows,\n"
    "       round(s.total_exec_time::numeric, 3), round(s.mean_exec_time::numeric, 6),\n"
    "       s.shared_blks_hit, s.shared_blks_read, s.temp_blks_written,\n"
    "       s.query\n"
    "  FROM pg_stat_statements s\n"
    " WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())\n"
    "   AND s.calls >= %s\n"
    " ORDER BY "
)


@dataclass
class QueryStat:
    """Statistics for one normalized statement (times in ms; block counts as 8kB block counts)."""
    queryid: int
    calls: int
    rows: int
    total_ms: float
    mean_ms: float
    shared_hit: int | None
    shared_read: int | None
    temp_written: int | None
    query: str


# ---------------------------- Availability probing and enable flow ----------------------------

def status(conn) -> dict:
    """pgss tri-state: {installed, created, preloaded} — installed = present in the install
    directory (visible via control), created = CREATE EXTENSION done in this database,
    preloaded = instance has it in shared_preload_libraries.
    Queryable (view readable) only when both created and preloaded."""
    installed = bool(pg.fetch_one(
        conn, f"SELECT 1 FROM pg_available_extensions WHERE name = '{PGSS_NAME}'"))
    created = bool(pg.fetch_one(
        conn, f"SELECT 1 FROM pg_extension WHERE extname = '{PGSS_NAME}'"))
    preloaded = PGSS_NAME in [
        p.strip() for p in pg.fetch_one(conn, "SHOW shared_preload_libraries")[0].split(",")
        if p.strip()]
    return {"installed": installed, "created": created, "preloaded": preloaded}


def _readable(conn) -> bool:
    """Is the view actually readable (querying it without preload raises 55P02 — created=True alone is not enough)?"""
    import psycopg2
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {PGSS_NAME} LIMIT 1")
            return True
    except psycopg2.Error:
        return False


def ensure_ready(dsn: str) -> str:
    """Ensure pgss is queryable on the target database; returns a status description, or
    raises RuntimeError with guidance: preloaded but no extension → CREATE EXTENSION directly
    (seconds); not preloaded → point to --setup."""
    conn = pg.connect_rw(dsn)
    try:
        st = status(conn)
        if st["created"] and _readable(conn):
            return "已启用"
        if not st["installed"]:
            raise RuntimeError(
                f"安装目录没有 {PGSS_NAME}(pg_available_extensions 不可见):"
                f"需在 PG 源码 contrib/{PGSS_NAME} 下 make PG_CONFIG=<bindir>/pg_config install")
        if st["preloaded"]:
            pg.execute(conn, f'CREATE EXTENSION IF NOT EXISTS "{PGSS_NAME}"')
            return "本库已补 CREATE EXTENSION(实例本就预载,无需重启)"
        raise RuntimeError(
            f"{PGSS_NAME} 未预载(shared_preload_libraries 不含):"
            f"自建实例跑 `pgtuner statements --setup --instance <名> --db <库>` 一键启用;"
            f"外部实例需 DBA 手工 ALTER SYSTEM SET shared_preload_libraries="
            f"'...,{PGSS_NAME}' 并重启后每库 CREATE EXTENSION")
    finally:
        conn.close()


def setup(instance_name: str, dbname: str, *, force: bool = False,
          registry_file=None) -> dict:
    """One-shot pgss enable (v0.5 design note §3.1, historical): availability check → active-backend
    gate → ALTER SYSTEM appends to shared_preload_libraries (preserving existing values) →
    stop/start → CREATE EXTENSION on the target database → read-back verification. Returns
    a step report.

    An explicit --setup carries the human-confirmation semantics (aligned with iron rule 4's
    restart confirmation); this is an instance-level persistent change.
    Externally registered instances (managed=False) cannot be restarted on their behalf:
    the preload part only prints manual guidance.
    """
    import psycopg2

    inst = instance_mod.get(instance_name, registry_file)
    dsn = instance_mod.dsn_of(instance_name, dbname, registry_file)

    conn = pg.connect_rw(dsn)
    try:
        st = status(conn)
        if st["created"] and _readable(conn):
            return {"changed": False, "restart": False, "note": f"{dbname}@{instance_name} 已启用"}
        if not st["installed"]:
            raise RuntimeError(
                f"实例 {instance_name} 的安装目录({inst.bindir})没有 {PGSS_NAME}:"
                f"需在 PG 源码 contrib/{PGSS_NAME} 下"
                f"make PG_CONFIG={inst.bindir}/pg_config install 后重试")
    finally:
        conn.close()
    if st["preloaded"]:                       # only the per-database extension is missing; no restart needed
        conn = pg.connect_rw(dsn)
        try:
            pg.execute(conn, f'CREATE EXTENSION IF NOT EXISTS "{PGSS_NAME}"')
        finally:
            conn.close()
        return {"changed": True, "restart": False, "note": "实例已预载,仅补了本库 CREATE EXTENSION"}

    if not inst.managed:
        raise RuntimeError(
            f"实例 {instance_name} 为外部注册(managed=False),无法代为重启。手工步骤:\n"
            f"  ALTER SYSTEM SET shared_preload_libraries = "
            f"'<既有值>,{PGSS_NAME}';\n"
            f"  重启实例后:CREATE EXTENSION {PGSS_NAME};")

    if not force:
        conn = pg.connect_rw(instance_mod.dsn_of(instance_name, "postgres", registry_file))
        try:
            n_active = pg.fetch_one(conn, (
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE state = 'active' AND backend_type = 'client backend' "
                "AND pid <> pg_backend_pid()"))[0]
        finally:
            conn.close()
        if n_active:
            raise RuntimeError(
                f"实例 {instance_name} 上有 {n_active} 个活跃客户端后端,重启会打断它们;"
                f"确认无碍加 --force(或等其空闲再试)")

    # append the preload (preserving existing values); takes effect on restart after ALTER SYSTEM
    conn = pg.connect_rw(dsn)
    try:
        cur_val = pg.fetch_one(conn, "SHOW shared_preload_libraries")[0]
        parts = [p.strip() for p in (cur_val or "").split(",") if p.strip()]
        if PGSS_NAME not in parts:
            parts.append(PGSS_NAME)
        new_val = ", ".join(parts).replace("'", "''")
        pg.execute(conn, f"ALTER SYSTEM SET shared_preload_libraries = '{new_val}'")
    finally:
        conn.close()
    instance_mod.stop(instance_name, registry_file=registry_file)
    instance_mod.start(instance_name, registry_file=registry_file)

    conn = pg.connect_rw(dsn)
    try:
        pg.execute(conn, f'CREATE EXTENSION IF NOT EXISTS "{PGSS_NAME}"')
        if not _readable(conn):
            raise RuntimeError("重启+CREATE EXTENSION 后视图仍不可读,请检查日志")
        ver = pg.fetch_one(conn, f"SELECT extversion FROM pg_extension WHERE extname = '{PGSS_NAME}'")
    finally:
        conn.close()
    return {"changed": True, "restart": True,
            "note": f"预载+重启+建扩展完成,extversion={ver[0] if ver else '?'};"
                    f"克隆模板库若也要带统计,对模板库再跑一遍 --setup(实例级已就绪,只会补建扩展)"}


# ---------------------------- top queries ----------------------------

def top_queries(conn, *, sort: str = "total", limit: int = 10,
                min_calls: int = 0) -> list[QueryStat]:
    """Take the top `limit` statements by sort (total/mean/calls/temp) (pg_stat_statements,
    current database). No filtering blacklist — this command's own queries being recorded
    is the honest metric, explained in the footnote."""
    if sort not in SORTS:
        raise ValueError(f"无效排序键: {sort}(应为 {'/'.join(SORTS)})")
    sql = _SQL_TOP + SORTS[sort] + " DESC NULLS LAST LIMIT %s"
    return [QueryStat(*r) for r in pg.fetch_all(conn, sql, (min_calls, limit))]


# ---------------------------- Rendering ----------------------------

def render_text(stats: list[QueryStat], *, sort: str) -> str:
    """Compact plain text (v0.6 data-plane slimming): aligned columns + one-line query previews; full fidelity via --format json."""
    from .render import align_table, oneline

    rows = [[f"{s.calls:,}", f"{s.total_ms:,.1f}", f"{s.mean_ms:,.3f}",
             f"{s.temp_written:,}" if s.temp_written is not None else "-",
             oneline(s.query, 56)] for s in stats]
    body = align_table(["调用", "总ms", "均ms", "临时写", "查询"], rows)
    return (f"pg_stat_statements top{len(stats)}(排序:{sort};当前库口径;耗时 ms;"
            f"块命中/块读等全字段见 --format json)\n{body}\n"
            f"(本命令自身的查询也会被记录;临时写>0 = 有排序/哈希落盘)")


def render_json(stats: list[QueryStat]) -> str:
    """Machine-readable JSON (full query text, pure JSON on stdout — same convention as sql/schema)."""
    import json
    return json.dumps([s.__dict__ for s in stats], ensure_ascii=False, default=str)
