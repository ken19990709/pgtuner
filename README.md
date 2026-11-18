# pgtuner

**English** | [简体中文](README.zh-CN.md)

[![CI](https://github.com/ken19990709/pgtuner/actions/workflows/ci.yml/badge.svg)](https://github.com/ken19990709/pgtuner/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/ken19990709/pgtuner/blob/main/LICENSE) [![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/) [![PostgreSQL 13-18](https://img.shields.io/badge/PostgreSQL-13--18-336791.svg?logo=postgresql&logoColor=white)](https://www.postgresql.org/)

**PostgreSQL performance experiments your AI agent can't fake.**

You've seen it: the agent runs `\timing` in `psql`, calls +8% a win, and the evidence
evaporates with the terminal. pgtuner makes honesty the path of least resistance — timing
only flows from controlled channels, every experiment lands in an evidence ledger with an
id, bare `psql`/`pgbench` is denied by the tooling itself, and below-noise-floor deltas get
labeled as such. Machines run the experiments. Humans run the audit.

```text
$ db=$(pgtuner lab clone --template bench_tpl)        # disposable db — measured 1.1 s
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

> Output captured from a real run — elided with `…`, no number altered (the query ships
> as `examples/q.sql`; captures show the original `q.sql` name); the full uncut capture
> is in [See it work](#see-it-work). Runtime messages are zh-CN today — CLI help is
> already bilingual (`PGTUNER_LANG=en`); see [Status & language](#status--language).

**681 automated tests** · CI on Python 3.9–3.12 · knowledge base covers PostgreSQL 13–18 ·
hardened through 11 adversarial review rounds (receipts in the test suite docstrings).

[The problem](#the-problem) · [60-second start](#60-second-start) · [See it work](#see-it-work) ·
[How it stays honest](#how-it-stays-honest) · [vs. usual tools](#pgtuner-vs-the-usual-tools)

## The problem

Agents (and hurried humans) doing PostgreSQL performance work share the same failures:

- **`\timing` as evidence** — network- and terminal-noisy, unreproducible, gone tomorrow.
- **Vanishing artifacts** — experiments that live and die inside one session.
- **Destructive experiments on shared databases** — index DDL "just to see" on production.
- **Conclusions from memory** — "+8% faster" with no A/B diff behind it.

## The fix

| Failure mode | pgtuner mechanism | Command |
|---|---|---|
| `\timing` taken as evidence | Read-only surfaces physically print no timings; timing comes only from controlled channels | `pgtuner sql` · `pgtuner bench` |
| Artifacts vanish | Every run is auto-ledgered under an experiment-id (`.pgtuner/ledger/`) | `pgtuner results list` |
| Conclusions from vibes | Cross-experiment diff with build fingerprint + noise-floor significance verdict | `pgtuner results diff <a> <b>` |
| Destructive DDL on shared dbs | Sealed templates → disposable clones (~1 s), owner-guarded zero-residue return | `db=$(pgtuner lab clone --template bench_tpl)` |
| Agent sneaks back to raw psql | `PreToolUse` hook hard-denies bare `psql`/`pgbench` and points to the pgtuner equivalent | [`scripts/guard_raw_cli.py`](scripts/guard_raw_cli.py) |

## 60-second start

Assumes an existing PostgreSQL install (13–18) — no PostgreSQL on the machine yet?
Unfold the bare-metal block directly below. Linux/WSL2 primary (POSIX sockets); macOS
best-effort; Windows: use WSL2. Python ≥ 3.9.

```bash
git clone https://github.com/ken19990709/pgtuner.git && cd pgtuner
bash scripts/setup.sh                 # repo .venv + editable install (idempotent, no sudo)
source .venv/bin/activate
pgtuner doctor                        # read-only env check — start when ❌ = 0
pgtuner instance create rel19 /path/to/pg/bin --port 5433   # no PostgreSQL yet? unfold below
pgtuner lab bootstrap --instance rel19   # pgss preload + sealed bench_tpl + default instance

db=$(pgtuner lab clone --template bench_tpl)      # first ledgered evidence
pgtuner diagnose --file examples/q.sql --db "$db" --verify --release
# the demo query ships as examples/q.sql (top-20 pgbench_history read;
# the bootstrapped template carries a 1M-row table)
```

<details><summary>Bare-metal machine, no PostgreSQL yet?</summary>

```bash
bash scripts/setup.sh --full <prefix>   # builds PostgreSQL (incl. contrib), staged & idempotent
# then point instance create/register at <prefix>/bin; all knobs: bash scripts/setup.sh --help
```
</details>

## See it work

**One-shot diagnosis with self-verification** — baseline plan → root-cause hints → candidate
ranking → greedy real-index build → A/B re-measure → auto-conclusion → clone returned. This
is the command behind the first-screen demo; its full uncut output:

```text
$ db=$(pgtuner lab clone --template bench_tpl)        # disposable db — measured 1.1 s
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

(captured from a real run)

**Plan A/B without touching your data** — a disposable clone, a variant DDL, per-node diff
(on a fresh clone — the one above was already released):

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

(captured from a real run)

**The diff that dares to say "not significant"** — identical config benched twice (after a
`--calibrate 3` noise-floor run, TPS CV = 2.00%); the ledger grades every delta:

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

(captured from a real run)

+0.27% is noise; +6.33% is drift worth explaining. That judgment is the product.

## What's inside

- **Instance registry** — register or create installs; debug/release tracked; conclusions only from release.
- **Clone pool (`lab`)** — sealed template → disposable clone in ~1 s (name-only stdout, `$(...)`-safe), zero-residue return.
- **Fast lane** — `bench` (session-level GUC via `PGOPTIONS`), `planbench` (plan A/B), `hypo` (hypothetical indexes), `advise` (candidate funnel), `diagnose` (one-shot loop).
- **Slow lane** — `rebench`: restart-level GUC A/B, traceless restore (`restored: True`, crash-journaled), or pure `--queries-file` timing + `--setup` interventions.
- **Read-only inspection** — `sql` (never prints timing), `schema tables/describe`, `statements` (pg_stat_statements), `watch` (live dashboard).
- **Evidence ledger** — `results list/show/diff/conclude`; debug × release refused; below-noise deltas must be called not significant.
- **GUC knowledge base** — 57 GUCs × 109 sourced formulas (PG 13–18) as shipped JSON; knowledge-free engine — edit the JSON, recommendations update.

| Area | Command |
|---|---|
| Instances | `instance register/create/list/status/start/stop/dsn/destroy` |
| Clone pool | `lab bootstrap/templates/make-tpl/clone/release/stats/audit/sweep` |
| Inspection (read-only) | `sql` · `schema tables/describe` · `statements` · `watch` |
| Fast lane | `bench` · `planbench` · `hypo` · `advise` · `diagnose` |
| Slow lane | `rebench` (restart-level A/B or pure timing) |
| Evidence | `results list/show/diff/conclude` |
| GUC tuning | `probe` · `recommend` · `check` · `knowledge` |
| Setup | `init` · `doctor` · `selftest` · `config` |

Full semantics live in `pgtuner --help` and per-command `--help` — help ships with the binary.

## How it stays honest

Differentiation here is architectural, not aspirational:

- **Evidence exists only in the ledger.** Experiments get auto-generated ids and land in
  `.pgtuner/ledger/`; hand-saved txt files are void by contract; conclusions must cite `results diff`.
- **Read-only exits cannot leak timings.** `pgtuner sql` prints no timing, ever — no door
  to walk out of with a fake number.
- **Raw psql/pgbench is mechanically denied.** A ZCode `PreToolUse` hook
  ([`scripts/guard_raw_cli.py`](scripts/guard_raw_cli.py)) blocks bare invocations
  (wrappers included) and points at the pgtuner equivalent. Escape hatch: `# allow-raw-psql`
  (wiring: [`.zcode/config.json`](.zcode/config.json)).
- **Noise floor is measured, not vibes.** Calibrate once; every diff grades each delta
  significant / not significant; latency deltas without a latency noise floor are left
  explicitly unjudged rather than blessed.
- **Release-only conclusions.** `results diff` fingerprints builds and refuses debug × release comparisons.
- **Restarts restore themselves — provably.** `rebench` receipts `restored: True` and
  journals the restore on disk first, so even a crash can't strand your server mid-experiment.

## pgtuner vs the usual tools

| | Raw psql + pgbench | Static config generators | pgtuner |
|---|---|---|---|
| Reproducible evidence trail | none (copy-paste luck) | none (a conf snippet) | ledger ids + diffs |
| Disposable experiment db | manual CREATE/DROP | n/a | `lab clone` ~1 s, zero residue |
| Plan A/B (index vs baseline) | manual EXPLAIN diffing | n/a | `planbench` per-node diff |
| Restart-level experiments | risky manual edits | static guesses only | `rebench` traceless restore |
| Agent governance | none | none | AGENTS contract + guard hook |

## The agent contract

[`AGENTS.md`](AGENTS.md) (English) and [`AGENTS.zh-CN.md`](AGENTS.zh-CN.md) (Chinese) carry
the five iron rules: pgtuner commands only for performance experiments; every experiment
ledgered; conclusions cite `results diff` and call below-noise deltas insignificant; the
slow channel asks for human confirmation with a duration estimate; conclusions only from
release instances. `pgtuner init` installs the bilingual combined edition into your
project — that dual-language contract is a feature.

## Use it inside your own project

```bash
cd ~/myproj && pgtuner init          # AGENTS.md / CLAUDE.md / skills / .gitignore + .pgtuner skeleton
pgtuner init --global                # user-level: skills + rule markers + PATH wiring
```

Your project gets its own ledger (`.pgtuner/`), never mixed with the tool repo's.

## Documentation

Everything ships in the repository: this README (usage), the agent contract
([AGENTS.md](AGENTS.md) · [AGENTS.zh-CN.md](AGENTS.zh-CN.md)), `CLAUDE.md` (pointer),
[CHANGELOG.md](https://github.com/ken19990709/pgtuner/blob/main/CHANGELOG.md),
[CONTRIBUTING.md](https://github.com/ken19990709/pgtuner/blob/main/CONTRIBUTING.md), and
the CLI's own bilingual `--help`.

## Status & language

v0.11.0 — history in [CHANGELOG.md](https://github.com/ken19990709/pgtuner/blob/main/CHANGELOG.md).
The repository also doubles as an internal evaluation harness (used to develop pgtuner
itself); its operator tooling is intentionally not part of the public interface.

Code comments and docstrings are English. CLI help is bilingual — zh-CN by default,
English via `PGTUNER_LANG=en`. Runtime messages produced by pgtuner itself (tables,
receipts, verdicts) are currently zh-CN, with English i18n on the roadmap; framework-level
errors come from click and are English in both modes. The knowledge base covers PG 13–18.

## Development

```bash
python3 -m pytest tests/ -q    # full suite (no PG required for most tests)
pgtuner selftest               # acceptance gates; --with-db adds the full DB path (release build)
```

See [CONTRIBUTING.md](https://github.com/ken19990709/pgtuner/blob/main/CONTRIBUTING.md)
for the dev setup and the repo's contract tests (bilingual help coverage, packaged-asset sync).

## License

[MIT](https://github.com/ken19990709/pgtuner/blob/main/LICENSE)
