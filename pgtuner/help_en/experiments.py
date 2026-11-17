"""English help table: experiment channels — bench/rebench/planbench/diagnose/
advise/hypo/statements/sql/schema/watch/results (translations of the Chinese
docstrings/help= values in pgtuner/cli.py; applied by pgtuner/i18n.py when
PGTUNER_LANG=en)."""

HELP = {
    ("advise",): """Index advice (transparent funnel): heuristic candidates (with evidence) → hypo estimation-based evaluation → ranking + verification prescription.

    advise does not endorse performance: estimates only screen candidates; conclusions follow the prescription through planbench/rebench for ledger evidence.""",
    ("advise", "--query"): "Query to optimize (repeatable)",
    ("advise", "--queries-file"): "Query file (split on semicolons)",
    ("advise", "--from-statements"): "Pull the top N statements of the real workload from pg_stat_statements as the query set (requires it to be already enabled; normalized text with $n placeholders → only candidates and a prescription are produced, verify after grounding them as concrete queries)",
    ("advise", "--dsn"): "libpq connection string (either this or --instance)",
    ("advise", "--instance"): "pgtuner instance name",
    ("advise", "--db"): "Target database (a clone recommended)",
    ("advise", "--analyze"): "Use EXPLAIN ANALYZE in hypo evaluation (executes the statements)",
    ("advise", "--format"): "json = pure JSON on stdout (machine-readable); text = terminal table",

    ("bench",): """pgbench candidate closed loop: observe → recommend → run each candidate with PGOPTIONS injection → TPS/latency comparison table (server configuration untouched).""",
    ("bench", "--dsn"): "libpq connection string; defaults to PGHOST/PGPORT/PGDATABASE or /tmp:5432/bench_new",
    ("bench", "--knowledge"): "Knowledge-base JSON path",
    ("bench", "--candidates"): "Candidate set: simple = baseline + recommended + work_mem×{0.5,2} single-variable perturbation; work_mem/rpc/parallel = add that family's variants on top of the recommended combination; all = all variants",
    ("bench", "--duration"): "pgbench duration per candidate (seconds)",
    ("bench", "--clients"): "pgbench client count",
    ("bench", "--pgbench"): "pgbench executable path; resolved automatically when omitted: instance bindir (preferred) > PATH",
    ("bench", "--init"): "CREATE DATABASE automatically when the database is missing; if pgbench_accounts is absent, initialize first with pgbench -i -s 10",
    ("bench", "--workload"): "Workload type (affects recommended values)",
    ("bench", "--risk-appetite"): "Trade-off level used when the cost models disagree",
    ("bench", "--disk-type"): "Override the probed disk type",
    ("bench", "--bench-path"): "Directory where disk micro-benchmark files are written",
    ("bench", "--instance"): "pgtuner instance name (dsn resolved automatically and recorded in the ledger; either this or --dsn)",
    ("bench", "--db"): "Target database on the instance (effective with --instance; defaults to bench_new)",
    ("bench", "--calibrate"): "N: skip the candidate comparison and only run baseline N times to record the noise floor (TPS coefficient of variation goes into the ledger; requires --instance)",

    ("diagnose",): """One-shot slow-query diagnosis: baseline plan + root-cause clues + hypothetical-index candidate ranking + (--verify) greedy index creation + measured before/after A/B.

    EXPLAIN → clues → hypo estimation comparison → ledger archiving, all inside one process (v0.6: the multi-command round trips were cut); v0.6.1: --verify upgraded to greedy multi-index (multi-query trade-offs in one pass) + automatic conclusion write-back + --release return. Accepts single-statement SELECT/WITH only; the estimation basis does not endorse performance — conclusions come only from the prescription command's/--verify's ledger evidence.
    """,
    ("diagnose", "--file"): "Query file (split on semicolons; with multiple queries they are evaluated together over the candidate set)",
    ("diagnose", "--dsn"): "libpq connection string (either this or --instance)",
    ("diagnose", "--instance"): "pgtuner instance name",
    ("diagnose", "--db"): "Target database — run it on a clone (tables are briefly locked while hypo creates the indexes)",
    ("diagnose", "--analyze"): "EXPLAIN ANALYZE (executes the statements; plan-level actual values, TPS conclusions still require bench/rebench)",
    ("diagnose", "--label"): "Ledger label (results references may use diagnose:<label>)",
    ("diagnose", "--hypothesis"): "One-sentence hypothesis (recorded in the ledger; the conclusion must match it)",
    ("diagnose", "--verify"): "Verification loop: measured baseline → greedy real index creation (at most --verify-max-indexes of them) → re-measure → A/B diff with automatic conclusion write-back; one command yields all the ledger evidence (really creates indexes — use only on a clone; requires --instance)",
    ("diagnose", "--verify-runs"): "Measurement passes per query for --verify (median taken)",
    ("diagnose", "--verify-max-indexes"): "Maximum number of indexes the --verify greedy pass lands (the index trade-off for multi-query workloads, done in one command)",
    ("diagnose", "--weights"): "Comma-separated query weights (calls from pg_stat_statements recommended; aligned with query order, all 1 by default)",
    ("diagnose", "--release"): "Return the target database when finished (lab release; return the clone as soon as you are done with it — the evidence is already in the ledger) — use together with $() to capture the clone",
    ("diagnose", "--format"): "json = pure JSON on stdout (machine-readable); text = terminal table",

    ("hypo",): """Hypothetical-index evaluation: create the index inside a transaction → EXPLAIN → rollback, zero residue (takes the fast path automatically when hypopg is installed).

    Output = estimation basis (per-query baseline vs hypothetical-index cost/plan change); performance conclusions still go
    through planbench/rebench to produce ledger evidence.""",
    ("hypo", "--index"): "Hypothetical-index DDL, e.g. --index \"CREATE INDEX ON t(k)\" (repeatable; with multiple candidates the combination set is evaluated automatically)",
    ("hypo", "--query"): "Query to evaluate (repeatable)",
    ("hypo", "--queries-file"): "Query file (split on semicolons)",
    ("hypo", "--dsn"): "libpq connection string (either this or --instance)",
    ("hypo", "--instance"): "pgtuner instance name",
    ("hypo", "--db"): "Target database — run it on a clone (tables are briefly locked during index creation)",
    ("hypo", "--analyze"): "EXPLAIN ANALYZE (executes the statements; timings are plan-level actual values, TPS conclusions still require bench/rebench)",

    ("planbench",): """EXPLAIN workbench: plan capture / index, DDL and maintenance-intervention variant comparison (before/after in one command) / cost-estimation accuracy / flip detection.""",
    ("planbench", "--dsn"): "libpq connection string (either this or --instance)",
    ("planbench", "--instance"): "pgtuner instance name (defaults to the default instance)",
    ("planbench", "--db"): "Target database on the instance (defaults to postgres; must be given explicitly with --variant or when the file contains write statements (DML / data-modifying CTEs))",
    ("planbench", "--variant"): "NAME=DDL, repeatable — not just indexes: maintenance interventions go here too, e.g. --variant fix=\"ANALYZE events\" (baseline is placed first automatically; plan + median timing + per-node diff before and after the intervention, all from one command)",
    ("planbench", "--accuracy"): "Emit an estimation-accuracy table per query (descending by |log2 error|)",
    ("planbench", "--flip-check"): "Run plan flip detection on the first query",
    ("planbench", "--runs"): "Repetitions per query in variant mode (median taken)",
    ("planbench", "--flip-runs"): "Samples per mode for --flip-check (more samples make the flip verdict more stable)",
    ("planbench", "--label"): "Ledger label (results references may use planbench:<label>)",
    ("planbench", "--hypothesis"): "One-sentence hypothesis (recorded in the ledger; the conclusion must match it)",
    ("planbench", "--format"): "json = pure JSON on stdout (ledger payload fully faithful, human-oriented hints go to stderr); text = terminal table",

    ("rebench",): """GUC restart benchmarks + pure-measurement timing, dual channel (--queries-file: single-pass script mode for utility statements / median for SELECTs;
    --setup \"SQL\" interventions before/after with evidence from one command; --format json emits pure JSON).""",
    ("rebench", "--guc"): "k=v, repeatable: each --guc is one candidate",
    ("rebench", "--guc-file"): "One k=v per line (# comments); each line is one candidate",
    ("rebench", "--template"): "Clone the experiment database from a template (returned automatically when finished)",
    ("rebench", "--dbname"): "Target database (alias of --db; must be given explicitly with --queries-file — the tables are almost always in a clone, and the default bench_new is often the wrong one)",
    ("rebench", "--queries-file"): "Custom-workload SQL file (split on ;). Contains utility statements (DDL/ANALYZE etc.) → script mode: the whole file is executed once in order, timed statement by statement, no re-runs (safe for non-idempotent DDL); pure SELECTs → 2 warmup passes per query + EXPLAIN ANALYZE × --runs, median taken",
    ("rebench", "--setup"): "Intervention statements run in order between the before/after measurement rounds (repeatable, e.g. --setup \"ANALYZE events\"): a single pass, timed statement by statement. Requires --queries-file to be a pure-SELECT workload; no confirmation needed (writes land only in the experiment clone)",
    ("rebench", "--runs"): "Repetitions per query for a custom workload (median taken); ignored for pgbench workloads",
    ("rebench", "--pgbench"): "pgbench executable path; resolved automatically when omitted: instance bindir (preferred) > PATH",
    ("rebench", "--label"): "Ledger label (results references may use rebench:<label>)",
    ("rebench", "--hypothesis"): "One-sentence hypothesis (recorded in the ledger; the conclusion must match it)",
    ("rebench", "--format"): "json = pure JSON on stdout (ledger payload fully faithful, human-oriented hints go to stderr); text = terminal table",
    ("rebench", "--yes"): "Proceed after confirming the slow-channel duration (the agent must obtain the user's consent first)",

    ("results",): """Experiment ledger: list / show / diff / conclude (the ledger lives in .pgtuner/ledger/).""",
    ("results", "conclude"): "Write the conclusion back (non-empty; the conclusion must stick to the evidence from results diff).",
    ("results", "diff"): """Cross-experiment comparison: fingerprint gate (debug×release rejected) + noise-floor significance verdict.

    Arguments may be ledger ids or labels (\"kind:label\" takes the latest of that kind; a bare label must be unique).""",
    ("results", "diff", "--format"): "json = pure JSON on stdout (verdict/metrics fully faithful); text = terminal table",
    ("results", "list"): "Recent experiment list: id/kind/label/build/conclusion summary (the id is what diff/show/conclude reference).",
    ("results", "list", "--kind"): "Filter by kind: bench/planbench/rebench/diagnose/calibration",
    ("results", "list", "--contains"): "Filter by label substring",
    ("results", "list", "--last"): "The last N entries",
    ("results", "list", "--format"): "json = a pure JSON array on stdout (for agent pipelines to consume)",
    ("results", "show"): "Full JSON of one experiment: payload/metrics/conclusion fully faithful (this is where to look for conclusion evidence).",
    ("results", "show", "--format"): "Output is always JSON (--format json can be written explicitly; agent-pipeline friendly)",

    ("schema",): """In-database introspection: table listing / full single-table picture (columns/indexes/constraints/triggers/statistics/partitions/DDL rebuild). Read-only.""",
    ("schema", "describe"): "Full single-table picture: overview/columns/indexes/constraints/triggers/partitions/view definitions (object names need no quotes, matched as-is).",
    ("schema", "describe", "--dsn"): "libpq connection string (either this or --instance)",
    ("schema", "describe", "--instance"): "pgtuner instance name (defaults to the default instance)",
    ("schema", "describe", "--db"): "Defaults to postgres",
    ("schema", "describe", "--ddl"): "Also emit semantically equivalent DDL rebuilt from the catalogs (see the output header note for the caveats)",
    ("schema", "describe", "--format"): "json = pure JSON on stdout (machine-readable); text = compact text",
    ("schema", "tables"): "User-object listing (tables/partitioned tables/views/materialized views): row counts by two measures + sizes by two measures + comments.",
    ("schema", "tables", "--dsn"): "libpq connection string (either this or --instance)",
    ("schema", "tables", "--instance"): "pgtuner instance name (defaults to the default instance)",
    ("schema", "tables", "--db"): "Defaults to postgres",
    ("schema", "tables", "--schema"): "List only this schema (default = all non-system schemas)",
    ("schema", "tables", "--format"): "json = pure JSON on stdout (machine-readable); text = compact text",

    ("sql",): """Read-only SQL execution entry (functional checks / data inspection). One statement at a time; no timings printed (iron rule 1: performance evidence goes through bench/rebench).

    Read-only is enforced at the database layer (writes/DDL/nextval fail outright); write needs go through a lab clone.
    Note: SELECT also advances pg_stat counters — point pre-benchmark diagnostics at a clone.
    QUERY is read from stdin when omitted or \"-\".""",
    ("sql", "--dsn"): "libpq connection string (either this or --instance)",
    ("sql", "--instance"): "pgtuner instance name",
    ("sql", "--db"): "Target database on the instance (defaults to postgres; one database per PG connection)",
    ("sql", "--max-rows"): "Row limit (over it, rows are truncated and flagged — no full fetch, no COUNT)",
    ("sql", "--timeout"): "statement_timeout in seconds (runaway-query protection)",
    ("sql", "--format"): "json = machine-readable, fully faithful; text = terminal table (over-wide cells truncated)",

    ("statements",): """pg_stat_statements top queries: real-workload hot spots (read-only counters, no statements executed).""",
    ("statements", "--dsn"): "libpq connection string (either this or --instance)",
    ("statements", "--instance"): "pgtuner instance name",
    ("statements", "--db"): "Target database on the instance (defaults to postgres; statistics are per database)",
    ("statements", "--sort"): "total = total time, mean = mean time, calls = call count, temp = temporary block writes (a spilling signal)",
    ("statements", "--min-calls"): "Filter out statements with too few calls",
    ("statements", "--setup"): "Enable pg_stat_statements in one step (requires --instance; the instance must be restarted — passing this flag explicitly counts as the confirmation; refused while client backends are active, --force overrides); external instances only get manual instructions",
    ("statements", "--force"): "With --setup, force the restart despite active backends (interrupts them)",
    ("statements", "--format"): "json = machine-readable (pure JSON on stdout)",

    ("watch",): """Live status dashboard: instance overview + top active sessions (read-only, single connection).""",
    ("watch", "--dsn"): "libpq connection string (either this or --instance)",
    ("watch", "--instance"): "pgtuner instance name",
    ("watch", "--db"): "Defaults to postgres",
    ("watch", "--duration"): "Seconds; runs indefinitely by default",
    ("watch", "--snapshot"): "Non-live mode: take 3 frames and print every frame's text (for agents/CI)",
}
