"""Report output: rich tables + postgresql.conf snippet generation."""
from __future__ import annotations

from datetime import date

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from . import __version__
from .engine import Recommendation, to_kb
from .hardware import HardwareInfo
from .inputs import is_extrapolated


def fmt_num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return repr(v) if isinstance(v, str) else str(v)


def _human_kb(kb: float) -> str:
    if kb < 1024:
        return f"{fmt_num(kb)}kB"
    if kb < 1024 * 1024:
        return f"{kb / 1024:g}MB"
    return f"{kb / 1024 / 1024:g}GB"


def humanize(value, unit: str) -> str:
    """Recommended value → human-readable unit (for display)."""
    if unit == "kB":
        return _human_kb(float(value))
    if unit == "pages_8k":
        return f"{_human_kb(float(value) * 8)}（{fmt_num(value)}×8kB页）"
    if unit == "MB":
        return f"{value:g}MB" if value < 1024 else f"{value / 1024:g}GB"
    if unit == "sec":
        v = int(value)
        if v % 3600 == 0:
            return f"{v // 3600}h"
        if v % 60 == 0:
            return f"{v // 60}min"
        return f"{v}s"
    if unit == "ms":
        return f"{fmt_num(value)}ms"
    if unit == "us":
        return f"{fmt_num(value)}us"
    return fmt_num(value)   # none: numeric/string enum as-is


def _conf_kb(kb: int) -> str:
    if kb % (1024 * 1024) == 0:
        return f"{kb // 1024 // 1024}GB"
    if kb % 1024 == 0:
        return f"{kb // 1024}MB"
    return f"{kb}kB"


def conf_value(value, unit: str) -> str:
    """Recommended value → value literal usable in postgresql.conf (PG syntax units)."""
    if isinstance(value, str):
        return f"'{value}'"
    if unit == "kB":
        return _conf_kb(int(value))
    if unit == "pages_8k":
        # PG conf does not accept "page counts"; converted to a kB-unit spelling (raw page count is in the trailing comment)
        return _conf_kb(int(to_kb(value, unit)))
    if unit == "MB":
        v = int(value)
        return f"{v // 1024}GB" if v % 1024 == 0 else f"{v}MB"
    if unit == "sec":
        v = int(value)
        if v % 3600 == 0:
            return f"{v // 3600}h"
        if v % 60 == 0:
            return f"{v // 60}min"
        return f"{v}s"
    if unit == "ms":
        return f"{int(value)}ms"
    if unit == "us":
        return f"{int(value)}us"
    return fmt_num(value)


def _origin_short(origin: str, limit: int = 28) -> str:
    return origin.split("（")[0][:limit]


def render_table(recs: list[Recommendation], *, current_values: dict | None = None,
                 all_origins: bool = False) -> Table:
    """GUC | recommended value | current value (optional) | alternatives (value + origin) | restart | pain | formula origin.
    The current-value column is omitted when there is no DB connection (current_values=None)."""
    table = Table(box=box.SIMPLE_HEAVY, title="pgtuner 推荐结果")
    table.add_column("GUC", style="bold cyan", no_wrap=True)
    table.add_column("推荐值", style="green")
    if current_values is not None:
        table.add_column("当前值")
    table.add_column("备选（值+出处）")
    table.add_column("重启", justify="center")
    table.add_column("痛点", max_width=38)
    table.add_column("公式出处", max_width=34)

    for rec in recs:
        p = rec.primary
        if p is not None:
            val = humanize(p.value, p.unit) + (f"\n[dim]{p.note}[/dim]" if p.note else "")
            origin = _origin_short(p.origin)
        else:
            first = rec.results[0] if rec.results else None
            why = first.reason if first else ""
            val = f"[dim]保持默认（{why}）[/dim]"
            origin = ""
        alts = []
        for r in rec.results:
            if r.status == "ok" and r is not p:
                alts.append(f"{humanize(r.value, r.unit)} ({_origin_short(r.origin)})")
            elif all_origins and r.status != "ok":
                alts.append(f"[dim]跳过[{r.index}]: {r.reason}[/dim]")
        shown = alts if all_origins else alts[:2]
        extra = f"\n[dim]+{len(alts) - 2} 更多（--all-origins 展开）[/dim]" if not all_origins and len(alts) > 2 else ""
        alt_cell = "\n".join(shown) + extra if shown else "[dim]—[/dim]"
        row = [rec.name, val]
        if current_values is not None:
            row.append(str(current_values.get(rec.name, "?")))
        row += [alt_cell,
                "是" if rec.restart_required else "否",
                (rec.why_hard[:60] + "…") if len(rec.why_hard) > 60 else rec.why_hard,
                origin]
        table.add_row(*row)
    return table


def render_conf(recs: list[Recommendation], inputs: dict, offline: bool = False) -> str:
    """conf snippet: `# pgtuner generated` header + restart-required grouping (comments mark pain_level and origin).

    When offline=True (recommend offline mode) the header notes it is unvalidated against the
    target instance; the check connected-mode output has already passed pg_settings
    post-validation (parameter existence / legal enum values + min_wal_size consistency)."""
    lines = [
        f"# pgtuner generated {date.today().isoformat()} (pgtuner {__version__})",
        "# 输入: " + " ".join(f"{k}={v}" for k, v in sorted(inputs.items())),
    ]
    if is_extrapolated(inputs):
        lines.append("# 注意: pg_version 超出知识库覆盖范围（13-18），公式按最新分支外推求值")
    if offline:
        lines.append("# 注意: 离线推荐未经目标实例校验，应用前建议跑 check -o")
    emitted = [r for r in recs if r.primary is not None]
    for label, restart in (("需重启（postmaster）", True), ("无需重启（reload/会话级）", False)):
        group = [r for r in emitted if r.restart_required is restart]
        if not group:
            continue
        lines.append("")
        lines.append(f"# --- {label} ---")
        for rec in group:
            p = rec.primary
            pain = rec.pain_level or "n/a"
            lines.append(f"# pain: {pain} | 出处: {_origin_short(p.origin, 48)}")
            if p.note:
                lines.append(f"# {p.note}")
            if p.unit == "pages_8k":
                lines.append(f"# 原公式输出 {p.value}×8kB 页，已换算")
            lines.append(f"{rec.name} = {conf_value(p.value, p.unit)}")
    skipped = [r.name for r in recs if r.primary is None]
    if skipped:
        lines.append("")
        lines.append("# 未输出（保持默认）: " + ", ".join(skipped))
    return "\n".join(lines) + "\n"


def render_hw_panel(hw: HardwareInfo) -> Panel:
    body = Table(box=None, show_header=False)
    body.add_column(style="bold")
    body.add_column()
    body.add_row("total_ram_gb", str(hw.total_ram_gb))
    body.add_row("cpu_cores", str(hw.cpu_cores))
    body.add_row("os_type", hw.os_type + ("（WSL2）" if hw.is_wsl2 else ""))
    body.add_row("disk_type", f"{hw.disk_type}（置信度 {hw.disk_confidence}）")
    body.add_row("bench", f"{hw.bench_iops:.0f} IOPS @ {hw.bench_path}" if hw.bench_iops
                 else f"微基准失败: {hw.bench_error}")
    for note in hw.disk_notes:
        body.add_row("·", f"[dim]{note}[/dim]")
    return Panel(body, title="硬件探测结果", expand=False)


def render_probes_table(probes: list) -> Table:
    """Shell command strings from the knowledge base's hardware_probes — reference display only, never executed (contains <pgdata> placeholders and Chinese annotations)."""
    table = Table(box=box.SIMPLE, title="知识库硬件探测命令参考（仅供参考，本工具不执行）")
    table.add_column("探测项", style="bold")
    table.add_column("说明", max_width=44)
    table.add_column("命令（截断）", max_width=72)
    for p in probes:
        for r in p.rules:
            cmd = r.get("formula", "")
            table.add_row(p.name.replace("__probe__", "").replace("__", "") or p.name,
                          p.description, cmd[:120] + ("…" if len(cmd) > 120 else ""))
    return table


def print_report(recs: list[Recommendation], inputs: dict, hw: HardwareInfo | None = None,
                 current_values: dict | None = None, all_origins: bool = False,
                 console: Console | None = None) -> None:
    console = console or Console()
    if hw is not None:
        console.print(render_hw_panel(hw))
    console.print(render_table(recs, current_values=current_values, all_origins=all_origins))


# ---------------------------- check layer: signal panel ----------------------------

def _ratio_health(r: float) -> str:
    if r >= 0.99:
        return "[green]优秀[/green]"
    if r >= 0.95:
        return "[green]良好[/green]"
    if r >= 0.9:
        return "[yellow]一般[/yellow]"
    return "[red]偏低（AWS：<0.9 考虑上调 shared_buffers）[/red]"


def render_signal_panel(sig) -> Panel:
    """Check-layer signal panel: observed values + health (hit ratio / temp spill / dead-live ratio / WAL rate)."""
    body = Table(box=None, show_header=False)
    body.add_column(style="bold")
    body.add_column()
    body.add_column(max_width=52)
    ver = sig.settings.get("server_version", {}).get("setting", "?")
    body.add_row("实例", f"{ver}", f"[dim]pg_version={sig.pg_major}（运行值）[/dim]")
    if sig.runtime_max_connections:
        body.add_row("max_connections", str(sig.runtime_max_connections),
                     "[dim]运行值回填（优先于用户默认）[/dim]")
    if sig.db_size_gb is not None:
        body.add_row("db_size_gb", f"{sig.db_size_gb} GB", "[dim]pg_database_size 实测[/dim]")
    if sig.cache_hit_ratio is None:
        body.add_row("buffer_cache_hit_ratio", "无数据", "[dim]blks_hit+blks_read=0，不注入[/dim]")
    else:
        body.add_row("buffer_cache_hit_ratio", f"{sig.cache_hit_ratio:.4f}",
                     _ratio_health(sig.cache_hit_ratio))
    if sig.temp_files is not None:
        health = ("[green]无溢盘记录[/green]" if sig.temp_files == 0
                  else "[yellow]溢盘信号：可考虑增大 work_mem[/yellow]")
        body.add_row("temp_files", f"{sig.temp_files} 个 / {sig.temp_bytes / 1048576:.1f} MB", health)
    if sig.n_live_tup is not None:
        dead_live = sig.n_dead_tup / sig.n_live_tup if sig.n_live_tup else None
        if dead_live is None:
            health = "[dim]无表统计[/dim]"
        elif dead_live < 0.1:
            health = "[green]健康[/green]"
        elif dead_live < 0.2:
            health = "[yellow]接近 autovacuum 默认触发点 0.2[/yellow]"
        else:
            health = "[red]超过 autovacuum 默认触发点 0.2[/red]"
        body.add_row("n_live_tup", f"{sig.n_live_tup:,}",
                     (f"dead {sig.n_dead_tup:,}（dead/live {dead_live:.1%}）" if dead_live is not None else "")
                     + health)
        if sig.biggest_table_live_tuples:
            body.add_row("biggest_table_live_tuples", f"{sig.biggest_table_live_tuples:,}",
                         f"[dim]target_dead_tuples = {int(sig.biggest_table_live_tuples * 0.01):,}"
                         f"（1% 启发式）[/dim]")
    if sig.wal_rate_mbps is not None:
        wal_note = "[dim]≈0：无写入负载，照实注入[/dim]" if sig.wal_rate_mbps < 0.01 else ""
        body.add_row("wal_rate_mbps", f"{sig.wal_rate_mbps} MB/s",
                     f"两次采样间隔 {sig.wal_sample_seconds:g}s {wal_note}")
    if sig.os_free_and_cached_gb is not None:
        body.add_row("os_free_and_cached_gb", f"{sig.os_free_and_cached_gb} GB",
                     f"[dim]freeable_mem_kb = {sig.freeable_mem_kb:,}（MemAvailable+Cached）[/dim]")
    if sig.loadavg_1m is not None:
        sat = sig.cpu_saturated
        health = ("[red]饱和[/red]" if sat else "[green]未饱和[/green]")
        body.add_row("cpu_saturated", str(sat),
                     f"load1={sig.loadavg_1m}（阈值 = cores×0.8）{health}" if sat is not None else health)
    for note in sig.notes:
        body.add_row("·", f"[dim]{note}[/dim]", "")
    return Panel(body, title="实测信号面板", expand=False)


# ---------------------------- bench layer: candidate comparison table ----------------------------

def render_bench_table(results: list) -> Table:
    """Candidate | injected-parameter summary | TPS | average latency | % vs baseline. The baseline is fixed as the first candidate."""
    table = Table(box=box.SIMPLE_HEAVY, title="pgbench 候选对比")
    table.add_column("候选", style="bold cyan", no_wrap=True)
    table.add_column("注入参数（仅 context=user）", max_width=46)
    table.add_column("TPS", justify="right")
    table.add_column("平均延迟", justify="right")
    table.add_column("vs baseline", justify="right")
    baseline_tps = results[0].tps if results else None
    for r in results:
        summary = ", ".join(f"{k}={v}" for k, v in r.candidate.gucs.items()) or "（无注入）"
        if r.candidate.skipped:
            summary += ("\n[dim]跳过: " + ", ".join(f"{n}({c})" for n, c in r.candidate.skipped)
                        + "[/dim]")
        if r.error:
            summary += f"\n[red]{r.error}[/red]"
        tps_cell = f"{r.tps:.2f}" if r.tps is not None else "[red]失败[/red]"
        lat_cell = f"{r.latency_ms:.3f} ms" if r.latency_ms is not None else "-"
        if r.tps is not None and baseline_tps:
            rel = f"{(r.tps / baseline_tps - 1) * 100:+.1f}%"
        else:
            rel = "-"
        table.add_row(r.candidate.name, summary, tps_cell, lat_cell, rel)
    return table
