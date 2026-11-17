# pgtuner

**English** | [简体中文](README.zh-CN.md)

[![CI](https://github.com/kenxx/pgtuner/actions/workflows/ci.yml/badge.svg)](https://github.com/kenxx/pgtuner/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/kenxx/pgtuner/blob/main/LICENSE) [![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

*An evidence-led PostgreSQL experiment workbench for AI coding agents.*

Every timing number comes out of a controlled channel, every experiment lands in an evidence ledger, and raw `psql` timing is banned outright — the correct way to run a performance experiment is also the easiest way.

Built for AI coding agents — and the careful humans who audit them: one-command disposable databases, read-only inspection commands that cannot leak timings, restart-level experiments that restore themselves (backstopped by an on-disk crash journal), and conclusions that must cite ledger diffs.

## Why

Agents doing PostgreSQL performance work share predictable failure modes:

- `\timing` in `psql` presented as evidence — network and terminal noise, unreproducible.
- Experiment artifacts that vanish when the session ends.
- Destructive DDL and index experiments run on shared databases.
- Conclusions asserted from memory instead of A/B diffs.

pgtuner encodes the intended workflow as CLI affordances, and the ledger (`pgtuner results diff`) is the only accepted evidence.

## Highlights

- **Instance registry** — register existing installs or create managed ones (`initdb` + start + registry); debug vs release builds are tracked, and only release instances may back performance conclusions.
- **Template clone pool (`lab`)** — seal a template database once, then `clone` a disposable copy in seconds (stdout prints the db name only, safe for `$(...)`) and `release` it with zero residue. Clones are owner-guarded; real (non-clone) databases are refused at release time.
- **Fast & slow measurement channels** — `bench` injects candidates session-level via `PGOPTIONS` (server config untouched); `rebench` runs restart-level GUC experiments with traceless restore (`restored: True` receipt, crash journaled on disk) or pure `--queries-file` timing with `--setup "ANALYZE …"` before/after interventions.
- **Plan workbench (`planbench`)** — EXPLAIN capture, `--variant NAME=DDL` A/B comparisons (indexes, DDL, maintenance like `ANALYZE`) with per-node diffs in one command, cost-estimate accuracy ranking, plan-flip detection.
- **Read-only inspection** — `sql` (one statement, never prints timing, `statement_timeout` guard), `schema tables/describe` (columns/indexes/constraints/triggers/partitions/DDL), `statements` (pg_stat_statements hotspots), `watch` (live dashboard).
- **Evidence ledger** — `results list / show / diff / conclude`; cross-experiment diff fingerprints the build (debug × release refused) and flags below-noise-floor deltas as not significant.
- **Guided loops** — `hypo` (hypothetical indexes, zero residue via rollback), `advise` (transparent funnel: heuristic candidates with evidence → hypothetical evaluation → ranked prescriptions), `diagnose` (one-shot slow-query: baseline plan → root-cause hints → candidate ranking → `--verify` greedy real-index build + before/after A/B + auto-conclusion).
- **Knowledge-driven GUC tuning** — `probe / recommend / check / knowledge`: 57 GUCs × 109 sourced formulas (PG 13–18) live in a JSON knowledge base shipped inside the package; the engine itself is knowledge-free (sandboxed expression evaluation, static unit/gating tables). Update the JSON, and the recommendations update.
- **Agent governance** — a 5-rule `AGENTS.md` contract plus a ZCode `PreToolUse` hook that hard-denies bare `psql`/`pgbench` commands (escape hatch: append `# allow-raw-psql` at the end of the command).

## Install

Linux / WSL2 is the primary platform (POSIX sockets, `/tmp` paths); macOS is best-effort. Windows: use WSL2. Python ≥ 3.9.

```bash
git clone https://github.com/kenxx/pgtuner.git && cd pgtuner
bash scripts/setup.sh        # repo .venv + editable install with extras (idempotent)
source .venv/bin/activate    # or call .venv/bin/pgtuner directly
```

or, inside your own environment:

```bash
pip install -e ".[db,dev]"   # click + rich; [db] adds psycopg2-binary (lazy — offline subcommands work without it)
```

`scripts/setup.sh` needs no sudo by default. Building PostgreSQL (incl. contrib) from scratch on a bare Debian/Ubuntu machine: `bash scripts/setup.sh --full <prefix>` (each stage is idempotent; `bash scripts/setup.sh --help` for all stages and knobs). After install, run the read-only environment check and start once everything is green:

```bash
pgtuner doctor
```

## Use it inside your own project

```bash
cd ~/myproj && pgtuner init          # installs AGENTS.md / CLAUDE.md / skills / .gitignore + .pgtuner state skeleton
pgtuner init --global                # user-level: skills to ~/.zcode ~/.claude ~/.codex + rule marker blocks + PATH wiring
```

Your project gets its own ledger (`.pgtuner/`), so experiments there never mix with the tool repo's.

## Quick start

```bash
# One-time: register (or `instance create`) an instance and bootstrap the clone pool
pgtuner instance register rel19 /path/to/pg/bin --port 5433   # built by setup.sh --pg: use <prefix>/bin
pgtuner lab bootstrap --instance rel19          # pgss preload + sealed bench_tpl template + default instance

# The 3-command experiment loop (bootstrap sets the default; manual: `pgtuner config set default-instance <name>`)
db=$(pgtuner lab clone --template bench_tpl)    # disposable db in seconds, name on stdout
#   … run your workload / warm up …
pgtuner diagnose --file q.sql --db "$db" --verify --release
#   baseline plan → root-cause hints → candidate ranking → greedy index build → re-measure
#   → A/B diff → auto-conclusion in the ledger → db returned to the pool
```

Plan-side experiments:

```bash
pgtuner planbench q.sql --db "$db" \
    --variant idx1="CREATE INDEX ON events (tenant_id, created_at)" \
    --variant fix="ANALYZE events" \
    --accuracy
```

Restart-level GUC experiments (slow channel — agents must report expected duration and ask first):

```bash
pgtuner rebench --instance rel19 --guc shared_buffers=512MB --guc shared_buffers=1GB --template bench_tpl
```

GUC tuning lane (the original core):

```bash
pgtuner probe                              # hardware report (RAM/CPU/OS/WSL2/disk signals + confidence)
pgtuner recommend --workload oltp          # probe → assemble inputs → evaluate formulas → conf snippet
pgtuner check --dsn "host=/tmp port=5432 dbname=bench_new"   # live read-only signals tune the recommendations
pgtuner knowledge shared --pain high       # browse the GUC knowledge base
```

## Command map

| Area | Command | What it does |
|---|---|---|
| Instances | `instance register/create/list/status/start/stop/dsn/destroy` | Registry + lifecycle; builds tracked (`debug`/`release`) |
| Clone pool | `lab bootstrap/templates/make-tpl/clone/release/stats/audit/sweep` | Sealed templates → disposable clones → zero-residue return |
| Inspection (read-only) | `sql` · `schema tables/describe` · `statements` · `watch` | Functional checks & data looks — no timings by design |
| Fast lane | `bench` · `planbench` · `hypo` · `advise` · `diagnose` | Session-level candidates, plan A/B, hypothetical indexes, one-shot diagnosis |
| Slow lane | `rebench` | Restart-level GUC A/B (traceless restore) or pure `--queries-file` measurement |
| Evidence | `results list/show/diff/conclude` | The ledger: the only accepted evidence channel |
| GUC tuning | `probe` · `recommend` · `check` · `knowledge` | Knowledge-driven configuration tuning |
| Setup | `init` · `doctor` · `selftest` · `config` | Project wiring, environment check, acceptance gates, defaults |

Full semantics: `pgtuner --help` and per-command `--help` — help is the single source of truth, in Chinese by default and in English via `PGTUNER_LANG=en` (e.g. `PGTUNER_LANG=en pgtuner --help`).

## The agent contract

`AGENTS.md` (installed into your project by `pgtuner init`) carries five iron rules: performance experiments only via pgtuner commands; every experiment ledgered; conclusions must cite `results diff` evidence and call below-noise deltas insignificant; the slow channel (`rebench`) needs human confirmation with a duration estimate; perf conclusions only from `release` instances.

This repo also ships the mechanical enforcement layer — a ZCode `PreToolUse` hook ([`scripts/guard_raw_cli.py`](scripts/guard_raw_cli.py) + [`.zcode/config.json`](.zcode/config.json)) that denies any Bash command running bare `psql`/`pgbench` (wrappers like `time psql` included, mentions in docs excluded) and points the agent at the pgtuner equivalents. Escape hatch for genuinely raw cases: append `# allow-raw-psql` at the end of the command.

## Documentation

Everything ships in the repository: this README (usage), `AGENTS.md` (the bilingual agent contract), `CLAUDE.md` (pointer), [CHANGELOG.md](https://github.com/kenxx/pgtuner/blob/main/CHANGELOG.md), [CONTRIBUTING.md](https://github.com/kenxx/pgtuner/blob/main/CONTRIBUTING.md), and the CLI's own bilingual `--help` — the single source of truth for command semantics.

## Status & language

v0.11.0 — see [CHANGELOG.md](https://github.com/kenxx/pgtuner/blob/main/CHANGELOG.md) for the version history. The repository also doubles as an internal benchmark harness for evaluating AI agents on real PostgreSQL work; its operator tooling is not part of the public interface.

Code comments and docstrings are English. CLI help is bilingual — Chinese by default, English via `PGTUNER_LANG=en`. Runtime messages produced by pgtuner itself (tables, receipts, classified error hints) are currently zh-CN, with English i18n on the roadmap; framework-level errors (e.g. unknown command) come from click and are English in both modes. The knowledge base covers PostgreSQL 13–18.

## Development

```bash
python3 -m pytest tests/ -q    # full suite (no PG required for most tests)
pgtuner selftest               # acceptance gates; --with-db adds the full DB path (release build)
```

See [CONTRIBUTING.md](https://github.com/kenxx/pgtuner/blob/main/CONTRIBUTING.md) for the dev setup and the repo's contract tests (bilingual help coverage, packaged-asset sync).

## License

[MIT](https://github.com/kenxx/pgtuner/blob/main/LICENSE)
