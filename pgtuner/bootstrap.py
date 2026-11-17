"""bootstrap: lab bootstrap engine — turns a (new) instance into a "ready to work"
state in one command (v0.9.0, absorbing the patch-trial F3 backlog: previously,
switching instances required four manual steps: create → pgss --setup → make-tpl →
config set).

Three-step closed loop (each step idempotent; reruns only fill gaps):
① statements.setup: pgss preload (with a stop/start restart (~10s) if necessary —
   explicitly running bootstrap carries the human-confirmation semantics, aligned
   with iron rule 4) + create the extension in the postgres database;
② standard template: CREATE DATABASE → **CREATE EXTENSION (must happen before
   sealing** — after sealing, datallowconn=false makes the database unreachable;
   clones inherit the extension via TEMPLATE so statements works out of the box
   in clones) → pgbench -i -s N (external binary) → template payload SQL
   (default: the packaged pgtuner/assets/template_setup.sql) → seal + read-back
   verification (skeleton instance._build_sealed_db; on any failure the
   half-built database is DROPped);
③ config set default-instance: set automatically when unset; when another
   instance is already set, don't steal it by default (only --set-default changes it).
"""
from __future__ import annotations

import os
from pathlib import Path

from . import bench as bench_mod
from . import config as config_mod
from . import instance as instance_mod
from . import pg as pg_mod
from . import statements as statements_mod

DEFAULT_TEMPLATE = "bench_tpl"
DEFAULT_SCALE = 10
# Packaged asset (v0.11.0 packaging fix): resolves inside the package for wheel
# installs and inside the repo for editable installs.
DEFAULT_SETUP_SQL = Path(__file__).resolve().parent / "assets" / "template_setup.sql"


def _exec(instance_name: str, dbname: str, sql: str, registry_file=None) -> None:
    """Execute a single (possibly multi-statement) SQL chunk on a rw connection; failure becomes RuntimeError."""
    conn = pg_mod.connect_rw(instance_mod.dsn_of(instance_name, dbname, registry_file))
    try:
        pg_mod.execute(conn, sql)
    except Exception as e:                                   # noqa: BLE001
        raise RuntimeError(f"SQL 执行失败（{dbname}@{instance_name}）: {e}") from None
    finally:
        conn.close()


def _build_bench_template(instance_name: str, template: str, scale: int,
                          setup_sql_file: str | Path | None, inst: instance_mod.Instance,
                          registry_file=None) -> None:
    """Populate phase of the standard bench template: extension → pgbench -i → payload
    SQL; database creation / sealing / verification / cleanup are handled by the
    _build_sealed_db skeleton."""
    sql_path = Path(setup_sql_file) if setup_sql_file else DEFAULT_SETUP_SQL
    if not sql_path.is_file():
        raise ValueError(f"模板载荷 SQL 不存在: {sql_path}(--setup-sql 可显式指定)")
    setup_sql = sql_path.read_text(encoding="utf-8")
    pgbench = bench_mod.resolve_pgbench(bindir=inst.bindir)
    if not pgbench:
        raise ValueError(
            "pgbench 未找到(实例 bindir/PATH 均无):bash scripts/setup.sh --pg <prefix> "
            "编译安装,或先 pgtuner instance create 登记一个带 bin 的安装")

    def populate(n: str, db: str, rf) -> None:
        # ① pgss extension: created before sealing; clones inherit it via TEMPLATE (same guidance as the note in statements.setup)
        _exec(n, db, 'CREATE EXTENSION IF NOT EXISTS "pg_stat_statements"', rf)
        # ② pgbench standard tables (-i -s N; external binary, not SQL, hence not expressible in a make-tpl single file)
        host, port, _ = pg_mod.target(instance_mod.dsn_of(n, db, rf))
        proc = bench_mod.run_init(pgbench, host, port, db, scale=scale)
        if proc.returncode != 0:
            raise RuntimeError("pgbench -i 失败: " + (proc.stderr or proc.stdout)[-300:])
        # ③ standard payload (fixed-width history backfill + b2_t etc.; empty file skipped — same as the B1 fix in make_template)
        if setup_sql.strip():
            _exec(n, db, setup_sql, rf)

    instance_mod._build_sealed_db(instance_name, template, populate, registry_file)


def bootstrap(instance_name: str, *, template: str = DEFAULT_TEMPLATE, scale: int = DEFAULT_SCALE,
              setup_sql_file: str | Path | None = None, force_default: bool = False,
              registry_file=None) -> dict:
    """Turn an instance into a ready-to-work state in three steps; returns
    {"steps": [{name, changed, note}...]}.
    Idempotent: skips when pgss is already enabled / the template exists / the
    default instance is already set; half-built artifacts are cleaned by the skeleton."""
    steps: list[dict] = []
    inst = instance_mod.get(instance_name, registry_file)     # missing → ValueError, fail early
    instance_mod.ensure_running(instance_name)
    if inst.build_type == "debug":
        steps.append({"name": "构建警告", "changed": False,
                      "note": "debug 构建:TPS 绝对值无意义,仅可组间对比(铁律 5)"})

    # ① pgss (idempotent; setup does a stop/start restart when not preloaded)
    r = statements_mod.setup(instance_name, "postgres", registry_file=registry_file)
    note = r["note"]
    if template in instance_mod.templates(instance_name, registry_file):
        note += ";模板已存在(扩展是否在模板内以 lab templates 前一次 bootstrap 为准)"
    else:
        # The "run --setup once more on the template database" tail of setup's note does
        # not apply in the bootstrap context: ② builds the extension directly into the
        # unsealed new template, clones inherit via TEMPLATE, no re-run needed
        note += "(②建模板时已把扩展建入模板,克隆库自动继承)"
    steps.append({"name": "pg_stat_statements", "changed": r["changed"], "note": note})

    # ② standard template (idempotent: skipped when the sealed database already exists — never rebuilt, never disturbs the existing clone pool)
    if template in instance_mod.templates(instance_name, registry_file):
        steps.append({"name": f"模板 {template}", "changed": False, "note": "已存在,跳过"})
    else:
        _build_bench_template(instance_name, template, scale, setup_sql_file, inst, registry_file)
        sql_name = Path(setup_sql_file).name if setup_sql_file else DEFAULT_SETUP_SQL.name
        steps.append({"name": f"模板 {template}", "changed": True,
                      "note": f"pgbench -i -s {scale} + {sql_name},已密封可克隆"})

    # ③ default instance (only writes the config file; env PGTUNER_INSTANCE always takes transient priority, noted in the note)
    file_cur = config_mod.load().get("default_instance")
    if file_cur == instance_name:
        note = f"已设为 {instance_name}"
        changed = False
    elif file_cur is None or force_default:
        config_mod.save("default_instance", instance_name)
        changed = True
        note = f"config set default-instance {instance_name}"
        if file_cur:
            note += f"(原 {file_cur} 被覆盖)"
    else:
        changed = False
        note = f"保持 {file_cur}(要改加 --set-default)"
    env_cur = os.environ.get("PGTUNER_INSTANCE")
    if env_cur and env_cur != instance_name:
        note += f";env PGTUNER_INSTANCE={env_cur} 仍会临时覆盖"
    steps.append({"name": "默认实例", "changed": changed, "note": note})

    return {"steps": steps}
