"""Adversarial-review R3 hardening regressions (cli.py layer only).

Pinned here (all unit / CliRunner level — no real instance, ledger or .pgtuner
state; the DB boundary is monkeypatched so assertions land exactly on the guard):

  - D2: advise --queries-file queries pass the read-only guard (file loading
    moved BEFORE the guard loop; a smuggled pg_terminate_backend used to really
    execute under --analyze);
  - D4: the sql surface rejects write-smuggled statements (WITH … DELETE …
    RETURNING) at the TOOL layer, and hypo/advise --query get the full
    diagnose-strength guard (head whitelist + masked smuggle + side effects);
    pg_sleep stays allowed on the sql surface (statement_timeout caps it) and
    banned on the analyze surfaces;
  - C-F4: a directory passed to a file option is a click UsageError
    (dir_okay=False), the whole OSError family renders via _CliGroup.invoke as
    a one-line Error (IsADirectoryError used to dump a traceback), and the
    psycopg2 exception family (matched by module name, no import needed)
    renders as a driver error while unrelated exceptions still propagate.

Round-4 additions (same hermetic style):

  - D-F3: planbench plain mode gates write statements (DML heads + masked
    write-smuggle keywords) behind an explicit --db/--dsn — EXPLAIN ANALYZE
    really executes them and the default target is the shared postgres
    database (the gate mirrors the --variant one);
  - D-F2: advise --from-statements + --analyze passes the pulled pgss queries
    through the read-only guard (a no-placeholder
    'select pg_terminate_backend(pid) …' really killed sessions); the
    non-analyze exemption and the placeholder-set boundary (advise runs no
    EXPLAIN there) stay open by design;
  - B-MINOR-1: a missing psycopg2 module (optional [db] extra) renders as a
    one-line install hint instead of a 12-line ModuleNotFoundError traceback.

Round-5 additions (D5-F3, same hermetic style):

  - Execution-channel statement guards: the planbench variant/flip-check
    measurement loops and the setup payload channels (rebench --setup,
    lab make-tpl SETUP_SQL, lab bootstrap --setup-sql) really execute
    server-side and used to bypass every tool-layer blacklist — a variant
    'COPY … TO PROGRAM' or a flip-check 'select pg_read_file(...)' ran for
    real while the same statement was refused in plain mode. Now every such
    statement passes the side-effect blacklist + COPY-head rejection before
    any connection/ledger side effect; the flip-check query additionally gets
    the masked write-smuggle check (measurement face), while overt DDL/DML
    heads stay allowed on the payload channels (their day job);
  - The write-smuggle matcher is reached through the diagnose contract
    (write_smuggle_search, perf-gated; regex fallback until it lands).

Round-6 additions (C6-F3/C6-F5, same hermetic style):

  - DO-block guard: a DO block's dollar-quoted body is executed procedural
    code, not string data — the literal masker blanks dollar bodies by
    design, so 'do $$ begin perform pg_read_file(…) end $$' passed the
    execution-channel guards and really ran on the server. Round 6 answered
    with an unmasked-text re-scan against the side-effect blacklist;

  - The write-smuggle bridge's regex fallback keeps the update…set bridge
    it had silently lost in the round-5 perf split (parity with
    diagnose.write_smuggle_search pinned at unit level).

Round-7 additions (D7-F1/C7-F5, same hermetic style):

  - DO blocks are refused OUTRIGHT on every payload channel: the round-6
    re-scan lost to dynamic name assembly — 'do $do$ begin execute
    ''select lo_cre'' || ''at(-1)''; end $do$' (format(...) / chr()||chr()
    splicing alike) never shows a blacklisted word contiguously, and an
    assembled COPY TO PROGRAM variant demonstrably executed in the postgres
    server process. Legit DO seeds are refused too (channel design
    decision — split into plain DDL/DML or run by hand in a clone); the
    refusal message carries that guidance;
  - Leading comments no longer dodge the head check: the exec guard takes
    the statement head after skipping leading comments itself (C7-F5 —
    '/* seed */ do $$…$$' handed raw to the guard used to yield head '' );
  - The 'call' head stays allowed (declared boundary: guards analyze
    statement text, not pg_proc bodies) and the packaged
    assets/template_setup.sql is pinned free of DO statements so the
    default bootstrap channel is unaffected.
"""
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from pgtuner import cli


# ---------------------------- shared monkeypatch scaffolding ----------------------------

def _hermetic_target(monkeypatch):
    """Guarantee hermeticity even if a guard fails open: resolving a target must
    never reach the real default instance / registry."""
    def _boom(*a, **k):
        raise AssertionError("guard failed open: command proceeded past the guard")
    monkeypatch.setattr(cli, "_resolve_target", _boom)


def _patch_sql_db(monkeypatch, recorder):
    """sql surface: fake dsn + fake run_query; the guard must be the only judge."""
    monkeypatch.setattr(cli, "_resolve_target", lambda *a, **k: ("host=/tmp port=1 dbname=x", None))
    def _fake_run(dsn, stmt, max_rows=100, timeout_s=30.0):
        recorder.append(stmt)
        return SimpleNamespace(columns=[], rows=[], truncated=False)
    monkeypatch.setattr(cli.sqlcmd, "run_query", _fake_run)


def _patch_advise_db(monkeypatch, recorder):
    """advise surface: fake dsn + fake evaluate returning the minimal shape."""
    monkeypatch.setattr(cli, "_resolve_target", lambda *a, **k: ("host=/tmp port=1 dbname=x", None))
    def _fake_eval(dsn, queries, weights=None, analyze=False):
        recorder.append(list(queries))
        return {"placeholders": []}
    monkeypatch.setattr(cli.advise_mod, "evaluate", _fake_eval)
    monkeypatch.setattr(cli.advise_mod, "render_text", lambda result: "advise-ok")


# ---------------------------- D2: advise --queries-file guarded ----------------------------

def test_advise_queries_file_side_effect_smuggle_rejected(tmp_path, monkeypatch):
    """R3 D2 PoC: 'select pg_terminate_backend(pg_backend_pid()) …' in the
    queries file used to be appended AFTER the guard loop and really executed
    with --analyze. It must now be refused at the tool layer, before any DB."""
    evil = tmp_path / "evil.sql"
    evil.write_text(
        "select pg_terminate_backend(pg_backend_pid()) from b2_t where id=1;\n",
        encoding="utf-8")
    calls: list = []
    _patch_advise_db(monkeypatch, calls)
    r = CliRunner().invoke(cli.cli, ["advise", "--queries-file", str(evil), "--analyze"])
    assert r.exit_code != 0, r.output
    assert "拒绝副作用函数" in r.output and "pg_terminate_backend" in r.output
    assert calls == []                                    # never reached the (faked) DB boundary


def test_advise_queries_file_write_cte_rejected(tmp_path, monkeypatch):
    """File-borne data-modifying CTE (FOR UPDATE too) hits the smuggle check."""
    evil = tmp_path / "cte.sql"
    evil.write_text("with d as (delete from t returning *) select * from d;\n"
                    "select * from accounts for update;\n", encoding="utf-8")
    calls: list = []
    _patch_advise_db(monkeypatch, calls)
    r = CliRunner().invoke(cli.cli, ["advise", "--queries-file", str(evil)])
    assert r.exit_code != 0 and "写/锁定关键字" in r.output
    assert calls == []


def test_advise_queries_file_legal_passes_guard(tmp_path, monkeypatch):
    """Contralateral: a legit multi-query file reaches evaluate intact."""
    ok = tmp_path / "ok.sql"
    ok.write_text("WITH x AS (SELECT 1) SELECT * FROM x;\n"
                  "SELECT count(*) FROM events;\n", encoding="utf-8")
    calls: list = []
    _patch_advise_db(monkeypatch, calls)
    r = CliRunner().invoke(cli.cli, ["advise", "--queries-file", str(ok)])
    assert r.exit_code == 0, r.output
    assert calls == [["WITH x AS (SELECT 1) SELECT * FROM x", "SELECT count(*) FROM events"]]


def test_advise_from_statements_exemption_kept(monkeypatch):
    """--from-statements substitutes what ALREADY ran on the server (pgss text,
    e.g. real UPDATEs) after the guard — that exemption is by design and must
    survive the D2 fix (no over-blocking of the workload-hotspot channel)."""
    calls: list = []
    monkeypatch.setattr(cli, "_resolve_target", lambda *a, **k: ("host=/tmp port=1 dbname=x", None))
    monkeypatch.setattr(cli.statements_mod, "ensure_ready", lambda dsn: None)
    monkeypatch.setattr(cli.pg, "connect",
                        lambda dsn: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli.statements_mod, "top_queries",
                        lambda conn, sort, limit, min_calls: [
                            SimpleNamespace(query="UPDATE events SET k = 1 WHERE id = $1",
                                            calls=7)])
    _patch_advise_db(monkeypatch, calls)
    r = CliRunner().invoke(cli.cli, ["advise", "--from-statements", "5"])
    assert r.exit_code == 0, r.output
    assert calls == [["UPDATE events SET k = 1 WHERE id = $1"]]


# ---------------------------- D4: sql surface smuggle check ----------------------------

def test_sql_with_delete_cte_rejected_at_tool_layer(monkeypatch):
    """R3 D4: 'with x as (delete from t returning *) select *' has a whitelisted
    head (with) — it used to pass the tool layer and lean on the connection-level
    readonly alone. Now the masked write-smuggle check refuses it here."""
    seen: list = []
    _patch_sql_db(monkeypatch, seen)
    r = CliRunner().invoke(
        cli.cli, ["sql", "with x as (delete from t returning *) select * from x"])
    assert r.exit_code != 0, r.output
    assert "写/锁定关键字" in r.output and "delete from" in r.output
    assert seen == []                                      # refused BEFORE any execution attempt


def test_sql_legal_with_select_cte_still_passes(monkeypatch):
    seen: list = []
    _patch_sql_db(monkeypatch, seen)
    r = CliRunner().invoke(cli.cli, ["sql", "with x as (select 1) select * from x"])
    assert r.exit_code == 0, r.output
    assert seen == ["with x as (select 1) select * from x"]


def test_sql_write_keywords_in_string_literal_not_overblocked(monkeypatch):
    """Masking contract on the sql surface: 'delete from t' inside a WHERE
    literal is data, not a write (guards the guard against false positives)."""
    seen: list = []
    _patch_sql_db(monkeypatch, seen)
    q = "SELECT * FROM logs WHERE msg = 'delete from t values (1)'"
    r = CliRunner().invoke(cli.cli, ["sql", q])
    assert r.exit_code == 0, r.output
    assert seen == [q]


# ---------------------------- D4: hypo/advise --query full-strength guard ----------------------------

@pytest.mark.parametrize("argv,needle", [
    (["hypo", "--index", "CREATE INDEX ON t(k)",
      "--query", "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d"], "写/锁定关键字"),
    (["hypo", "--index", "CREATE INDEX ON t(k)", "--query", "UPDATE t SET k = 1"],
     "只接受只读查询头"),
    (["advise", "--query", "DELETE FROM accounts"], "只接受只读查询头"),
    (["advise", "--query", "SELECT * FROM accounts FOR UPDATE"], "写/锁定关键字"),
    (["advise", "--query",
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"], "拒绝副作用函数"),
])
def test_hypo_advise_query_dml_smuggle_rejected(argv, needle, monkeypatch):
    """R3 D4: --query DML used to be stopped only incidentally by the hypo
    transaction rollback; the analyze surfaces now carry the full
    diagnose-strength guard (head whitelist + masked smuggle + side effects)."""
    _hermetic_target(monkeypatch)                          # guards run BEFORE target resolution
    r = CliRunner().invoke(cli.cli, argv)
    assert r.exit_code != 0, (argv, r.output)
    assert needle in r.output, (argv, r.output)


def test_advise_query_legal_select_passes_guard(monkeypatch):
    calls: list = []
    _patch_advise_db(monkeypatch, calls)
    r = CliRunner().invoke(cli.cli, ["advise", "--query", "SELECT count(*) FROM events"])
    assert r.exit_code == 0, r.output
    assert calls == [["SELECT count(*) FROM events"]]


# ---------------------------- pg_sleep surface semantics ----------------------------

def test_pg_sleep_allowed_on_sql_denied_on_analyze(monkeypatch):
    """Surface split pinned: the sql surface allows pg_sleep (statement_timeout
    caps it there) while the EXPLAIN-ANALYZE surfaces keep refusing it."""
    seen: list = []
    _patch_sql_db(monkeypatch, seen)
    r = CliRunner().invoke(cli.cli, ["sql", "SELECT pg_sleep(0.1)"])
    assert r.exit_code == 0, r.output
    assert seen == ["SELECT pg_sleep(0.1)"]

    _hermetic_target(monkeypatch)
    for argv in (["advise", "--query", "SELECT pg_sleep(1)"],
                 ["hypo", "--index", "CREATE INDEX ON t(k)", "--query", "SELECT pg_sleep(1)"]):
        r = CliRunner().invoke(cli.cli, argv)
        assert r.exit_code != 0, (argv, r.output)
        assert "拒绝副作用函数" in r.output and "pg_sleep" in r.output, (argv, r.output)


# ---------------------------- C-F4: directory inputs + exception normalization ----------------------------

def test_directory_path_options_rejected_as_usage_error(tmp_path):
    """dir_okay=False on every file-type click.Path option: a directory must be
    refused by click parameter validation (exit 2 UsageError), never reach the
    command body where Path(...).read_text() used to raise IsADirectoryError."""
    d = tmp_path / "dir"
    d.mkdir()
    cases = [
        ["rebench", "--instance", "x", "--guc-file", str(d)],
        ["rebench", "--instance", "x", "--queries-file", str(d)],
        ["diagnose", "--file", str(d)],
        ["advise", "--queries-file", str(d)],
        ["hypo", "--queries-file", str(d)],
        ["planbench", str(d)],
    ]
    for argv in cases:
        r = CliRunner().invoke(cli.cli, argv)
        assert r.exit_code == 2, (argv, r.output)          # UsageError, not a crash
        assert "is a directory" in r.output, (argv, r.output)
        assert "Traceback" not in r.output, (argv, r.output)


def test_invoke_converts_isadirectoryerror_to_clean_error(monkeypatch):
    """Second safety net: an OSError sibling (IsADirectoryError) escaping a
    command body renders as a one-line ClickException via _CliGroup.invoke —
    the old (FileNotFoundError, PermissionError) list missed it (20-line
    traceback)."""
    monkeypatch.setattr(cli, "_resolve_target", lambda *a, **k: ("host=/tmp port=1 dbname=x", None))
    def _raise(dsn, stmt, max_rows=100, timeout_s=30.0):
        raise IsADirectoryError(21, "Is a directory", "/tmp/x")
    monkeypatch.setattr(cli.sqlcmd, "run_query", _raise)
    r = CliRunner().invoke(cli.cli, ["sql", "SELECT 1"])
    assert r.exit_code == 1, r.output
    assert "Error:" in r.output and "Is a directory" in r.output
    assert "Traceback" not in r.output


def test_invoke_renders_psycopg2_family_without_import(monkeypatch):
    """The psycopg2 exception family is matched by module NAME inside
    _CliGroup.invoke (the driver is optional; no top-level import) and renders
    as a driver error; unrelated exceptions keep propagating untouched."""

    class _FakePgError(Exception):
        pass
    _FakePgError.__module__ = "psycopg2"                   # exactly how psycopg2.Error reports

    monkeypatch.setattr(cli, "_resolve_target", lambda *a, **k: ("host=/tmp port=1 dbname=x", None))
    monkeypatch.setattr(cli.sqlcmd, "run_query",
                        lambda dsn, stmt, max_rows=100, timeout_s=30.0:
                        (_ for _ in ()).throw(_FakePgError("server closed the connection")))
    r = CliRunner().invoke(cli.cli, ["sql", "SELECT 1"])
    assert r.exit_code == 1, r.output
    assert "Error:" in r.output
    assert "PostgreSQL 驱动错误" in r.output
    assert "server closed the connection" in r.output       # original message preserved
    assert "Traceback" not in r.output

    class _Unrelated(Exception):
        pass

    monkeypatch.setattr(cli.sqlcmd, "run_query",
                        lambda dsn, stmt, max_rows=100, timeout_s=30.0:
                        (_ for _ in ()).throw(_Unrelated("keep propagating")))
    r2 = CliRunner().invoke(cli.cli, ["sql", "SELECT 1"])
    assert r2.exit_code != 0
    assert isinstance(r2.exception, _Unrelated)             # non-driver errors re-raise (no blanket swallow)


# ---------------------------- R4 D-F3: planbench plain-mode write gate ----------------------------

def test_planbench_plain_dml_without_db_rejected(tmp_path, monkeypatch):
    """R4 D-F3 PoC: a pure 'delete from x;' file without --db used to really
    execute on the shared postgres database (planbench.explain's head whitelist
    deliberately admits DML). The CLI gate must refuse BEFORE target resolution
    — no instance, no registry, no ledger entry."""
    q = tmp_path / "del.sql"
    q.write_text("delete from x;\n", encoding="utf-8")
    _hermetic_target(monkeypatch)                           # gate runs before _resolve_target
    r = CliRunner().invoke(cli.cli, ["planbench", str(q)])
    assert r.exit_code != 0, r.output
    assert "写语句对比请显式 --db <克隆库>" in r.output and "克隆库" in r.output
    assert "Traceback" not in r.output


def test_planbench_plain_write_cte_without_db_rejected(tmp_path, monkeypatch):
    """A whitelisted head (with) carrying a data-modifying CTE hits the same
    gate via the masked _WRITE_SMUGGLE check (same lexing as the sql surface)."""
    q = tmp_path / "cte.sql"
    q.write_text("with d as (delete from t returning *) select * from d;\n", encoding="utf-8")
    _hermetic_target(monkeypatch)
    r = CliRunner().invoke(cli.cli, ["planbench", str(q)])
    assert r.exit_code != 0, r.output
    assert "写/锁定关键字" in r.output and "delete from" in r.output
    assert "显式 --db" in r.output


def test_planbench_write_gate_passes_with_explicit_target(tmp_path, monkeypatch):
    """Contralateral: an explicit --db (or --dsn) is exactly what makes the
    write comparison legal — the gate opens and the run proceeds to (faked)
    EXPLAIN; write keywords inside string literals stay data (mask contract)."""
    from pgtuner import ledger as ledger_mod
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "l.jsonl")
    monkeypatch.setattr(cli, "_resolve_target",
                        lambda *a, **k: ("host=/tmp port=5 dbname=clone1", None))
    monkeypatch.setattr(cli, "_safe_fp", lambda *a, **k: None)
    monkeypatch.setattr(cli, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(cli.planbench_mod, "explain",
                        lambda dsn, q: {"Node Type": "ModifyTable", "Plan Rows": 1,
                                        "Actual Rows": 1, "Actual Total Time": 0.1,
                                        "Total Cost": 1.0})
    q = tmp_path / "del.sql"
    q.write_text("delete from x where note = 'delete from y';\n", encoding="utf-8")
    r = CliRunner().invoke(cli.cli, ["planbench", str(q), "--db", "clone1"])
    assert r.exit_code == 0, r.output
    # dsn-given form: same legality, checked straight on the gate helper
    cli._planbench_write_gate(["update t set k = 1"], db_given=False, dsn_given=True)    # no raise
    cli._planbench_write_gate(["select * from logs where msg = 'insert into t'"],
                              db_given=False, dsn_given=False)                           # literal = data


# ---------------------------- R4 D-F2: advise --from-statements under --analyze ----------------------------

def _patch_pgss(monkeypatch, queries):
    """advise --from-statements scaffolding: fake dsn + pgss top_queries."""
    monkeypatch.setattr(cli, "_resolve_target", lambda *a, **k: ("host=/tmp port=1 dbname=x", None))
    monkeypatch.setattr(cli.statements_mod, "ensure_ready", lambda dsn: None)
    monkeypatch.setattr(cli.pg, "connect", lambda dsn: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli.statements_mod, "top_queries",
                        lambda conn, sort, limit, min_calls: [
                            SimpleNamespace(query=q, calls=3) for q in queries])


def test_advise_from_statements_analyze_side_effect_rejected(monkeypatch):
    """R4 D-F2 PoC: the no-$n-placeholder pgss form is reachable from normal
    business traffic — 'select pg_terminate_backend(pid) … where pid = pid'
    really killed sessions under --analyze. "Already ran on the server" does
    not authorize the tool to run it again; the guard fires before evaluate."""
    calls: list = []
    _patch_advise_db(monkeypatch, calls)
    _patch_pgss(monkeypatch, ["select pg_terminate_backend(pid) from t where pid = pid"])
    r = CliRunner().invoke(cli.cli, ["advise", "--from-statements", "1", "--analyze"])
    assert r.exit_code != 0, r.output
    assert "拒绝副作用函数" in r.output and "advise --from-statements" in r.output
    assert calls == []                                      # never reached the (faked) evaluation


def test_advise_from_statements_analyze_dml_head_rejected(monkeypatch):
    """A plain DML statement pulled from pgss (no placeholders) is refused too —
    EXPLAIN ANALYZE would really execute the write."""
    calls: list = []
    _patch_advise_db(monkeypatch, calls)
    _patch_pgss(monkeypatch, ["UPDATE events SET k = 1 WHERE id = 11"])
    r = CliRunner().invoke(cli.cli, ["advise", "--from-statements", "1", "--analyze"])
    assert r.exit_code != 0, r.output
    assert "只接受只读查询头" in r.output and "advise --from-statements" in r.output
    assert calls == []


def test_advise_from_statements_placeholder_set_still_exempts_under_analyze(monkeypatch):
    """Boundary pinned: advise skips EXPLAIN entirely for $n-placeholder sets
    (has_placeholders) — nothing executes, so the exemption survives --analyze
    there (guarding an unexecuted set would only break the candidates+recipe
    flow for normal pgss workloads that mix in writes)."""
    calls: list = []
    _patch_advise_db(monkeypatch, calls)
    _patch_pgss(monkeypatch, ["UPDATE events SET k = 1 WHERE id = $1",
                              "SELECT count(*) FROM events WHERE id = $2"])
    r = CliRunner().invoke(cli.cli, ["advise", "--from-statements", "2", "--analyze"])
    assert r.exit_code == 0, r.output
    assert calls == [["UPDATE events SET k = 1 WHERE id = $1",
                      "SELECT count(*) FROM events WHERE id = $2"]]


# ---------------------------- R4 B-MINOR-1: missing psycopg2 renders as one line ----------------------------

def test_invoke_renders_missing_psycopg2_as_install_hint(monkeypatch):
    """The driver-less wheel install is a documented state; the lazy `import
    psycopg2` at the call boundary used to escape as a 12-line
    ModuleNotFoundError traceback (pg.py already wrapped its own imports, so
    `statements` showed a one-line hint while sql/diagnose/planbench crashed).
    The fake raise stands in for the real import failure — deterministic even
    on dev machines where the [db] extra IS installed."""
    monkeypatch.setattr(cli, "_resolve_target", lambda *a, **k: ("host=/tmp port=1 dbname=x", None))

    def _no_driver(dsn, stmt, max_rows=100, timeout_s=30.0):
        raise ModuleNotFoundError("No module named 'psycopg2'", name="psycopg2")
    monkeypatch.setattr(cli.sqlcmd, "run_query", _no_driver)
    r = CliRunner().invoke(cli.cli, ["sql", "SELECT 1"])
    assert r.exit_code == 1, r.output
    assert "psycopg2 未安装" in r.output and "pgtuner[db]" in r.output
    assert "Traceback" not in r.output and "ModuleNotFoundError" not in r.output

    # submodule form (psycopg2.errors) is the same missing driver
    def _no_driver_sub(dsn, stmt, max_rows=100, timeout_s=30.0):
        raise ModuleNotFoundError("No module named 'psycopg2.errors'", name="psycopg2.errors")
    monkeypatch.setattr(cli.sqlcmd, "run_query", _no_driver_sub)
    r_sub = CliRunner().invoke(cli.cli, ["sql", "SELECT 1"])
    assert r_sub.exit_code == 1 and "psycopg2 未安装" in r_sub.output

    # any other missing module stays a loud crash (real packaging bugs)
    def _no_other(dsn, stmt, max_rows=100, timeout_s=30.0):
        raise ModuleNotFoundError("No module named 'leftpad'", name="leftpad")
    monkeypatch.setattr(cli.sqlcmd, "run_query", _no_other)
    r_other = CliRunner().invoke(cli.cli, ["sql", "SELECT 1"])
    assert r_other.exit_code != 0
    assert isinstance(r_other.exception, ModuleNotFoundError)   # not swallowed


# ---------------------------- R5 D5-F3: execution-channel statement guards ----------------------------

def test_planbench_variant_copy_to_program_rejected(tmp_path, monkeypatch):
    """R5 D5-F3 PoC: --variant DDL is executed verbatim by the variant loop —
    'COPY (SELECT 1) TO PROGRAM ...' spawned a real shell in the server process
    even against a clone (the marker file appeared). The DDL payload channel
    keeps CREATE/ALTER/DROP/ANALYZE heads, but COPY heads are refused
    unconditionally, BEFORE target resolution (no instance, no ledger)."""
    q = tmp_path / "q.sql"
    q.write_text("SELECT 1;\n", encoding="utf-8")
    _hermetic_target(monkeypatch)
    r = CliRunner().invoke(
        cli.cli, ["planbench", str(q), "--db", "clone1",
                  "--variant", "x=COPY (SELECT 1) TO PROGRAM 'touch /tmp/pwned'"])
    assert r.exit_code != 0, r.output
    assert "planbench --variant" in r.output and "COPY 语句头" in r.output
    assert "Traceback" not in r.output


def test_planbench_variant_side_effect_function_rejected(tmp_path, monkeypatch):
    """Blacklist parity for the variant loop: a query-shaped variant carrying a
    side-effect call is refused in plain mode by explain()'s own blacklist —
    the variant channel must not be the bypass around it."""
    q = tmp_path / "q.sql"
    q.write_text("SELECT 1;\n", encoding="utf-8")
    _hermetic_target(monkeypatch)
    r = CliRunner().invoke(
        cli.cli, ["planbench", str(q), "--db", "clone1",
                  "--variant", "peek=select pg_read_file('postgresql.conf', 0, 100)"])
    assert r.exit_code != 0, r.output
    assert "拒绝副作用函数" in r.output and "pg_read_file" in r.output
    assert "planbench --variant" in r.output


def test_planbench_variant_mode_query_blacklist_parity(tmp_path, monkeypatch):
    """In variant mode the FILE's queries run as raw EXPLAIN (ANALYZE …) inside
    run_variants, skipping explain()'s blacklist — the same pg_read_file query
    refused in plain mode must be refused here too. Overt DML with an explicit
    target stays legal (the channel's documented write-comparison job)."""
    q = tmp_path / "q.sql"
    q.write_text("select pg_read_file('PG_VERSION', 0, 16);\n", encoding="utf-8")
    _hermetic_target(monkeypatch)
    r = CliRunner().invoke(
        cli.cli, ["planbench", str(q), "--db", "clone1",
                  "--variant", "idx=CREATE INDEX ON t(a)"])
    assert r.exit_code != 0, r.output
    assert "拒绝副作用函数" in r.output and "planbench --variant" in r.output
    # contralateral (unit level): overt DML passes the exec guard — the --db
    # write gate keeps governing it, the exec guard adds no second opinion
    cli._guard_exec_stmt("delete from x where note = 'delete from y'", "planbench --variant")


def test_planbench_variant_legal_ddl_still_passes(tmp_path, monkeypatch):
    """Contralateral: CREATE INDEX / ANALYZE variants reach (faked) run_variants
    untouched — the payload channel's day job must survive the guard."""
    from pgtuner import ledger as ledger_mod
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "l.jsonl")
    monkeypatch.setattr(cli, "_resolve_target",
                        lambda *a, **k: ("host=/tmp port=5 dbname=clone1", None))
    monkeypatch.setattr(cli, "_safe_fp", lambda *a, **k: None)
    ran: list = []

    def _fake_run_variants(dsn, queries, vlist, runs=3):
        ran.append((queries, vlist))
        return {"variants": [{"name": v.name, "queries": []} for v in vlist],
                "diffs": {}}
    monkeypatch.setattr(cli.planbench_mod, "run_variants", _fake_run_variants)
    q = tmp_path / "q.sql"
    q.write_text("SELECT count(*) FROM events;\n", encoding="utf-8")
    r = CliRunner().invoke(
        cli.cli, ["planbench", str(q), "--db", "clone1",
                  "--variant", "idx=CREATE INDEX ON events(tenant_id, created_at)",
                  "--variant", "fix=ANALYZE events"])
    assert r.exit_code == 0, r.output
    assert ran and [v.name for v in ran[0][1]] == ["idx", "fix"]


def test_planbench_flip_check_side_effect_rejected(tmp_path, monkeypatch):
    """R5 D5-F3 PoC: --flip-check really executes queries[0] (PREPARE +
    EXPLAIN ANALYZE EXECUTE ×N) with no --db requirement and no guard in front
    — the same 'select pg_read_file(...)' refused in plain mode ran 3 times."""
    q = tmp_path / "q.sql"
    q.write_text("select pg_read_file('postgresql.conf', 0, 64);\n", encoding="utf-8")
    _hermetic_target(monkeypatch)
    r = CliRunner().invoke(cli.cli, ["planbench", str(q), "--flip-check"])
    assert r.exit_code != 0, r.output
    assert "拒绝副作用函数" in r.output and "pg_read_file" in r.output
    assert "planbench --flip-check" in r.output


def test_planbench_flip_check_copy_head_and_smuggle_rejected(tmp_path, monkeypatch):
    """The flip query is a measurement face: even with the --db write gate open,
    COPY heads and smuggled writes stay refused there."""
    q = tmp_path / "q.sql"
    q.write_text("COPY (SELECT 1) TO PROGRAM 'id';\n", encoding="utf-8")
    _hermetic_target(monkeypatch)
    r = CliRunner().invoke(cli.cli, ["planbench", str(q), "--db", "clone1", "--flip-check"])
    assert r.exit_code != 0, r.output
    assert "拒绝 COPY 语句头" in r.output and "planbench --flip-check" in r.output

    q2 = tmp_path / "cte.sql"
    q2.write_text("with d as (delete from t returning *) select * from d;\n", encoding="utf-8")
    r2 = CliRunner().invoke(cli.cli, ["planbench", str(q2), "--db", "clone1", "--flip-check"])
    assert r2.exit_code != 0, r2.output
    assert "写/锁定关键字" in r2.output and "planbench --flip-check" in r2.output


def test_rebench_setup_copy_and_side_effect_rejected(tmp_path, monkeypatch):
    """R5 D5-F3 PoC: rebench --setup 'COPY … TO PROGRAM …' spawned a real shell
    on the read-write measurement connection. The DDL payload channel keeps
    table-building/intervention heads; side-effect functions and COPY heads are
    refused before any instance/registry/ledger touch."""
    qf = tmp_path / "w.sql"
    qf.write_text("SELECT 1;\n", encoding="utf-8")
    _hermetic_target(monkeypatch)

    def _boom(*a, **k):
        raise AssertionError("guard failed open: command proceeded past the setup guard")
    monkeypatch.setattr(cli.instance_mod, "pool_stats", _boom)
    for evil, needle in (("COPY (SELECT 1) TO PROGRAM 'touch /tmp/pwned'", "COPY 语句头"),
                         ("select pg_read_file('postgresql.conf', 0, 64)", "拒绝副作用函数")):
        r = CliRunner().invoke(
            cli.cli, ["rebench", "--instance", "t9", "--queries-file", str(qf),
                      "--db=d1", "--setup", evil])
        assert r.exit_code != 0, (evil, r.output)
        assert "rebench --setup" in r.output and needle in r.output
    # contralateral: a plain DDL/DML payload passes the guard (unit level — the
    # run itself is the slow channel and stays out of the hermetic suite)
    cli._guard_exec_payload("create table t(i int); insert into t values (1)", "rebench --setup")


def test_rebench_setup_smuggled_second_statement_rejected(tmp_path, monkeypatch):
    """One --setup string is split lexically: a COPY smuggled behind a legal
    first statement does not ride past on its coattails."""
    qf = tmp_path / "w.sql"
    qf.write_text("SELECT 1;\n", encoding="utf-8")
    _hermetic_target(monkeypatch)
    r = CliRunner().invoke(
        cli.cli, ["rebench", "--instance", "t9", "--queries-file", str(qf),
                  "--db=d1", "--setup", "ANALYZE events; COPY (SELECT 1) TO PROGRAM 'id'"])
    assert r.exit_code != 0, r.output
    assert "rebench --setup" in r.output and "COPY 语句头" in r.output


def test_lab_make_tpl_setup_sql_guard(tmp_path, monkeypatch):
    """lab make-tpl SETUP_SQL: same DDL payload channel — COPY heads and
    side-effect functions are refused before the instance is even resolved."""
    evil = tmp_path / "evil.sql"
    evil.write_text("create table t(i int);\nCOPY (SELECT 1) TO PROGRAM 'id';\n",
                    encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("guard failed open: command proceeded past the setup guard")
    monkeypatch.setattr(cli, "_need_instance", _boom)
    r = CliRunner().invoke(cli.cli, ["lab", "make-tpl", "--instance", "t9", "tpl1", str(evil)])
    assert r.exit_code != 0, r.output
    assert "lab make-tpl" in r.output and "COPY 语句头" in r.output
    assert "Traceback" not in r.output
    # contralateral: a table-building payload passes (unit level)
    ok = tmp_path / "ok.sql"
    ok.write_text("create table t(i int);\n", encoding="utf-8")
    cli._guard_exec_payload(ok.read_text(encoding="utf-8"), "lab make-tpl SETUP_SQL")


def test_lab_bootstrap_setup_sql_guard(tmp_path, monkeypatch):
    """lab bootstrap --setup-sql: the explicitly given payload file is user
    input — side-effect functions refused before any bootstrap step runs."""
    evil = tmp_path / "evil.sql"
    evil.write_text("select lo_export(0, '/tmp/x');\n", encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("guard failed open: command proceeded past the setup guard")
    monkeypatch.setattr(cli, "_need_instance", _boom)
    r = CliRunner().invoke(
        cli.cli, ["lab", "bootstrap", "--instance", "t9", "--setup-sql", str(evil)])
    assert r.exit_code != 0, r.output
    assert "拒绝副作用函数" in r.output and "lab bootstrap --setup-sql" in r.output


def test_write_smuggle_search_bridge_preferred(monkeypatch):
    """Round-5 Z1 contract pin: once diagnose.write_smuggle_search exists, every
    cli call site routes through it (the perf-gated matcher); until it lands the
    regex fallback keeps identical semantics — either way neither surface may
    touch _WRITE_SMUGGLE directly."""
    seen: list = []

    def _fake(masked):
        seen.append(masked)
        return None
    monkeypatch.setattr(cli.diagnose_mod, "write_smuggle_search", _fake, raising=False)
    cli._check_write_smuggle("select 1", "sql")                  # no raise
    cli._planbench_write_gate(["select 1"], db_given=False, dsn_given=False)
    assert seen == ["select 1", "select 1"]


# ---------------------------- R7 D7-F1: DO-head outright refusal on payload channels ----------------------------

_R7_DO_FORMS = [
    ("plain-dollars", "do $$ begin perform pg_read_file('/etc/passwd', 0, 64); end $$"),
    ("tagged-delim", "do $tag$ begin perform pg_terminate_backend(42); end $tag$"),
    ("upper-case", "DO $$ BEGIN PERFORM PG_READ_FILE('postgresql.conf', 0, 64); END $$"),
    ("leading-comment", "/* seed */ do $$ begin perform pg_read_file('x', 0, 1); end $$"),
]


@pytest.mark.parametrize("form,evil", _R7_DO_FORMS,
                         ids=[f[0] for f in _R7_DO_FORMS])
def test_do_head_refused_on_four_channels(form, evil, tmp_path, monkeypatch):
    """R7 D7-F1: DO statements are refused OUTRIGHT on every payload channel —
    dollar-tagged delimiters, UPPERCASE DO and a leading '/* seed */' comment
    included. The refusal lands before any instance/registry/ledger touch and
    the message carries the channel-design guidance (clone or split into plain
    DDL/DML), not a blacklist word list."""
    qf = tmp_path / "w.sql"
    qf.write_text("SELECT 1;\n", encoding="utf-8")
    setup = tmp_path / "setup.sql"
    setup.write_text(evil + ";\n", encoding="utf-8")
    _hermetic_target(monkeypatch)

    def _boom(*a, **k):
        raise AssertionError("guard failed open: command proceeded past the DO guard")
    monkeypatch.setattr(cli.instance_mod, "pool_stats", _boom)
    monkeypatch.setattr(cli, "_need_instance", _boom)
    channels = [
        (["rebench", "--instance", "t9", "--queries-file", str(qf),
          "--db=d1", "--setup", evil], "rebench --setup"),
        (["lab", "make-tpl", "--instance", "t9", "tpl1", str(setup)], "lab make-tpl"),
        (["lab", "bootstrap", "--instance", "t9", "--setup-sql", str(setup)],
         "lab bootstrap --setup-sql"),
        (["planbench", str(qf), "--db", "clone1", "--variant", f"seed={evil}"],
         "planbench --variant"),
    ]
    for argv, source in channels:
        r = CliRunner().invoke(cli.cli, argv)
        assert r.exit_code != 0, (source, form, r.output)
        assert source in r.output, (source, form, r.output)
        assert "拒绝 DO 语句头" in r.output, (source, form, r.output)
        assert "克隆库" in r.output and "拆成普通 DDL/DML 语句" in r.output, \
            (source, form, r.output)
        assert "Traceback" not in r.output, (source, form, r.output)


_R7_DO_RCE_POC = [
    "do $do$ begin execute 'select lo_cre' || 'at(-1)'; end $do$",
    "do $d$ begin execute $$select lo_cre$$ || $$at(-1)$$; end $d$",
    "do $$ begin execute format('select lo_im%s(-1)', 'port'); end $$",
    "do $$ begin execute chr(108) || chr(111) || chr(95) || chr(99) ||"
    " chr(114) || chr(101) || chr(97) || chr(116); end $$",
]


def test_do_dynamic_name_assembly_rce_poc_refused():
    """R7 D7-F1 BLOCKER PoC corpus: names assembled at RUNTIME inside the DO
    body ('…lo_cre' || 'at(-1)', format(...), chr()||chr() splicing) — the
    blacklisted word never appears contiguously, which is exactly why the
    round-6 unmasked re-scan (any static blacklist, in fact) lost here: a live
    COPY TO PROGRAM variant assembled this way executed in the server process.
    Pinned first: none of these texts matches the side-effect blacklist at all
    (static analysis is structurally blind); then: the head refusal covers the
    whole class regardless of body content."""
    import re as _re
    from pgtuner import sqlsplit
    for poc in _R7_DO_RCE_POC:
        assert not sqlsplit.SIDE_EFFECT_FUNCTIONS.search(poc), poc   # the bypass, on record
        with pytest.raises(click.ClickException) as ei:
            cli._guard_exec_stmt(poc, "rebench --setup")
        assert "拒绝 DO 语句头" in str(ei.value), poc


def test_do_legal_seed_also_refused_with_guidance():
    """Flipped W3 (round-7 channel design decision): even a clean DO seed —
    null body, plain insert, no blacklisted name anywhere — is refused now.
    The error message must point the way out (run by hand in a clone, or split
    into plain DDL/DML statements) instead of leaving the user stranded, and
    per-statement guarding inside one multi-statement payload stays intact
    (the DO is caught as its own statement, the CREATE around it is not
    blamed)."""
    for seed in ("do $$ begin null; end $$",
                 "do $seed$ begin insert into t values (1); end $seed$"):
        with pytest.raises(click.ClickException) as ei:
            cli._guard_exec_stmt(seed, "lab make-tpl SETUP_SQL")
        msg = str(ei.value)
        assert "拒绝 DO 语句头" in msg and "克隆库" in msg
        assert "拆成普通 DDL/DML 语句" in msg
    # the smuggled-DO-inside-a-legal-payload shape: only the DO statement refuses
    with pytest.raises(click.ClickException) as ei2:
        cli._guard_exec_payload("create table t(i int); do $$ begin null; end $$",
                                "rebench --setup")
    assert "拒绝 DO 语句头" in str(ei2.value)


def test_do_head_leading_comment_defense_without_splitter():
    """C7-F5: statement_head cannot see past a leading comment (it returns ''
    for '/* seed */ do $$…$$'), and the round-6 logic keyed on head=='do' — a
    raw statement handed straight to the guard, bypassing _split_exec_payload/
    _split_sql_file, used to slip both the DO and the COPY head checks. The
    guard now skips leading comments itself (defense in depth: all six call
    sites split first); the head-based refusals still fire, and an unclosed
    leading block comment is a lexical refusal, not a pass."""
    with pytest.raises(click.ClickException) as do_ei:
        cli._guard_exec_stmt("/* seed */ do $$ begin null; end $$", "rebench --setup")
    assert "拒绝 DO 语句头" in str(do_ei.value)
    with pytest.raises(click.ClickException) as copy_ei:
        cli._guard_exec_stmt(
            "-- warmup\ncopy (select 1) to program 'touch /tmp/pwned'",
            "rebench --setup")
    assert "拒绝 COPY 语句头" in str(copy_ei.value)
    with pytest.raises(click.ClickException) as unclosed_ei:
        cli._guard_exec_stmt("/* never closed do $$ begin null; end $$",
                             "rebench --setup")
    assert "语句词法错误" in str(unclosed_ei.value)


def test_call_head_stays_allowed_declared_boundary():
    """Boundary pinned (unchanged by D7-F1): the 'call' head keeps passing the
    exec guard — calling a pre-existing database function is the documented
    known boundary (guards analyze statement text, not pg_proc bodies;
    database content is not tool input). Refusing DO does not extend to CALL."""
    cli._guard_exec_stmt("call seed_proc()", "rebench --setup")
    cli._guard_exec_stmt("CALL pgbench_seed(100)", "lab make-tpl SETUP_SQL")


def test_packaged_template_setup_sql_has_no_do_statements():
    """The default bootstrap channel must stay unaffected by the DO refusal:
    the packaged assets/template_setup.sql (tool-owned, never guarded) is
    pinned free of DO statements — lexically (every split statement's head)
    and grep-style (no DO dollar-opener anywhere in the raw text)."""
    import re as _re
    from pgtuner import bootstrap as bootstrap_mod
    from pgtuner import sqlsplit
    text = bootstrap_mod.DEFAULT_SETUP_SQL.read_text(encoding="utf-8")
    stmts = sqlsplit.split_statements(text)
    assert stmts, "template_setup.sql unexpectedly empty"
    for s in stmts:
        assert sqlsplit.statement_head(s) != "do", s
    assert not _re.search(r"(?i)\bdo\s*\$", text)


# ---------------------------- R6 C6-F5: write-smuggle fallback keeps the update bridge ----------------------------

def test_write_smuggle_fallback_keeps_update_bridge(monkeypatch):
    """R6 C6-F5: with diagnose.write_smuggle_search (contract-pinned) present
    the bridge routes through it; if it ever goes missing, the fallback must
    stay EQUIVALENT — the round-5 perf split left diagnose._WRITE_SMUGGLE with
    the bounded alternatives only, and the old bare `.search` fallback
    silently missed 'update … set' smuggles it used to catch. The inlined
    two-step retrieval restores parity (leftmost-match included)."""
    monkeypatch.delattr(cli.diagnose_mod, "write_smuggle_search", raising=False)
    # the lost branch: update…set still found by the fallback
    m = cli._write_smuggle_match("with u as (update t set k = 1 returning *) select * from u")
    assert m is not None and m.group(0).lower().startswith("update")
    # leftmost wins across the two retrievals (same rule as write_smuggle_search)
    both = cli._write_smuggle_match("update t set k = 1 where x in (select * from y for update)")
    assert both is not None and both.group(0).lower().startswith("update")
    # bounded alternatives keep firing without the update branch; clean text stays clean
    m2 = cli._write_smuggle_match("select * from t for update")
    assert m2 is not None and m2.group(0).lower() == "for update"
    assert cli._write_smuggle_match("select 1") is None
    # parity with the primary matcher on the same inputs
    monkeypatch.undo()
    for text in ("with u as (update t set k = 1 returning *) select * from u",
                 "update t set k = 1 where x in (select * from y for update)",
                 "select * from t for update", "select 1"):
        p = cli.diagnose_mod.write_smuggle_search(text)
        monkeypatch.delattr(cli.diagnose_mod, "write_smuggle_search", raising=False)
        f = cli._write_smuggle_match(text)
        monkeypatch.undo()
        assert (p is None) == (f is None), text
        if p is not None:
            assert (p.start(), p.end()) == (f.start(), f.end()), text
