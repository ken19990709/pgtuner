"""English help table: setup & GUC-tuning lanes — init/doctor/selftest/knowledge/
probe/recommend/check (+ hidden benchmark group) (translations of the Chinese
docstrings/help= values in pgtuner/cli.py; applied by pgtuner/i18n.py when
PGTUNER_LANG=en)."""

HELP = {
    ("benchmark", "setup"): """Prepare a benchmark run: clone the template, write task materials into a run dir,
    stamp the ledger, and print the full mission packet with concrete values.

    Hand the printed packet verbatim to the agent under test. Grading entry point:
    pgtuner benchmark grade <task_id> (defaults to the most recent run).""",
    ("check",): "Fine-tune from measured signals: read-only observation collection from a live database → inject for evaluation → signal panel + recommendations with a diff against current values + unlocked-rules list.",
    ("check", "--dsn"): "libpq connection string (e.g. 'host=/tmp port=5432 dbname=bench_new'); defaults to PGHOST/PGPORT/PGDATABASE or /tmp:5432/bench_new",
    ("check", "--knowledge"): "Knowledge base JSON path",
    ("check", "--workload"): "Workload type",
    ("check", "--risk-appetite"): "Trade-off level used when cost models disagree",
    ("check", "--disk-type"): "Override the probed disk type",
    ("check", "--bench-path"): "Directory where disk micro-benchmark test files are written",
    ("check", "--data-dir"): "Data directory path (fallback for statvfs free-space probing; in live-DB mode the measured show data_directory takes precedence)",
    ("check", "--wal-sample-seconds"): "WAL-rate sampling window in seconds (under mixed load a short window can easily sample a stalled stretch)",
    ("check", "-o"): "Write the conf snippet to a file (reuses recommend's rendering: restart-required group first)",
    ("check", "--all-origins"): "Expand all competing formulas (including skipped rules and the reasons)",
    ("doctor",): """Environment health check (read-only, zero side effects): dependencies / instance registry / ledger / default instance / templates / pgss / pgbench —
    ❌ = blocking (exit 1), ⚠️ = degraded but usable; see the "Fix" column for repair commands (scripts/setup.sh / lab bootstrap).""",
    ("doctor", "--instance"): "Health-check a specific instance (defaults to the default instance: config set / PGTUNER_INSTANCE)",
    ("init",): """Install pgtuner into any project: AGENTS.md / CLAUDE.md / skills×2 / .gitignore / state skeleton.

    Idempotent; re-running only fills in what is missing. Once done, the project's agents have pgtuner knowledge, and the experiment ledger / instance registry land in
    <project>/.pgtuner (state anchors follow the project, no ledger cross-talk with the tool repo); run pgtuner doctor in that directory to re-check.""",
    ("init", "--global"): "User-level: install skills to ~/.zcode / ~/.claude / ~/.codex, plus rule marker blocks and PATH wiring (applies to all projects)",
    ("init", "--force"): "Overwrite existing skill / AGENTS marker blocks whose content differs",
    ("knowledge",): """Browse the GUC knowledge base (filter by NAME substring, --pain filter).

    Called `list` before v0.7.1 (a v0.1 legacy name) — a top-level list is semantically vague and naturally collides with the customary sub-group names
    (`lab list` etc.); renamed to be self-describing (same root as --knowledge).""",
    ("knowledge", "--pain"): "Filter by pain level",
    ("probe",): "Hardware probe report (each signal + confidence; does not run pg_test_fsync).",
    ("probe", "--knowledge"): "Knowledge base JSON path",
    ("probe", "--bench-path"): "Directory where disk micro-benchmark test files are written",
    ("recommend",): "Probe → assemble → evaluate → report.",
    ("recommend", "--knowledge"): "Knowledge base JSON path",
    ("recommend", "--workload"): "Workload type",
    ("recommend", "--max-connections"): "Maximum number of connections (a user-set value, used as formula input)",
    ("recommend", "--db-size"): "Database size in GB (default = total_ram_gb)",
    ("recommend", "--risk-appetite"): "Trade-off level used when cost models disagree",
    ("recommend", "--disk-type"): "Override the probed disk type",
    ("recommend", "--pg-version"): "PostgreSQL major version (default 18, i.e. the knowledge-base ceiling)",
    ("recommend", "--bench-path"): "Directory where disk micro-benchmark test files are written",
    ("recommend", "--data-dir"): "Data directory path (statvfs probes free disk space for the max_wal_size upper bound)",
    ("recommend", "-o"): "Write the conf snippet to a file (default: print to stdout)",
    ("recommend", "--all-origins"): "Expand all competing formulas (including skipped rules and the reasons)",
    ("selftest",): "Acceptance-gate suite: fast requires no PG; --with-db exercises the full chain.",
    ("selftest", "--with-db"): "Additional DB gates (release install dir, PGTUNER_RELEASE_BINDIR can override): clone-pool stress / watch / slow-lane restore",
    ("selftest", "--cycles"): "Number of clone-pool checkout/return cycles",
}
