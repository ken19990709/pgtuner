# pgtuner

**简体中文** | [English](README.md)

[![CI](https://github.com/ken19990709/pgtuner/actions/workflows/ci.yml/badge.svg)](https://github.com/ken19990709/pgtuner/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/ken19990709/pgtuner/blob/main/LICENSE) [![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/) [![PostgreSQL 13-18](https://img.shields.io/badge/PostgreSQL-13--18-336791.svg?logo=postgresql&logoColor=white)](https://www.postgresql.org/)

**AI agent 伪造不出来的 PostgreSQL 性能证据。**

你一定见过:agent 在 psql 里跑个 `\timing`,宣布"快了 8%",证据随终端一起蒸发。pgtuner 让诚实成为最省事的选择——计时只出自受控通道,每次实验自动落进带 id 的证据账本,裸 `psql`/`pgbench` 被工具链直接拦下,低于噪声底的差异会被明说"不显著"。机器用它跑实验,人类用它查账。

```text
$ db=$(pgtuner lab clone --template bench_tpl)        # 一次性实验库——实测 1.1 秒
用法: --dsn "host=/tmp port=5433 dbname='bench_tpl_c3eb395f3'"；用完: pgtuner lab release rel19 bench_tpl_c3eb395f3

$ pgtuner diagnose --file q.sql --db "$db" --verify --release --label demo-diagnose
…
候选排名(EXPLAIN 估算,benefit=Σ w·(1-ratio);TPS 结论需实测):
ratio   benefit  候选DDL
------  -------  ------------------------------------------------------------
×0.000    +1.00  CREATE INDEX ON pgbench_history (aid) INCLUDE (delta, mtime)
…
  baseline|Q0_ms: 11.612 → 0.008 ms(Δ -99.93%,显著?-)
  复查: pgtuner results diff exp-261117-232009-8ce0a0c4 exp-261117-232010-4ec189d6
结论已自动回填 [exp-261117-232008-1d498b02] diagnose:demo-diagnose(…)
已归还克隆 bench_tpl_c3eb395f3(证据在账本 exp-261117-232008-1d498b02;推荐索引 DDL 见上方,落地到目标库执行即可)
```

> 以上输出为真实运行捕获,`…` 为截断标记——未改写一个数字;完整未截断版见
> [看它干活](#看它干活)(该查询随仓库以 `examples/q.sql` 分发;演示捕获时文件名为 q.sql)。

**681 条自动化测试** · CI 覆盖 Python 3.9–3.12 · 知识库覆盖 PostgreSQL 13–18 ·
11 轮对抗评审硬化(回执见测试 docstring)。

[为什么需要它](#为什么需要它) · [60 秒上手](#60-秒上手) · [看它干活](#看它干活) ·
[它怎么保持诚实](#它怎么保持诚实) · [对比常用做法](#对比常用做法)

## 为什么需要它

Agent(和赶时间的人类)做 PostgreSQL 性能工作,翻车姿势高度雷同:

- **拿 `\timing` 当证据**——受网络与终端噪声污染,不可复现,明天就没了。
- **实验产物蒸发**——实验活在会话里,死在会话里。
- **在共享库上搞破坏性实验**——索引 DDL"就试一下",试在了生产库上。
- **结论凭记忆**——一句"+8% faster",背后没有任何 A/B diff。

## 它怎么治

| 翻车模式 | pgtuner 机制 | 命令 |
|---|---|---|
| 拿 `\timing` 当证据 | 只读出口物理上不输出计时;计时只出自受控通道 | `pgtuner sql` · `pgtuner bench` |
| 产物蒸发 | 每次运行自动落账本、带实验 id(`.pgtuner/ledger/`) | `pgtuner results list` |
| 结论凭感觉 | 跨实验 diff 带构建指纹 + 噪声底显著性判定 | `pgtuner results diff <a> <b>` |
| 在共享库上搞破坏 | 密封模板 → 一次性克隆(约 1 秒),属主防护、零残留归还 | `db=$(pgtuner lab clone --template bench_tpl)` |
| agent 偷跑裸 psql | `PreToolUse` hook 硬拦裸 `psql`/`pgbench` 并指路等价命令 | [`scripts/guard_raw_cli.py`](scripts/guard_raw_cli.py) |

## 60 秒上手

默认你已有 PostgreSQL 安装(13–18)——裸机上还没有?展开正下方折叠块。主平台
Linux / WSL2(POSIX socket);macOS 尽力而为;Windows 请用 WSL2。Python ≥ 3.9。

```bash
git clone https://github.com/ken19990709/pgtuner.git && cd pgtuner
bash scripts/setup.sh                 # 仓库 .venv + 可编辑安装(幂等,免 sudo)
source .venv/bin/activate
pgtuner doctor                        # 只读环境体检——❌ 清零再动手
pgtuner instance create rel19 /path/to/pg/bin --port 5433   # 还没有 PG?展开下方
pgtuner lab bootstrap --instance rel19   # pgss 预载 + 密封 bench_tpl 模板 + 默认实例

db=$(pgtuner lab clone --template bench_tpl)      # 第一条落账证据
pgtuner diagnose --file examples/q.sql --db "$db" --verify --release
# 演示查询随仓库自带 examples/q.sql(百万行 pgbench_history 上的 top-20 读取)
```

<details><summary>裸机还没有 PostgreSQL?</summary>

```bash
bash scripts/setup.sh --full <prefix>   # 从零编译 PostgreSQL(含 contrib),分阶段、幂等
# 之后 instance create/register 指向 <prefix>/bin;全部旋钮: bash scripts/setup.sh --help
```
</details>

## 看它干活

**一键诊断,自带验证闭环**——基线 plan、根因线索、候选排名、贪心真实建索引、A/B 复测、自动回填
结论、归还克隆,一条命令。首屏演示就是它(截断版),完整未截断输出如下:

```text
$ db=$(pgtuner lab clone --template bench_tpl)        # 一次性实验库——实测 1.1 秒
用法: --dsn "host=/tmp port=5433 dbname='bench_tpl_c3eb395f3'"；用完: pgtuner lab release rel19 bench_tpl_c3eb395f3

$ pgtuner diagnose --file q.sql --db "$db" --verify --release --label demo-diagnose
…
Q1: SELECT delta, mtime FROM pgbench_history WHERE aid = 424242 ORD…
  基线: Limit cost=12578.5 est_rows=1
  表 pgbench_history 全表扫描(估算输出 1 行);现有索引: 无
  线索: 谓词列 aid,mtime 不被任何现有索引覆盖(索引方向)
候选排名(EXPLAIN 估算,benefit=Σ w·(1-ratio);TPS 结论需实测):
ratio   benefit  候选DDL
------  -------  ------------------------------------------------------------
×0.000    +1.00  CREATE INDEX ON pgbench_history (aid) INCLUDE (delta, mtime)
×1.000    +0.00  CREATE INDEX ON pgbench_history (mtime)
…
验证闭环(基线实测→贪心建 1 条索引→复测,证据=账本 A/B):
  落地索引 pgbench_history_aid_delta_mtime_idx: CREATE INDEX ON pgbench_history (aid) INCLUDE (delta, mtime)(估算 +100%,…
  baseline|Q0_ms: 11.612 → 0.008 ms(Δ -99.93%,显著?-)
  复查: pgtuner results diff exp-261117-232009-8ce0a0c4 exp-261117-232010-4ec189d6
结论已自动回填 [exp-261117-232008-1d498b02] diagnose:demo-diagnose(…)
已归还克隆 bench_tpl_c3eb395f3(证据在账本 exp-261117-232008-1d498b02;推荐索引 DDL 见上方,落地到目标库执行即可)
```

(真实运行捕获)

**不动你的数据做 plan A/B**——一次性克隆 + 一条变体 DDL,逐节点 diff
(此处用一个新克隆——上面那个已归还):

```text
$ pgtuner planbench q.sql --db "$db" --variant idx="CREATE INDEX ON pgbench_history (aid, mtime DESC)" --label demo-planbench
  variant      Q0
 ━━━━━━━━━━━━━━━━━
  baseline   11.8
  idx         0.0
──────────────────────── plan diff: baseline → idx · Q0 ────────────────────────
路径      节点                 变更         est行 a→b       实际行 a→b      比值
耗时ms a→b
0         Limit                timing_shift 1→1             1→1             1
11.6→0.003
0.0       Index Scan           added        -→1             -→1             1
-→0.003
…
0.0.0.0   Seq Scan             removed      1→-             0.33→-          3.03
9.43→-
已落账本  planbench:demo-planbench
```

(真实运行捕获)

**敢说"不显著"的 diff**——同配置连跑两遍 bench(先 `--calibrate 3` 铺噪声底,TPS CV = 2.00%),
账本给每个 delta 单独下判定:

```text
$ pgtuner results diff exp-261117-231825-9a67943c exp-261117-231917-98e81f98
verdict: mixed
…
  指标                 A         B      Δ%   显著?
  baseline       1388.27   1476.17   +6.33     是
  recommended    1477.41   1481.47   +0.27   不显著
  work_mem×0.5   1472.61   1469.53   -0.21   不显著
  work_mem×2     1474.89   1409.87   -4.41     是
```

(真实运行捕获)

+0.27% 是噪声,+6.33% 是需要解释的漂移——敢下这种判定,本身就是产品。

## 里面有什么

- **实例注册表**——登记既有安装或托管创建;跟踪 debug/release 构建,性能结论只认 release。
- **模板克隆池(`lab`)**——模板密封一次,之后约 1 秒 `clone` 出一次性库(stdout 只有库名,可安全
  `$(...)` 捕获),`release` 零残留归还。
- **快通道**——`bench` 走 `PGOPTIONS` 会话级注入 GUC 候选(不动服务器配置);`planbench` 做索引/DDL/
  `ANALYZE` 的 plan A/B;`hypo` 假设索引;`advise` 透明漏斗;`diagnose` 一键闭环。
- **慢通道**——`rebench`:重启级 GUC A/B(无痕还原,回执 `restored: True`,还原清单落盘抗崩溃),或
  `--queries-file` 纯计时 + `--setup "ANALYZE …"` 前后干预。
- **只读探看**——`sql`(一次一条,绝不输出计时)、`schema tables/describe`、`statements`(pgss 热点)、
  `watch`(实时仪表盘)。
- **证据账本**——`results list/show/diff/conclude`;debug × release 拒绝比对;低于噪声底必须说"不显著"。
- **GUC 知识库**——57 个 GUC × 109 条带出处的公式(PG 13–18)随包内 JSON 分发;求值引擎零公式
  知识,改 JSON 即改推荐。

| 领域 | 命令 |
|---|---|
| 实例 | `instance register/create/list/status/start/stop/dsn/destroy` |
| 克隆池 | `lab bootstrap/templates/make-tpl/clone/release/stats/audit/sweep` |
| 只读探看 | `sql` · `schema tables/describe` · `statements` · `watch` |
| 快通道 | `bench` · `planbench` · `hypo` · `advise` · `diagnose` |
| 慢通道 | `rebench`(重启级 A/B 或纯计时) |
| 证据 | `results list/show/diff/conclude` |
| GUC 调优 | `probe` · `recommend` · `check` · `knowledge` |
| 装配 | `init` · `doctor` · `selftest` · `config` |

完整语义看 `pgtuner --help` 与各命令 `--help`——help 随二进制走、永不滞后。

## 它怎么保持诚实

本项目的差异化是架构性的,不是口号:

- **证据只存在于账本。** 实验自动生成 id、自动落 `.pgtuner/ledger/`;绕过账本手存 txt = 实验作废
  (契约写死)。结论必须引用 `results diff` 的输出。
- **只读出口不输出计时。** `pgtuner sql` 永远不打计时——出口上就不给"带走一个假数字"留门。
- **裸 psql/pgbench 被机械拦截。** ZCode `PreToolUse` hook([`scripts/guard_raw_cli.py`](scripts/guard_raw_cli.py))
  拒绝任何执行裸 `psql`/`pgbench` 的命令(含包装形式)并指路等价命令。确需裸跑:命令尾加
  `# allow-raw-psql`(接线见 [`.zcode/config.json`](.zcode/config.json))。
- **噪声底是测出来的,不是感觉。** 校准一次,之后每次 diff 逐 delta 判"显著/不显著";耗时类指标没有
  耗时口径噪声底时,宁可明确"不判定",也不顺手祝福一个数字。
- **结论只认 release。** `results diff` 带构建指纹,debug × release 拒绝比对;debug 实例的 TPS 禁止
  出现在结论里。
- **重启实验自还原,且可证明。** `rebench` 输出 `restored: True` 回执,还原清单先落盘——就算进程崩
  了,也不会把你的服务器留在实验态。

## 对比常用做法

| | 裸 psql + pgbench | 静态配置生成器 | pgtuner |
|---|---|---|---|
| 可复现的证据链 | 无(靠复制粘贴运气) | 无(给一段 conf) | 账本 id + diff |
| 一次性实验库 | 手工 CREATE/DROP | 不适用 | `lab clone` 约 1 秒、零残留 |
| plan A/B(索引对照) | 手工对比 EXPLAIN | 不适用 | `planbench` 逐节点 diff |
| 重启级实验 | 手改配置、风险自负 | 只有静态猜测 | `rebench` 无痕还原 |
| agent 治理 | 无 | 无 | AGENTS 契约 + 拦截 hook |

## Agent 契约

[AGENTS.zh-CN.md](AGENTS.zh-CN.md)(中文)与 [AGENTS.md](AGENTS.md)(英文)承载五条铁律:性能实验
一律走 pgtuner 命令;每次实验必须落账本;结论必须引用 `results diff`、低于噪声底表述为"不显著";慢通道
先报预计时长、征得确认再跑;性能结论只认 release 实例。`pgtuner init` 装进你项目的是双语合并版——
双语契约是特性。

## 装进你自己的项目

```bash
cd ~/myproj && pgtuner init          # 落 AGENTS.md / CLAUDE.md / skills / .gitignore + .pgtuner 骨架
pgtuner init --global                # 用户级:装 skills + 规则标记块 + PATH 接线
```

你的项目拥有自己的账本(`.pgtuner/`),与工具仓的实验不串账。

## 文档

一切随仓库自带:本 README(用法)、Agent 契约([AGENTS.zh-CN.md](AGENTS.zh-CN.md) ·
[AGENTS.md](AGENTS.md))、`CLAUDE.md`(指针)、[CHANGELOG.md](https://github.com/ken19990709/pgtuner/blob/main/CHANGELOG.md)、
[CONTRIBUTING.md](https://github.com/ken19990709/pgtuner/blob/main/CONTRIBUTING.md),以及 CLI 自带的双语
`--help`。

## 状态与语言

v0.11.0——版本历史见 [CHANGELOG.md](https://github.com/ken19990709/pgtuner/blob/main/CHANGELOG.md)。本仓库
同时是一套内部评估设施(用于 pgtuner 自身开发);其操作员工具不属于公开接口。

代码注释与 docstring 为英文。CLI 帮助双语——默认中文,`PGTUNER_LANG=en` 切英文。pgtuner 自产的运行期
消息(表格/回执/判定)当前为中文,英文 i18n 在路线图上;框架层错误(如未知命令)来自 click,两种模式下
均为英文。知识库覆盖 PostgreSQL 13–18。

## 开发

```bash
python3 -m pytest tests/ -q    # 全量测试(多数无需 PG)
pgtuner selftest               # 验收门槛;--with-db 走完整 DB 链路(release 构建)
```

开发环境与仓库契约测试(双语 help 覆盖、打包资产同步)见
[CONTRIBUTING.md](https://github.com/ken19990709/pgtuner/blob/main/CONTRIBUTING.md)。

## 许可

[MIT](https://github.com/ken19990709/pgtuner/blob/main/LICENSE)
