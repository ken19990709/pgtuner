# pgtuner

**简体中文** | [English](README.md)

[![CI](https://github.com/kenxx/pgtuner/actions/workflows/ci.yml/badge.svg)](https://github.com/kenxx/pgtuner/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/kenxx/pgtuner/blob/main/LICENSE) [![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

*面向 AI coding agent 的、以证据为底的 PostgreSQL 实验工作台。*

所有计时数字都出自受控命令通道，所有实验都落证据账本，裸 `psql` 计时被一律禁掉——正确的实验方式同时就是最省事的方式。

为 AI coding agent（以及审计它们的人类）而设计：一条命令拿一次性实验库、只读探看不输出计时、重启级实验自动无痕还原（盘上崩溃日志兜底）、结论必须引用账本 diff。

## 为什么需要它

Agent 做 PostgreSQL 性能工作有几类高频翻车：

- 拿 `psql` 的 `\timing` 当证据——受网络与终端噪声影响，不可复现。
- 实验产物随会话结束一起消失。
- 破坏性 DDL / 索引实验直接跑在共享库上。
- 结论凭记忆断言，而不是 A/B diff。

pgtuner 把应然的工作流做成了 CLI 便利性；账本（`pgtuner results diff`）是唯一被认的证据通道。

## 能力一览

- **实例注册表**——登记既有安装或托管创建（`initdb` + 启动 + 登记）；跟踪 debug/release 构建，性能结论只认 release 实例。
- **模板克隆池（`lab`）**——模板库密封一次，之后秒级 `clone` 出一次性实验库（stdout 只打库名，可安全 `$(...)` 捕获），用完 `release` 零残留；克隆有属主防护，非克隆库（真实业务库）在 release 时被拒绝。
- **快慢双通道**——`bench` 走 `PGOPTIONS` 会话级注入候选（不动服务器配置）；`rebench` 做重启级 GUC 实验（无痕还原，回执 `restored: True`，还原清单落盘抗崩溃）或 `--queries-file` 纯测量 + `--setup "ANALYZE …"` 前后干预。
- **plan 工作台（`planbench`）**——EXPLAIN 采集、`--variant NAME=DDL` 变体 A/B（索引 / DDL / `ANALYZE` 类维护干预，一条命令出逐节点 diff）、代价估算准确率、plan 翻转检测。
- **只读探看**——`sql`（一次一条，绝不输出计时，带 `statement_timeout` 保护）、`schema tables/describe`（列/索引/约束/触发器/分区/DDL）、`statements`（pg_stat_statements 热点）、`watch`（实时仪表盘）。
- **证据账本**——`results list / show / diff / conclude`；跨实验 diff 带构建指纹（debug × release 拒绝比对），低于噪声底的差异必须表述为不显著。
- **引导式闭环**——`hypo`（假设索引，事务内建后回滚零残留）、`advise`（透明漏斗：启发式候选带证据 → 假设索引评估 → 排名处方）、`diagnose`（一键慢查询：基线 plan → 根因线索 → 候选排名 → `--verify` 贪心真实建索引 + 前后实测 A/B + 自动回填结论）。
- **知识驱动的 GUC 调优**——`probe / recommend / check / knowledge`：57 个 GUC × 109 条带出处的公式（PG 13–18）随包内 JSON 知识库分发；求值引擎本身零公式知识（受限求值 + 静态单位/门禁表）。更新 JSON 即更新推荐。
- **Agent 治理**——5 条铁律的 `AGENTS.md` 契约 + ZCode `PreToolUse` hook 硬阻断裸 `psql`/`pgbench`（逃生口：在命令尾加 `# allow-raw-psql`）。

## 安装

主平台 Linux / WSL2（POSIX socket、`/tmp` 路径）；macOS 尽力而为；Windows 请用 WSL2。Python ≥ 3.9。

```bash
git clone https://github.com/kenxx/pgtuner.git && cd pgtuner
bash scripts/setup.sh        # 仓库 .venv + 可编辑安装带 extras（幂等，可重复跑）
source .venv/bin/activate    # 或直接用 .venv/bin/pgtuner
```

或在你自己的环境里：

```bash
pip install -e ".[db,dev]"   # click + rich；[db] 加 psycopg2-binary（惰性导入——离线子命令不装也能用）
```

`scripts/setup.sh` 默认免 sudo。裸 Debian/Ubuntu 机器从零编译 PostgreSQL（含 contrib）：`bash scripts/setup.sh --full <prefix>`（各阶段幂等；全部阶段与环境变量见 `bash scripts/setup.sh --help`）。装完跑只读环境体检，全绿即开工：

```bash
pgtuner doctor
```

## 装进你自己的项目

```bash
cd ~/myproj && pgtuner init          # 落 AGENTS.md / CLAUDE.md / skills / .gitignore + .pgtuner 状态骨架
pgtuner init --global                # 用户级：装 skills 到 ~/.zcode、~/.claude、~/.codex，并写入规则标记块与 PATH 接线
```

你的项目会拥有自己的账本（`.pgtuner/`），与工具仓实验不串账。

## 快速上手

```bash
# 一次性：登记（或 instance create）实例并铺好克隆池
pgtuner instance register rel19 /path/to/pg/bin --port 5433   # setup.sh --pg 构建的填 <prefix>/bin
pgtuner lab bootstrap --instance rel19          # pgss 预载 + 密封 bench_tpl 模板 + 默认实例

# 三命令实验闭环（bootstrap 已设默认实例；手工设：`pgtuner config set default-instance <name>`）
db=$(pgtuner lab clone --template bench_tpl)    # 秒级拿一次性库，stdout 只有库名
#   … 跑负载 / 预热 …
pgtuner diagnose --file q.sql --db "$db" --verify --release
#   基线 plan → 根因线索 → 候选排名 → 贪心建索引 → 复测 → A/B diff
#   → 结论自动回填账本 → 库归还克隆池
```

plan 侧实验：

```bash
pgtuner planbench q.sql --db "$db" \
    --variant idx1="CREATE INDEX ON events (tenant_id, created_at)" \
    --variant fix="ANALYZE events" \
    --accuracy
```

重启级 GUC 实验（慢通道——agent 须先报预计时长并征得确认）：

```bash
pgtuner rebench --instance rel19 --guc shared_buffers=512MB --guc shared_buffers=1GB --template bench_tpl
```

GUC 调优线（项目最初的内核）：

```bash
pgtuner probe                              # 硬件探测报告（内存/CPU/OS/WSL2/磁盘多信号 + 置信度）
pgtuner recommend --workload oltp          # 探测 → 装配输入 → 公式求值 → conf 片段
pgtuner check --dsn "host=/tmp port=5432 dbname=bench_new"   # 连活库只读信号微调推荐
pgtuner knowledge shared --pain high       # 浏览 GUC 知识库
```

## 命令地图

| 领域 | 命令 | 作用 |
|---|---|---|
| 实例 | `instance register/create/list/status/start/stop/dsn/destroy` | 注册表 + 生命周期；跟踪构建（`debug`/`release`） |
| 克隆池 | `lab bootstrap/templates/make-tpl/clone/release/stats/audit/sweep` | 密封模板 → 一次性克隆 → 零残留归还 |
| 只读探看 | `sql` · `schema tables/describe` · `statements` · `watch` | 功能检查 / 数据探看——出口上就不输出计时 |
| 快通道 | `bench` · `planbench` · `hypo` · `advise` · `diagnose` | 会话级候选、plan A/B、假设索引、一键诊断 |
| 慢通道 | `rebench` | 重启级 GUC A/B（无痕还原）或 `--queries-file` 纯测量 |
| 证据 | `results list/show/diff/conclude` | 账本：唯一被认的证据通道 |
| GUC 调优 | `probe` · `recommend` · `check` · `knowledge` | 知识驱动的配置调优 |
| 装配 | `init` · `doctor` · `selftest` · `config` | 项目布线、环境体检、验收门槛、部署级缺省 |

完整语义看 `pgtuner --help` 与各命令 `--help`——help 是唯一事实源，默认中文，`PGTUNER_LANG=en` 切英文（如 `PGTUNER_LANG=en pgtuner --help`）。

## Agent 契约

`AGENTS.md`（由 `pgtuner init` 装进你的项目）承载五条铁律：性能实验一律走 pgtuner 命令；每次实验必须带 experiment-id 落账本；结论必须引用 `results diff` 证据、低于噪声底的差异表述为不显著；慢通道（`rebench`）需先报预计时长、征得确认后再跑；性能结论只认 `release` 实例。

本仓还附带机械执法层——ZCode `PreToolUse` hook（[`scripts/guard_raw_cli.py`](scripts/guard_raw_cli.py) + [`.zcode/config.json`](.zcode/config.json)）拒绝任何执行裸 `psql`/`pgbench` 的 Bash 命令（含 `time psql` 等包装形式；文档中提及不算），并指路 pgtuner 等价命令。确需裸跑的逃生口：在命令尾加 `# allow-raw-psql`。

## 文档

一切随仓库自带：本 README（用法）、`AGENTS.md`（双语 agent 契约）、`CLAUDE.md`（指针）、[CHANGELOG.md](https://github.com/kenxx/pgtuner/blob/main/CHANGELOG.md)、[CONTRIBUTING.md](https://github.com/kenxx/pgtuner/blob/main/CONTRIBUTING.md)，以及 CLI 自带的双语 `--help`——命令语义的唯一事实源。

## 状态与语言

v0.11.0——版本历史见 [CHANGELOG.md](https://github.com/kenxx/pgtuner/blob/main/CHANGELOG.md)。本仓库同时是一套评估 AI agent 真实 PG 运维能力的内部基准设施；其操作员工具不属于公开接口。

代码注释与 docstring 为英文。CLI 帮助已双语——默认中文，`PGTUNER_LANG=en` 切英文。pgtuner 自产的运行期消息（表格/回执/分类错误提示）当前为中文，英文 i18n 在路线图上；框架层错误（如未知命令）来自 click，两种模式下均为英文。知识库覆盖 PostgreSQL 13–18。

## 开发

```bash
python3 -m pytest tests/ -q    # 全量测试（多数无需 PG）
pgtuner selftest               # 验收门槛；--with-db 走完整 DB 链路（release 构建）
```

开发环境搭建与仓库契约测试（双语 help 覆盖、打包资产同步）见 [CONTRIBUTING.md](https://github.com/kenxx/pgtuner/blob/main/CONTRIBUTING.md)。

## 许可

[MIT](https://github.com/kenxx/pgtuner/blob/main/LICENSE)
