"""pgtuner — an Agent × PostgreSQL probing workbench.

Four foundation layers: probe / recommend+list / check / bench (pgbench
candidate closed loop); toolset extensions (L0-L8): instance management /
lab template-clone pool / rebench slow channel / planbench plan comparison /
results experiment ledger / watch live dashboard / selftest acceptance gates;
initialization layer (v0.9.0): scripts/setup.sh one-command dependencies /
lab bootstrap one-command instance provisioning / doctor environment pre-check;
distribution layer (v0.10.0): pgtuner init installs the knowledge layer into
any project (or user-globally), with the state anchor following the project root;
user-level full bundle + portability gates (v0.11.0): init --global seven-pack
(skills×3 + rules×3 + PATH), hygiene zero-hardcoding scan;
measurement-plane turn slimming (v0.11.0): rebench --setup before/after
intervention produces evidence in one command, rebench/planbench/results diff
--format json.
information-architecture convergence (v0.11.0): help as the single source of
truth (completing subcommand docstrings and prerequisite hints), AGENTS.md/
SKILL.md slimmed into pure hooks, ZCode PreToolUse hook hard-blocking raw
psql/pgbench.
Unified documentation: README.md + README.zh-CN.md (one language each); agent iron
rules: AGENTS.md (English) + AGENTS.zh-CN.md (Chinese) at the repo root, with
pgtuner/assets/AGENTS.md as the bilingual merged edition (language-switch lines
stripped) that `pgtuner init` installs into user projects. CLI help is bilingual
too: PGTUNER_LANG=en switches --help to English.
"""

__version__ = "0.11.0"
