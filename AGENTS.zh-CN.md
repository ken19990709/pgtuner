# AGENTS.md — 在本仓库做 PG 实验的 Agent 行为铁律

**简体中文** | [English](AGENTS.md)

> 适用于所有 AI coding agent(Claude Code / ZCode / Cursor 等)。**命令用法一律以
> `pgtuner --help` 与 `pgtuner <cmd> --help` 为唯一事实源**——随二进制走、永不滞后
> (`PGTUNER_LANG=en` 可切英文帮助);本文只立规矩,不复述用法(人读展开见仓库 README,
> 可能滞后于 help)。

## 五条铁律

1. **性能实验一律走 pgtuner 命令**(bench / rebench / planbench / lab),禁止裸 psql/pgbench 计时下性能结论(psql `\timing` 受网络与终端影响,不可作证据)。功能性检查/数据探看用 `pgtuner sql`(只读强制、不输出计时——出口上就不给违纪留门)。
2. **每次实验必须带 experiment-id**:由工具自动生成并落账本(`.pgtuner/ledger/`);自己绕过账本手工存 txt = 实验作废。
3. **结论必须引用账本证据**:`pgtuner results diff <a> <b>` 的输出(或 citation),不许凭感觉。低于噪声地板的差异必须表述为"不显著"。
4. **慢通道(rebench,含重启)需人确认后再跑**:预计时长以命令启动时的报告为准,确认后加 `--yes`;跑完必须确认输出含 `restored: True`。
5. **性能结论只认 release 实例**(instance list 里 build_type=release);debug 实例只做功能验证,其 TPS 数据禁止出现在结论里。

## 探测任务的标准循环

登记一句话假设 → 按场景选通道(负载热点 statements / 会话级 GUC bench / 重启级 GUC·纯测量计时·前后干预 rebench / 索引与写法 planbench / 假设索引 hypo / 一键慢查询闭环 diagnose)→ `lab clone` 拿一次性库 → 跑(账本自动落档)→ `results diff` → 结论回填 `results conclude`。验证充分即停,不做锦上添花的追加迭代。触发器/DDL 类破坏性探测**必须**只在 clone 里做,不要碰模板库和别人的库。

新机器/新会话开工先 `pgtuner doctor`(❌ 清零再动手);省往返铁则:相关命令在一次 Bash 调用里 `&&` 串联、`$()` 捕获。
