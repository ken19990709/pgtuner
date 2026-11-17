"""English help table: root + instance + lab + config (translations of the Chinese
docstrings/help= values in pgtuner/cli.py; applied by pgtuner/i18n.py when
PGTUNER_LANG=en)."""

HELP = {
    (): """pgtuner — the PostgreSQL experiment workbench for agents (performance evidence goes through the ledger; bare psql timing is forbidden).

    The three-command probe loop (set the default instance once: pgtuner config set default-instance <N>):

    \b
      db=$(pgtuner lab clone --template <template>)   # stdout is the db name only; safe for $()
      <run workload / warm-up>                        # python3 ... "$db" / pgtuner bench
      pgtuner diagnose --file q.sql --db "$db" --verify --release

    \b
    --verify: measure the baseline → greedily build indexes (at most 3) → re-measure → A/B diff, conclusions backfilled automatically
    --release: return the db when done (evidence is already in the ledger; the clone goes back to the pool)
    """,
    ("config",): "Deployment-level defaults (.pgtuner/config.json; currently: default-instance — data-plane commands omit --instance).",
    ("config", "set"): "Set deployment-level defaults (default-instance: subsequent commands / lab clone can omit the instance name).",
    ("config", "show"): "Show the current deployment-level defaults (also displayed when the PGTUNER_INSTANCE env var overrides them).",
    ("instance",): "Instance registration and lifecycle (L0 tier: registration-based / managed initdb via create).",
    ("instance", "create"): "Managed creation: initdb (trust / local socket) → start → register. Data directory /tmp/pgtuner-instances/<name>.",
    ("instance", "create", "--port"): "Auto-assigned starting from 5433 by default",
    ("instance", "destroy"): "Stop and delete a managed instance (data directory + clone records removed together); an externally registered instance is only unregistered, requires --force.",
    ("instance", "destroy", "--force"): "Force-unregister a registered instance (its data directory is not deleted)",
    ("instance", "destroy", "--yes"): "Confirm destruction (required in non-interactive/agent contexts; a managed instance's data directory is deleted irreversibly)",
    ("instance", "dsn"): "Print the libpq connection string of a database on this instance (host=/tmp port=... dbname=...).",
    ("instance", "list"): "Registry listing: version / build (debug|release) / port / running state / managed or not.",
    ("instance", "register"): "Register an existing installation (bindir must contain pg_config/initdb etc.).",
    ("instance", "register", "--port"): "Port this instance listens on (socket is fixed at /tmp)",
    ("instance", "start"): "Start a stopped managed instance (pg_ctl start, idempotent; externally registered instances must be started on their own).",
    ("instance", "status"): "Single-instance overview: version / build / running state / DSN / data directory.",
    ("instance", "stop"): "Stop an instance (pg_ctl stop, idempotent; externally registered instances must be stopped on their own).",
    ("instance", "stop", "--mode"): "fast=refuse new connections and let active transactions finish; immediate=interrupt at once (next startup goes through crash recovery)",
    ("lab",): "Template clone pool: get a disposable experiment database within seconds (TEMPLATE clone, release when done, zero residue).",
    ("lab", "audit"): "Clone-pool audit: who cloned / released / attempted to release whose clone, and when (tail of <registry>.audit.jsonl).",
    ("lab", "audit", "--instance"): "Only this instance (default: all)",
    ("lab", "audit", "--last"): "Last N entries (default: 20)",
    ("lab", "bootstrap"): """One command gets a (new) instance ready for work: preload pgss (+ create the extension in postgres) → standard template (sealed) → default instance.

    Every step is idempotent; rerunning only fills in what is missing. When pgss is not yet preloaded, the instance is restarted with stop/start (~10s) — running this command explicitly counts as the manual confirmation (aligns with iron rule 4); the extension is created before sealing, so clones inherit pgss via TEMPLATE. Run pgtuner doctor afterwards to re-check.""",
    ("lab", "bootstrap", "--instance"): "pgtuner instance name (defaults to the default instance)",
    ("lab", "bootstrap", "--template"): "Standard template name (skipped if it already exists, idempotent)",
    ("lab", "bootstrap", "--scale"): "Scale for pgbench -i",
    ("lab", "bootstrap", "--setup-sql"): "Template payload SQL (default: the packaged template_setup.sql)",
    ("lab", "bootstrap", "--set-default"): "Also force the default instance to this one when it is already set to another instance (always set automatically when unset)",
    ("lab", "clone"): """Clone a disposable experiment database from a template: stdout prints only the db name (safe to capture with $(...)); usage hints go to stderr.

    The instance name can be omitted (v0.6.1): defaults come from config set default-instance / PGTUNER_INSTANCE.""",
    ("lab", "clone", "--instance"): "Equivalent to the positional argument (mistake-proofing: in S5-A1 agents were observed to naturally write --instance)",
    ("lab", "list"): "Alias of lab stats (in S5 both agents tried `lab list` first — the intuitive name).",
    ("lab", "make-tpl"): "Create a template database: CREATE DATABASE → run setup_sql → disallow connections (clonable at any time). The instance name can be omitted (deployment-level default).",
    ("lab", "make-tpl", "--instance"): "pgtuner instance name (defaults to the default instance)",
    ("lab", "release"): """Return a clone (terminate + DROP, zero residue). Release by a non-owner or of a non-clone is refused by default (ownership guard, tool-w1).

    Only clone-pool members are returned; unregistered databases (including clone-shaped orphans) go through lab sweep, genuinely mistaken cleanup needs --force, and system databases are refused under any flag.""",
    ("lab", "release", "--force"): "Release someone else's clone beyond your ownership (emergencies / genuinely mistaken takes only; the action lands in the audit log with a forced flag)",
    ("lab", "stats"): "Active clone listing (instance × clone db @ owner; an empty pool reports no residue).",
    ("lab", "sweep"): """Clean up clone residue: databases registered in the clone pool + unregistered <template>_c<8-char hex> orphan databases (dry-run by default).

    Never touches system databases / sealed templates / ordinary databases not named like clones; DROP only with --yes.
    """,
    ("lab", "sweep", "--instance"): "Sweep only this instance (default = all registered instances)",
    ("lab", "sweep", "--older-than"): "Only clean databases that have reached the age limit (≥ N minutes); creation time ≈ mtime of the base/<oid> directory",
    ("lab", "sweep", "--yes"): "Actually perform the cleanup (default is dry-run: list only, touch nothing)",
    ("lab", "templates"): "List template databases on the instance (databases with datallowconn=false). The instance name can be omitted (deployment-level default).",
    ("lab", "templates", "--instance"): "pgtuner instance name (defaults to the default instance)",
}
