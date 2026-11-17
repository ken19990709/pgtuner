"""psycopg2 connection helpers: PGHOST/PGPORT/PGDATABASE defaults → /tmp:5432/bench_new; --dsn support; read-only queries.

Shared by the check / bench layers. psycopg2 is imported lazily — the first
layer (probe/recommend/list) works without the driver installed.
Connection failures get cause-specific hints: server not running / database
missing / driver missing.
"""
from __future__ import annotations

import os

DEFAULT_HOST = "/tmp"     # socket directory
DEFAULT_PORT = 5432
DEFAULT_DB = "bench_new"


def target(dsn: str | None = None) -> tuple[str, str, str]:
    """Resolve the connection target → (host, port, dbname). dsn accepts libpq keyword strings and postgresql:// URIs."""
    host = os.environ.get("PGHOST") or DEFAULT_HOST
    port = os.environ.get("PGPORT") or str(DEFAULT_PORT)
    dbname = os.environ.get("PGDATABASE") or DEFAULT_DB
    if dsn:
        from psycopg2.extensions import parse_dsn   # lazy import
        kw = parse_dsn(dsn)
        host = kw.get("host") or host
        port = kw.get("port") or port
        dbname = kw.get("dbname") or dbname
    return host, port, dbname


def connect(dsn: str | None = None):
    """Read-only connection (autocommit + readonly). Failure raises ConnectionError with cause-classified hints."""
    try:
        import psycopg2
    except ImportError:
        raise ConnectionError(
            "psycopg2 未安装：pip install psycopg2-binary（第一层 probe/recommend/list 不需要它，"
            "check/bench 需要）") from None
    if dsn:
        conninfo = dsn
    else:
        host, port, dbname = target()
        conninfo = f"host={host} port={port} dbname={dbname}"
    try:
        conn = psycopg2.connect(conninfo, connect_timeout=5)
    except psycopg2.OperationalError as e:
        full = str(e).strip()
        msg = full.splitlines()[-1] if full else "unknown"
        host, port, dbname = target(dsn)
        where = f"{host}:{port}/{dbname}"
        if "connection refused" in full or "No such file or directory" in full:
            hint = (f"服务没起？目标 {where}。可用 pgtuner instance list 查已注册实例、"
                    f"pgtuner instance dsn NAME DB 取连接串，或用 --dsn 指向已有库；"
                    f"裸起库请用你安装目录的 pg_ctl（或检查 PGHOST/PGPORT）")
        elif "does not exist" in msg:
            hint = (f"库不存在？目标 {where}。创建：createdb -h {host} -p {port} {dbname}，"
                    f"或用 --dsn 指向已有库")
        elif "password" in msg:
            hint = (f"认证失败：目标 {where}（本工具假定 trust 免密；需要密码时优先用 PGPASSWORD "
                    f"环境变量或 ~/.pgpass，再以 --dsn 'host=...' 指定——密码写在命令行会进 "
                    f"shell 历史与 /proc/*/cmdline）")
        else:
            hint = f"连接失败：目标 {where}"
        raise ConnectionError(f"{hint}\n原始错误: {msg}") from None
    conn.set_session(autocommit=True, readonly=True)   # read-only throughout
    return conn


def create_db(dsn: str | None = None, dbname: str | None = None) -> None:
    """CREATE DATABASE dbname on the maintenance database postgres (bench --init
    creates the database automatically when missing).

    Only host/port/credentials are taken from dsn (make_dsn overrides
    dbname=postgres); dbname is the database to create (defaulting to the target
    database resolved from dsn/environment). If the database already exists
    (DuplicateDatabase), skip silently — idempotent; if connecting to the
    maintenance database fails, raise ConnectionError (when the server is not
    running, bench --init still reports the original connection error);
    any other creation failure raises RuntimeError (e.g. insufficient privileges).
    """
    try:
        import psycopg2
    except ImportError:
        raise ConnectionError("psycopg2 未安装：pip install psycopg2-binary") from None
    if not dbname:
        dbname = target(dsn)[2]
    if dsn:
        from psycopg2.extensions import make_dsn     # lazy import
        maint = make_dsn(dsn, dbname="postgres")     # only overrides dbname; host/port/credentials kept
    else:
        host, port, _ = target()
        maint = f"host={host} port={port} dbname=postgres"
    try:
        conn = psycopg2.connect(maint, connect_timeout=5)
    except psycopg2.OperationalError as e:
        msg = str(e).strip().splitlines()[-1] if str(e).strip() else "unknown"
        raise ConnectionError(f"维护库 postgres 连接失败: {msg}") from None
    conn.set_session(autocommit=True, readonly=False)   # CREATE DATABASE must run outside a transaction
    try:
        with conn.cursor() as cur:
            cur.execute('CREATE DATABASE "' + dbname.replace('"', '""') + '"')
    except psycopg2.errors.DuplicateDatabase:
        pass                                            # already exists: idempotent skip
    except psycopg2.Error as e:
        raise RuntimeError(f"CREATE DATABASE {dbname} 失败: "
                           f"{str(e).strip().splitlines()[-1]}") from None
    finally:
        conn.close()


def connect_rw(dsn: str | None = None, autocommit: bool = True):
    """Read-write connection (autocommit). Used by planbench / instance / restartbench —
    EXPLAIN ANALYZE executes the statement, and CREATE DATABASE/INDEX and ALTER SYSTEM
    all need a non-read-only session. Failure hints reuse connect's classification."""
    try:
        import psycopg2
    except ImportError:
        raise ConnectionError("psycopg2 未安装：pip install psycopg2-binary") from None
    if not dsn:
        host, port, dbname = target()
        dsn = f"host={host} port={port} dbname={dbname}"
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except psycopg2.OperationalError as e:
        raise ConnectionError(f"连接失败（rw）: {str(e).strip().splitlines()[-1]}") from None
    conn.set_session(autocommit=autocommit, readonly=False)
    return conn


def execute(conn, sql: str):
    """Execute a write statement (DDL/DML); committed directly on an autocommit connection. Returns the affected row count (None rows when the cursor has no description)."""
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.rowcount


def fetch_one(conn, sql: str, params: tuple | None = None):
    """Execute read-only SQL and return a single row (tuple). params is a psycopg2
    parameter tuple (safe even when values contain quotes/newlines, so callers
    don't have to build f-string SQL)."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_all(conn, sql: str, params: tuple | None = None) -> list:
    """Execute read-only SQL and return all rows (params as in fetch_one)."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()
