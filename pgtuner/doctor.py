"""doctor: environment pre-flight check (read-only, zero side effects) — one command before
starting work on a new machine / with a new agent answers "can I start, what's missing,
how to fix it" (v0.9.0, fixing the "no checkup on init" gap).

Division of labor with selftest: selftest validates **the tool itself** (code-quality
gates), doctor validates **environment readiness** (python deps / instance registry /
default instance / templates / pgss / pgbench / ledger writability). doctor never starts
or stops instances and never writes any state — repair actions belong to scripts/setup.sh
and lab bootstrap; doctor only hands out precise repair commands.

Levels: ok=ready / warn=usable degraded (not blocking) / fail=blocks work (CLI exit 1) /
skip=no checkable target.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import bench as bench_mod
from . import config as config_mod
from . import instance as instance_mod
from . import paths as paths_mod
from . import pg as pg_mod
from . import statements as statements_mod
from .paths import LEDGER_DIR

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

# Shared repair entry for the template/pgss checks: bootstrap lays everything down in one command
_BOOTSTRAP_FIX = "pgtuner lab bootstrap --instance <名>   # 一键:pgss 预载+标准模板+默认实例"
# Dependency-class repairs point at the tool repo's install script (in an outside-project
# context, scripts/setup.sh is a path inside the tool repo)
_SETUP_FIX = "在 pgtuner 工具仓跑 bash scripts/setup.sh(见其 README 安装节)"
_SETUP_PG_FIX = "在 pgtuner 工具仓跑 bash scripts/setup.sh --pg <prefix>(见其 README 安装节)"


def _row(name: str, level: str, detail: str, fix: str = "") -> dict:
    return {"name": name, "level": level, "detail": detail, "fix": fix}


def _check_deps() -> dict:
    """psycopg2 importability (a lazy dependency; missing it makes every DB-connecting command unusable → blocking)."""
    try:
        import psycopg2  # noqa: F401
        return _row("python 依赖", OK, "click/rich/psycopg2 可导入")
    except ImportError as e:
        return _row("python 依赖", FAIL, f"psycopg2 不可导入({e})——所有连库命令不可用",
                    _SETUP_FIX)


def _check_registry() -> dict:
    try:
        insts = instance_mod.list_instances()
    except (RuntimeError, OSError) as e:
        return _row("实例注册表", FAIL, f"instances.json 不可读: {e}",
                    "修复 .pgtuner/instances.json 或删除后重新登记(pgtuner instance register/create)")
    if not insts:
        return _row("实例注册表", FAIL, "0 个实例",
                    f"{_SETUP_PG_FIX} 编译安装 → pgtuner instance create <名> <prefix>")
    running = [i for i in insts if instance_mod.is_ready(i.name)]
    release = [i for i in insts if i.build_type == "release"]
    detail = f"{len(insts)} 实例 / {len(running)} 在跑 / {len(release)} release"
    if not release:
        return _row("实例注册表", WARN, detail + "——只有 debug(铁律 5:TPS 禁入结论)",
                    f"{_SETUP_PG_FIX} 编译 release 并 instance create")
    return _row("实例注册表", OK, detail)


def _check_ledger() -> dict:
    """Ledger writability (iron rule 2: every experiment must land in the ledger; unwritable
    = all experiments voided → blocking). Read-only probe: a missing directory is not an
    error — walk up to the deepest ancestor that **exists**; if that ancestor is writable,
    the whole path can be auto-created (on a fresh checkout without even .pgtuner, this
    falls back to judging at the project root, avoiding a false blocking report)."""
    if LEDGER_DIR.exists() and not LEDGER_DIR.is_dir():
        return _row("账本可写", FAIL, f"{LEDGER_DIR} 不是目录", "移除同名文件后重跑")
    probe = LEDGER_DIR
    while not probe.exists():
        probe = probe.parent
    where = str(LEDGER_DIR) if probe == LEDGER_DIR else f"{LEDGER_DIR}(待自动创建)"
    if os.access(probe, os.W_OK):
        return _row("账本可写", OK, where)
    return _row("账本可写", FAIL, f"{probe} 不可写", "检查 .pgtuner/ 属主与权限(chown/chmod)")


def _target(instance_name: str | None) -> instance_mod.Instance | None:
    """Checkup target for templates/pgss/pgbench: explicit --instance > default instance; unusable (unset / not running) → None."""
    name = instance_name or config_mod.default_instance()
    if not name:
        return None
    try:
        inst = instance_mod.get(name)
    except ValueError:
        return None
    if not instance_mod.is_ready(name):
        return None
    return inst


def _check_default() -> dict:
    name = config_mod.default_instance()
    env_note = "(env PGTUNER_INSTANCE 覆盖)" if os.environ.get("PGTUNER_INSTANCE") else ""
    if not name:
        return _row("默认实例", WARN, "未设——数据面命令每次都要 --instance",
                    "pgtuner config set default-instance <名>  或 pgtuner lab bootstrap")
    try:
        inst = instance_mod.get(name)
    except ValueError:
        return _row("默认实例", WARN, f"{name} 不在注册表 {env_note}",
                    "pgtuner config set default-instance <已注册名>")
    if not instance_mod.is_ready(name):
        return _row("默认实例", WARN, f"{name}({inst.build_type}) 未在跑 {env_note}",
                    f"pgtuner instance start {name}")
    return _row("默认实例", OK, f"{name}({inst.pg_version} {inst.build_type}) 在跑 {env_note}".rstrip())


def _check_templates(inst: instance_mod.Instance | None) -> dict:
    if inst is None:
        return _row("模板", SKIP, "无可用的默认实例(--instance 指定或 config set)")
    try:
        tpls = instance_mod.templates(inst.name)
    except Exception as e:                                   # noqa: BLE001
        return _row("模板", WARN, f"查询失败: {e}", _BOOTSTRAP_FIX)
    if not tpls:
        return _row("模板", WARN, "无模板库——lab clone 不可用", _BOOTSTRAP_FIX)
    if "bench_tpl" not in tpls:
        return _row("模板", WARN, f"{', '.join(tpls)}(缺标准 bench_tpl)", _BOOTSTRAP_FIX)
    return _row("模板", OK, ", ".join(tpls))


def _check_pgss(inst: instance_mod.Instance | None) -> dict:
    if inst is None:
        return _row("pg_stat_statements", SKIP, "无可用的默认实例(--instance 指定或 config set)")
    try:
        conn = pg_mod.connect_rw(instance_mod.dsn_of(inst.name, "postgres"))
    except ConnectionError as e:
        return _row("pg_stat_statements", WARN, f"连不上 postgres 库: {e}")
    try:
        st = statements_mod.status(conn)
    except Exception as e:                                   # noqa: BLE001
        return _row("pg_stat_statements", WARN, f"探测失败: {e}")
    finally:
        conn.close()
    if not st["installed"]:
        return _row("pg_stat_statements", WARN, "安装目录无扩展(contrib 未编译安装)",
                    f"{_SETUP_PG_FIX}  # 含 contrib/pg_stat_statements")
    if not st["preloaded"]:
        return _row("pg_stat_statements", WARN,
                    "未预载(shared_preload_libraries)——statements/diagnose --verify 不可用",
                    _BOOTSTRAP_FIX)
    if not st["created"]:
        return _row("pg_stat_statements", OK, "已预载(postgres 库未建扩展;bootstrap 的模板库会带)")
    return _row("pg_stat_statements", OK, "已预载+postgres 已建扩展")


def _check_pgbench(inst: instance_mod.Instance | None) -> dict:
    path = bench_mod.resolve_pgbench(bindir=inst.bindir if inst else None)
    if path:
        return _row("pgbench 二进制", OK, path)
    return _row("pgbench 二进制", WARN,
                "bindir/PATH 均未找到——bench/rebench 的 pgbench 负载不可用",
                f"{_SETUP_PG_FIX},或命令上 --pgbench 显式指定")


def _check_anchor() -> dict:
    """State anchor (troubleshooting-critical, v0.10.0): which project root the
    ledger/registry land in, and which marker hit. A bare cwd (no .git, no .pgtuner)
    → warn: state would land in the current directory; recommend pgtuner init."""
    env = os.environ.get("PGTUNER_PROJECT_ROOT")
    if env:
        return _row("状态锚点", OK, f"{Path(env).resolve()}(env PGTUNER_PROJECT_ROOT)")
    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / ".pgtuner").exists():
            return _row("状态锚点", OK, f"{d}(.pgtuner)")
        if (d / ".git").exists():
            return _row("状态锚点", OK, f"{d}(git)")
    return _row("状态锚点", WARN,
                f"{paths_mod.PROJECT_ROOT}(裸 cwd,无 .git/.pgtuner——状态落当前目录)",
                "在该项目跑 pgtuner init(落状态骨架+知识层),或设 PGTUNER_PROJECT_ROOT")


def run(instance_name: str | None = None) -> list[dict]:
    """Run all checks and return result rows (name/level/detail/fix). Read-only with zero
    side effects; check only, never modify."""
    try:
        inst = _target(instance_name)
    except (ValueError, RuntimeError):
        raise        # explicit --instance naming a nonexistent instance: let the CLI error out directly, don't degrade to skip
    return [
        _check_deps(),
        _check_registry(),
        _check_ledger(),
        _check_default(),
        _check_templates(inst),
        _check_pgss(inst),
        _check_pgbench(inst),
        _check_anchor(),
    ]
