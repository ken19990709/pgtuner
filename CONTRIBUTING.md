# Contributing to pgtuner

Thanks for considering a contribution. The repo is deliberately small on documents
— this file plus the READMEs, AGENTS.md and the CLI's own `--help` are the whole
written surface — so a few invariants live here instead.

## Dev setup

```bash
bash scripts/setup.sh        # repo .venv + editable install with extras (idempotent)
source .venv/bin/activate
python3 -m pytest tests/ -q  # full suite; most tests need no running PostgreSQL
pgtuner selftest             # acceptance gates (fast lane is offline)
```

`pip install -e ".[db,dev]"` works too — the `dev` extra is enough for a fully
green offline test run (psycopg2 is imported at module top by five test files).

## Invariants the CI and contract tests enforce

- **Help is the single source of truth for command semantics.** Chinese help texts
  live in `pgtuner/cli.py`; `pgtuner/help_en/` holds the English table.
  If you add or change any command/option help, update the matching
  `pgtuner/help_en/*` entry — `tests/test_help_i18n.py` fails on missing or stale
  keys automatically.
- **Packaged assets must not drift.** `pgtuner/assets/AGENTS.md` and `SKILL.md`
  are byte-for-byte copies of the repo-root originals (`AGENTS.md`,
  `.zcode/skills/pg-experiment/SKILL.md`); `tests/test_packaging.py` asserts the
  sync. Re-copy after editing the originals. Data files (`guc_knowledge.json`,
  `template_setup.sql`) belong inside the package so wheel installs work.
- **Language conventions.** Code comments and docstrings: English. Runtime
  messages produced by pgtuner (tables, receipts, classified error hints):
  zh-CN for now (English i18n is on the roadmap). Click command docstrings are
  help text, not comments — translate them through `help_en`, never in place.
- **Experiment discipline.** Any performance claim in a PR must cite ledger
  evidence (`pgtuner results diff <a> <b>`); below-noise-floor deltas are stated
  as insignificant. See AGENTS.md for the five iron rules — they apply to
  contributor experiments just as much as to agent sessions.
- **Read-only surfaces must stay read-only at the connection layer**, not by
  string filtering alone; destructive operations need explicit gates
  (`--yes`/`--force`) and audit trails.
- **On every PostgreSQL major-version upgrade, rerun the `pg_catalog` sweep
  and refresh the snapshot contract.** The side-effect blacklists in
  `pgtuner/sqlsplit.py` are reconciled against the full `pg_proc` name dump
  of the newest live instance (inverted classification: dump − blacklists −
  allow groups − documented read-only pattern groups, manual-review remainder
  must reach zero); `tests/test_hardening_r1.py::test_round9_sweep_snapshot_contract`
  freezes the result — update it together with the blacklists. The pattern
  groups and per-name rationale live in the sweep comment block in
  `pgtuner/sqlsplit.py`.
- **Project-state anchoring order is fixed**: `PGTUNER_PROJECT_ROOT` env >
  upward search for a `.pgtuner`/`.git` marker (system temp roots like `/tmp`
  are never anchors) > cwd fallback — see `pgtuner/paths.py`
  (`resolve_project_root`).

## Pull requests

1. Keep `python3 -m pytest tests/ -q` green (CI runs Python 3.9–3.12).
2. Add a test for every behavior change; regression tests reference the review
   finding they pin (see existing `adversarial review R1-*` markers).
3. One logical change per PR; update `CHANGELOG.md` under `## Unreleased` (add
   that section when you need it).
4. The internal benchmark harness is operator tooling and intentionally not part
   of the public interface — don't expose its commands in README/help changes.
