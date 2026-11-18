# AGENTS.md — Iron rules for PostgreSQL experiments in this repo

**English** | [简体中文](AGENTS.zh-CN.md)

> Applies to every AI coding agent (Claude Code / ZCode / Cursor, …). **Command usage has
> exactly one source of truth: `pgtuner --help` and `pgtuner <cmd> --help`** — it ships with
> the binary and never goes stale (`PGTUNER_LANG=en` switches help to English). This file
> sets rules only and does not restate usage (the human-readable README may lag behind
> help).

## The five iron rules

1. **Performance experiments go through pgtuner commands only** (bench / rebench /
   planbench / lab); never draw performance conclusions from raw psql/pgbench timing
   (psql `\timing` is network- and terminal-noisy — not evidence). Functional checks and
   data peeks use `pgtuner sql` (read-only enforced, never prints timing — the exit itself
   leaves no door for violations).
2. **Every experiment carries an experiment-id**: generated and ledgered automatically
   (`.pgtuner/ledger/`); bypassing the ledger with hand-saved txt files = the experiment
   is void.
3. **Conclusions must cite ledger evidence**: the output of `pgtuner results diff <a> <b>`
   (or its citation) — never vibes. Differences below the noise floor must be stated as
   "not significant".
4. **The slow channel (rebench, restart-level) needs human confirmation first**: expected
   duration is reported at command start; confirm, then add `--yes`; after the run, verify
   the output contains `restored: True`.
5. **Performance conclusions recognize release instances only** (build_type=release in
   `instance list`); debug instances are for functional verification — their TPS numbers
   are banned from conclusions.

## The standard probing loop

Register a one-line hypothesis → pick the channel for the scenario (workload hotspots →
statements / session-level GUC → bench / restart-level GUC · pure timing · before-after
intervention → rebench / indexes & SQL shapes → planbench / hypothetical indexes → hypo /
one-shot slow-query loop → diagnose) → `lab clone` for a disposable db → run (ledgered
automatically) → `results diff` → backfill with `results conclude`. Stop once verified — no
gold-plating iterations. Destructive probes (triggers / DDL) run **only** in a clone, never
on template dbs or other people's dbs.

On a new machine or session, run `pgtuner doctor` first (start working when the ❌ count is
zero); to save round trips, chain related commands with `&&` in a single Bash call and
capture outputs with `$()`.
