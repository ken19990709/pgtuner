---
name: pg-experiment
description: PostgreSQL performance/behavior experiments in this repo follow a fixed flow: single-query diagnosis goes to diagnose --verify (one command, full evidence); otherwise register a hypothesis → pick a channel → clone a disposable db → run → ledger → results diff. Use when the task is "why is this query slow", "measure the effect of a GUC / index / SQL shape", "investigate optimizer behavior (cost estimates / plan choice)", or "verify destructive scenarios (triggers / DDL)". pgtuner commands only — raw psql timing is banned. 在本仓库执行 PostgreSQL 性能/行为探测实验的固定流程:单查询诊断首选 diagnose --verify 一条出齐;其余场景登记假设→选通道→克隆实验库→运行→落账本→diff 出结论。当任务是"这条查询为什么慢"、"测某 GUC/索引/写法对性能的影响"、"探究优化器行为(代价估算/plan 选择)"、"验证触发器/DDL 等破坏性场景"时使用。强制走 pgtuner 命令,禁止裸 psql 计时。
---

# PG experiments: pgtuner only; usage lives in --help

PG performance/behavior experiments in this repo (and in any project with pgtuner
installed) use **pgtuner commands only** — raw psql/pgbench timing is banned; the complete
iron rules and ledger discipline live in the repo `AGENTS.md` (this file is only a hook and
does not restate the rules).

**Single source of usage truth**: `pgtuner --help` (top-level scenario routing + the
preferred 3-command loop) → `pgtuner <cmd> --help` (that command's semantics and flags).
Help ships with the binary and never goes stale; any markdown (this file included) may lag
— when they conflict, help wins. `PGTUNER_LANG=en` switches help to English.

Standard loop: register a one-line hypothesis → pick the channel → `lab clone` for a
disposable db → run (ledgered automatically) → `results diff` → backfill with
`results conclude`.

# PG 实验:一律走 pgtuner,用法看 --help

本仓库(及一切装了 pgtuner 的项目)的 PG 性能/行为实验**只用 pgtuner 命令**,禁止裸
psql/pgbench 计时;铁律与账本纪律的完整版见仓库 `AGENTS.md`(双语;本文件只是钩子,
不复述规矩)。

用法**唯一事实源**:`pgtuner --help`(顶层场景路由 + 首选三命令闭环)→
`pgtuner <cmd> --help`(该命令的语义与旗标细节)。help 随二进制走、永不滞后;任何 md
(含本文件)都可能滞后,与 help 冲突时一律以 help 为准。`PGTUNER_LANG=en` 可切英文帮助。

标准循环:登记一句话假设 → 按场景选通道 → `lab clone` 拿一次性库 → 跑(自动落账本)→
`results diff` → `conclude` 回填。
