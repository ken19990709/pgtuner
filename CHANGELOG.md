# Changelog

All notable changes to pgtuner are documented here. The format is loosely based on
[Keep a Changelog](https://keepachangelog.com/); versions follow the repo's own
history. Dates are omitted — see `git log` for exact timing.

## Unreleased

### Fixed

- CI portability, two roots: the PreToolUse guard hook now rejects command
  strings over 1 MiB (8x the Linux `MAX_ARG_STRLEN` per-argument cap)
  fail-closed before any scanning, and the deeply-nested-payload timing test
  asserts hardware-normalized ratios against same-size flat baselines plus a
  pinned constant-time test for the size cap — the retired absolute
  wall-clock budgets measured 1.9 s locally but 2.8–7.3 s on GitHub 2-core
  runners, reddening the whole matrix. Follow-up calibration from the first
  real post-push run: the ratio threshold is 10x (the nested/flat cost
  ratio is interpreter-version-dependent — 3.07–3.23 on CI 3.10/3.11 vs
  ~2.0 on 3.12 — while a genuinely superlinear blow-up measures in the
  tens-to-hundreds), and the matrix runs on `ubuntu-24.04` because the
  Ubuntu 26.04 runner image no longer provisions CPython 3.9.
- `.zcode/config.json` and `.zcode/skills/pg-experiment/SKILL.md` are
  trackable again (`.gitignore` re-include rules): fresh clones missed them,
  so the packaging drift guard raised FileNotFoundError in CI and the README
  link to `config.json` 404'd on GitHub.
- `diagnose --verify` help now states the real requirement — `--instance` or
  a configured default instance (the README paste flow relies on the latter);
  the English help table says the same.
- Both READMEs link `.zcode/config.json` again as the guard-hook wiring
  reference (the README rework had dropped the only pointer to a file the
  repo and sdist still ship).

### Changed

- Packaging identity aligned with the GitHub account: `pyproject.toml`
  author and the LICENSE copyright line now say `ken19990709` (the URLs
  already did).
- `.gitignore`/`MANIFEST.in` narrowed to exactly the two tracked `.zcode/`
  artifacts (per-directory re-include chain; plain `include` instead of
  `recursive-include .zcode *.md`), so future files under `.zcode/` can no
  longer leak into `git status` or the sdist.
- README (en/zh-CN) reworked toward conversion: slimmer first-screen demo,
  the 60-second start now uses `examples/q.sql` (new) with its
  bare-metal fallback moved directly under the main block, PostgreSQL
  preconditions noted, and the test count refreshed.
- AGENTS.md split into English (`AGENTS.md`) and Chinese
  (`AGENTS.zh-CN.md`) repo-root files; the init-distributed asset is their
  bilingual merged edition with each file's single language-switch line
  stripped (dead link inside packages and user projects);
  `MANIFEST.in` ships `AGENTS.zh-CN.md` and `examples/q.sql` so an
  unpacked sdist passes the packaging tests; regeneration command in
  CONTRIBUTING.md.
- Repository URLs corrected to the real GitHub repository
  (`ken19990709/pgtuner`).

## 0.11.0 — open-source readiness drop

The version published as the first public release. Everything below is relative to
0.8.0.

### Added

- Distribution layer: `pgtuner init` (project-level AGENTS.md / CLAUDE.md / skills /
  `.gitignore` + `.pgtuner` state skeleton) and `pgtuner init --global` (user-level
  skills ×3 + rule marker blocks ×3 + PATH wiring); idempotent reruns, marker-block
  merges.
- `doctor`: read-only environment pre-flight (deps / registry / ledger / templates /
  pgss / pgbench) with per-check fix pointers — the first command on a new machine.
- `selftest`: acceptance gate collection (fast lane needs no PostgreSQL;
  `--with-db` exercises the full chain).
- One-command dependency provisioning: `scripts/setup.sh` (repo `.venv`, editable
  install; `--system` apt build deps; `--pg <prefix>` builds PostgreSQL incl.
  pg_stat_statements; `--full` chains all three; every stage idempotent).
- Bilingual CLI help: Chinese stays the default, `PGTUNER_LANG=en` switches every
  `--help` to English (fail-open translation table + contract tests).
- `rebench --setup "SQL"`: before/after intervention measurement in one command.
- ZCode `PreToolUse` hook denying bare `psql`/`pgbench` Bash commands, with the
  `# allow-raw-psql` trailing-comment escape hatch.
- Portable instance data root (`PGTUNER_INSTANCE_DATA_ROOT`), verified by a
  machine-to-machine migration test.

### Changed

- Packaging: the GUC knowledge base and the init-distributed assets now ship
  inside the package (`pgtuner/data`, `pgtuner/assets`), so non-editable installs
  work; `packages.find` + package-data replace the single-package list.
- All code comments and docstrings are English; user-facing runtime messages stay
  zh-CN (English i18n on the roadmap).
- Documentation collapsed into README (en/zh-CN) + AGENTS.md + CLI `--help`;
  the former `docs/` tree was removed.

### Hardened (adversarial review round 1)

- `diagnose`/`hypo`/`advise` positional and `--query` inputs are now lexically
  split and must be exactly one statement — multi-statement strings can no longer
  smuggle writes past the read-only head filter — and data-modifying CTEs
  (`WITH d AS (DELETE …) SELECT …`) plus locked reads (`FOR UPDATE`) are rejected
  too; plain `EXPLAIN` (no ANALYZE) now runs on a session-level read-only
  connection.
- `lab release` / `diagnose --release` refuse to DROP databases that are neither
  clone-pool members nor clone-named; system databases are always refused;
  `instance destroy` requires interactive confirmation or `--yes`.
- Instance names are validated (`[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`) — they become
  data-directory path components.
- `rebench` restore is crash-safe: an on-disk journal (baseline + touched GUCs)
  survives kills and is auto-recovered on the next run; SIGINT/SIGTERM are
  delayed until the restore completes; the ledger's `restored` field now carries
  the verified per-cycle receipts instead of a literal.
- The evidence ledger isolates a truncated trailing line into `*.corrupt`
  (middle-line corruption still fails loudly) and its updates are atomic
  (tmp + fsync + rename); `init`'s user-file writes (~/.bashrc included) and
  `config.json` writes are atomic too; a corrupt `config.json` warns on stderr
  instead of silently behaving like an empty config.
- Common operational errors (unknown instance, connection failures) render as
  `Error: <classified hint>` instead of a Python traceback; `doctor` output is
  120 columns when piped so the fix column survives.
- The test suite is hermetic: running pytest from any cwd no longer plants
  `.pgtuner` state there.

### Hardened (adversarial review rounds 2–11)

- Read-only surfaces defend in depth at all five exits (`sql` / `diagnose` /
  `hypo` / `advise` / `planbench`): string-literal masking, a statement-head
  whitelist, and side-effect function blacklists stack on the head filter; the
  quoted-identifier call-form bypass is closed and a single `E''` escape
  scanner is shared everywhere. `planbench`'s plain mode additionally gates
  write statements (DML heads, masked write-smuggle keywords) behind an
  explicit `--db`/`--dsn` — EXPLAIN ANALYZE really executes them and the
  default target is the shared `postgres` database.
- `advise --queries-file` inputs pass the same read-only guard (queries are
  loaded before the check runs — nothing executes first, `--analyze`
  included).
- The crash-safe restore journal refuses a corrupted journal file instead of
  laundering the drift into a "restored" verdict.
- DSN dbnames are quoted as conninfo values, so unusual characters can no
  longer break or retarget the connection.
- CLI error rendering covers the OSError family and the psycopg2 exception
  family: one-line classified errors, no tracebacks.
- File-path options reject directories as usage errors.
- Destructive gates, per command: `instance destroy` requires interactive
  confirmation or `--yes`; `rebench` reports the expected slow-channel duration
  up front and proceeds only with `--yes`; `planbench` has no confirmation
  flow — its gate is that write statements (DML heads, data-modifying CTEs)
  require an explicit `--db`/`--dsn` target before anything runs.
- Execution-channel statement guards (round 5): the variant/flip-check
  measurement loops and the setup payload channels (`--variant`,
  `--flip-check`, `--setup`, `--setup-sql`) now run every statement through
  the side-effect function blacklist and reject COPY statement heads
  (COPY … TO PROGRAM is a shell, not DDL) before any connection or ledger
  side effect — those loops really execute server-side and used to bypass the
  read-only surfaces' own blacklists. The flip-check measurement query
  additionally gets the masked write-smuggle check; overt DDL/DML heads stay
  allowed on the payload channels (creating tables is their day job).
- Side-effect function blacklists extended with the large-object write family
  and the operations family (round 5).
- Flip-check's plan_cache_mode candidates corrected to PostgreSQL's legal
  `force_custom_plan`/`force_generic_plan` values — the short names made the
  second mode fail on every run.
- Known boundary — a pre-existing database function that internally wraps a
  blacklisted call remains callable; the guards analyze the statement text,
  not pg_proc bodies (database content, not tool input — under superuser any
  client could call it anyway). The boundary equally covers two-phase
  function creation inside one invocation on the explicit `--db` write
  channel: `--variant "v1=CREATE FUNCTION f … AS $$ … EXECUTE
  <dynamically assembled statement> $$"` followed by the variant query
  `select f()` — the same dynamic-assembly construct the DO head is refused
  for, carried here by the explicit `--db`/`--dsn` authorization and the
  superuser context instead of a refusal.
- The test suite is green on Python 3.9 again.
- `PreToolUse` hook hardening: redirection prefixes, quote-splicing and
  backslash normalization, `su`-wrapper shapes, a nesting depth cap that
  fails closed, and line-continuation folding.
- DO-block guard (round 6): a DO block's dollar-quoted body is procedural code
  the server executes verbatim, not string data — yet the literal masker blanks
  dollar bodies by design, so `do $$ begin perform pg_read_file(…) end $$`
  passed the execution-channel guards on every payload surface
  (`--setup` / `--setup-sql` / SETUP_SQL / `--variant`) and really ran.
  DO-headed statements now re-scan the UNMASKED statement text against the
  analyze-face side-effect function blacklist (conservatively: a blacklisted
  name even inside a body string literal is refused — refuse beats release);
  clean DO seeds stay allowed. The write-smuggle bridge's regex fallback also
  regained the `update … set` branch it had silently lost in the round-5
  perf split.
- DO-block execution refused outright in the payload channels (round 7): the
  round-6 re-scan lost to dynamic name assembly — `do $do$ begin execute
  'select lo_cre' || 'at(-1)'; end $do$` (likewise `format(...)` and
  `chr()||chr()` splicing) never shows a blacklisted word contiguously, and an
  assembled COPY TO PROGRAM variant demonstrably executed in the postgres
  server process (RCE). DO-headed statements are now refused on
  `rebench --setup` / `lab make-tpl` / `lab bootstrap --setup-sql` /
  `planbench --variant` regardless of body content — including clean seeds
  (split them into plain DDL/DML or run them by hand in a clone; the refusal
  message says so). Leading comments before the head no longer dodge the
  check either (`/* seed */ do $$…$$`), and the packaged
  `assets/template_setup.sql` is verified free of DO statements so the
  default bootstrap channel is unaffected. No benchmark subcommand names
  changed.
- Side-effect blacklist expanded (round 7): index-maintenance,
  replication-slot, WAL, stats-reset and directory-listing families.
- `PreToolUse` hook (round 7): quote-aware tokenization, `ProxyCommand`
  coverage, the `{}` find fix and `user@host` stripping.
- Write-smuggle matcher (diagnose.py, round 7): the substring gate probes
  casefold-equivalent keywords, keeping the matcher honest for mixed-case
  smuggling (`re.I` folds `ſ` onto `s`; plain `lower()` does not).
- Side-effect blacklist (round 8): systematic pg_catalog sweep — stats
  import/clear + collation import families; a snapshot contract test pins
  future re-audit.
- `PreToolUse` hook (round 8): fixes for the `=`-short-circuit, the quoted
  `-o` value, and ANSI-C `$'…'` decode before matching; the container-exec
  boundary is declared.
- Known-boundary naming (round 8): two-phase function creation —
  `--variant "v1=CREATE FUNCTION … AS $$ … EXECUTE <dynamically assembled
  statement> $$"` plus the variant query `select f()` — is now named as part
  of the pre-stored-function boundary (guard docstring + the entry above).
  No benchmark subcommand names changed.
- Side-effect blacklist (round 9): second sweep, this time inverted against the
  full function catalog — the catalog is diffed against the blacklist instead of
  hunting families one by one, and every surviving write-capable gap is closed:
  data-checksums toggles, replication-slot copy twins, memory-context and
  server-log dump functions, WAL snapshot writers, transaction-id allocators,
  and the legacy adminpack file-writing names; the inverted-audit contract test
  is updated to pin the new coverage. No benchmark subcommand names changed.
- `PreToolUse` hook join-semantics fixes (round 9): the remote argv of
  eval/local-ssh shapes is joined back into one string and re-scanned — a
  payload split across argv words no longer slips past token-by-token matching;
  ANSI `\cX` control-character decoding now truncates at NUL the way the shell
  does; `env -S` split-string payloads are reassembled before matching; and
  `ProxyCommand` values get ANSI decoding too. No benchmark subcommand names
  changed.
- Clone timestamp determinism (round 9): clone registration captures a single
  clock read reused by both clone_meta.created_at and the clone audit entry's
  ts — two independent reads could straddle a whole-second boundary and let the
  audit ts drift off created_at (an occasionally flaky ownership-audit test);
  equality now holds by construction.
- Query-string executor category (round 10): ts_stat and the query_to_xml/
  cursor_to_xml families join both blacklists — a literal inner query is
  masked as data (lexically invisible), and SPI read_only does not stop
  function side effects (live-verified via ts_stat('select pg_stat_reset()')
  on both read-only surfaces). No benchmark subcommand names changed.
- `PreToolUse` hook (round 10): local-ssh join stops option extraction at
  the first command word; `env -S` values join their operands before
  rescanning; `watch`/GNU `parallel` payloads are joined and rescanned;
  `\cX` decodes by the first UTF-8 byte like bash. No benchmark subcommand
  names changed.
- `PreToolUse` hook (round 11, final): `find -exec` clauses join-rescan
  watch/parallel/env payloads; ssh/sudo value-option tables completed
  (`-c/-m/-Q/-O/-w`, `-g/-p/...`); stdin-fed bare shells (`<<<` herestrings
  and pipe-left segments) are rescanned; systemd-run/gdb wrappers and
  ash/busybox shells pinned. No benchmark subcommand names changed.

## 0.8.0

- Metric self-checks, six new checks, lock-incident / cross-build-diff task
  profiles for the internal agent benchmark; partial-index candidates and
  whole-library dedup in the advisory funnel.

## 0.6.x — 0.7.2

- The probing loop collapsed into guided commands (`advise` / `hypo` /
  `statements` / `diagnose` / `render` / `config`); self-hosted agent benchmark
  suite with a 6-point grader; restart-level A/B channel with traceless restore.

## 0.4.0 — 0.5

- Toolset extensions: instance registry, template clone pool, evidence ledger,
  `planbench`, `watch`, read-only `sql`/`schema` exits; AGENTS.md iron rules.

## ≤ 0.3.0

- The original core: hardware probe → input assembly → knowledge-driven GUC
  recommendation (`probe` / `recommend` / `check` / `knowledge`), 57 GUCs × 109
  sourced formulas covering PostgreSQL 13–18.
