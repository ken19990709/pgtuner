"""click entry point: the probe / recommend / list subcommands.

To add check / bench later: just append another @cli.command function in the
"subcommand registration zone" below — no other code needs to change.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import click
from rich.console import Console

from . import __version__, advise as advise_mod, bench as bench_mod, config as config_mod
from . import bootstrap as bootstrap_mod, diagnose as diagnose_mod, doctor as doctor_mod
from . import projinit as projinit_mod
from . import hardware, hypo as hypo_mod
from . import introspect, pg, report, signals, sqlcmd, statements as statements_mod
from . import instance as instance_mod
from . import ledger as ledger_mod
from . import planbench as planbench_mod
from . import restartbench as rebench_mod
from . import selftest as selftest_mod
from . import sqlsplit, watch as watch_mod
from . import benchmark as benchmark_mod
from . import i18n
from .paths import KNOWLEDGE_FILE, PLANS_DIR, REPO_ROOT, ensure_dirs
from .engine import recommend_all
from .inputs import (DISK_TYPES, PG_VERSION_CAP, RISK_APPETITES, WORKLOAD_TYPES,
                     build_inputs, is_extrapolated)
from .knowledge import KnowledgeError, load_knowledge

# Repository root anchored to the package file location (same approach as
# paths.LEDGER_FILE; editable install → tool repo), independent of the process
# cwd: bare commands from any directory still resolve the default knowledge base (tool-w3)
DEFAULT_KNOWLEDGE = str(KNOWLEDGE_FILE)
# Fixed width for sql/schema text output: when piped in agent/CI scenarios,
# rich's default 80 columns squeezes numeric columns into truncated heads like
# "估 …" (unreadable); fixed width 120 mirrors watch snapshot's width=100 (explicit width)
_WIDE = 120


def _wide_console() -> Console:
    return Console(width=_WIDE)


class _CliGroup(click.Group):
    """Root group: PGTUNER_LANG=en help translation + uniform error rendering.

    All commands are registered at import time, so one eager walk here covers
    every subcommand's --help; missing table entries fall back to Chinese
    (fail-open, see pgtuner/i18n.py).

    invoke() additionally converts the common library exceptions (unregistered
    instance, connection failures with their classified hints, …) into
    ClickException so agents get "Error: <hint>" instead of a 20-line traceback
    (adversarial review R1-B: the single most frequent operational error — a
    typo'd instance name — used to dump a stack trace). The net covers the whole
    OSError family (R3 C-F4: IsADirectoryError from a directory passed to a file
    option) and the psycopg2 exception family (matched by module name; the
    driver stays an optional import). ClickException/Abort pass through
    untouched. A missing psycopg2 module (the documented driver-less wheel
    install) renders as a one-line install hint too (B-MINOR-1).
    """

    def main(self, *args, **kwargs):
        i18n.apply(self)
        return super().main(*args, **kwargs)

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (click.ClickException, click.exceptions.Abort, click.exceptions.Exit):
            raise                                    # Exit/Abort subclass RuntimeError — click's own control flow, not an error
        except (OSError, ValueError, RuntimeError) as e:
            # OSError replaces the old (FileNotFoundError, PermissionError) list:
            # it also covers the siblings that list missed — IsADirectoryError,
            # TimeoutError, … (R3 C-F4: a directory passed to a file option used
            # to escape as a 20-line traceback). ConnectionError is an OSError
            # subclass, so the old ConnectionError entry is subsumed.
            raise click.ClickException(str(e) or type(e).__name__) from e
        except ModuleNotFoundError as e:
            # B-MINOR-1 (round 4): psycopg2 is an optional [db] extra (README
            # documents wheel installs without it); sql/diagnose/planbench import
            # it lazily at the call boundary, so a missing driver used to escape
            # as a 12-line ModuleNotFoundError traceback while `statements`
            # (wrapped in pg.py) already rendered a one-line install hint.
            # Matched by module name, psycopg2* only — other missing modules
            # stay a loud traceback (real packaging bugs, not a known state).
            if e.name and e.name.split(".")[0] == "psycopg2":
                raise click.ClickException(
                    'psycopg2 未安装：pip install "psycopg2-binary" 或 pip install "pgtuner[db]"') from e
            raise
        except Exception as e:
            # psycopg2's exception family (psycopg2.Error and its SQLSTATE
            # subclasses, raised wherever a driver call escapes the classified
            # ConnectionError paths) renders as a one-line driver error instead
            # of a traceback. Matched by module NAME, never by import — psycopg2
            # is an optional dependency and the CLI must run without it.
            if type(e).__module__.split(".")[0] == "psycopg2":
                raise click.ClickException(f"PostgreSQL 驱动错误: {e}") from e
            raise


@click.group(cls=_CliGroup)
@click.version_option(__version__)
def cli() -> None:
    """pgtuner —— Agent 的 PostgreSQL 实验工作台(性能证据走账本,禁止裸 psql 计时)。

    探测实验三命令闭环(默认实例一次设定: pgtuner config set default-instance <N>):

    \b
      db=$(pgtuner lab clone --template <模板>)   # stdout 仅库名,可直接 $() 捕获
      <跑负载/预热>                               # python3 …"$db" / pgtuner bench
      pgtuner diagnose --file q.sql --db "$db" --verify --release

    \b
    --verify: 基线实测→贪心建索引(至多3条)→复测→A/B diff,自动回填结论
    --release: 用完即还(证据已在账本,克隆归还克隆池)
    """


# ---------------------------- subcommand registration zone ----------------------------

@cli.command()
@click.option("--knowledge", "knowledge_path", default=DEFAULT_KNOWLEDGE,
              show_default=True, help="知识库 JSON 路径")
@click.option("--bench-path", default="/tmp", show_default=True,
              help="磁盘微基准测试文件的写入目录")
def probe(knowledge_path: str, bench_path: str) -> None:
    """硬件探测报告（各信号 + 置信度；不运行 pg_test_fsync）。"""
    console = Console()
    hw = hardware.probe(bench_path)
    console.print(report.render_hw_panel(hw))
    fsync = shutil.which("pg_test_fsync")
    if fsync:
        console.print(f"[green]pg_test_fsync 可用[/green]: {fsync}"
                      "（如需实测 wal_sync_method 可手动运行 pg_test_fsync -s 2 -f <pgdata>/fsync_testfile，本工具不主动运行）")
    else:
        console.print("[yellow]pg_test_fsync 不可用[/yellow]（wal_sync_method 实测需自行编译安装 PostgreSQL contrib）")
    try:
        knowledge = load_knowledge(knowledge_path)
    except KnowledgeError as e:
        console.print(f"[yellow]知识库不可用（{e}），跳过探测命令参考展示[/yellow]")
        return
    console.print(report.render_probes_table(knowledge.hardware_probes))


@cli.command()
@click.option("--knowledge", "knowledge_path", default=DEFAULT_KNOWLEDGE,
              show_default=True, help="知识库 JSON 路径")
@click.option("--workload", type=click.Choice(WORKLOAD_TYPES), default="mixed",
              show_default=True, help="负载类型")
@click.option("--max-connections", type=click.IntRange(min=1), default=100,
              show_default=True, help="最大连接数（用户设定值，作为公式输入）")
@click.option("--db-size", "db_size_gb", type=click.FloatRange(min=0), default=None,
              help="数据库体积 GB（默认 = total_ram_gb）")
@click.option("--risk-appetite", type=click.Choice(RISK_APPETITES), default="balanced",
              show_default=True, help="代价模型分歧时的取舍档")
@click.option("--disk-type", type=click.Choice(DISK_TYPES), default=None,
              help="覆盖探测的磁盘类型")
@click.option("--pg-version", type=click.IntRange(min=13), default=None,
              help=f"PostgreSQL 大版本（默认 {PG_VERSION_CAP}，即知识库上限）")
@click.option("--bench-path", default="/tmp", show_default=True,
              help="磁盘微基准测试文件的写入目录")
@click.option("--data-dir", default="/tmp", show_default=True,
              help="数据目录路径（statvfs 探测磁盘余量，max_wal_size 上界用）")
@click.option("-o", "--output", type=click.Path(dir_okay=False, writable=True),
              default=None, help="conf 片段写入文件（缺省打印 stdout）")
@click.option("--all-origins", is_flag=True, help="展开全部竞争公式（含被跳过的规则及原因）")
def recommend(knowledge_path: str, workload: str, max_connections: int, db_size_gb: float | None,
              risk_appetite: str, disk_type: str | None, pg_version: int | None,
              bench_path: str, data_dir: str, output: str | None, all_origins: bool) -> None:
    """探测 → 装配 → 求值 → 报告。"""
    console = Console()
    try:
        knowledge = load_knowledge(knowledge_path)
    except KnowledgeError as e:
        raise click.ClickException(str(e)) from None
    hw = hardware.probe(bench_path, data_dir)
    try:
        inputs = build_inputs(
            hw, workload_type=workload, max_connections=max_connections,
            db_size_gb=db_size_gb, risk_appetite=risk_appetite,
            pg_version=pg_version, disk_type=disk_type, db_observations={},  # no DB connection at this layer; interface reserved for the check layer
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    if is_extrapolated(inputs):
        console.print("[yellow]注意: pg_version 超出知识库覆盖范围（13-18），公式按最新分支外推求值[/yellow]")

    recs = recommend_all(knowledge, inputs)
    report.print_report(recs, inputs, hw=hw, current_values=None, all_origins=all_origins,
                        console=console)
    conf = report.render_conf(recs, inputs, offline=True)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(conf)
        console.print(f"[green]conf 片段已写入 {output}[/green]")
    else:
        console.rule("[bold]postgresql.conf 片段")
        console.print(conf, markup=False, highlight=False)


@cli.command("knowledge")
@click.argument("name", required=False)
@click.option("--pain", type=click.Choice(["high", "medium", "low"]), default=None,
              help="按痛点等级过滤")
def list_gucs(name: str | None, pain: str | None) -> None:
    """浏览 GUC 知识库（NAME 子串过滤、--pain 过滤）。

    v0.7.1 前叫 `list`(v0.1 遗留名)——顶层叫 list 语义含混且与子组惯用名
    (`lab list` 等)天然相撞;改名自描述(与 --knowledge 同词根)。"""
    console = Console()
    try:
        knowledge = load_knowledge(DEFAULT_KNOWLEDGE)
    except KnowledgeError as e:
        raise click.ClickException(str(e)) from None
    table = report.Table(box=report.box.SIMPLE_HEAVY,
                         title=f"GUC 知识库（pg_versions {knowledge.pg_versions}，共 {len(knowledge.gucs)} 条）")
    table.add_column("GUC", style="bold cyan", no_wrap=True)
    table.add_column("痛点", justify="center")
    table.add_column("重启", justify="center")
    table.add_column("规则数", justify="right")
    table.add_column("默认值", max_width=30)
    table.add_column("说明", max_width=60)
    shown = 0
    for g in knowledge.gucs:
        if name and name.lower() not in g.name.lower():
            continue
        if pain and g.pain_level != pain:
            continue
        table.add_row(g.name, g.pain_level or "-", "是" if g.restart_required else "否",
                      str(len(g.rules)), g.default_value[:40], g.description[:80])
        shown += 1
    if not shown:
        console.print("[yellow]无匹配的 GUC[/yellow]")
        return
    console.print(table)


@cli.command()
@click.option("--dsn", default=None,
              help="libpq 连接串（如 'host=/tmp port=5432 dbname=bench_new'）；缺省用 PGHOST/PGPORT/PGDATABASE 或 /tmp:5432/bench_new")
@click.option("--knowledge", "knowledge_path", default=DEFAULT_KNOWLEDGE,
              show_default=True, help="知识库 JSON 路径")
@click.option("--workload", type=click.Choice(WORKLOAD_TYPES), default="mixed",
              show_default=True, help="负载类型")
@click.option("--risk-appetite", type=click.Choice(RISK_APPETITES), default="balanced",
              show_default=True, help="代价模型分歧时的取舍档")
@click.option("--disk-type", type=click.Choice(DISK_TYPES), default=None,
              help="覆盖探测的磁盘类型")
@click.option("--bench-path", default="/tmp", show_default=True,
              help="磁盘微基准测试文件的写入目录")
@click.option("--data-dir", default="/tmp", show_default=True,
              help="数据目录路径（statvfs 探测磁盘余量的兜底；活库模式优先用 show data_directory 实测）")
@click.option("--wal-sample-seconds", type=click.FloatRange(min=0), default=signals.WAL_SAMPLE_SECONDS,
              show_default=True, help="WAL 速率采样窗口秒数（混载下短窗易恰好采到阻塞段）")
@click.option("-o", "--output", type=click.Path(dir_okay=False, writable=True),
              default=None, help="conf 片段写入文件（复用 recommend 的渲染：需重启组在前）")
@click.option("--all-origins", is_flag=True, help="展开全部竞争公式（含被跳过的规则及原因）")
def check(dsn: str | None, knowledge_path: str, workload: str, risk_appetite: str,
          disk_type: str | None, bench_path: str, data_dir: str,
          wal_sample_seconds: float, output: str | None,
          all_origins: bool) -> None:
    """实测信号微调：连活库只读采集观测 → 注入求值 → 信号面板 + 带当前值 diff 的推荐 + 解锁规则清单。"""
    console = Console()
    try:
        knowledge = load_knowledge(knowledge_path)
    except KnowledgeError as e:
        raise click.ClickException(str(e)) from None
    try:
        conn = pg.connect(dsn)
    except ConnectionError as e:
        raise click.ClickException(str(e)) from None
    hw = hardware.probe(bench_path, data_dir)
    sig = signals.collect(conn, cpu_cores=hw.cpu_cores, wal_sample_seconds=wal_sample_seconds)
    # Runtime values take precedence over user defaults: max_connections /
    # pg_version / db_size_gb are backfilled from live measurement
    rt_max_conn = sig.runtime_max_connections or 100
    inputs = build_inputs(
        hw, workload_type=workload, max_connections=rt_max_conn,
        db_size_gb=sig.db_size_gb, risk_appetite=risk_appetite,
        pg_version=sig.pg_major, disk_type=disk_type, db_observations=sig.observations,
    )
    if is_extrapolated(inputs):
        console.print(f"[yellow]注意: 实测 pg_version={sig.pg_major} 超出知识库覆盖范围（13-18），"
                      f"公式按最新分支外推求值[/yellow]")
    recs = recommend_all(knowledge, inputs)
    # Post-validation (2026-11 application verification fix): recommendations are
    # checked against the live pg_settings (parameter existence / valid enum values)
    signals.validate_against_instance(recs, sig)

    # ③ Observations unlocking: diff of missing_input against a no-observation run
    # (all other inputs identical)
    base_recs = recommend_all(knowledge, build_inputs(
        hw, workload_type=workload, max_connections=rt_max_conn,
        db_size_gb=sig.db_size_gb, risk_appetite=risk_appetite,
        pg_version=sig.pg_major, disk_type=disk_type, db_observations={}))

    # ① Signal panel ② Recommendation diff with a current-values column
    console.print(report.render_signal_panel(sig))
    current_values = {n: signals.display_value(s["setting"], s["unit"])
                      for n, s in sig.settings.items()}
    console.print(report.render_table(recs, current_values=current_values, all_origins=all_origins))

    unlocked = [(rec.name, r.index, r.origin)
                for rec, brec in zip(recs, base_recs)
                for r, br in zip(rec.results, brec.results)
                if br.status == "missing_input" and r.status == "ok"]
    if unlocked:
        console.rule(f"[bold]观测解锁的规则（{len(unlocked)} 条）")
        t = report.Table(box=report.box.SIMPLE)
        t.add_column("规则", style="bold cyan")
        t.add_column("公式出处", max_width=56)
        for name, idx, origin in unlocked:
            t.add_row(f"{name}[{idx}]", origin.split("（")[0][:56])
        console.print(t)
    else:
        console.print("[dim]无观测解锁的规则（依赖观测的规则仍缺其它输入，或原本就已求值）[/dim]")

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(report.render_conf(recs, inputs))
        console.print(f"[green]conf 片段已写入 {output}[/green]")


@cli.command("bench")
@click.option("--dsn", default=None,
              help="libpq 连接串；缺省用 PGHOST/PGPORT/PGDATABASE 或 /tmp:5432/bench_new")
@click.option("--knowledge", "knowledge_path", default=DEFAULT_KNOWLEDGE,
              show_default=True, help="知识库 JSON 路径")
@click.option("--candidates", type=click.Choice(bench_mod.CANDIDATE_SETS), default="simple",
              show_default=True,
              help="候选集：simple=baseline+recommended+work_mem×{0.5,2} 单变量扰动；"
                   "work_mem/rpc/parallel=在 recommended 组合上叠加该族变体；all=全部变体")
@click.option("--duration", type=click.IntRange(min=1), default=30, show_default=True,
              help="每候选 pgbench 时长（秒）")
@click.option("--clients", type=click.IntRange(min=1), default=4, show_default=True,
              help="pgbench 客户端数")
@click.option("--pgbench", "pgbench_path", default=None,
              help="pgbench 可执行文件路径;缺省自动解析:实例 bindir(优先)> PATH")
@click.option("--init", "do_init", is_flag=True,
              help="缺库时自动 CREATE DATABASE；pgbench_accounts 缺失时先 pgbench -i -s 10 初始化")
@click.option("--workload", type=click.Choice(WORKLOAD_TYPES), default="mixed",
              show_default=True, help="负载类型（影响推荐值）")
@click.option("--risk-appetite", type=click.Choice(RISK_APPETITES), default="balanced",
              show_default=True, help="代价模型分歧时的取舍档")
@click.option("--disk-type", type=click.Choice(DISK_TYPES), default=None,
              help="覆盖探测的磁盘类型")
@click.option("--bench-path", default="/tmp", show_default=True,
              help="磁盘微基准测试文件的写入目录")
@click.option("--instance", "instance_name", default=None,
              help="pgtuner 实例名（自动解析 dsn 并落账本；与 --dsn 二选一）")
@click.option("--db", default=None, help="实例上的目标库（--instance 时生效，缺省 bench_new）")
@click.option("--calibrate", "calibrate_n", type=click.IntRange(min=2), default=None,
              help="N：跳过候选对比，只跑 baseline N 次记录噪声地板（TPS 变异系数入账本；需 --instance）")
def bench_cmd(dsn: str | None, knowledge_path: str, candidates: str, duration: int,
              clients: int, pgbench_path: str, do_init: bool, workload: str,
              risk_appetite: str, disk_type: str | None, bench_path: str,
              instance_name: str | None, db: str | None, calibrate_n: int | None) -> None:
    """pgbench 候选闭环：观测 → 推荐 → PGOPTIONS 注入逐候选跑 → TPS/延迟对比表（不改服务器配置）。"""
    console = Console()
    try:
        knowledge = load_knowledge(knowledge_path)
    except KnowledgeError as e:
        raise click.ClickException(str(e)) from None
    inst_meta = None
    inst = None
    if instance_name:
        inst = instance_mod.get(instance_name)
        instance_mod.ensure_running(instance_name)
        dsn = instance_mod.dsn_of(instance_name, db or "bench_new")
        inst_meta = _inst_meta(inst)
    elif not dsn:
        # v0.11.x: fall back to the default instance when neither --instance nor
        # --dsn is given (same as _resolve_dsn, but not enforced): its bindir then
        # feeds the pgbench resolution chain below — new machines without pgbench on
        # PATH no longer misreport it as missing.
        name = config_mod.default_instance()
        if name:
            inst = instance_mod.get(name)
            instance_mod.ensure_running(name)
            dsn = instance_mod.dsn_of(name, db or "bench_new")
            inst_meta = _inst_meta(inst)
    # tool-w3: when pgbench is not given explicitly, resolve via instance bindir (version-matched, preferred) > PATH
    pgbench_path = _fallback_pgbench(pgbench_path, bindir=inst.bindir if inst else None)
    if not pgbench_path:
        raise click.ClickException(
            "pgbench 未找到(未显式指定且 bindir/PATH 均无):用 --pgbench 指定路径,"
            "或 --instance 用已注册实例的配套二进制;编译安装: bash scripts/setup.sh --pg <prefix>")
    if not (os.path.isfile(pgbench_path) and os.access(pgbench_path, os.X_OK)):
        raise click.ClickException(
            f"pgbench 不可执行: {pgbench_path}（用 --pgbench 指定路径，"
            f"或 --instance 用已注册实例的配套二进制）")
    conn = _connect_or_create(dsn, do_init, console)
    host, port, dbname = pg.target(dsn)

    hw = hardware.probe(bench_path)
    sig = signals.collect(conn, cpu_cores=hw.cpu_cores)
    if sig.debug_build:
        console.print("[bold red]警告: 目标库 debug_assertions=on（Debug 构建）——"
                      "TPS 绝对值无意义，仅可做组间对比！[/bold red]")
    if pg.fetch_one(conn, "SELECT to_regclass('public.pgbench_accounts')")[0] is None:
        if not do_init:
            raise click.ClickException(
                "pgbench_accounts 表不存在：加 --init 由本命令执行 pgbench -i -s 10 初始化，"
                "或手工运行 pgbench -i -s 10 后重试")
        console.print("初始化 pgbench 数据（pgbench -i -s 10）…")
        proc = bench_mod.run_init(pgbench_path, host, port, dbname)
        if proc.returncode != 0:
            raise click.ClickException("pgbench 初始化失败: " + (proc.stderr or proc.stdout)[-300:])

    inputs = build_inputs(
        hw, workload_type=workload, max_connections=sig.runtime_max_connections or 100,
        db_size_gb=sig.db_size_gb, risk_appetite=risk_appetite,
        pg_version=sig.pg_major, disk_type=disk_type, db_observations=sig.observations,
    )
    recs = recommend_all(knowledge, inputs)
    cands = bench_mod.build_candidates(recs, sig.settings, candidates)

    if calibrate_n:
        if not inst_meta:
            raise click.ClickException("--calibrate 需要配合 --instance（噪声地板按实例指纹归档）")
        tps_list = []
        for i in range(calibrate_n):
            proc = bench_mod.run_pgbench(pgbench_path, host, port, dbname,
                                         duration=min(duration, 10), clients=clients)
            if proc.returncode == 0:
                t = bench_mod.parse_pgbench_output(proc.stdout)["tps"]
                if t:
                    tps_list.append(t)
                    console.print(f"  calibrate {i + 1}/{calibrate_n}: tps={t:.2f}")
        if len(tps_list) < 2:
            raise click.ClickException("校准有效样本不足 2 个，无法计算噪声地板")
        ensure_dirs()
        exp = ledger_mod.record_calibration(inst_meta, _fingerprint(dsn, inst_meta), tps_list)
        console.print(f"[green]噪声地板已入账本[/green]（TPS CV = {exp.noise_floor['tps_cv_pct']:.2f}%） "
                      f"{ledger_mod.citation(exp.id)}")
        return

    console.print(f"候选 {len(cands)} 个 × {duration}s（clients={clients}, jobs=4, "
                  f"{dbname}@{host}:{port}）——预计 {len(cands) * duration}s 左右")

    def _progress(r: bench_mod.BenchResult) -> None:
        if r.tps is not None:
            console.print(f"  {r.candidate.name}: tps={r.tps:.2f}"
                          + (f" latency={r.latency_ms:.3f}ms" if r.latency_ms is not None else ""))
        else:
            console.print(f"  {r.candidate.name}: [red]失败[/red] {r.error or ''}")

    try:
        results = bench_mod.run_candidates(cands, pgbench_path=pgbench_path, host=host,
                                           port=port, dbname=dbname, duration=duration,
                                           clients=clients, on_result=_progress)
    except subprocess.TimeoutExpired as e:
        raise click.ClickException(f"pgbench 超时被杀: {e}") from None

    console.print(report.render_bench_table(results))
    ok = [r for r in results if r.tps is not None]
    if ok:
        win = max(ok, key=lambda r: r.tps)
        rel = (f"（较 baseline {(win.tps / results[0].tps - 1) * 100:+.1f}%）"
               if results[0].tps else "")
        console.print(f"[green]TPS 最高[/green]: {win.candidate.name} = {win.tps:.2f} {rel}")
    notes = ["公式建议值≠实测最优——两者不一致时以实测为准", "全程未修改服务器配置（PGOPTIONS 只影响本次 pgbench 会话）"]
    if sig.debug_build:
        notes.insert(0, "Debug 构建数据仅可组间对比，TPS 绝对值无意义")
    console.print("[yellow]提醒[/yellow]: " + "；".join(notes))
    if inst_meta:
        try:
            fp = _fingerprint(dsn, inst_meta)
        except Exception:                                            # noqa: BLE001
            fp = None
        ensure_dirs()
        metrics = {r.candidate.name: r.tps for r in results if r.tps is not None}
        exp = ledger_mod.new_experiment(
            "bench", f"bench:{candidates}", instance=inst_meta, fingerprint=fp,
            payload={"metrics": metrics,
                     "candidates": [{"name": r.candidate.name, "gucs": r.candidate.gucs,
                                     "tps": r.tps, "error": r.error} for r in results]})
        console.print(f"[green]已落账本[/green] {ledger_mod.citation(exp.id)}"
                      "（跨实验对比用 results diff <a> <b>）")


# ===================== Toolset extensions: instance / lab / results / planbench / rebench / watch / selftest =====================

def _pg_major(version: str | None) -> int | None:
    m = re.match(r"(\d+)", version or "")
    return int(m.group(1)) if m else None


def _inst_meta(inst) -> dict:
    """Instance metadata quadruple for the ledger (single source shared by bench/rebench/benchmark/resolve_target)."""
    return {"name": inst.name, "pg_version": inst.pg_version,
            "build_type": inst.build_type, "port": inst.port}


def _resolve_target(instance_name: str | None, db: str | None, dsn: str | None
                    ) -> tuple[str, dict | None]:
    """--instance/--db/--dsn → (dsn, instance metadata). The instance is
    ensure_running'd automatically; failure to resolve a dsn raises immediately
    (the identical error from 9 former call sites, consolidated here).

    v0.6.1: when neither --instance nor --dsn is given, fall back to the
    deployment-level default (flag > env PGTUNER_INSTANCE > config set
    default-instance) — saving per-command --instance and instance list in loops.
    The resolved name lands in inst_meta["name"] so callers can backfill their local variable."""
    inst_meta = None
    if not instance_name and not dsn:
        instance_name = config_mod.default_instance()
    if instance_name:
        inst = instance_mod.get(instance_name)
        instance_mod.ensure_running(instance_name)
        dsn = instance_mod.dsn_of(instance_name, db or "postgres")
        inst_meta = _inst_meta(inst)
    if not dsn:
        raise click.ClickException(
            "需要 --dsn 或 --instance(或先 pgtuner config set default-instance <实例> 一劳永逸)")
    return dsn, inst_meta


def _fingerprint(dsn: str | None, inst_meta: dict | None) -> dict | None:
    if not dsn:
        return None
    conn = pg.connect_rw(dsn)
    try:
        rows = pg.fetch_all(conn, "SELECT name, setting, source FROM pg_settings")
        settings = {n: {"setting": s, "source": src} for n, s, src in rows}
        # F2 (patch-trial): binary provenance goes into the fingerprint — full
        # server_version string + instance name. Two experiments sharing
        # pg_major/build_type may come from different binaries (instances built
        # before and after a patch); diff uses this to emit a "cross-instance/
        # cross-binary" warning so build differences are not mistaken for experiment effects.
        sv = pg.fetch_all(conn, "SELECT current_setting('server_version')")
        server_version = sv[0][0] if sv else None
    finally:
        conn.close()
    meta_fp = {"pg_major": _pg_major((inst_meta or {}).get("pg_version")),
               "build_type": (inst_meta or {}).get("build_type")}
    fp = ledger_mod.fingerprint_of(settings, meta_fp)
    if server_version:
        fp["server_version"] = server_version
    if inst_meta and inst_meta.get("name"):
        fp["instance"] = inst_meta["name"]
    return fp


def _safe_fp(dsn: str | None, inst_meta: dict | None) -> dict | None:
    try:
        return _fingerprint(dsn, inst_meta)
    except Exception:                                                # noqa: BLE001
        return None


def _scan_sql(text: str) -> list[str]:
    """Lexically scan and split SQL text: cut only at semicolons in "bare regions".
    Implementation lives in the sqlsplit module (single implementation shared by
    cli and sqlcmd; this function remains as the historical entry name inside cli).

    Region rules (tool-w3 fix, see the sqlsplit docstring for details): semicolons
    inside single-quoted strings ('' escape, E'' backslash escapes) / dollar-quotes
    (same-tag closing, nestable, $ glued to an identifier does not start one) /
    double-quoted identifiers / line comments and block comments (replaced by a
    single space) are not cut. Unclosed strings/dollar-quotes/block comments raise
    ValueError (a local error beats shipping garbage statements to the server)."""
    return sqlsplit.split_statements(text)


# Read-only surface guards (R2-D findings): PG read-only transactions do not
# prevent function side effects or program execution (COPY ... TO PROGRAM checks
# read-only only on the COPY FROM branch; pgtuner instances are superuser+trust),
# so the agent-facing read-only exits enforce a statement-head whitelist, a
# masked write-smuggle keyword check and a side-effect function blacklist on top
# of the connection-level readonly (R3 D4: all three layers, on every surface).
_SQL_ALLOWED_HEADS = ("select", "with", "table", "values", "show", "explain")
_INDEX_DDL_HEAD = re.compile(r"^create\s+(unique\s+)?index\b", re.I)


def _write_smuggle_match(masked: str) -> re.Match | None:
    """Single bridge to diagnose's write-smuggle matcher.

    Round-5 contract (parallel change in diagnose.py): diagnose.write_smuggle_search(masked)
    is the perf-gated equivalent of _WRITE_SMUGGLE.search. Resolved lazily at
    call time so this module works whichever of the two parallel changes lands
    first. C6-F5 (round 6): the fallback below now inlines the update…set
    bridge — since diagnose's C5-F2 perf split, _WRITE_SMUGGLE alone carries
    only the bounded alternatives (the unbounded update branch lives in
    diagnose._UPDATE_SMUGGLE behind write_smuggle_search's keyword gate), so
    the old bare `.search` fallback silently missed 'update … set' smuggles
    and the "keeps the semantics identical either way" claim was false. The
    inlined two-step retrieval keeps the fallback equivalent to
    write_smuggle_search (leftmost-match included); if diagnose ever drops
    _UPDATE_SMUGGLE too, the AttributeError is the loud failure we want, not
    a silently weaker guard. Every cli-layer call site goes through here —
    never touch _WRITE_SMUGGLE directly."""
    search = getattr(diagnose_mod, "write_smuggle_search", None)
    if search is not None:
        return search(masked)
    m = diagnose_mod._WRITE_SMUGGLE.search(masked)
    low = masked.lower()
    if "update" not in low or "set" not in low:
        return m
    u = diagnose_mod._UPDATE_SMUGGLE.search(masked)
    if u is not None and (m is None or u.start() < m.start()):
        return u
    return m


def _check_write_smuggle(stmt: str, source: str) -> None:
    """Masked write/lock-keyword check shared by the sql and analyze surfaces.

    Reuses diagnose's write-smuggle matcher (via _write_smuggle_match) on
    sqlsplit.mask_literals output (single shared lexing — do not re-invent it
    here): keywords inside 'string literals' are data, while a statement whose
    HEAD is read-only but whose body carries data-modifying CTEs / FOR UPDATE /
    utility verbs is refused. R3 D4: the sql surface used to have only the head
    whitelist + side-effect blacklist, so 'WITH x AS (DELETE FROM t RETURNING *)
    SELECT …' passed the tool layer and leaned on the connection-level readonly
    alone."""
    try:
        masked = sqlsplit.mask_literals(stmt)
    except ValueError as e:
        raise click.ClickException(f"{source}: 语句词法错误: {e}") from None
    m = _write_smuggle_match(masked)
    if m:
        raise click.ClickException(
            f"{source}: 拒绝疑似写/锁定关键字 {m.group(0)!r}"
            "(数据修改 CTE 与 FOR UPDATE 也会真改/真锁——写需求请走 lab 克隆库):"
            f"{stmt[:60]!r}")


def _guard_sql_statement(stmt: str) -> None:
    """Head whitelist + masked write-smuggle + side-effect blacklist for
    `pgtuner sql` (one statement).

    Known boundary (round-5 D5-F4): a pre-existing database function that
    internally wraps a blacklisted call remains callable — the guards analyze
    the statement text, not pg_proc bodies (database content is not tool
    input; under superuser any client could call it anyway)."""
    head = sqlsplit.statement_head(stmt)
    if head not in _SQL_ALLOWED_HEADS:
        raise click.ClickException(
            f"sql 只接受只读语句头({'/'.join(_SQL_ALLOWED_HEADS)}):{head!r} 被拒绝——"
            "COPY … TO PROGRAM 这类语句在只读事务里也会执行外部程序(超级用户下),"
            "'只读'不能只靠数据库层;写需求走 lab 克隆库")
    _check_write_smuggle(stmt, "sql")
    calls = sqlsplit.find_side_effect_calls(stmt, surface="sql")
    if calls:
        raise click.ClickException(
            f"sql 拒绝副作用函数:{', '.join(calls)}(只读事务拦不住函数副作用:"
            "杀会话/读写服务器文件/外部程序);运维动作请走对应子命令或克隆库上下文")


def _guard_analyze_query(stmt: str, source: str) -> None:
    """Read-only guard for the EXPLAIN(-ANALYZE) surfaces (hypo/advise --query and
    --queries-file): head whitelist + masked write-smuggle + side-effect function
    blacklist — the same strength as diagnose._validate (which keeps its own
    query-count caps for the one-shot flow). R3 D4: --query DML used to be
    stopped only incidentally by the hypo transaction rollback; ANALYZE really
    executes the statement on a read-write connection, so the tool layer must
    refuse on its own. pg_sleep stays banned here (EXPLAIN ANALYZE would run it)
    while the sql surface keeps allowing it (statement_timeout caps it there)."""
    if not diagnose_mod._READ_ONLY_HEAD.match(stmt):
        raise click.ClickException(
            f"{source}: 只接受只读查询头(select/with/table/values/explain):"
            f"{stmt[:60]!r}——EXPLAIN ANALYZE 会真执行,写需求请走 lab 克隆库")
    _check_write_smuggle(stmt, source)
    calls = sqlsplit.find_side_effect_calls(stmt, surface="analyze")
    if calls:
        raise click.ClickException(
            f"{source}: 拒绝副作用函数 {', '.join(calls)}(EXPLAIN ANALYZE 会真执行:"
            "杀会话/读写文件/外部程序不可回滚)")


# planbench's deliberate write channel: EXPLAIN ANALYZE on DML is a legitimate
# plan-comparison need, but only against a target the user named explicitly.
_PLANBENCH_DML_HEADS = ("insert", "update", "delete", "merge")


def _planbench_write_gate(queries: list[str], *, db_given: bool, dsn_given: bool) -> None:
    """D-F3 (round 4): planbench plain mode used to have no gate of its own.

    planbench.explain's head whitelist deliberately admits DML (comparing write
    plans is a real need), and EXPLAIN ANALYZE really executes the statement —
    so a pure "delete from x;" file without --db landed on the shared postgres
    database (the default target) and emptied it. Mirror the --variant gate:
    DML heads or masked write-smuggle keywords (same matcher via
    _write_smuggle_match as _check_write_smuggle — data-modifying CTEs write
    too) require an explicit --db/--dsn; WITH an explicit target the write
    comparison stays available (unlike the sql surface, this is not a refusal)."""
    if db_given or dsn_given:
        return
    for q in queries:
        head = sqlsplit.statement_head(q)
        try:
            masked = sqlsplit.mask_literals(q)
        except ValueError as e:
            raise click.ClickException(f"planbench: 语句词法错误: {e}") from None
        m = _write_smuggle_match(masked)
        if head in _PLANBENCH_DML_HEADS or m:
            why = (f"DML 语句头 {head!r}" if head in _PLANBENCH_DML_HEADS
                   else f"写/锁定关键字 {m.group(0)!r}")
            raise click.ClickException(
                f"planbench: 检测到{why}——EXPLAIN ANALYZE 会真实执行,缺省会落在共享 postgres 库;"
                "写语句对比请显式 --db <克隆库>(或 --dsn 指定目标);先 pgtuner lab clone 拿一次性库再跑")


def _split_exec_payload(text: str, source: str) -> list[str]:
    """Lexically split a possibly multi-statement execution-channel payload
    (--variant DDL, one --setup string, a setup_sql file) so every statement is
    guarded — a smuggled 'ANALYZE t; COPY … TO PROGRAM …' inside one payload
    must not ride past on the first statement's coattails."""
    try:
        return sqlsplit.split_statements(text)
    except ValueError as e:
        raise click.ClickException(f"{source}: 语句词法错误: {e}") from None


def _skip_leading_comments(stmt: str) -> str:
    """Return stmt with leading whitespace/comments removed (for head detection).

    C7-F5 (round 7): sqlsplit.statement_head documents a COMMENT-STRIPPED
    statement as its precondition — its word-regex cannot see past a leading
    '/* seed */' and returns '' for it, so a raw '/* seed */ do $$…$$' handed
    straight to _guard_exec_stmt would dodge the do/copy head checks entirely.
    Every current call site feeds the guard output of _split_exec_payload /
    _split_sql_file (split_statements already replaces comments with a space),
    so this is defense in depth, not the primary barrier. Line comments (-- …)
    and nestable block comments (/* … */) are skipped exactly the way
    split_statements/mask_literals treat them; an unclosed block comment raises
    ValueError (fail-safe = refuse). The FIRST non-comment character starts the
    statement proper — anything lexically odd falls through unchanged and
    statement_head's word-regex judges it."""
    i, n = 0, len(stmt)
    while i < n:
        ch = stmt[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "-" and stmt[i + 1:i + 2] == "-":
            j = stmt.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if ch == "/" and stmt[i + 1:i + 2] == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if stmt[j:j + 2] == "/*":
                    depth += 1
                    j += 2
                elif stmt[j:j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise ValueError("未闭合的 /* 块注释")
            i = j
            continue
        return stmt[i:]
    return ""


def _guard_exec_stmt(stmt: str, source: str, *, write_gate: bool = False) -> None:
    """Execution-channel statement guard (round-5 D5-F3).

    The channels whose statements REALLY execute server-side outside the
    read-only surfaces used to bypass the tool's own blacklists while doing so:
    the --variant loop ran its DDL and its queries through a raw cursor (plain
    mode's explain() guards never saw them), the --flip-check loop went through
    PREPARE / EXPLAIN ANALYZE EXECUTE, and the --setup payload channels execute
    intervention statements verbatim. Layers, uniform across those channels:

      1. side-effect function blacklist, analyze surface (the statement is
         really executed, so the sleep family counts too);
      2. DO statement head — refused OUTRIGHT (round-7 D7-F1, replacing the
         round-6 C6-F3 unmasked re-scan). R7 live evidence: a DO block can
         ASSEMBLE statement/function names at runtime —
         'do $do$ begin execute ''select lo_cre'' || ''at(-1)''; end $do$'
         (likewise format(...) and chr()||chr() splicing) — so the blacklisted
         word NEVER appears contiguously in the statement text and no static
         blacklist can see it; a COPY TO PROGRAM variant assembled this way
         made the postgres server process execute a program (RCE, observed).
         Static analysis loses this game by construction, so every DO-headed
         statement is refused on all payload channels (rebench --setup /
         lab make-tpl / lab bootstrap --setup-sql / planbench --variant; the
         shared guard covers the --flip-check face too — there DO used to fail
         only later at PREPARE, on a live connection). Channel design
         decision: even a "clean" DO seed (insert/create/raise) is refused —
         split it into plain DDL/DML statements or run procedural seeds by
         hand in a clone. A leading '/* seed */' no longer dodges the head
         check either (C7-F5): the head is taken after _skip_leading_comments
         (defensive — all six call sites split first, which already strips
         comments);
      3. COPY statement head — COPY … TO PROGRAM spawns a shell inside the
         postgres server process and COPY … TO/FROM <file> touches the server
         filesystem as superuser (read-only transactions do not stop either);
         COPY is not "creating tables" on any channel;
      4. write_gate=True (the measurement face: the --flip-check query): the
         masked write-smuggle check — a measurement statement stays read-only;
         write comparisons belong to the plain/variant channels, where the
         explicit --db/--dsn gate governs them.

    Overt DDL/DML heads stay allowed by design on the payload channels
    (--variant DDL, --setup, setup_sql): creating/altering/dropping tables is
    those channels' day job. The 'call' head stays allowed too (declared
    boundary, same as the known pre-stored-function wrapper: the guards
    analyze statement text, not pg_proc bodies) — and that boundary
    explicitly includes two-phase function creation: on the explicit --db
    write channel ONE invocation suffices, --variant "v1=CREATE FUNCTION f …
    AS $$ … EXECUTE <dynamically assembled statement> $$" plus the variant
    query 'select f()' (run_variants executes it under EXPLAIN ANALYZE); a
    $$ body that splices statements at runtime is the very construct the DO
    head is refused for, exempt here because the explicit --db/--dsn
    authorization and the superuser context carry that trust. Callers place
    the guard BEFORE any connection or ledger side effect (refuse first,
    touch nothing)."""
    try:
        calls = sqlsplit.find_side_effect_calls(stmt, surface="analyze")
    except ValueError as e:
        raise click.ClickException(f"{source}: 语句词法错误: {e}") from None
    if calls:
        raise click.ClickException(
            f"{source}: 拒绝副作用函数 {', '.join(calls)}(该通道会真实执行:"
            f"杀会话/读写文件/外部程序不可回滚):{stmt[:60]!r}")
    try:
        head = sqlsplit.statement_head(_skip_leading_comments(stmt))
    except ValueError as e:
        raise click.ClickException(f"{source}: 语句词法错误: {e}") from None
    if head == "do":
        # D7-F1: dynamic name assembly inside DO bodies defeats every static
        # blacklist (the assembled word is never contiguous) — refuse the head.
        raise click.ClickException(
            f"{source}: 拒绝 DO 语句头:{stmt[:60]!r}——"
            "DO 块可动态拼装函数名/语句(execute/format/chr 拼串,拼名后黑名单词"
            "不再连续出现,静态扫描无法可靠分析);需要过程性种子请先在克隆库手工执行,"
            "或拆成普通 DDL/DML 语句")
    if head == "copy":
        raise click.ClickException(
            f"{source}: 拒绝 COPY 语句头(COPY … TO PROGRAM 在服务器进程执行外部程序,"
            f"TO/FROM 文件以超级用户读写服务器文件,只读事务也拦不住):{stmt[:60]!r}")
    if write_gate:
        _check_write_smuggle(stmt, source)


def _guard_exec_payload(text: str, source: str, *, write_gate: bool = False) -> None:
    """Split one execution-channel payload and guard every statement in it
    (see _guard_exec_stmt for the layering)."""
    for stmt in _split_exec_payload(text, source):
        _guard_exec_stmt(stmt, source, write_gate=write_gate)


def _single_query_arg(text: str, source: str) -> str:
    """Positional/--query inputs must be exactly ONE statement.

    diagnose/hypo/advise EXPLAIN these strings on read-write autocommit
    connections, so a smuggled "SELECT 1; DROP TABLE x" used to execute and
    commit the second statement past the head-regex read-only filter
    (adversarial review R1-D). Same rule as sql_cmd: split lexically, require
    exactly one."""
    try:
        stmts = sqlsplit.split_statements(text)
    except ValueError as e:
        raise click.ClickException(f"{source}: 语句词法错误: {e}") from None
    if len(stmts) != 1:
        raise click.ClickException(
            f"{source}: 一次只接受一条语句(检出 {len(stmts)} 条,分号切分)——"
            "多条查询请用 --queries-file/--file(逐条校验)")
    return stmts[0]


def _split_sql_file(path: str) -> list[str]:
    """Split a SQL file at semicolons: cut only at semicolons in bare regions (see _scan_sql).

    B2 acceptance defect D1: semicolons inside comments once produced garbage
    statements (first fixed by stripping comments); tool-w3 fix: semicolons inside
    strings/dollar-quotes/double-quoted identifiers are no longer mis-cut — trigger
    bodies / PLpgSQL function bodies can go to rebench --queries-file / planbench whole."""
    try:
        return _scan_sql(Path(path).read_text(encoding="utf-8"))
    except ValueError as e:                       # unclosed construct: convert to a CLI-friendly error
        raise click.ClickException(f"{path}: {e}") from None


def _fallback_pgbench(pgbench_path: str | None, bindir: str | None = None) -> str | None:
    """pgbench path resolution: since v0.9.0 delegated to bench.resolve_pgbench
    (symbol kept so existing call sites stay unchanged); an explicit --pgbench is
    respected as-is, the default resolves via instance bindir (version-matched,
    preferred) > PATH, and None is returned when all fall through so the caller
    reports the fix guidance. The machine-hardcoded default (DEFAULT_PGBENCH) is gone."""
    return bench_mod.resolve_pgbench(pgbench_path, bindir=bindir)


def _connect_or_create(dsn: str | None, do_init: bool, console):
    """bench connects to the target db; with --init and a missing db, CREATE DATABASE
    first, then reconnect (tool-w1 fix).

    Without --init, or when the connection failure is not a missing db (server not
    started / auth failure — creating the db would not reach the maintenance db
    either), or when creating the db itself fails, keep raising connect's
    classified error as before."""
    try:
        return pg.connect(dsn)
    except ConnectionError as e:
        if not do_init:
            raise click.ClickException(str(e)) from None
        dbname = pg.target(dsn)[2]
        try:
            pg.create_db(dsn, dbname)
        except ConnectionError:
            raise click.ClickException(str(e)) from None   # server not started etc.: still report the original connection error
        except Exception as ce:                            # noqa: BLE001
            raise click.ClickException(f"{e}\n（--init 自动建库失败: {ce}）") from None
        console.print(f"[green]库 {dbname} 不存在，已自动创建（--init）[/green]")
        try:
            return pg.connect(dsn)
        except ConnectionError as ce:
            raise click.ClickException(str(ce)) from None


@cli.group("instance")
def instance_grp() -> None:
    """实例注册与生命周期（L0 一级：注册制 / create 托管 initdb）。"""


@instance_grp.command("register")
@click.argument("name")
@click.argument("bindir", type=click.Path(exists=True))
@click.option("--port", required=True, type=click.IntRange(min=1), help="该实例监听端口（socket 固定 /tmp）")
def instance_register(name: str, bindir: str, port: int) -> None:
    """登记既有安装（bindir 须含 pg_config/initdb 等）。"""
    inst = instance_mod.register(name, bindir, port=port)
    Console().print(f"[green]已注册[/green] {inst.name}: PG {inst.pg_version} "
                    f"({inst.build_type}) port={inst.port} → {instance_mod.dsn_of(name, 'postgres')}")


@instance_grp.command("create")
@click.argument("name")
@click.argument("bindir", type=click.Path(exists=True))
@click.option("--port", type=click.IntRange(min=1), default=None, help="缺省从 5433 起自动分配")
def instance_create(name: str, bindir: str, port: int | None) -> None:
    """托管创建：initdb(trust/本地 socket)→启动→登记。数据目录 /tmp/pgtuner-instances/<name>。"""
    console = Console()
    console.print(f"initdb + 启动中（{bindir}）…")
    inst = instance_mod.create(name, bindir, port=port)
    console.print(f"[green]已创建并启动[/green] {inst.name}: PG {inst.pg_version} ({inst.build_type}) "
                  f"port={inst.port} data={inst.data_dir}")


@instance_grp.command("start")
@click.argument("name")
def instance_start(name: str) -> None:
    """启动已停止的托管实例（pg_ctl start，幂等；外部注册实例须自行启动）。"""
    instance_mod.start(name)
    Console().print(f"[green]{name} 运行中[/green] → {instance_mod.dsn_of(name, 'postgres')}")


@instance_grp.command("stop")
@click.argument("name")
@click.option("--mode", type=click.Choice(["fast", "immediate"]), default="fast", show_default=True,
              help="fast=断新连接等活跃事务收尾;immediate=立即中断(下次启动走崩溃恢复)")
def instance_stop(name: str, mode: str) -> None:
    """停止实例（pg_ctl stop，幂等；外部注册实例须自行停止）。"""
    instance_mod.stop(name, mode=mode)
    Console().print(f"[green]{name} 已停止[/green]（{mode}）")


@instance_grp.command("status")
@click.argument("name")
def instance_status(name: str) -> None:
    """单实例概况：版本/构建/运行状态/DSN/数据目录。"""
    inst = instance_mod.get(name)
    console = Console()
    ready = instance_mod.is_ready(name)
    console.print(f"实例: {inst.name}  PG {inst.pg_version} ({inst.build_type})")
    console.print(f"状态: {'running' if ready else 'stopped'}  端口: {inst.port}  socket: /tmp")
    console.print(f"DSN: {instance_mod.dsn_of(name, 'postgres')}  数据目录: {inst.data_dir or '(注册制,无)'}")


@instance_grp.command("list")
def instance_list() -> None:
    """注册表清单：版本/构建(debug|release)/端口/运行状态/是否托管。"""
    insts = instance_mod.list_instances()
    if not insts:
        Console().print("[yellow]无已注册实例：instance register NAME BINDIR --port N / instance create[/yellow]")
        return
    t = report.Table(box=report.box.SIMPLE_HEAVY, title="pgtuner 实例")
    t.add_column("名称", style="bold cyan")
    t.add_column("版本")
    t.add_column("构建")
    t.add_column("端口", justify="right")
    t.add_column("状态")
    t.add_column("托管", justify="center")
    for i in insts:
        ready = instance_mod.is_ready(i.name)
        t.add_row(i.name, i.pg_version, "[red]debug[/red]" if i.build_type == "debug"
                  else "[green]release[/green]", str(i.port),
                  "running" if ready else "stopped", "是" if i.managed else "否")
    Console().print(t)


@instance_grp.command("destroy")
@click.argument("name")
@click.option("--force", is_flag=True, help="注册制实例强制注销（不删其数据目录）")
@click.option("--yes", is_flag=True,
              help="确认销毁(非交互/agent 场景必须;托管实例的数据目录删除不可逆)")
def instance_destroy(name: str, force: bool, yes: bool) -> None:
    """停止并删除托管实例（数据目录+克隆记录一并清）；外部注册实例仅注销、需 --force。"""
    inst = instance_mod.get(name)
    if not yes:
        # Adversarial review R1-D: the most irreversible command had the weakest
        # gate — one typo'd instance name used to rmtree a whole data directory.
        if inst.managed:
            msg = (f"销毁实例 {name}: PG {inst.pg_version} "
                   f"数据目录 {inst.data_dir or '(注册制,无)'} 将被整目录删除(不可逆)。")
        else:
            msg = (f"注销外部注册实例 {name}: PG {inst.pg_version}"
                   "(仅移除登记,不碰其数据目录;另需 --force)。")
        if sys.stdin.isatty():
            click.confirm(msg + " 确认?", abort=True)
        else:
            raise click.ClickException(msg + "非交互场景请加 --yes 确认。")
    instance_mod.destroy(name, force=force)
    Console().print(f"[green]{name} 已销毁并注销[/green]")


@instance_grp.command("dsn")
@click.argument("name")
@click.argument("dbname")
def instance_dsn(name: str, dbname: str) -> None:
    """打印该实例某库的 libpq 连接串（host=/tmp port=... dbname=...）。"""
    Console().print(instance_mod.dsn_of(name, dbname))


@cli.group("lab")
def lab_grp() -> None:
    """模板克隆池：秒级取用一次性实验库（TEMPLATE 克隆，用完 release，零残留）。"""


def _need_instance(instance_name: str | None, usage: str) -> str:
    """Shared by the lab group: fall back for a missing instance name (config/env);
    if still missing, raise a didactic error (three identical former sites consolidated)."""
    instance_name = instance_name or config_mod.default_instance()
    if not instance_name:
        raise click.ClickException(
            f"缺实例名:{usage},或先 pgtuner config set default-instance <实例>")
    return instance_name


def _print_steps(title: str, steps: list[dict]) -> None:
    """Steps table (name/changed/note) shared by bootstrap/init. Commands must not
    call each other directly; shared logic goes through private helpers (extracted
    in v0.10.0; guards against the command-calls-command antipattern noted in
    _print_lab_stats)."""
    t = report.Table(box=report.box.SIMPLE_HEAVY, title=title)
    t.add_column("步骤", style="bold cyan")
    t.add_column("变更", justify="center")
    t.add_column("说明", max_width=72)
    for s in steps:
        changed = "[green]是[/green]" if s["changed"] else "[dim]否[/dim]"
        t.add_row(s["name"], changed, s["note"])
    Console().print(t)


@lab_grp.command("templates")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名(缺省默认实例)")
def lab_templates(instance_name: str | None) -> None:
    """列出实例上的模板库（datallowconn=false 的库）。实例名可省(部署级缺省)。"""
    instance_name = _need_instance(instance_name, "--instance <名>")
    instance_mod.ensure_running(instance_name)
    names = instance_mod.templates(instance_name)   # v0.9.0 consolidated into instance.templates (single definition)
    if not names:
        Console().print("[yellow]无模板库：lab make-tpl / lab bootstrap 创建[/yellow]")
        return
    Console().print("\n".join(names))


@lab_grp.command("make-tpl")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名(缺省默认实例)")
@click.argument("template_db")
@click.argument("setup_sql", type=click.Path(exists=True, dir_okay=False))
def lab_make_tpl(instance_name: str | None, template_db: str, setup_sql: str) -> None:
    """建模板库：CREATE DATABASE → 执行 setup_sql → 禁连接（可随时克隆）。实例名可省(部署级缺省)。"""
    # D5-F3 (round 5): guard the setup payload BEFORE anything else — instance
    # start / CREATE DATABASE are side effects too (refuse first, touch
    # nothing). Explicit DDL channel: table-building heads stay allowed;
    # side-effect functions and COPY heads never do.
    _guard_exec_payload(Path(setup_sql).read_text(encoding="utf-8"), "lab make-tpl SETUP_SQL")
    instance_name = _need_instance(instance_name, "--instance <名>")
    instance_mod.ensure_running(instance_name)
    instance_mod.make_template(instance_name, template_db, setup_sql)
    Console().print(f"[green]模板就绪[/green]: {template_db}（lab clone {instance_name} --template {template_db}）")


@lab_grp.command("bootstrap")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名(缺省默认实例)")
@click.option("--template", default=bootstrap_mod.DEFAULT_TEMPLATE, show_default=True,
              help="标准模板名(存在即跳过,幂等)")
@click.option("--scale", type=click.IntRange(min=1), default=bootstrap_mod.DEFAULT_SCALE,
              show_default=True, help="pgbench -i 的 scale")
@click.option("--setup-sql", type=click.Path(exists=True, dir_okay=False), default=None,
              help=f"模板载荷 SQL(缺省随包 {bootstrap_mod.DEFAULT_SETUP_SQL.name})")
@click.option("--set-default", is_flag=True,
              help="默认实例已设为别的实例时也强制改为本实例(未设时总是自动设)")
def lab_bootstrap(instance_name: str | None, template: str, scale: int,
                  setup_sql: str | None, set_default: bool) -> None:
    """一命令把(新)实例铺成可开工：pgss 预载(+postgres 建扩展) → 标准模板(密封) → 默认实例。

    每步幂等,重跑只补缺。未预载 pgss 时会 stop/start 重启实例(~10s)——显式运行本命令即人工确认(对齐铁律 4);扩展建在密封前,克隆库经 TEMPLATE 继承 pgss。完成后跑 pgtuner doctor 复检。"""
    if setup_sql:
        # D5-F3 (round 5): an explicitly given --setup-sql is user input for the
        # DDL payload channel — same two-layer guard as lab make-tpl, before
        # the (possibly restarting) bootstrap touches anything. The packaged
        # default payload is tool-owned, not tool input, and stays ungated.
        _guard_exec_payload(Path(setup_sql).read_text(encoding="utf-8"),
                            "lab bootstrap --setup-sql")
    instance_name = _need_instance(instance_name, "lab bootstrap --instance <名>")
    Console().print("[yellow]① pgss 未预载时将重启实例(~10s)——显式运行本命令即确认[/yellow]")
    try:
        result = bootstrap_mod.bootstrap(instance_name, template=template, scale=scale,
                                         setup_sql_file=setup_sql, force_default=set_default)
    except (ValueError, RuntimeError) as e:
        raise click.ClickException(str(e)) from None
    _print_steps(f"lab bootstrap — {instance_name}", result["steps"])
    Console().print(f"[green]可开工[/green]: pgtuner lab clone --template {template};复检: pgtuner doctor")


@lab_grp.command("clone")
@click.argument("instance_name", required=False)
@click.option("--template", required=True)
@click.option("--instance", "instance_opt", default=None,
              help="与位置参数等价(失误防护:S5-A1 实测 agent 会顺手写 --instance)")
def lab_clone(instance_name: str | None, template: str, instance_opt: str | None) -> None:
    """从模板克隆一次性实验库：stdout 只打印库名（可安全 $(...) 捕获），用法提示走 stderr。

    实例名可省(v0.6.1):缺省取 config set default-instance / PGTUNER_INSTANCE。"""
    instance_name = _need_instance(instance_name or instance_opt,
                                   "lab clone <实例> --template <模板>")
    instance_mod.ensure_running(instance_name)
    db = instance_mod.clone(instance_name, template)
    print(db)                                    # R2 fix: stdout carries only the db name; script capture no longer mixes in hint lines
    Console(stderr=True).print(
        f"[dim]用法: --dsn \"{instance_mod.dsn_of(instance_name, db)}\"；"
        f"用完: pgtuner lab release {instance_name} {db}[/dim]")


@lab_grp.command("release")
@click.argument("instance_name")
@click.argument("dbname")
@click.option("--force", is_flag=True,
              help="越权释放他人克隆（仅应急/确属误占；操作将落审计并带 forced 标记）")
def lab_release(instance_name: str, dbname: str, force: bool) -> None:
    """归还克隆（terminate+DROP，零残留）。非属主/非克隆库默认拒绝（归属防护 tool-w1）。

    只归还克隆池登记库;未登记库(含形似克隆命名的孤儿)走 lab sweep,确属误删清理加
    --force;系统库任何旗标下拒绝。"""
    try:
        instance_mod.drop_db(instance_name, dbname, force=force)
    except (ValueError, RuntimeError) as e:
        raise click.ClickException(str(e)) from None
    Console().print(f"[green]已归还[/green]: {dbname}")


def _print_lab_stats() -> None:
    """Shared implementation of lab stats/list (commands must not call each other
    directly: once decorated with @command the function is a Command object; a bare
    call re-parses the original argv — the root cause of `lab list` blowing up with
    "Got unexpected extra arguments", per the v0.7.1 postmortem)."""
    details = instance_mod.pool_details()
    if not any(details.values()):
        Console().print("[dim]无活跃克隆[/dim]")
        return
    t = report.Table(box=report.box.SIMPLE, title="活跃克隆")
    t.add_column("实例")
    t.add_column("克隆库(属主)")
    for name, dbs in details.items():
        cells = [db if not meta.get("owner") else f"{db} @{meta['owner']}"
                 for db, meta in dbs.items()]
        t.add_row(name, ", ".join(cells) if cells else "[dim]-[/dim]")
    Console().print(t)


@lab_grp.command("stats")
def lab_stats() -> None:
    """活跃克隆清单（实例 × 克隆库@属主；空池提示无残留）。"""
    _print_lab_stats()


@lab_grp.command("list", hidden=True)
def lab_list() -> None:
    """lab stats 的别名(S5 实测两个 agent 都先试 `lab list`——名字直觉)。"""
    _print_lab_stats()


@lab_grp.command("audit")
@click.option("--instance", "instance_name", default=None, help="只看该实例（缺省全部）")
@click.option("--last", "last", type=click.IntRange(min=1), default=20,
              help="尾部 N 条（缺省 20）")
def lab_audit(instance_name: str | None, last: int) -> None:
    """克隆池审计：谁在何时 clone/release/试图释放谁的克隆（<注册表>.audit.jsonl 尾部）。"""
    rows = instance_mod.audit_tail(instance_name=instance_name, limit=last)
    if not rows:
        Console().print("[dim]审计日志为空[/dim]")
        return
    t = report.Table(box=report.box.SIMPLE, title="克隆审计（尾部）")
    for col in ("时间", "事件", "实例", "克隆库", "属主", "操作者"):
        t.add_column(col)
    for r in rows:
        t.add_row(str(r.get("ts", "")), str(r.get("event", "")), str(r.get("instance", "")),
                  str(r.get("db", "")),
                  r.get("owner") or "[dim]-[/dim]", r.get("actor") or "[dim]-[/dim]")
    Console().print(t)


@lab_grp.command("sweep")
@click.option("--instance", "instance_name", default=None,
              help="只扫该实例（缺省=全部已注册实例）")
@click.option("--older-than", "older_than", type=click.FloatRange(min=0), default=None,
              help="只清创建已到龄（≥N 分钟）的库；创建时间≈base/<oid> 目录 mtime")
@click.option("--yes", is_flag=True, help="真正执行清理（缺省 dry-run：只列不动）")
def lab_sweep(instance_name: str, older_than: float | None, yes: bool) -> None:
    """清理克隆残留：克隆池登记库 + 未登记的 <模板>_c<8位hex> 孤儿库（默认 dry-run）。

    绝不触碰系统库 / 密封模板 / 非克隆命名的普通库；--yes 才 DROP。
    """
    console = Console()
    try:
        rep = instance_mod.sweep(instance_name, older_than=older_than, yes=yes)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    kind_tag = {"registered": "[cyan]登记[/cyan]",
                "orphan": "[yellow]孤儿[/yellow]",
                "stale-entry": "[magenta]失效登记[/magenta]"}
    ignored_by_inst = {}
    for i in rep["ignored"]:
        ignored_by_inst.setdefault(i["instance"], []).append(i)
    for inst in rep["scanned"]:
        tg = [t for t in rep["targets"] if t["instance"] == inst]
        ig = ignored_by_inst.get(inst, [])
        if not tg and not ig:
            console.print(f"[dim]{inst}: 无克隆残留[/dim]")
            continue
        console.print(f"[bold]{inst}[/bold]:")
        for t in tg:
            age = "-" if t["age_minutes"] is None else f"{t['age_minutes']:.1f}min"
            extra = f" ← {t['template']}" if t["template"] else ""
            console.print(f"  {kind_tag[t['kind']]} {t['dbname']}{extra}  (age {age})")
        for i in ig:
            console.print(f"  [dim]跳过 {i['dbname']}: {i['reason']}[/dim]")
    for s in rep["skipped_instances"]:
        console.print(f"[red]实例 {s['instance']} 跳过: {s['reason']}[/red]")
    for e in rep["errors"]:
        console.print(f"[red]清理失败 {e['instance']}/{e['dbname']}: {e['error']}[/red]")
    if not yes:
        if rep["targets"]:
            console.print(f"[yellow]DRY-RUN[/yellow]: {len(rep['targets'])} 个目标待清理，加 --yes 执行")
        else:
            console.print("[green]无克隆残留[/green]")
    else:
        console.print(f"[green]已清理[/green]: DROP {len(rep['dropped'])} 个库，"
                      f"移除失效登记 {len(rep['stale_removed'])} 条"
                      + (f"，失败 {len(rep['errors'])} 个" if rep["errors"] else ""))


@cli.group("config")
def config_grp() -> None:
    """部署级缺省(.pgtuner/config.json;当前: default-instance——数据面命令省 --instance)。"""


@config_grp.command("set")
@click.argument("key", type=click.Choice(["default-instance"]))
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """设部署级缺省(default-instance: 后续命令/lab clone 可省实例名)。"""
    try:
        instance_mod.get(value)                              # verify the instance is registered
    except Exception as e:                                   # noqa: BLE001
        raise click.ClickException(f"实例 {value!r} 不在注册表(instance list 查看):{e}") from None
    cfg = config_mod.save("default_instance", value)
    Console().print(f"[green]已设[/green] default-instance = {value}"
                    "(后续 sql/schema/statements/diagnose/… 可省 --instance;lab clone 可省实例名)")


@config_grp.command("show")
def config_show() -> None:
    """查看当前部署级缺省(env PGTUNER_INSTANCE 覆盖时一并显示)。"""
    cfg = config_mod.load()
    env = os.environ.get("PGTUNER_INSTANCE")
    if not cfg and not env:
        Console().print("[dim]未设置(pgtuner config set default-instance <实例>)[/dim]")
        return
    Console().print_json(json.dumps(
        {"default_instance": cfg.get("default_instance"),
         **({"PGTUNER_INSTANCE(env,优先)": env} if env else {})}, ensure_ascii=False))


@cli.group("results")
def results_grp() -> None:
    """实验账本：list / show / diff / conclude（账本在 .pgtuner/ledger/）。"""


@results_grp.command("list")
@click.option("--kind", default=None,
              help="按类型过滤 bench/planbench/rebench/diagnose/calibration")
@click.option("--contains", default=None, help="label 子串过滤")
@click.option("--last", type=click.IntRange(min=1), default=10, show_default=True,
              help="尾部 N 条")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text",
              show_default=True, help="json=stdout 纯 JSON 数组(agent 管道消费)")
def results_list(kind: str | None, contains: str | None, last: int, fmt: str) -> None:
    """最近实验清单：id/kind/label/构建/结论摘要（id 供 diff/show/conclude 引用）。"""
    ensure_dirs()
    rows = ledger_mod.list_experiments(kind=kind, label_contains=contains, last=last)
    if fmt == "json":
        print(json.dumps([{"id": e.id, "ts": e.ts, "kind": e.kind, "label": e.label,
                           "conclusion": e.conclusion} for e in rows], ensure_ascii=False))
        return
    if not rows:
        Console().print("[yellow]账本为空[/yellow]")
        return
    t = report.Table(box=report.box.SIMPLE_HEAVY, title=f"账本最近 {len(rows)} 条")
    t.add_column("ID", style="bold cyan", no_wrap=True)
    t.add_column("kind")
    t.add_column("label", max_width=36)
    t.add_column("构建", justify="center")
    t.add_column("结论", max_width=40)
    for e in rows:
        bt = (e.fingerprint or {}).get("build_type") or "-"
        t.add_row(e.id, e.kind, e.label, bt, (e.conclusion or "")[:40])
    Console().print(t)


def _resolve_exp_ref(ref: str) -> str:
    """Ledger reference resolution: ids pass through; labels ("kind:label" takes
    the latest of that kind, a bare label must be unique).

    v0.6: benchmark agents once turned `diff/conclude rebench:mylabel` into a
    failed round trip — label references match how label-naming agents naturally
    think, so the entry and exit points should both support them."""
    try:
        ledger_mod.get(ref)
        return ref
    except Exception:                                              # noqa: BLE001
        kind, _, lab = ref.rpartition(":")
        rows = ledger_mod.list_experiments(kind=kind or None, label_contains=lab,
                                           last=50)
        if not rows and kind:                                      # fallback for the "label:xxx" form
            rows = ledger_mod.list_experiments(label_contains=ref, last=50)
        if not rows:
            raise click.ClickException(
                f"{ref!r} 不是账本 id 也不匹配任何 label(账本 id 见 results list)") from None
        if len(rows) > 1 and not kind:
            hint = ";".join(r.id for r in rows[:3])
            raise click.ClickException(
                f"label {lab!r} 匹配多条({len(rows)}),请用 id:{hint}") from None
        resolved = rows[0].id
        if resolved != ref:
            Console().print(f"[dim]label 解析: {ref} → {resolved}[/dim]")
        return resolved


@results_grp.command("show")
@click.argument("exp_id")
@click.option("--format", "fmt", type=click.Choice(["json"]), default="json",
              help="输出恒为 JSON(--format json 显式可写,agent 管道友好)")
def results_show(exp_id: str, fmt: str) -> None:
    """单实验全量 JSON：payload/metrics/conclusion 全保真（结论证据看这里）。"""
    ensure_dirs()
    e = ledger_mod.get(_resolve_exp_ref(exp_id))
    print(json.dumps(e.__dict__, ensure_ascii=False, default=str))


@results_grp.command("diff")
@click.argument("exp_id_a")
@click.argument("exp_id_b")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text",
              show_default=True, help="json=stdout 纯 JSON(verdict/metrics 全保真);text=终端表")
def results_diff(exp_id_a: str, exp_id_b: str, fmt: str) -> None:
    """跨实验对比：指纹守门（debug×release 拒绝）+ 噪声底显著性判定。

    参数可以是账本 id,也可以是 label("kind:label" 取该 kind 最新;裸 label 需唯一)。"""
    ensure_dirs()
    exp_id_a, exp_id_b = _resolve_exp_ref(exp_id_a), _resolve_exp_ref(exp_id_b)
    d = ledger_mod.diff(exp_id_a, exp_id_b)
    if fmt == "json":
        print(json.dumps({"a": exp_id_a, "b": exp_id_b, **d}, ensure_ascii=False, default=str))
        return
    console = Console()
    color = "green" if d["compatible"] else "red"
    console.print(f"[{color}]verdict: {d['verdict']}[/{color}]")
    for w in d["warnings"]:
        console.print(f"[yellow]警告[/yellow]: {w}")
    if d["metrics"]:
        t = report.Table(box=report.box.SIMPLE, title=f"{exp_id_a} → {exp_id_b}")
        t.add_column("指标")
        t.add_column("A", justify="right")
        t.add_column("B", justify="right")
        t.add_column("Δ%", justify="right")
        t.add_column("显著?", justify="center")
        for m in d["metrics"]:
            sig = "-" if m["significant"] is None else ("是" if m["significant"] else "[dim]不显著[/dim]")
            t.add_row(m["name"], f"{m['a']:.2f}", f"{m['b']:.2f}", f"{m['delta_pct']:+.2f}", sig)
        console.print(t)
    else:
        console.print("[dim]两实验无公共 metrics 键,仅作指纹校验[/dim]")


@results_grp.command("conclude")
@click.argument("exp_id")
@click.argument("text")
def results_conclude(exp_id: str, text: str) -> None:
    """结论回填（非空；结论必须贴 results diff 的证据）。"""
    ensure_dirs()
    if not text.strip():
        # An empty conclusion would silently wipe an existing one (often the
        # auto-conclusion from diagnose --verify) — S5-A4 showed agents doing
        # exactly that, erasing the evidence chain. Reject empty text with a
        # didactic pointer.
        raise click.ClickException(
            "结论不能为空:若实验已有自动结论(diagnose --verify 回填)则无需手工 conclude,"
            "results show 即可查看;要改写请给非空文本")
    e = ledger_mod.update(_resolve_exp_ref(exp_id), conclusion=text)
    Console().print(f"[green]结论已回填[/green] {ledger_mod.citation(e.id)}")


@cli.command("planbench")
@click.argument("sqlfile", type=click.Path(exists=True, dir_okay=False))
@click.option("--dsn", default=None, help="libpq 连接串（与 --instance 二选一）")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名(缺省默认实例)")
@click.option("--db", default=None, help="实例上的目标库（缺省 postgres；带 --variant 或文件含写语句(DML/数据修改 CTE)时必须显式给）")
@click.option("--variant", "variants", multiple=True,
              help='NAME=DDL 可重复——不止索引:维护干预也走这里,如 --variant fix="ANALYZE events"'
                   '(baseline 自动在首位,干预前后 plan+中位耗时+逐节点 diff 一条命令出)')
@click.option("--accuracy", is_flag=True, help="每查询输出估算准确率表（按 |log2 误差| 降序）")
@click.option("--flip-check", is_flag=True, help="第一条查询做 plan 翻转检测")
@click.option("--runs", type=click.IntRange(min=1), default=3, show_default=True,
              help="变体模式每查询重复次数（取中位数）")
@click.option("--flip-runs", type=click.IntRange(min=3), default=20, show_default=True,
              help="--flip-check 每模式采样次数(越多翻转判定越稳)")
@click.option("--label", default=None, help="账本 label(results 引用可写 planbench:<label>)")
@click.option("--hypothesis", default=None, help="一句话假设(落账本,结论须对得上)")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text",
              show_default=True,
              help="json=stdout 纯 JSON(账本 payload 全保真,人读提示走 stderr);text=终端表")
def planbench_cmd(sqlfile: str, dsn: str | None, instance_name: str | None, db: str | None,
                  variants: tuple[str, ...], accuracy: bool, flip_check: bool, runs: int,
                  flip_runs: int, label: str | None, hypothesis: str | None, fmt: str) -> None:
    """EXPLAIN 工作台：plan 采集 / 索引·DDL·维护干预变体对比(前后一条命令) / 代价估算准确率 / 翻转检测。"""
    # In json mode stdout must be pure JSON: all human-readable output (tables/rules/receipts) goes to stderr instead
    console = Console(stderr=True) if fmt == "json" else Console()
    # D-F3 (round 4): the file is read and gated BEFORE target resolution — the
    # write gate must refuse without touching the instance/registry at all (and
    # the R1 lesson holds: dsn_given is captured before _resolve_target backfills
    # dsn from the instance, so "resolved" never counts as "explicitly given").
    queries = _split_sql_file(sqlfile)
    if not queries:
        raise click.ClickException("SQL 文件里没有可执行语句")
    dsn_given = dsn is not None      # captured before _resolve_target backfills dsn from the instance
    _planbench_write_gate(queries, db_given=db is not None, dsn_given=dsn_given)
    if flip_check:
        # D5-F3 (round 5): the flip-check loop really executes queries[0]
        # (PREPARE + EXPLAIN ANALYZE EXECUTE ×N) while skipping every guard
        # plain mode applies — a 'select pg_read_file(...)' refused in plain
        # mode ran 3 times here. Measurement face: side-effect blacklist +
        # COPY head + write-smuggle, all absolute (write comparisons belong to
        # the plain/variant channels), all BEFORE target resolution/ledger.
        _guard_exec_stmt(queries[0], "planbench --flip-check", write_gate=True)
    vlist = []
    for v in variants:
        name, sep, ddl = v.partition("=")
        if not sep or not ddl.strip():
            raise click.ClickException(f'--variant 格式应为 NAME=DDL: {v!r}')
        # D5-F3 (round 5): variant DDL is executed verbatim by run_variants
        # with no explain()-layer guard in front of it — the DDL payload
        # channel keeps its design semantics (CREATE/ALTER/DROP/ANALYZE/SET
        # heads allowed) but side-effect functions and COPY heads are refused
        # unconditionally (COPY … TO PROGRAM is a shell, not an intervention).
        _guard_exec_payload(ddl, "planbench --variant")
        vlist.append(planbench_mod.Variant(name.strip(), ddl.strip()))
    if vlist and db is None and not dsn_given:
        # Adversarial review R1-D: variant DDL really executes (CREATE INDEX /
        # ANALYZE) — without this gate a forgotten --db lands it on the shared
        # postgres database by default (rebench already enforces the same).
        raise click.ClickException(
            "--variant 的 DDL 会真实执行——请显式 --db <克隆库>(或 --dsn 指定目标),"
            "缺省会落在共享 postgres 库;先 pgtuner lab clone 拿一次性库再跑")
    if vlist:
        # D5-F3 (round 5): in variant mode the file's queries run as raw
        # 'EXPLAIN (ANALYZE, FORMAT JSON) <stmt>' inside run_variants —
        # planbench.explain()'s own side-effect blacklist never sees them, so
        # 'select pg_read_file(...)' bypassed the plain-mode refusal here too.
        # Blacklist parity only: DML heads / data-modifying CTEs stay governed
        # by the --db write gate above (write comparison is this channel's
        # documented job once the target is explicit).
        for q in queries:
            _guard_exec_stmt(q, "planbench --variant")
    dsn, inst_meta = _resolve_target(instance_name, db, dsn)
    ensure_dirs()
    exp = ledger_mod.new_experiment("planbench", label or Path(sqlfile).name,
                                    hypothesis=hypothesis, instance=inst_meta,
                                    fingerprint=_safe_fp(dsn, inst_meta),
                                    payload={"queries": [q[:160] for q in queries],
                                             "dbname": db or (pg.target(dsn)[2] if dsn else None)})
    if flip_check:
        r = planbench_mod.run_flip_check(dsn, queries[0], runs=flip_runs)
        for mode, info in r.items():
            console.print(f"  flip-check {mode}: flips={info['flips']} hashes={info['hashes']}")
        ledger_mod.update(exp.id, payload_merge={"flip_check": {m: i["flips"] for m, i in r.items()}})
    if vlist:
        out = planbench_mod.run_variants(dsn, queries, vlist, runs=runs)
        t = report.Table(box=report.box.SIMPLE_HEAVY, title="变体 × 查询 中位耗时(ms)")
        t.add_column("variant", style="bold cyan")
        for i in range(len(queries)):
            t.add_column(f"Q{i}", justify="right")
        for v in out["variants"]:
            row = [v["name"]]
            for q in v["queries"]:
                med = q.get("median_ms")
                row.append(f"{med:.1f}" if med is not None else "[red]err[/red]")
            t.add_row(*row)
        console.print(t)
        for (va, vb), per_q in out.get("diffs", {}).items():
            for qi, diffs in per_q.items():
                if diffs:
                    console.rule(f"plan diff: {va} → {vb} · Q{qi}")
                    console.print(planbench_mod.render_diff(diffs))
        metrics = {}
        for v in out["variants"]:
            if v["queries"] and v["queries"][0].get("median_ms") is not None:
                metrics[v["name"]] = v["queries"][0]["median_ms"]
        # D3: diffs' tuple keys are not JSON-serializable; convert to "a → b" strings before writing to the ledger
        diffs_json = {f"{va} → {vb}": per_q for (va, vb), per_q in out.get("diffs", {}).items()}
        ledger_mod.update(exp.id, payload_merge={"variants": {**out, "diffs": diffs_json},
                                                 "metrics": metrics})
    else:
        acc_summary: dict = {}
        acc_metrics: dict = {}
        # F-C (patch-trial): plain mode also writes plan archives (.pgtuner/plans)
        # + payload + a key-line summary; failure text goes into the payload
        # (old behavior: failures only printed to stdout and vanished from the ledger)
        plain_plans: dict = {}
        plain_errors: dict = {}
        for i, q in enumerate(queries):
            try:
                plan = planbench_mod.explain(dsn, q)
            except RuntimeError as e:
                console.print(f"  Q{i}: [red]失败[/red] {e}")
                plain_errors[f"Q{i}"] = str(e)[:400]
                continue
            console.print(f"  Q{i}: {plan.get('Node Type')} est_rows={plan.get('Plan Rows')}"
                          f" actual_rows={plan.get('Actual Rows')}"
                          f" exec_ms={plan.get('Actual Total Time', plan.get('Total Cost'))}")
            plain_plans[f"Q{i}"] = plan
            if accuracy:
                rows = planbench_mod.accuracy_report(plan)
                console.print(planbench_mod.render_accuracy(rows))
                if rows:
                    acc_summary[f"Q{i}"] = {"worst": f"{rows[0].node_type}@{rows[0].path}",
                                            "log2": rows[0].log2_error, "nodes": len(rows)}
                    acc_metrics[f"Q{i}_abslog2err"] = round(abs(rows[0].log2_error), 3)
        merge: dict = {"plain_plans": plain_plans}
        if plain_errors:
            merge["errors"] = plain_errors
        if accuracy and acc_summary:
            # D5: accuracy mode also writes payload+metrics, usable by cross-experiment results diff
            merge["accuracy"] = acc_summary
            merge["metrics"] = acc_metrics
        ledger_mod.update(exp.id, payload_merge=merge)
        if plain_plans:
            # F-C: plans are also written to .pgtuner/plans/<exp_id>/ (the dir used to be created but never written, so agents could not retrieve archives)
            pdir = PLANS_DIR / exp.id
            pdir.mkdir(parents=True, exist_ok=True)
            for k, plan in plain_plans.items():
                (pdir / f"{k}.json").write_text(
                    json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
            console.print(f"[dim]plan 档已写: {pdir}[/dim]")
    console.print(f"[green]已落账本[/green] {ledger_mod.citation(exp.id)}")
    if fmt == "json":
        # The final ledger entry goes to stdout verbatim (including progressively merged variants/plain_plans/accuracy) — zero drift
        e = ledger_mod.get(exp.id)
        print(json.dumps({"id": e.id, "kind": "planbench", "label": e.label, **(e.payload or {})},
                         ensure_ascii=False, default=str))


@cli.command("rebench")
@click.option("--instance", "instance_name", required=True)
@click.option("--guc", "gucs", multiple=True, help="k=v 可重复:每个 --guc 是一个候选")
@click.option("--guc-file", type=click.Path(exists=True, dir_okay=False), help="每行 k=v(# 注释),每行一个候选")
@click.option("--template", default=None, help="实验库从模板克隆(结束自动归还)")
@click.option("--dbname", "--db", "dbname", default="bench_new", show_default=True,
              help="目标库(--db 别名;--queries-file 时必须显式给——表几乎总在克隆库,"
                   "缺省 bench_new 常是错库)")
@click.option("--queries-file", type=click.Path(exists=True, dir_okay=False),
              help="custom 负载 SQL 文件(按 ; 切分)。含 utility 语句(DDL/ANALYZE 等)→ "
                   "script 模式:整文件按序单遍执行、逐条计时、不重跑(非幂等 DDL 安全);"
                   "纯 SELECT → 每查询预热 2 遍 + EXPLAIN ANALYZE × --runs 取中位")
@click.option("--setup", "setup_stmts", multiple=True,
              help="before/after 两轮测量之间依序执行的干预语句(可重复,如 "
                   "--setup \"ANALYZE events\"):单遍执行逐条计时。要求 --queries-file "
                   "为纯 SELECT 负载;免确认(写只落在实验克隆)")
@click.option("--duration", type=click.IntRange(min=1), default=20, show_default=True)
@click.option("--warmup", type=click.IntRange(min=0), default=10, show_default=True)
@click.option("--clients", type=click.IntRange(min=1), default=4, show_default=True)
@click.option("--runs", type=click.IntRange(min=1), default=3, show_default=True,
              help="custom 负载每查询重复次数(取中位);pgbench 负载忽略此参数")
@click.option("--pgbench", "pgbench_path", default=None,
              help="pgbench 可执行文件路径;缺省自动解析:实例 bindir(优先)> PATH")
@click.option("--label", default=None, help="账本 label(results 引用可写 rebench:<label>)")
@click.option("--hypothesis", default=None, help="一句话假设(落账本,结论须对得上)")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text",
              show_default=True,
              help="json=stdout 纯 JSON(账本 payload 全保真,人读提示走 stderr);text=终端表")
@click.option("--yes", is_flag=True, help="确认慢通道时长后执行(agent 须先向用户征得同意)")
@click.pass_context
def rebench_cmd(ctx: click.Context, instance_name: str, gucs: tuple[str, ...], guc_file: str | None, template: str | None,
                dbname: str, queries_file: str | None, setup_stmts: tuple[str, ...], duration: int,
                warmup: int, clients: int, runs: int, pgbench_path: str, label: str | None,
                hypothesis: str | None, fmt: str, yes: bool) -> None:
    """GUC 重启实测 + 纯测量计时双通道(--queries-file:utility 语句单遍 script 模式/SELECT 取中位;
    --setup "SQL" 前后干预一条命令出证据;--format json 出纯 JSON)。"""
    # In json mode stdout must be pure JSON: all human-readable output (including confirmation prompts/green receipts) goes to stderr instead
    console = Console(stderr=True) if fmt == "json" else Console()
    cand_gucs: list[dict] = []
    for g in gucs:
        k, sep, v = g.partition("=")
        if not sep or not v.strip():
            raise click.ClickException(f"--guc 格式应为 k=v: {g!r}")
        cand_gucs.append({k.strip(): v.strip()})
    if guc_file:
        for line in Path(guc_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            k, sep, v = line.partition("=")
            if sep and v.strip():
                cand_gucs.append({k.strip(): v.strip()})
    # --setup (v0.11.0) pre-validation: usage errors should not take the ledger failure path; teach the correct usage directly
    if setup_stmts and not queries_file:
        raise click.ClickException(
            "--setup 需要 --queries-file 纯 SELECT 测量负载:干预语句夹在前后两轮测量之间,"
            "没有负载就无前后可言;utility 语句整文件计时请直接把语句写进 --queries-file(script 模式)")
    setup_list = list(setup_stmts) or None
    if setup_list:
        # D5-F3 (round 5): --setup is the explicit DDL payload channel (its day
        # job: CREATE TABLE / ANALYZE / maintenance interventions), so overt
        # DDL/DML heads stay allowed — but side-effect functions and COPY heads
        # are refused unconditionally, BEFORE any connection/ledger side effect
        # (COPY … TO PROGRAM executed a real shell here; a read-only transaction
        # would not have stopped it — the statements run on a rw connection).
        for s in setup_list:
            _guard_exec_payload(s, "rebench --setup")
    custom_queries = _split_sql_file(queries_file) if queries_file else None
    if setup_list and custom_queries and any(
            rebench_mod._is_utility(rebench_mod._strip_query(q)) for q in custom_queries):
        raise click.ClickException(
            "--setup 要求 --queries-file 为纯 SELECT(EXPLAIN 可包装):utility 语句"
            "(DDL/ANALYZE)要么整文件 script 模式单遍跑,要么作为 --setup 干预语句")
    # F-D (patch-trial): no --guc plus --queries-file → measure-only mode (no param
    # changes / no restart / no confirmation, for comparisons such as cross-build
    # A/B that need no parameter changes); otherwise at least one candidate is required
    measure_only = not cand_gucs
    if setup_list is not None:
        measure_only = True                 # setup only rides on measure-only; the GUC-carrying combination is rejected below
        if cand_gucs:
            raise click.ClickException(
                "--setup 只挂纯测量(免确认)通道,不与 GUC 重启对比混用;GUC 实验请去掉 --setup")
    if measure_only and not queries_file:
        raise click.ClickException(
            "至少给一个 --guc k=v 或 --guc-file;或改用 --queries-file 进入纯测量模式(不改参不重启)")
    # Whether the user explicitly passed --db/--dbname (including the --db=x
    # attached form; a bare "in sys.argv" check misses attached values and would
    # misjudge --db=mydb as "not given" and refuse to run — shared by two checks,
    # computed once)
    db_given = any(a.split("=", 1)[0] in ("--db", "--dbname") for a in sys.argv)
    # v0.6: both S2 benchmark agents omitted --db → hit the default bench_new,
    # wasting a round plus a garbage ledger entry. Custom-workload tables almost
    # always live in a clone; require an explicit --db (prompt first, no silent default)
    if queries_file and not db_given and not template:
        raise click.ClickException(
            "custom 负载(--queries-file)请显式 --db <目标库/克隆库>"
            "(缺省会落到 bench_new;两个基准 agent 曾因此白跑一轮)")
    if measure_only:
        console.print("[green]纯测量模式[/green]:无 GUC 候选 → 不重启、不还原,只测当前状态"
                      "(预热 2 遍 + 每查询 " + str(runs) + " 次取中位)")
        if setup_list:
            # R2-D gate: the setup statements really execute on the target db — the
            # no-confirmation channel is only honest when the target is a pool
            # clone (--template clones one; anything else needs --yes)
            in_pool = dbname in instance_mod.pool_stats().get(instance_name, [])
            if not template and not in_pool and not yes:
                console.print(
                    f"[yellow]慢通道确认[/yellow]: --setup 干预语句将真实写入非克隆池登记库 "
                    f"{dbname}@{instance_name}(免确认通道只对克隆库诚实)。确认执行请加 --yes")
                # C-NIT-3: not-run-without-consent exits nonzero — a chained
                # `rebench ... && results diff ...` must not read "refused" as
                # success. Output stays friendly (no Error prefix): ctx.exit is
                # click control flow, not an exception render.
                ctx.exit(1)
            console.print("[green]setup 干预[/green](在前后两轮测量之间依序单遍执行,写落在 --db 目标库):")
            for s in setup_list:
                console.print(f"  [bold cyan]{s.strip()}[/bold cyan]")
    candidates = [rebench_mod.RestartCandidate(",".join(f"{k}={v}" for k, v in g.items())[:40], g)
                  for g in cand_gucs]
    n = len(candidates) + 1        # baseline is added automatically
    is_custom = bool(queries_file)
    if not yes and not measure_only:
        if is_custom:
            console.print(f"[yellow]慢通道确认[/yellow]: {n} 个候选 ×(重启≈15s + 预热 2 遍 + 每查询 "
                          f"{runs} 次测量)。custom 负载时长取决于查询本身,无法精确预估,请确认后加 --yes")
        else:
            est = n * (warmup + duration + 15)
            console.print(f"[yellow]慢通道确认[/yellow]: {n} 个候选 ×(预热 {warmup}s + 测量 {duration}s + 重启≈15s)"
                          f" ≈ {est}s(约 {est // 60} 分钟)。确认执行请加 --yes")
        # C-NIT-3 (round 4): the estimate-only stop used to leave rc=0, so agent
        # chains (`rebench ... && results diff ...`) treated "never ran" as
        # success. Exit nonzero instead; the message above stays friendly (the
        # Exit raised here is click control flow, not an Error-prefixed render).
        ctx.exit(1)
    inst = instance_mod.get(instance_name)
    inst_meta = _inst_meta(inst)
    if not db_given and not template:
        Console(stderr=True).print(
            "[yellow]提示[/yellow]: 未指定 --db(默认 bench_new);表在克隆库时加 --db <克隆名>"
            "(v0.6:基准 agent 曾因此连错库白跑一轮)")
    # B1 defect D6 fix: when pgbench is not given explicitly, prefer the
    # instance's own binary (version-matched), then fall back to PATH (tool-w3:
    # de-machine-bound); custom workloads (--queries-file) need no pgbench
    pgbench_path = _fallback_pgbench(pgbench_path, bindir=inst.bindir)
    ensure_dirs()
    # Fingerprint and noise floor are taken up front — failure paths must still
    # reach the ledger (B1 defect D4: iron rule 2 requires every experiment to leave a trace)
    fp = _safe_fp(instance_mod.dsn_of(instance_name, "postgres"), inst_meta)
    cv = ledger_mod.latest_noise_floor(fp or {}) if fp else None
    owned_clone = None
    workload = "custom" if custom_queries else "pgbench"
    if workload == "pgbench" and not pgbench_path:
        raise click.ClickException(
            "pgbench 未找到(未显式指定且实例 bindir/PATH 均无):用 --pgbench 指定路径;"
            "编译安装: bash scripts/setup.sh --pg <prefix>")
    base_payload = {"workload": workload, "duration": duration, "warmup": warmup,
                    "runs": runs, "dbname": dbname, "gucs": [c.gucs for c in candidates],
                    "setup": setup_list,
                    "measure_only": measure_only,
                    "noise_floor": {"tps_cv_pct": cv} if cv is not None else None}
    try:
        if template:
            instance_mod.ensure_running(instance_name)
            owned_clone = instance_mod.clone(instance_name, template)   # R4: clone failures also take the ledger-writing path
            dbname = owned_clone
            console.print(f"实验库克隆: {owned_clone}(结束自动归还)")
        results = rebench_mod.run_restart_bench(
            instance_name, candidates, dbname=dbname, workload=workload,
            duration=duration, warmup=warmup, clients=clients,
            pgbench_path=pgbench_path, custom_queries=custom_queries, runs=runs,
            measure_only=measure_only, setup_statements=setup_list)
    except Exception as e:                                         # noqa: BLE001
        fail = ledger_mod.new_experiment(
            "rebench", label or f"rebench:{instance_name}", hypothesis=hypothesis,
            instance=inst_meta, fingerprint=fp,
            payload={**base_payload, "failed": True,
                     "error": f"{type(e).__name__}: {e}"[:400]})
        console.print(f"[red]实验失败[/red]: {type(e).__name__}: {e}\n"
                      f"[yellow]失败记录已落账本(铁律 2)[/yellow] {ledger_mod.citation(fail.id)}")
        if fmt == "json":
            print(json.dumps({"id": fail.id, "kind": "rebench",
                              "label": label or f"rebench:{instance_name}",
                              **{**base_payload, "failed": True,
                                 "error": f"{type(e).__name__}: {e}"[:400]}},
                             ensure_ascii=False, default=str))
        raise click.ClickException(f"rebench 失败(已记账本,还原是否完成见上方模块输出)") from e
    finally:
        if owned_clone:
            instance_mod.drop_db(instance_name, owned_clone)
    if setup_list:
        # setup A/B dedicated view: per-statement single-pass timing + per-query before → after
        by_name = {r.candidate.name: r for r in results}
        setup_r = by_name.get("setup")
        if setup_r and setup_r.payload and setup_r.payload.get("queries"):
            st = report.Table(box=report.box.SIMPLE, title="setup(依序单遍执行)")
            st.add_column("语句", style="bold cyan")
            st.add_column("耗时 ms", justify="right")
            for q in setup_r.payload["queries"]:
                st.add_row(q["query"], f"{q['execution_ms']:.1f}"
                          if q.get("execution_ms") is not None else "[red]err[/red]")
            console.print(st)
        bt = report.Table(box=report.box.SIMPLE,
                          title="before → after(每查询中位 ms,↓优;runs 明细在账本)")
        bt.add_column("查询", style="bold cyan")
        bt.add_column("before", justify="right")
        bt.add_column("after", justify="right")
        bt.add_column("Δ", justify="right")

        def _med(r, i):
            if r and r.payload and i < len(r.payload.get("queries") or []):
                v = r.payload["queries"][i].get("execution_ms")
                return f"{v:.2f}" if v is not None else "[red]err[/red]"
            return "[red]err[/red]"
        bef, aft = by_name.get("before"), by_name.get("after")
        for i in range(len(custom_queries or [])):
            b = (bef.payload["queries"][i].get("execution_ms")
                 if bef and bef.payload and i < len(bef.payload.get("queries") or []) else None)
            a = (aft.payload["queries"][i].get("execution_ms")
                 if aft and aft.payload and i < len(aft.payload.get("queries") or []) else None)
            bt.add_row(f"Q{i}", _med(bef, i), _med(aft, i),
                       f"{(a / b - 1) * 100:+.1f}%" if (a and b) else "-")
        console.print(bt)
    else:
        # Inline comparison table (baseline = entry 0); the significance column is
        # pgbench-workload only (R2: TPS-based CV is a misfit proxy for latency metrics)
        better_is_high = not bool(custom_queries)          # custom = median latency, lower is better
        unit = "TPS↑" if better_is_high else "中位ms↓"
        show_sig = not bool(custom_queries)
        t = report.Table(box=report.box.SIMPLE_HEAVY,
                         title=f"rebench {instance_name} · {unit}"
                               + (f" · 噪声底CV={cv:.1f}%" if (cv and show_sig) else ""))
        t.add_column("候选", style="bold cyan")
        t.add_column(unit.split("↑")[0].split("↓")[0], justify="right")
        t.add_column("vs baseline", justify="right")
        t.add_column("显著?", justify="center")
        base = next((r for r in results if r.candidate.name == "baseline"), results[0])
        for r in results:
            delta = ""
            sig = "-"
            if r.tps is not None and base.tps and r is not base:
                pct = (r.tps / base.tps - 1) * 100
                delta = f"{pct:+.1f}%"
                if cv is not None and show_sig:
                    sig = "是" if abs(pct) >= cv else "[dim]不显著[/dim]"
                elif not show_sig:
                    sig = "[dim]n/a[/dim]"
            t.add_row(r.candidate.name, f"{r.tps:.2f}" if r.tps is not None else "[red]err[/red]",
                      delta, sig)
        console.print(t)
        # F-E (patch-trial): custom workloads print the per-query median detail straight to stdout; no need to dig through the ledger payload
        if custom_queries:
            qt = report.Table(box=report.box.SIMPLE, title="每查询中位耗时 ms(↓优;runs 明细在账本)")
            qt.add_column("候选", style="bold cyan")
            for i in range(len(custom_queries)):
                qt.add_column(f"Q{i}", justify="right")
            for r in results:
                if r.payload and "queries" in r.payload:
                    row = [r.candidate.name]
                    for q in r.payload["queries"]:
                        row.append(f"{q['execution_ms']:.2f}" if q.get("execution_ms") is not None
                                   else "[red]err[/red]")
                    qt.add_row(*row)
            console.print(qt)
    if any(r.error for r in results):
        console.print("[yellow]部分候选失败(已记录,还原不受影响)[/yellow]")
    ensure_dirs()
    # R3 fix: custom-workload metrics are ledgered at candidate|query granularity
    # (an aggregated median is meaningless for diff); pgbench workloads keep
    # candidate-granularity TPS
    metrics: dict = {}
    for r in results:
        if r.tps is None:
            continue
        if custom_queries and r.payload and "queries" in r.payload:
            for qi, q in enumerate(r.payload["queries"]):
                if q.get("execution_ms") is not None:
                        # F-G: metric names carry a unit suffix; ledger.diff uses it to detect the lower_better direction
                    metrics[f"{r.candidate.name}|Q{qi}_ms"] = q["execution_ms"]
        else:
            metrics[r.candidate.name] = r.tps
    # R1-D hardening: "restored" now carries the verified receipts from each cycle's
    # restore_and_verify (see restartbench.CycleResult.restore), not a hardcoded
    # literal; measure_only cycles changed no config and carry restore=None.
    receipts = [getattr(r, "restore", None) for r in results]
    restored = all(r is None or bool(r.get("restored")) for r in receipts)
    final_payload = {**base_payload,
                     "metrics": metrics,
                     "cycles": [{"name": r.candidate.name, "gucs": r.candidate.gucs, "tps": r.tps,
                                 "error": r.error, "restore": getattr(r, "restore", None),
                                 **({"queries": r.payload["queries"]} if r.payload and "queries" in r.payload else {})}
                                for r in results],
                     "restored": restored}
    exp = ledger_mod.new_experiment(
        "rebench", label or f"rebench:{instance_name}", hypothesis=hypothesis,
        instance=inst_meta, fingerprint=fp, payload=final_payload)
    if cv is not None:            # R5 fix: the top-level noise_floor field is persisted too
        ledger_mod.update(exp.id, noise_floor={"tps_cv_pct": cv})
    done_word = ("纯测量完成(未改配置,restored: True)" if (measure_only and restored)
                 else f"无痕还原完成(restored: {restored})")
    console.print(f"[green]{done_word},已落账本[/green] {ledger_mod.citation(exp.id)}")
    if fmt == "json":
        # Ledger payload verbatim + envelope: stdout and the ledger have zero drift (human-readable receipt already on stderr)
        print(json.dumps({"id": exp.id, "kind": "rebench",
                          "label": label or f"rebench:{instance_name}", **final_payload},
                         ensure_ascii=False, default=str))


@cli.command("watch")
@click.option("--dsn", default=None, help="libpq 连接串(与 --instance 二选一)")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名")
@click.option("--db", default=None, help="缺省 postgres")
@click.option("--interval", type=click.FloatRange(min=0.2), default=1.0, show_default=True)
@click.option("--duration", type=click.FloatRange(min=1), default=None, help="秒,缺省一直运行")
@click.option("--snapshot", is_flag=True, help="无 Live 模式:采 3 帧输出全部帧文本(agent/CI 用)")
def watch_cmd(dsn: str | None, instance_name: str | None, db: str | None,
              interval: float, duration: float | None, snapshot: bool) -> None:
    """实时状态仪表盘:实例概况 + 活动会话 TOP(只读,单连接)。"""
    dsn, _ = _resolve_target(instance_name, db, dsn)
    if snapshot:
        Console().print(watch_mod.snapshot(dsn, frames=3, interval=interval))
    else:
        watch_mod.run_live(dsn, interval=interval, duration=duration)


@cli.command("sql")
@click.argument("query", required=False)
@click.option("--dsn", default=None, help="libpq 连接串(与 --instance 二选一)")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名")
@click.option("--db", default=None, help="实例上的目标库(缺省 postgres;PG 一连接一库)")
@click.option("--max-rows", type=click.IntRange(min=1), default=100, show_default=True,
              help="行数上限(超限截断并标注,不全取不 COUNT)")
@click.option("--timeout", type=click.FloatRange(min=0.1), default=30.0, show_default=True,
              help="statement_timeout 秒(失控查询保护)")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text",
              show_default=True, help="json=机器可读全保真;text=终端表(单元格超宽截断)")
def sql_cmd(query: str | None, dsn: str | None, instance_name: str | None, db: str | None,
            max_rows: int, timeout: float, fmt: str) -> None:
    """只读 SQL 执行入口(功能检查/数据探看)。一次一条;不输出计时(铁律 1:性能证据走 bench/rebench)。

    只读双重强制:工具层语句头白名单 + 写关键字/副作用函数拒绝;数据库层只读事务兜底。写需求走 lab 克隆库。
    注意:SELECT 也会推进 pg_stat 计数器——基准测量前的诊断建议指向克隆库。
    QUERY 缺省或为 "-" 时从 stdin 读。"""
    console = Console()
    text = query if query and query != "-" else sys.stdin.read()
    try:
        stmts = sqlsplit.split_statements(text)
    except ValueError as e:
        raise click.ClickException(f"语句词法错误: {e}") from None
    if not stmts:
        raise click.ClickException("没有可执行语句")
    if len(stmts) > 1:
        raise click.ClickException(
            f"一次只执行一条语句(检出 {len(stmts)} 条,分号切分)——请拆开逐条执行;"
            f"多语句脚本建库请走 lab make-tpl(整文件单次执行)")
    _guard_sql_statement(stmts[0])
    dsn, _ = _resolve_target(instance_name, db, dsn)
    try:
        res = sqlcmd.run_query(dsn, stmts[0], max_rows=max_rows, timeout_s=timeout)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None
    if not res.columns:
        console.print("[dim](无结果集)[/dim]")
        return
    if fmt == "json":
        # Machine-readable path: bare stdout (not through rich — print_json's
        # re-wrapping/line breaks break JSON parsing); human-readable hints all
        # go to stderr, keeping stdout pure JSON
        print(sqlcmd.render_json(res))
    else:
        # v0.6 data-plane slimming: render_text returns a plain-text str (aligned tables / EXPLAIN lines verbatim), printed bare
        print(sqlcmd.render_text(res))
    if res.truncated:
        Console(stderr=True).print(f"[yellow]已截断[/yellow]:仅显示前 {len(res.rows)} 行"
                                   f"(--max-rows 调整;json 格式的 truncated 字段同标记)")


@cli.group("schema")
def schema_grp() -> None:
    """库内省内省:表清单 / 单表全貌(列/索引/约束/触发器/统计/分区/DDL 重建)。只读。"""


@schema_grp.command("tables")
@click.option("--dsn", default=None, help="libpq 连接串(与 --instance 二选一)")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名(缺省默认实例)")
@click.option("--db", default=None, help="缺省 postgres")
@click.option("--schema", "schema_name", default=None, help="只列该 schema(缺省=全部非系统 schema)")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text",
              show_default=True, help="json=stdout 纯 JSON(机器可读);text=紧凑文本")
def schema_tables(dsn: str | None, instance_name: str | None, db: str | None,
                  schema_name: str | None, fmt: str) -> None:
    """用户对象清单(表/分区表/视图/物化视图):行数双口径 + 大小双口径 + 注释。"""
    dsn, _ = _resolve_target(instance_name, db, dsn)
    conn = pg.connect(dsn)                                     # read-only
    try:
        summaries = introspect.list_tables(conn, schema_name)
    finally:
        conn.close()
    if not summaries:
        Console().print("[yellow]无用户对象(或该 schema 为空)[/yellow]")
        return
    if fmt == "json":
        print(introspect.render_tables_json(summaries))     # bare stdout; rich line-wrapping would break JSON
    else:
        print(introspect.render_tables_text(summaries))     # v0.6 data-plane slimming: plain text printed bare


@schema_grp.command("describe")
@click.argument("table")
@click.option("--dsn", default=None, help="libpq 连接串(与 --instance 二选一)")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名(缺省默认实例)")
@click.option("--db", default=None, help="缺省 postgres")
@click.option("--schema", "schema_name", default="public", show_default=True)
@click.option("--ddl", is_flag=True, help="附 catalog 重建的语义等价 DDL(边界见输出头注)")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text",
              show_default=True, help="json=stdout 纯 JSON(机器可读);text=紧凑文本")
def schema_describe(table: str, dsn: str | None, instance_name: str | None, db: str | None,
                    schema_name: str, ddl: bool, fmt: str) -> None:
    """单表全貌:概况/列/索引/约束/触发器/分区/视图定义(对象名不需引号,原样匹配)。"""
    dsn, _ = _resolve_target(instance_name, db, dsn)
    conn = pg.connect(dsn)
    try:
        try:
            desc = introspect.describe_table(conn, table, schema_name)
        except ValueError as e:
            raise click.ClickException(str(e)) from None
    finally:
        conn.close()
    if fmt == "json":
        print(introspect.render_desc_json(desc))            # bare stdout; rich line-wrapping would break JSON
    else:
        print(introspect.render_desc_text(desc))            # v0.6 data-plane slimming: plain text printed bare
    if ddl:
        print("-- 重建 DDL --")
        print(introspect.synthesize_ddl(desc))


@cli.command("statements")
@click.option("--dsn", default=None, help="libpq 连接串(与 --instance 二选一)")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名")
@click.option("--db", default=None, help="实例上的目标库(缺省 postgres;统计按库分)")
@click.option("--sort", type=click.Choice(sorted(statements_mod.SORTS)), default="total",
              show_default=True, help="total=总耗时 mean=均耗 calls=调用次数 temp=临时块写(落盘信号)")
@click.option("--limit", type=click.IntRange(1, 200), default=10, show_default=True)
@click.option("--min-calls", type=click.IntRange(min=1), default=1, show_default=True,
              help="过滤调用过少的语句")
@click.option("--setup", is_flag=True,
              help="一键启用 pg_stat_statements(需 --instance;实例需重启,显式给本旗标即确认,"
                   "有活跃客户端后端时拒绝,--force 越权);外部实例只给手工指引")
@click.option("--force", is_flag=True, help="--setup 时无视活跃后端强行重启(会打断它们)")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text",
              show_default=True, help="json=机器可读(stdout 纯 JSON)")
def statements_cmd(dsn: str | None, instance_name: str | None, db: str | None,
                   sort: str, limit: int, min_calls: int, setup: bool, force: bool,
                   fmt: str) -> None:
    """pg_stat_statements top queries:真实负载热点(只读计数器,不执行语句)。"""
    if setup:
        if not instance_name:
            raise click.ClickException("--setup 需要 --instance(它要对实例做预载+重启)")
        rep = statements_mod.setup(instance_name, db or "postgres", force=force)
        Console().print(f"[green]{rep['note']}[/green] "
                        f"(restart={'是' if rep['restart'] else '否'})")
    dsn, _ = _resolve_target(instance_name, db, dsn)
    try:
        statements_mod.ensure_ready(dsn)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None
    conn = pg.connect(dsn)
    try:
        stats = statements_mod.top_queries(conn, sort=sort, limit=limit, min_calls=min_calls)
    finally:
        conn.close()
    if not stats:
        Console().print("[yellow]无满足条件的语句(库刚启用/过滤太严)[/yellow]")
        return
    if fmt == "json":
        print(statements_mod.render_json(stats))
    else:
        print(statements_mod.render_text(stats, sort=sort))


@cli.command("hypo")
@click.option("--index", "indexes", multiple=True,
              help='假设索引 DDL,如 --index "CREATE INDEX ON t(k)"(可重复;多候选自动加评组合集)')
@click.option("--query", "query_args", multiple=True, help="评估查询(可重复)")
@click.option("--queries-file", type=click.Path(exists=True, dir_okay=False), help="查询文件(分号切分)")
@click.option("--dsn", default=None, help="libpq 连接串(与 --instance 二选一)")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名")
@click.option("--db", default=None, help="目标库——请在克隆库上跑(建索引期间表会短暂加锁)")
@click.option("--analyze", is_flag=True,
              help="EXPLAIN ANALYZE(会执行语句;耗时是 plan 级实际值,TPS 结论仍需 bench/rebench)")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text",
              show_default=True)
def hypo_cmd(indexes: tuple[str, ...], query_args: tuple[str, ...], queries_file: str | None,
             dsn: str | None, instance_name: str | None, db: str | None,
             analyze: bool, fmt: str) -> None:
    """假设索引评估:事务内建索引→EXPLAIN→回滚,零残留(已装 hypopg 则自动走快路径)。

    输出=估算口径(每查询 baseline vs 假设索引的代价/计划变化);性能结论仍走
    planbench/rebench 出账本证据。"""
    if not indexes:
        raise click.ClickException('至少一个 --index,如 --index "CREATE INDEX ON t(k)"')
    # R2-D: --index executes the DDL verbatim (rolled back afterwards, but external
    # side effects don't roll back) — whitelist CREATE [UNIQUE] INDEX + single statement
    for idx in indexes:
        try:
            idx_stmts = sqlsplit.split_statements(idx)
        except ValueError as e:
            raise click.ClickException(f"--index: 语句词法错误: {e}") from None
        if len(idx_stmts) != 1 or not _INDEX_DDL_HEAD.match(idx_stmts[0]):
            raise click.ClickException(
                f'--index 只接受一条 CREATE [UNIQUE] INDEX DDL: {idx[:60]!r}'
                "(事务回滚罩不住外部副作用,其它干预走 planbench --variant,那里有 --db 闸门)")
    # single-statement guard per --query (see _single_query_arg); the read-only
    # guard runs on --query and --queries-file alike (file queries merge into the
    # list BEFORE the loop — the same shape advise got in R3 D2)
    queries = [_single_query_arg(q, "--query") for q in query_args]
    if queries_file:
        queries += _split_sql_file(queries_file)
    for q in queries:
        _guard_analyze_query(q, "hypo --query/--queries-file")
    if not queries:
        raise click.ClickException("至少一条查询(--query 或 --queries-file)")
    dsn, _ = _resolve_target(instance_name, db, dsn)
    Console().print("[yellow]提示[/yellow]:评估会在目标库真实建索引(随即回滚,零残留),"
                    "建索引期间表短暂加锁——请在克隆库上跑")
    try:
        report_ = hypo_mod.eval_candidates(dsn, list(indexes), queries, analyze=analyze)
    except (RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from None
    if fmt == "json":
        print(hypo_mod.render_json(report_))
    else:
        print(hypo_mod.render_text(report_))


@cli.command("advise")
@click.option("--query", "query_args", multiple=True, help="要优化的查询(可重复)")
@click.option("--queries-file", type=click.Path(exists=True, dir_okay=False), help="查询文件(分号切分)")
@click.option("--from-statements", "from_ss", type=click.IntRange(1, 50), default=None,
              help="从 pg_stat_statements 拉真实负载 top N 作查询集(需已启用;归一化文本"
                   "带 $n 占位→只出候选与处方,落地为具体查询后再验)")
@click.option("--dsn", default=None, help="libpq 连接串(与 --instance 二选一)")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名")
@click.option("--db", default=None, help="目标库(建议克隆库)")
@click.option("--analyze", is_flag=True, help="hypo 评估用 EXPLAIN ANALYZE(会执行语句)")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text",
              show_default=True, help="json=stdout 纯 JSON(机器可读);text=终端表")
def advise_cmd(query_args: tuple[str, ...], queries_file: str | None, from_ss: int | None,
               dsn: str | None, instance_name: str | None, db: str | None,
               analyze: bool, fmt: str) -> None:
    """索引建议(透明漏斗):启发式候选(带证据)→ hypo 估算评估 → 排名 + 验证处方。

    advise 不背书性能:估算只筛候选,结论按处方走 planbench/rebench 出账本证据。"""
    # single-statement guard per --query (see _single_query_arg); the read-only
    # guard covers --queries-file too — R3 D2: file queries used to be appended
    # AFTER this loop, so advise --queries-file evil.sql --analyze really executed
    # smuggled side effects. pg_stat_statements text (--from-statements below) is
    # the one exemption — and only on the non-analyze boundary (see the D-F2
    # guard inside the from_ss block): --analyze really executes, so the pulled
    # queries pass the same guard there.
    queries = [_single_query_arg(q, "--query") for q in query_args]
    if queries_file:
        queries += _split_sql_file(queries_file)
    for q in queries:
        _guard_analyze_query(q, "advise --query/--queries-file")
    dsn, _ = _resolve_target(instance_name, db, dsn)
    weights = None
    if from_ss:
        try:
            statements_mod.ensure_ready(dsn)
        except RuntimeError as e:
            raise click.ClickException(str(e)) from None
        conn = pg.connect(dsn)
        try:
            stats = statements_mod.top_queries(conn, sort="total", limit=from_ss, min_calls=1)
        finally:
            conn.close()
        queries = [s.query for s in stats] or queries
        weights = [float(s.calls) for s in stats] if stats else None
        # D-F2 (round 4): the pgss exemption holds only where nothing executes.
        # Pure EXPLAIN (no --analyze) plans the statement — boundary documented,
        # exemption kept (it is what already ran on the server). But --analyze
        # really executes it, and the no-$n-placeholder form is reachable from
        # normal business traffic ('select pg_terminate_backend(pid) from t
        # where pid = pid' really killed sessions): "already ran once" does not
        # authorize this tool to run it again. advise skips EXPLAIN entirely for
        # placeholder sets (has_placeholders), so the guard guards exactly the
        # set that would reach EXPLAIN ANALYZE.
        if stats and analyze and not advise_mod.has_placeholders(queries):
            for q in queries:
                _guard_analyze_query(q, "advise --from-statements")
    if not queries:
        raise click.ClickException("需要 --query / --queries-file / --from-statements 之一")
    try:
        result = advise_mod.evaluate(dsn, queries, weights=weights, analyze=analyze)
    except (RuntimeError, ValueError) as e:
        raise click.ClickException(str(e)) from None
    if fmt == "json":
        print(advise_mod.render_json(result))
    else:
        print(advise_mod.render_text(result))
    if result["placeholders"]:
        Console(stderr=True).print(
            "[yellow]查询带 $n 占位(pg_stat_statements 归一化),未做 EXPLAIN 评估;"
            "把 $n 落地为真实参数后再跑 hypo/planbench 验证[/yellow]")


@cli.command("diagnose")
@click.argument("query", required=False)
@click.option("--file", "qfile", type=click.Path(exists=True, dir_okay=False),
              help="查询文件(分号切分;多查询时按候选集一起评估)")
@click.option("--dsn", default=None, help="libpq 连接串(与 --instance 二选一)")
@click.option("--instance", "instance_name", default=None, help="pgtuner 实例名")
@click.option("--db", default=None, help="目标库——请在克隆库上跑(hypo 建索引期间表短暂加锁)")
@click.option("--analyze", is_flag=True,
              help="EXPLAIN ANALYZE(会执行语句;plan 级实际值,TPS 结论仍需 bench/rebench)")
@click.option("--label", default=None, help="账本 label(results 引用可写 diagnose:<label>)")
@click.option("--hypothesis", default=None, help="一句话假设(落账本,结论须对得上)")
@click.option("--verify", is_flag=True,
              help="验证闭环:基线实测→贪心真实建索引(至多 --verify-max-indexes 条)→复测→A/B diff"
                   "并自动回填结论,一次命令出全部账本证据(会真实建索引——只在克隆库上用;需 --instance)")
@click.option("--verify-runs", type=click.IntRange(min=1), default=5, show_default=True,
              help="--verify 每查询测量次数(取中位)")
@click.option("--verify-max-indexes", type=click.IntRange(min=1, max=6), default=3,
              show_default=True,
              help="--verify 贪心至多落地索引条数(多查询负载的索引权衡,一条命令做完)")
@click.option("--weights", "weights_spec", default=None,
              help="逗号分隔查询权重(建议用 pg_stat_statements 的 calls;与查询顺序一致,缺省全 1)")
@click.option("--release", is_flag=True,
              help="结束后归还目标库(lab release;克隆用完即还,证据已在账本)——配合 $() 捕获克隆使用")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text",
              show_default=True, help="json=stdout 纯 JSON(机器可读);text=终端表")
def diagnose_cmd(query: str | None, qfile: str | None, dsn: str | None,
                 instance_name: str | None, db: str | None, analyze: bool,
                 label: str | None, hypothesis: str | None, verify: bool,
                 verify_runs: int, verify_max_indexes: int, weights_spec: str | None,
                 release: bool, fmt: str) -> None:
    """一键慢查询诊断:基线 plan + 根因线索 + 假设索引候选排名 + (--verify)贪心建索引+前后实测 A/B。

    一个进程内完成 EXPLAIN→线索→hypo 估算对比→账本落档(v0.6:砍掉多命令往返);v0.6.1:--verify 升级贪心多索引(多查询权衡一次做完)+自动回填结论+--release 归还。只吃单条 SELECT/WITH;估算口径不背书性能,结论出口=处方命令/--verify 的账本证据。
    """
    queries = [_single_query_arg(query, "QUERY")] if query else []
    if qfile:
        queries += _split_sql_file(qfile)
    try:
        diagnose_mod._validate(queries)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    weights = _parse_weights(weights_spec, len(queries))
    dsn, inst_meta = _resolve_target(instance_name, db, dsn)
    instance_name = instance_name or (inst_meta or {}).get("name")   # backfill from the deployment-level default
    if verify and not instance_name:
        raise click.ClickException("--verify 需要 --instance(要在克隆库真实建索引与实测)")
    if verify and not db:
        # R2-D: same gate as planbench --variant — greedy real CREATE INDEX must
        # not default onto the shared postgres database
        raise click.ClickException(
            "--verify 需要 --db <克隆库>(贪心真实建索引+ANALYZE 会写目标库;"
            "先 pgtuner lab clone --template <tpl> 拿一次性库)")
    ensure_dirs()
    fp = _safe_fp(dsn, inst_meta)
    exp = ledger_mod.new_experiment(
        "diagnose", label or ("诊断:" + " ".join(queries[0].split())[:40]),
        hypothesis=hypothesis, instance=inst_meta,
        fingerprint=fp, payload={"queries": [q[:160] for q in queries],
                                 "dbname": db or pg.target(dsn)[2],
                                 **({"weights": weights} if weights else {})})
    qpath = Path(f"/tmp/pgtuner-diag-{exp.id}.sql")
    qpath.write_text(";\n".join(queries) + "\n", encoding="utf-8")
    Console(stderr=True).print("[yellow]提示[/yellow]:hypo 评估会在目标库真实建索引"
                               "(随即回滚,零残留),建索引期间表短暂加锁——请在克隆库上跑")
    try:
        try:
            result = diagnose_mod.run(dsn, queries, analyze=analyze, weights=weights,
                                      instance_name=instance_name, db=db, qfile=str(qpath))
        except (RuntimeError, ValueError) as e:
            raise click.ClickException(f"{e}(失败已落账本 {ledger_mod.citation(exp.id)})") from None

        verify_doc = None
        if verify:
            verify_doc = _diagnose_verify(exp, instance_name, db, queries, result, inst_meta,
                                          fp, verify_runs, max_indexes=verify_max_indexes,
                                          weights=weights)

        ledger_mod.update(exp.id, payload_merge={
            "facts": [{"query": pq["query"], **pq["facts"], "clues": pq["clues"]}
                      for pq in result["per_query"]],
            "ranking": result["ranking"], "best": result["best"],
            "mechanism": result["mechanism"], "recipe_file": str(qpath),
            **({"verify": verify_doc} if verify_doc else {})})

        auto_conclusion = None
        if verify_doc and "baseline_id" in verify_doc:
            auto_conclusion = _auto_conclude(exp, verify_doc, db)
            result["recipe"] = []            # the verify loop already produced A/B evidence; recipe lines are redundant (saves tokens on the data plane)
        if fmt == "json":
            if verify_doc:
                result["verify"] = verify_doc
            print(diagnose_mod.render_json(result))
        else:
            print(diagnose_mod.render_text(result))
            if verify_doc and "baseline_id" in verify_doc:
                n = len(verify_doc.get("indexes") or [])
                nd = len(verify_doc.get("dropped") or [])
                tail = f",实测回检剔除 {nd} 条" if nd else ""
                print(f"验证闭环(基线实测→贪心建 {n} 条索引{tail}→复测,证据=账本 A/B):")
                for c in verify_doc.get("indexes") or []:
                    print(f"  落地索引 {c['index'] or '(未知名)'}: {c['ddl']}"
                          f"(估算 {c['est_gain']:+.0%},实测回检 {c.get('measured_gain', 0):+.0%}"
                          "/查询均值;保留在克隆库=方案落地)")
                for c in verify_doc.get("dropped") or []:
                    print(f"  剔除索引 {c['ddl']}: {c['reason']}")
                print(f"  基线 {verify_doc['baseline_id']} → 建索引后 {verify_doc['after_id']}")
                for m in verify_doc.get("diff_metrics", []):
                    sig = "-" if m["significant"] is None else ("是" if m["significant"] else "不显著")
                    print(f"  {m['name']}: {m['a']:.3f} → {m['b']:.3f} ms(Δ {m['delta_pct']:+.2f}%,显著?{sig})")
                print(f"  复查: pgtuner results diff {verify_doc['baseline_id']} {verify_doc['after_id']}")
            elif verify_doc:
                print(f"验证闭环未执行: {verify_doc.get('skipped', verify_doc.get('error'))}")
            if auto_conclusion is not None:
                print(f"结论已自动回填 {ledger_mod.citation(exp.id)}(可 results show 查看全文)")
            else:
                print(f"已落账本 {ledger_mod.citation(exp.id)}"
                      "(结论回填: pgtuner results conclude " + exp.id + " \"...\")")
    finally:
        if release:
            _release_target(instance_name, db, exp.id)


def _parse_weights(spec: str | None, n_queries: int) -> list[float] | None:
    """--weights "150,100,25" → [150.0,100.0,25.0]; a count mismatch / non-numeric input gets a didactic error."""
    if not spec:
        return None
    try:
        w = [float(x) for x in spec.split(",")]
    except ValueError:
        raise click.ClickException(f"--weights 需逗号分隔数字,得到 {spec!r}") from None
    if len(w) != n_queries:
        raise click.ClickException(
            f"--weights 数量({len(w)})与查询数({n_queries})不一致,按查询顺序一一对应")
    return w


def _release_target(instance_name: str | None, db: str | None, exp_id: str) -> None:
    """--release: return the target clone (a non-clone db is refused by drop_db's
    owner guard — warn only, don't fail: the evidence is already in the ledger, and
    a cleanup failure must not overturn the experiment conclusion)."""
    if not (instance_name and db):
        Console(stderr=True).print("[yellow]--release 跳过[/yellow]:需要 --instance+--db 的克隆库")
        return
    try:
        instance_mod.drop_db(instance_name, db)
        print(f"已归还克隆 {db}(证据在账本 {exp_id};推荐索引 DDL 见上方,落地到目标库执行即可)")
    except Exception as e:                                           # noqa: BLE001
        Console(stderr=True).print(f"[yellow]--release 失败(不影响的证据): {e}[/yellow]")


def _auto_conclude(exp, verify_doc: dict, db: str | None) -> str:
    """Auto-backfill the conclusion after the verify loop (iron rule 3 style: per-metric A/B ids with significance, tool-generated and marked as such)."""
    parts = []
    for m in verify_doc.get("diff_metrics") or []:
        sig = "显著" if m["significant"] else ("不显著" if m["significant"] is not None else "口径外")
        parts.append(f"{m['name']} {m['a']:.3f}→{m['b']:.3f}ms"
                     f"({m['delta_pct']:+.1f}%,{sig})")
    idx = verify_doc.get("indexes") or []
    drp = verify_doc.get("dropped") or []
    text = (f"[diagnose --verify 自动结论] {db or 'postgres'} 落地 {len(idx)} 条索引("
            + "; ".join(c["ddl"] for c in idx) + "):"
            + (";".join(parts) if parts else "无公共指标")
            + (f";实测回检剔除 {len(drp)} 条(" + ";".join(d["ddl"] for d in drp)
               + ",估算收益为口径偏差)" if drp else "")
            + f"。证据 {verify_doc['baseline_id']}(基线) vs {verify_doc['after_id']}(建索引后),"
              "显著性按账本噪声底口径;TPS/并发结论仍需 bench/rebench。")
    ledger_mod.update(exp.id, conclusion=text)
    return text


def _diagnose_verify(exp, instance_name: str, db: str | None, queries: list[str],
                     result: dict, inst_meta: dict | None, fp: dict | None,
                     verify_runs: int, max_indexes: int = 3,
                     weights: list[float] | None = None) -> dict:
    """Inner loop of diagnose --verify: measure-only baseline → greedy real index
    creation → re-measure → diff (each ledgered).

    Greedy (v0.6.1, multi-query index trade-off in one command): each round re-runs
    hypo evaluation on the **remaining candidates** — already-landed real indexes
    are visible to the evaluation, so each round's ratio is a "marginal increment";
    land the candidate with the highest weighted gain (Σ w·(1-ratio)/query count,
    same basis as advise), stop when the weighted mean gain < 5% (marginal, not
    significant — an honest basis), at most max_indexes indexes.

    Measured re-check (v0.6.3, S4-A3 lesson: a candidate estimated at +16% measured
    zero gain and the agent spent 12 calls pruning it by hand): right after landing
    one, run a light measurement; a weighted mean reduction < 5% counts as
    estimation basis bias — **DROP it on the spot and record it in dropped** — so
    every index in the final kept set is backed by a measurement and agents never
    hand-prune indexes again.

    Attribution in a ledger shared by parallel agents is costly (v0.6 S1 first
    wave: A1 spent 7 calls locating its own entry) → both rebench entries' labels
    carry the db name and the diagnose experiment id; intermediate re-checks are
    not ledgered (payload only).
    """
    if not result["ranking"]:
        return {"skipped": "无估算收益为正的候选,无可验证索引"}
    _MIN_GAIN = 0.05                                    # stop line for the weighted mean estimated reduction
    _MIN_MEASURED = 0.05                                # measured re-check: below this = estimation bias, roll back
    w = weights or [1.0] * len(queries)
    target_db = db or "postgres"

    def _run_measure():
        """Run one measure-only pass (not ledgered); returns (rebench results, weighted mean ms)."""
        res = rebench_mod.run_restart_bench(
            instance_name, [], dbname=target_db, workload="custom",
            custom_queries=queries, runs=verify_runs, measure_only=True)
        per_q: dict[int, float] = {}
        for r in res:
            if r.payload and "queries" in r.payload:
                for qi, q in enumerate(r.payload["queries"]):
                    if q.get("execution_ms") is not None:
                        per_q[qi] = q["execution_ms"]
        return res, diagnose_mod.weighted_ms([per_q.get(i) for i in range(len(queries))], w)

    def _measure(tag: str):
        vb = ledger_mod.new_experiment(
            "rebench", f"diagnose{tag}:{target_db}({exp.id})", hypothesis=exp.hypothesis,
            instance=inst_meta, fingerprint=fp,
            payload={"workload": "custom", "dbname": target_db, "runs": verify_runs,
                     "measure_only": True, "diagnose_id": exp.id, "gucs": [],
                     "duration": 0, "warmup": 0})
        try:
            res, wmean = _run_measure()
        except Exception as e:                                     # noqa: BLE001
            ledger_mod.update(vb.id, payload_merge={"failed": True,
                                                    "error": f"{type(e).__name__}: {e}"[:300]})
            raise
        metrics, cycles = {}, []
        for r in res:
            if r.tps is None:
                continue
            if r.payload and "queries" in r.payload:
                for qi, q in enumerate(r.payload["queries"]):
                    if q.get("execution_ms") is not None:
                        metrics[f"{r.candidate.name}|Q{qi}_ms"] = q["execution_ms"]
            cycles.append({"name": r.candidate.name, "gucs": r.candidate.gucs, "tps": r.tps,
                           "error": r.error})
        ledger_mod.update(vb.id, payload_merge={"metrics": metrics, "cycles": cycles,
                                                "restored": True})
        return vb.id, wmean

    base_id, prev_wmean = _measure("基线")
    dsn = instance_mod.dsn_of(instance_name, target_db)
    remaining = [r["ddl"] for r in result["ranking"] if not r["error"] and (r["benefit"] or 0) > 0]
    chosen: list[dict] = []
    dropped: list[dict] = []
    conn = pg.connect_rw(dsn)
    try:
        with conn.cursor() as cur:
            attempts = 0
            while remaining and len(chosen) < max_indexes and attempts < max_indexes + 3:
                attempts += 1
                rep = hypo_mod.eval_candidates(dsn, remaining, queries, analyze=False)
                best_i, best_gain = None, 0.0
                for i, (se, ddl) in enumerate(zip(rep["sets"][:len(remaining)], remaining)):
                    if se.error or se.avg_ratio is None:
                        continue
                    gain = diagnose_mod.weighted_gain(se.queries, w)
                    if gain > best_gain:
                        best_i, best_gain = i, gain
                if best_i is None or best_gain < _MIN_GAIN:
                    break                                     # marginal gain not significant, stop
                ddl = remaining.pop(best_i)
                parsed = benchmark_mod.parse_index_ddl(ddl)     # used to diff pg_indexes before/after index creation
                tbl = parsed[0] if parsed else None
                added_name = None
                if tbl:
                    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", (tbl,))
                    before = {r[0] for r in cur.fetchall()}
                cur.execute(ddl)
                conn.commit()
                if tbl:
                    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = %s", (tbl,))
                    added = sorted({r[0] for r in cur.fetchall()} - before)
                    added_name = added[0] if added else None
                # Measured re-check: for candidates that passed the estimate filter,
                # a measured weighted mean reduction < 5% → DROP on the spot (estimation basis bias)
                _, wmean = _run_measure()
                measured = (1.0 - wmean / prev_wmean) if prev_wmean > 0 else 0.0
                if measured < _MIN_MEASURED:
                    if added_name:
                        cur.execute(f'DROP INDEX IF EXISTS "{added_name}"')
                        conn.commit()
                    dropped.append({"ddl": ddl, "index": added_name,
                                    "est_gain": round(best_gain, 4),
                                    "measured_gain": round(measured, 4),
                                    "reason": f"实测回检:加权均值降幅 {measured:+.1%} < "
                                              f"{int(_MIN_MEASURED * 100)}%(估算 {best_gain:+.1%} 为口径偏差),已回滚"})
                    continue
                chosen.append({"ddl": ddl, "index": added_name,
                               "est_gain": round(best_gain, 4),
                               "measured_gain": round(measured, 4)})
                prev_wmean = wmean
    finally:
        conn.close()
    if not chosen:
        return {"skipped": f"贪心未落地任何索引:候选估算收益不足或实测回检全部回滚"
                           f"(诚实口径;剔除 {len(dropped)} 条)", "baseline_id": base_id,
                "dropped": dropped}
    after_id, _ = _measure("后测")
    d = ledger_mod.diff(base_id, after_id)
    return {"baseline_id": base_id, "after_id": after_id, "indexes": chosen,
            "dropped": dropped,
            "ddl": chosen[0]["ddl"], "index_created": chosen[0].get("index"),
            "diff_metrics": d.get("metrics", []),
            "verdict": d.get("verdict"), "warnings": d.get("warnings", [])}


@cli.command("selftest")
@click.option("--with-db", is_flag=True,
              help="附加 DB 门槛(release 安装目录,PGTUNER_RELEASE_BINDIR 可覆盖):克隆池压力/watch/慢通道还原")
@click.option("--cycles", type=click.IntRange(min=1), default=100, show_default=True,
              help="克隆池取还循环次数")
def selftest_cmd(with_db: bool, cycles: int) -> None:
    """验收门槛集合:fast 无 PG;--with-db 走完整链路。"""
    gates = selftest_mod.run(with_db=with_db, cycles=cycles)
    t = report.Table(box=report.box.SIMPLE_HEAVY, title="selftest")
    t.add_column("gate", style="bold cyan")
    t.add_column("结果", justify="center")
    t.add_column("详情", max_width=60)
    for name, status, detail in gates:
        t.add_row(name, f"[green]{status}[/green]" if status == "PASS" else f"[red]{status}[/red]", detail)
    console = Console()
    console.print(t)
    fails = [g for g in gates if g[1] == "FAIL"]
    console.print(f"[{'green' if not fails else 'red'}]{len(gates) - len(fails)}/{len(gates)} 通过[/{'green' if not fails else 'red'}]")
    if fails:
        raise click.ClickException(f"{len(fails)} 个门槛未通过")


# ------------------- doctor: environment pre-flight (v0.9.0, the first command on a new machine / for a new agent) -------------------
@cli.command("doctor")
@click.option("--instance", "instance_name", default=None,
              help="体检指定实例(缺省默认实例:config set / PGTUNER_INSTANCE)")
def doctor_cmd(instance_name: str | None) -> None:
    """环境体检(只读零副作用)：依赖/实例注册表/账本/默认实例/模板/pgss/pgbench——
    ❌=阻断(exit 1),⚠️=降级可用,修复命令见"修复"列(scripts/setup.sh / lab bootstrap)。"""
    if instance_name:
        try:
            instance_mod.get(instance_name)   # explicitly named but missing: fail early instead of degrading to skip
        except ValueError as e:
            raise click.ClickException(str(e)) from None
    try:
        rows = doctor_mod.run(instance_name)
    except (ValueError, RuntimeError) as e:
        raise click.ClickException(str(e)) from None
    icons = {"ok": "[green]✅[/green]", "warn": "[yellow]⚠️ [/yellow]",
             "fail": "[red]❌[/red]", "skip": "[dim]—[/dim]"}
    t = report.Table(box=report.box.SIMPLE_HEAVY, title="pgtuner doctor")
    t.add_column("检查", style="bold cyan")
    t.add_column("状态", justify="center")
    t.add_column("详情", max_width=52)
    t.add_column("修复", max_width=52)
    for r in rows:
        t.add_row(r["name"], icons[r["level"]], r["detail"], r["fix"] or "—")
    console = _wide_console()      # agent/CI read doctor piped: 80 cols truncates the fix column mid-word
    console.print(t)
    fails = [r for r in rows if r["level"] == "fail"]
    warns = [r for r in rows if r["level"] == "warn"]
    n_ok = len(rows) - len(fails) - len(warns)
    console.print(f"[{'red' if fails else 'green'}]{n_ok}✅ / {len(warns)}⚠️ / {len(fails)}❌[/{'red' if fails else 'green'}]")
    if fails:
        raise click.ClickException(f"{len(fails)} 项阻断(❌)——按修复列补齐后再开工")


# ------------------- init: distribution layer (v0.10.0, enable pgtuner in any project / user-globally) -------------------
@cli.command("init")
@click.argument("target_dir", default=".")
@click.option("--global", "do_global", is_flag=True,
              help="用户级:装 skills 到 ~/.zcode / ~/.claude / ~/.codex,并写入规则标记块与 PATH 接线(对所有项目生效)")
@click.option("--force", is_flag=True, help="覆盖已存在且内容不同的 skill / AGENTS 标记块")
def init_cmd(target_dir: str, do_global: bool, force: bool) -> None:
    """把 pgtuner 装进任意项目：AGENTS.md / CLAUDE.md / skills×2 / .gitignore / 状态骨架。

    幂等,重跑只补缺。落完后该项目 agent 即有 pgtuner 知识,实验账本/实例注册表落
    <该项目>/.pgtuner(状态锚点随项目,与工具仓不串账);在该目录跑 pgtuner doctor 复检。"""
    try:
        if do_global:
            result = projinit_mod.init_global(force=force)
        else:
            result = projinit_mod.init(target_dir, force=force)
    except (ValueError, OSError) as e:
        raise click.ClickException(str(e)) from None
    _print_steps("pgtuner init — 用户级(skills+规则标记块+PATH 接线)" if do_global else
                 f"pgtuner init — {result.get('target', '')}", result["steps"])
    if not do_global:
        Console().print(f"[green]就绪[/green]: 该项目 agent 即有 pgtuner 知识;"
                        f"账本/注册表落 {result['target']}/.pgtuner;复检: cd {result['target']} && pgtuner doctor")


# ------------------- benchmark: standard benchmark task suite (v0.7.0, English-only mission packets) -------------------
# Evaluation asset: mission packets contain red-herring designs and reference
# answers, handed to the agent under test only via setup; do not paste task
# content into documents the tested party can read (AGENTS.md / the toolset
# manual) (S4 contamination lesson).

@cli.group("benchmark", hidden=True)   # operator tool kept out of help: a tested agent once trolled the top-level help into
def benchmark_grp() -> None:          # show --plan (grading plan/reference answers) — leaking answers and burning turns (observed)
    """Standard benchmark suite for PG operation agents (English mission packets).

    Operator loop: list → show <task> [--variant cli|psql] [--plan] →
    setup <task> (clone + materials + ledger stamp + full mission packet with
    real values) → grade <task> (scorecard; conclusion written back to the stamp).
    """


@benchmark_grp.command("list")
def benchmark_list() -> None:
    """List available benchmark tasks."""
    from . import render
    rows = [[t.id, t.difficulty, t.template, t.summary] for t in benchmark_mod.TASKS.values()]
    print(render.align_table(["task", "difficulty", "template", "summary"], rows))


@benchmark_grp.command("show")
@click.argument("task_id")
@click.option("--variant", type=click.Choice(["cli", "psql"]), default="cli", show_default=True,
              help="Which toolset paragraph the mission packet carries (cli/psql symmetric)")
@click.option("--plan", "show_plan", is_flag=True,
              help="Also print the grading plan + reference answer (operator only; never hand to the agent under test)")
def benchmark_show(task_id: str, variant: str, show_plan: bool) -> None:
    """Print the mission packet (with <placeholders>; `setup` prints it with real values)."""
    try:
        task = benchmark_mod.get_task(task_id)
    except ValueError as e:
        raise click.ClickException(str(e))
    print(benchmark_mod.mission_text(task, variant))
    if show_plan:
        print()
        print("--- grading plan (operator only) ---")
        print(benchmark_mod.plan_text(task))


def _xbuild_peer(instance_name: str, task, console):
    """xbuild pre-step: ensure the peer release instance is running + templates
    exist on both sides (materialized on demand from deterministic setup SQL).

    Returns the peer instance object (the main instance's template is also ensured
    here — the generic clone depends on it being in place)."""
    peer_name = benchmark_mod.XBUILD_PEER_INSTANCE
    try:
        peer = instance_mod.ensure_running(peer_name)
    except Exception as e:                                           # noqa: BLE001
        raise click.ClickException(
            f"xbuild-diff needs peer instance {peer_name!r}: {e}") from None
    if peer.build_type != "release":
        raise click.ClickException(f"xbuild-diff peer {peer_name!r} is {peer.build_type}, "
                                   "not release (iron rule 5)")
    setup_sql = REPO_ROOT / "benchmarks" / "b2_setup.sql"
    for iname in (instance_name, peer_name):
        conn = pg.connect_rw(instance_mod.dsn_of(iname, "postgres"))
        try:
            has = pg.fetch_one(conn, "SELECT 1 FROM pg_database WHERE datname = %s",
                               (task.template,))
        finally:
            conn.close()
        if not has:
            console.print(f"materializing template {task.template} on {iname} "
                          "(deterministic setup SQL, ~200k rows)…")
            instance_mod.make_template(iname, task.template, str(setup_sql))
    return peer


def _stage_lock_incident(instance_name: str, task, db: str, run_dir, console) -> dict:
    """lock-incident staging: an innocent bystander db (registered under someone
    else's ownership) + a resident script (holder/victim sessions).

    The script writes the three backend pids to staged.json; we read them back
    into RunState.staged (the attribution anchor). Staging files
    (holder.py/staged.json) go into the side directory <run_id>.staging/ — not
    into the handed-out run_dir: the task demands attribution from live database
    state, and the `cat staged.json` free-answer loophole stays closed (S4
    contamination lesson). Staging failures are not silent: return the part
    already obtained; the corresponding grade checks then naturally SKIP/FAIL."""
    staging_dir = run_dir.parent / f"{run_dir.name}.staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    decoy_db = instance_mod.clone(instance_name, task.template, owner="app-team-batch")
    holder = staging_dir / "holder.py"
    holder.write_text(benchmark_mod.holder_py_text(db, decoy_db), encoding="utf-8")
    staged_json = staging_dir / "staged.json"
    port = instance_mod.get(instance_name).port
    proc = subprocess.Popen(
        [sys.executable, str(holder), str(port), db, decoy_db, str(staged_json)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):                       # staging ready (backend pids on disk) within ≤10s
        if staged_json.exists():
            break
        time.sleep(0.2)
    if not staged_json.exists():
        console.print(f"[yellow]warning[/yellow]: staging not ready in 10s "
                      f"(holder launcher pid {proc.pid}); run with the incident un-staged")
        return {"decoy_db": decoy_db}
    staged = json.loads(staged_json.read_text(encoding="utf-8"))
    staged["decoy_db"] = decoy_db
    return staged


@benchmark_grp.command("setup")
@click.argument("task_id")
@click.option("--instance", "instance_name", default=None,
              help="Defaults to config default-instance / PGTUNER_INSTANCE")
@click.option("--variant", type=click.Choice(["cli", "psql"]), default="cli", show_default=True,
              help="Recorded on the run stamp; also selects the printed mission packet")
def benchmark_setup(task_id: str, instance_name: str | None, variant: str) -> None:
    """Prepare a benchmark run: clone the template, write task materials into a run dir,
    stamp the ledger, and print the full mission packet with concrete values.

    Hand the printed packet verbatim to the agent under test. 评分入口:
    pgtuner benchmark grade <task_id>(缺省评最近一次 run)。"""
    console = Console()
    try:
        task = benchmark_mod.get_task(task_id)
    except ValueError as e:
        raise click.ClickException(str(e))
    instance_name = instance_name or config_mod.default_instance()
    if not instance_name:
        raise click.ClickException(
            "missing instance: --instance <name>, or first run "
            "pgtuner config set default-instance <instance>")
    inst = instance_mod.ensure_running(instance_name)
    if inst.build_type != "release":
        console.print("[yellow]warning[/yellow]: instance is a debug build — "
                      "performance numbers graded on it cannot be cited (iron rule 5)")
    # Staging pre-step (xbuild): templates on both sides (materialized from
    # deterministic setup SQL if missing); the main clone depends on them being in place
    peer = None
    if task.stage == "xbuild":
        peer = _xbuild_peer(instance_name, task, console)
    # Zero-handout tasks (clone_on_setup=False) get no proxy clone: cloning it
    # yourself is a task step (S5-A1 lesson — a harness-made clone becomes
    # residue); for fed tasks the clone is the {db} in the mission packet
    db = instance_mod.clone(instance_name, task.template) if task.clone_on_setup else None
    run_id = benchmark_mod.new_run_id(task.id)
    run_dir = benchmark_mod.RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    materials = []
    if task.queries:
        qf = run_dir / "queries.sql"
        qf.write_text(";\n\n".join(task.queries) + ";\n", encoding="utf-8")
        materials.append(qf)
    if task.writes_workload:
        wf = run_dir / "workload.py"
        wf.write_text(benchmark_mod.workload_py_text(inst.port), encoding="utf-8")
        materials.append(wf)
    for name, content in (task.extra_files or {}).items():
        pf = run_dir / name
        pf.write_text(content, encoding="utf-8")
        materials.append(pf)
    # Staging post-step: incident sessions (lock-incident) / peer clone + statistics (xbuild)
    staged: dict = {}
    if task.stage == "lock-incident" and db:
        staged = _stage_lock_incident(instance_name, task, db, run_dir, console)
    if task.stage == "xbuild" and db and peer is not None:
        peer_db = instance_mod.clone(peer.name, task.template)
        for iname, dbname in ((instance_name, db), (peer.name, peer_db)):
            conn = pg.connect_rw(instance_mod.dsn_of(iname, dbname))
            try:
                pg.execute(conn, "ANALYZE")   # single-statement autocommit: statistics ready, timing comparable across builds
            finally:
                conn.close()
        staged = {"peer_instance": peer.name, "peer_db": peer_db, "peer_port": peer.port}
        try:
            instance_mod.get("dbg20")         # the instance behind the release-only trap (if absent, degrade only — not blocking)
        except Exception:                                          # noqa: BLE001
            console.print("[yellow]warning[/yellow]: no debug instance registered — "
                          "release-only discipline is still checked, but the trap is inert")
    # Hygiene snapshot: the set of connectable business dbs at setup end
    # (zero-handout grading requires current ⊆ snapshot)
    dbs_at_setup: list[str] = []
    try:
        conn = pg.connect_rw(instance_mod.dsn_of(instance_name, "postgres"))
        try:
            dbs_at_setup = [r[0] for r in pg.fetch_all(conn, benchmark_mod.DB_SNAPSHOT_SQL)]
        finally:
            conn.close()
    except Exception as e:                                          # noqa: BLE001
        console.print(f"[yellow]warning[/yellow]: snapshot query failed ({e})")
    inst_meta = _inst_meta(inst)
    exp = ledger_mod.new_experiment(
        "benchmark", f"benchmark:{task.id}", instance=inst_meta,
        payload={"task": task.id, "variant": variant, "db": db,
                 "run_dir": str(run_dir), "template": task.template,
                 "dbs_at_setup": dbs_at_setup})
    state = benchmark_mod.RunState(run_id=run_id, task=task.id, instance=inst.name, db=db,
                                   template=task.template, variant=variant,
                                   ledger_exp=exp.id, run_dir=str(run_dir),
                                   dbs_at_setup=dbs_at_setup, staged=staged)
    benchmark_mod.save_run(state)
    print(f"run-id: {run_id}")
    print(f"instance: {inst.name} ({inst.build_type})   db: {db or '(zero-handout: agent clones its own)'}")
    print(f"run-dir: {run_dir}")
    for f in materials:
        print(f"material: {f}")
    print(f"ledger stamp: {ledger_mod.citation(exp.id)}")
    print()
    print(benchmark_mod.mission_text(task, variant, port=inst.port, run_dir=run_dir, db=db,
                                     instance=inst.name, **{
                                         k: v for k, v in staged.items()
                                         if k in ("peer_port", "peer_instance", "peer_db")}))


@benchmark_grp.command("grade")
@click.argument("task_id")
@click.option("--run", "run_id", default=None, help="Run id (default: latest run of this task)")
@click.option("--report", "report_file", type=click.Path(exists=True, dir_okay=False), default=None,
              help="Agent's final report file; CREATE INDEX lines in it count as landed DDLs")
def benchmark_grade(task_id: str, run_id: str | None, report_file: str | None) -> None:
    """Grade a run: scorecard (hygiene/evidence + profile-specific checks), conclusion
    written back to the stamped ledger entry. Exit code 1 if any check FAILs."""
    try:
        task = benchmark_mod.get_task(task_id)
    except ValueError as e:
        raise click.ClickException(str(e))
    try:
        run = benchmark_mod.load_run(run_id) if run_id else benchmark_mod.latest_run(task.id)
    except ValueError as e:
        raise click.ClickException(str(e))
    if run is None:
        raise click.ClickException(f"no run found for task {task.id}; start one: "
                                   f"pgtuner benchmark setup {task.id}")
    if run.task != task.id:
        raise click.ClickException(f"run {run.run_id} belongs to task {run.task}, not {task.id}")
    # Ledger window: every row after the stamping experiment (the ledger is
    # append-ordered; the stamp row itself is not evidence). The window is
    # **frozen on first grade** (grade_window stored on the stamp experiment): if
    # a later re-grade still extended to the ledger tail it would be polluted by
    # unrelated runs' experiments (evidence/leanness misjudged — hit in S8
    # testing); re-grades always reuse the frozen window.
    exps = ledger_mod.list_experiments()
    frozen = (ledger_mod.get(run.ledger_exp).payload or {}).get("grade_window")
    if frozen:
        by_id = {e.id: e for e in exps}
        exps_after = [by_id[i] for i in frozen if i in by_id]
    else:
        try:
            start = next(i for i, e in enumerate(exps) if e.id == run.ledger_exp)
        except StopIteration:
            raise click.ClickException(
                f"run stamp {run.ledger_exp} not found in ledger") from None
        exps_after = exps[start + 1:]
        ledger_mod.update(run.ledger_exp,
                          payload_merge={"grade_window": [e.id for e in exps_after]})
    # Live probing: the current list of connectable business dbs (for snapshot
    # hygiene); the fed run db additionally gets an existence check, and when
    # present its non-primary-key index definitions are grabbed along the way
    # (one source of landed-artifact evidence)
    db_state, db_indexes, current_dbs = "unknown", None, None
    try:
        conn = pg.connect_rw(instance_mod.dsn_of(run.instance, "postgres"))
        try:
            current_dbs = [r[0] for r in pg.fetch_all(conn, benchmark_mod.DB_SNAPSHOT_SQL)]
            if run.db is not None:
                exists = pg.fetch_one(
                    conn, "SELECT 1 FROM pg_database WHERE datname = %s", (run.db,))
                db_state = "gone" if not exists else "present"
        finally:
            conn.close()
        if db_state == "present":
            conn = pg.connect_rw(instance_mod.dsn_of(run.instance, run.db))
            try:
                db_indexes = [r[0] for r in pg.fetch_all(conn,
                    "SELECT i.indexdef FROM pg_indexes i "
                    "JOIN pg_index g ON g.indexrelid = (i.schemaname || '.' || i.indexname)::regclass "
                    "WHERE i.schemaname = 'public' AND NOT g.indisprimary")]
            finally:
                conn.close()
    except Exception as e:                                          # noqa: BLE001
        Console(stderr=True).print(
            f"[yellow]instance unreachable ({e}) — grading continues, hygiene unverifiable[/yellow]")
    # Staging forensics: the incident profile takes the clone-pool audit +
    # bystander db existence; xbuild takes peer clone existence
    audit_rows = decoy_state = peer_db_state = None
    try:
        if task.profile == "incident":
            rows = instance_mod.audit_tail(instance_name=run.instance, limit=300)
            recent = [r for r in rows if str(r.get("ts", "")) >= run.started]
            audit_rows = recent or rows
            decoy_db = (run.staged or {}).get("decoy_db")
            if decoy_db:
                conn = pg.connect_rw(instance_mod.dsn_of(run.instance, "postgres"))
                try:
                    decoy_state = "present" if pg.fetch_one(
                        conn, "SELECT 1 FROM pg_database WHERE datname = %s", (decoy_db,)) else "gone"
                finally:
                    conn.close()
        if task.profile == "xbuild":
            peer = run.staged or {}
            if peer.get("peer_db"):
                conn = pg.connect_rw(instance_mod.dsn_of(peer["peer_instance"], "postgres"))
                try:
                    peer_db_state = "present" if pg.fetch_one(
                        conn, "SELECT 1 FROM pg_database WHERE datname = %s",
                        (peer["peer_db"],)) else "gone"
                finally:
                    conn.close()
    except Exception as e:                                          # noqa: BLE001
        Console(stderr=True).print(
            f"[yellow]staging facts unreadable ({e}) — profile-specific checks may skip[/yellow]")
    report_text = Path(report_file).read_text(encoding="utf-8") if report_file else None
    checks = benchmark_mod.grade_run(task, run, exps_after, db_state=db_state,
                                     db_indexes=db_indexes, report_text=report_text,
                                     current_dbs=current_dbs, audit_rows=audit_rows,
                                     decoy_state=decoy_state, peer_db_state=peer_db_state)
    print(f"run {run.run_id} (task {task.id}, variant {run.variant}, stamp {run.ledger_exp})")
    print(benchmark_mod.scorecard(checks))
    judged = [c for c in checks if c.passed is not None]
    passed = sum(1 for c in judged if c.passed)
    summary = (f"[benchmark grade] task {task.id} run {run.run_id}: "
               f"{passed}/{len(judged)} checks passed — "
               + "; ".join(f"{c.name}={c.verdict}" for c in checks))
    ledger_mod.update(run.ledger_exp, conclusion=summary,
                      payload_merge={"grade": [{"name": c.name, "verdict": c.verdict,
                                                "detail": c.detail} for c in checks]})
    print(f"grade written back to {ledger_mod.citation(run.ledger_exp)}")
    if any(c.passed is False for c in checks):
        sys.exit(1)
