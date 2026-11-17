"""Adversarial-review R1 hardening regressions (agent-facing safety gates).

Pinned here: read-only single-statement inputs, destroy confirmation, planbench
variant db gate, clean error rendering for the most common operational error
(typo'd instance name), and the non-clone DROP refusal. No PostgreSQL needed —
every path fails (or is mocked) before any connection.
"""
from types import SimpleNamespace

from click.testing import CliRunner

from pgtuner import cli


def _fake_inst(name="rel19"):
    return SimpleNamespace(name=name, pg_version="19devel", build_type="release",
                           bindir="/fake/bin", data_dir="/tmp/fake-data", port=5433,
                           socket_dir="/tmp", managed=True, status="running")


def test_diagnose_positional_multistatement_refused():
    """R1-D blocker: 'SELECT 1; DROP TABLE x' used to pass the head-regex read-only
    filter and execute both statements on a read-write autocommit connection."""
    r = CliRunner().invoke(cli.cli, ["diagnose", "SELECT 1; DROP TABLE accounts"])
    assert r.exit_code != 0
    assert "一次只接受一条语句" in r.output


def test_hypo_advise_query_multistatement_refused():
    for argv in (["hypo", "--index", "CREATE INDEX ON t(k)", "--query", "SELECT 1; SELECT 2"],
                 ["advise", "--query", "SELECT 1; SELECT 2"]):
        r = CliRunner().invoke(cli.cli, argv)
        assert r.exit_code != 0, argv
        assert "一次只接受一条语句" in r.output, argv


def test_unknown_instance_renders_clean_error():
    """R1-B major: the single most frequent operational error (typo'd instance name)
    must render as 'Error: <hint>', never a Python traceback."""
    r = CliRunner().invoke(cli.cli, ["sql", "--instance", "does-not-exist", "SELECT 1"])
    assert r.exit_code != 0
    assert "Error: 实例未注册: does-not-exist" in r.output
    assert "Traceback" not in r.output


def test_destroy_requires_yes_when_not_tty(monkeypatch):
    monkeypatch.setattr(cli.instance_mod, "get", lambda n: _fake_inst())
    calls: list = []
    monkeypatch.setattr(cli.instance_mod, "destroy",
                        lambda n, force=False: calls.append(n))
    r = CliRunner().invoke(cli.cli, ["instance", "destroy", "rel19"])
    assert r.exit_code != 0 and "非交互场景请加 --yes" in r.output
    assert calls == []                                   # nothing destroyed without consent
    r2 = CliRunner().invoke(cli.cli, ["instance", "destroy", "rel19", "--yes"])
    assert r2.exit_code == 0 and calls == ["rel19"]


def test_planbench_variant_requires_explicit_db(monkeypatch, tmp_path):
    """R1-D: variant DDL really executes — a forgotten --db used to land it on the
    shared postgres database by default."""
    q = tmp_path / "q.sql"
    q.write_text("SELECT 1;", encoding="utf-8")
    inst = _fake_inst()
    monkeypatch.setattr(cli.instance_mod, "get", lambda n: inst)
    monkeypatch.setattr(cli.instance_mod, "ensure_running", lambda n: inst)
    monkeypatch.setattr(cli.instance_mod, "dsn_of",
                        lambda n, db, registry_file=None: f"host=/tmp port=5433 dbname={db}")
    r = CliRunner().invoke(
        cli.cli, ["planbench", str(q), "--instance", "rel19",
                  "--variant", "idx=CREATE INDEX ON t(k)"])
    assert r.exit_code != 0
    assert "显式 --db" in r.output


def test_diagnose_rejects_data_modifying_cte():
    """Self-caught residual hole after R1: WITH … DELETE/UPDATE … passes the head
    regex but really writes under --analyze (EXPLAIN ANALYZE executes the CTE).
    Validation is unit-level on purpose — a CLI-level invoke with a legit query
    would resolve the deployment default instance and touch a real database
    (R2-C finding: this test once polluted the real ledger)."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    with pytest.raises(ValueError, match="只读"):
        diagnose_mod._validate(
            ["WITH d AS (DELETE FROM accounts RETURNING *) SELECT * FROM d"])
    with pytest.raises(ValueError, match="只读"):
        diagnose_mod._validate(["SELECT * FROM t FOR UPDATE"])
    with pytest.raises(ValueError, match="只读"):
        diagnose_mod._validate(["SELECT * FROM t FOR KEY SHARE"])
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate(
            ["SELECT pg_terminate_backend(pid) FROM pg_stat_activity"])
    # legit WITH stays allowed (literal bodies are masked, not matched)
    diagnose_mod._validate(["WITH x AS (SELECT 1) SELECT * FROM x"])
    diagnose_mod._validate(
        ["SELECT * FROM logs WHERE msg = 'insert into t values (1)'"])
    diagnose_mod._validate(["SELECT last_update, update_hint FROM t"])


def test_sql_guard_blocks_copy_to_program_and_side_effect_functions():
    """R2-D blocker: read-only transactions do not prevent COPY ... TO PROGRAM or
    side-effect functions — the sql exit needs its own statement-level guard."""
    from click.testing import CliRunner as R
    r = R().invoke(cli.cli, ["sql", "COPY (SELECT 1) TO PROGRAM 'id'"])
    assert r.exit_code != 0 and "只读语句头" in r.output
    r2 = R().invoke(cli.cli, ["sql",
                              "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"])
    assert r2.exit_code != 0 and "副作用函数" in r2.output
    # legit statements pass the guard (they fail later on connection, not here)
    r3 = R().invoke(cli.cli, ["sql", "SHOW work_mem"])
    assert "只读语句头" not in r3.output and "副作用函数" not in r3.output


def test_hypo_index_whitelisted_to_create_index():
    """R2-D M2: --index executes the DDL verbatim — anything that is not exactly
    one CREATE [UNIQUE] INDEX is refused (rollback cannot undo external effects)."""
    from click.testing import CliRunner as R
    r = R().invoke(cli.cli, ["hypo", "--index=\"COPY (SELECT 1) TO PROGRAM 'id'\"",
                             "--query", "SELECT 1"])
    assert r.exit_code != 0 and "CREATE [UNIQUE] INDEX" in r.output
    r2 = R().invoke(cli.cli, ["hypo", "--index", "CREATE INDEX ON t(k); DROP TABLE x",
                              "--query", "SELECT 1"])
    assert r2.exit_code != 0 and "CREATE [UNIQUE] INDEX" in r2.output


def test_planbench_explain_rejects_utility_heads_and_side_effects():
    """R2-D: EXPLAIN ANALYZE really executes — utility heads and side-effect
    functions are refused at the explain() bottleneck."""
    import pytest
    from pgtuner import planbench as pb
    with pytest.raises(RuntimeError, match="语句头"):
        pb.explain(None, "COPY (SELECT 1) TO PROGRAM 'id'", analyze=True)
    with pytest.raises(RuntimeError, match="副作用"):
        pb.explain(None, "SELECT pg_read_file('postgresql.conf')", analyze=True)


# ---------------- round-3 adversarial review: lexical guard defects (F1/F2/F3) ----------------
# Quoted identifiers are SYNTAX, not data: PG runs select "pg_read_file"('…') just
# fine, so the masks/guards below must treat "name"( and name( as equivalent.

_QUOTED_FNS = ["pg_ls_dir", "pg_read_file", "lo_import", "lo_export",
               "pg_terminate_backend", "dblink_exec"]


def test_quoted_function_names_all_rejected():
    """Round-3 F1 (blocker): mask_literals used to blank double-quoted identifier
    BODIES, blinding the side-effect blacklists to quoted function names —
    readable-server-file / persistent-large-object-write calls slipped through
    both `pgtuner sql` and `diagnose --analyze`. Asserted at both the
    find_side_effect_calls and diagnose._validate levels."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in _QUOTED_FNS + ["pg_sleep"]:
        sql = f'select "{fn}"(\'x\')'
        assert find_side_effect_calls(sql, surface="analyze") == [fn], sql
    for fn in _QUOTED_FNS:            # same names on the sql surface (sleep family: see differential test)
        assert find_side_effect_calls(f'select "{fn}"(\'x\')', surface="sql") == [fn]
    assert find_side_effect_calls('select "PG_READ_FILE"(\'x\')', surface="sql") \
        == ["pg_read_file"]           # blacklist is case-insensitive, so mixed quoting/case sneaks neither
    for fn in _QUOTED_FNS + ["pg_sleep", "PG_READ_FILE"]:
        with pytest.raises(ValueError, match="副作用"):
            diagnose_mod._validate([f'select "{fn}"(\'x\')'])


def test_quoted_identifier_write_smuggle_rejected():
    """Round-3 F2: a quoted UPDATE target used to break the
    update\\s+\\S{1,63}\\s+set token on masked text (quotes blank, and a name
    with inner spaces is not \\S+), letting data-modifying CTEs through _validate.
    DELETE/INSERT prefix patterns only involve bare keywords — variants pinned too.
    Known, accepted tradeoff (defense first, refusing beats releasing): a plain
    identifier reference whose body collides with a write keyword (e.g.
    select "into" from t) is now rejected as well — documented limitation."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    for q in ['WITH u AS (UPDATE "mytable" SET y=1 RETURNING *) SELECT * FROM u',
              'WITH u AS (UPDATE "my table" SET y=1 RETURNING *) SELECT * FROM u',
              'WITH d AS (DELETE FROM "my table" RETURNING *) SELECT * FROM d',
              'WITH i AS (INSERT INTO "mytab" SELECT 1 RETURNING *) SELECT * FROM i']:
        with pytest.raises(ValueError, match="只读"):
            diagnose_mod._validate([q])
    with pytest.raises(ValueError, match="SELECT/WITH"):     # head check refuses non-SELECT outright
        diagnose_mod._validate(["UPDATE \"my table\" SET y=1"])


def test_escape_string_mask_split_span_consistency():
    """Round-3 F3: mask_literals must decide E'' backslash escapes exactly like
    split_statements (shared _single_quoted_end scanner). The old mask ignored
    the escapes: E'a\\'b' raised "unclosed" while split said one string, and
    E'\\'; DROP TABLE t; --' closed early and false-positived as write smuggling."""
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import mask_literals, split_statements
    # legal SELECTs whose E'' payload contains smuggle-looking text: 1 statement,
    # no mask error, and _validate must let them through
    for q in ("SELECT E'\\''", "SELECT E'a\\'b'", "SELECT E'\\'; DROP TABLE t; --'"):
        assert len(split_statements(q)) == 1, q
        mask_literals(q)                                    # must not raise "unclosed"
        diagnose_mod._validate([q])                         # must be allowed
    # E'a\\\\' closes at the second quote (\\\\ is an escaped backslash): the DROP is
    # a REAL second statement for both scanners — split emits it, mask keeps it
    # visible and eats the trailing --' as a comment. Same spans, no divergence.
    q2 = r"SELECT E'a\\' ; DROP TABLE x; --'"
    assert split_statements(q2) == [r"SELECT E'a\\'", "DROP TABLE x"]
    m = mask_literals(q2)                                   # must not raise
    assert "DROP TABLE x" in m and m.count("'") == 2


def test_legit_literals_comments_and_identifiers_still_pass():
    """R2-C false-positive protection must survive the F1/F2 tightening: write
    keywords inside string literals / comments are data, and ordinary
    double-quoted identifier references are not function calls."""
    from pgtuner import diagnose as diagnose_mod
    for q in ("SELECT * FROM logs WHERE msg = 'delete from t'",
              "SELECT * FROM logs WHERE msg = 'update x set y=1'",
              "SELECT 1 /* update t set x=1; delete from y */ AS v",
              "SELECT 1 -- drop table t",
              'select "user" from "my table"',
              'SELECT "pg_sleep_like_col" FROM t',
              "SELECT last_update, update_hint FROM t",
              "WITH x AS (SELECT 1) SELECT * FROM x"):
        diagnose_mod._validate([q])                         # does not raise


def test_pg_sleep_surface_differential_kept():
    """sql surface: statement_timeout caps pg_sleep, so it stays allowed there
    (quoted or not); analyze surface: EXPLAIN ANALYZE would really run it —
    blocked. The F1 unquoting must not flatten this differential."""
    from pgtuner.sqlsplit import find_side_effect_calls
    for sql in ("select pg_sleep(1)", 'select "pg_sleep"(1)', 'select "PG_SLEEP"(1)'):
        assert find_side_effect_calls(sql, surface="sql") == []
        assert find_side_effect_calls(sql, surface="analyze") == ["pg_sleep"]


# ---------------- round-4 adversarial review: U& Unicode-escaped identifiers (D-F1),
# unbounded update…set bridge (C-F1), sequence-advancing functions (D-F6) ----------------

def test_unicode_escaped_identifier_side_effect_calls():
    """Round-4 D-F1 (blocker): mask_literals only recognized bare quotes, so
    U&"pg_\\0072ead_file"('…') — a Unicode-escaped IDENTIFIER the server calls
    just like a plain quoted one (live-verified: U&"v\\0065rsion"() ran
    version()) — pierced all five read-only surfaces (sql surface read
    /etc/hostname, diagnose --analyze persisted a lo_import). The whole U& token
    must be replaced by space+DECODED body+space so the blacklists see the real
    server-side name. Asserted at both find_side_effect_calls and
    diagnose._validate levels. Semantics live-verified against a PG 19devel
    server: \\XXXX / \\+XXXXXX forms, doubled escape, custom and glued UESCAPE,
    comment-separated UESCAPE."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for sql in ('select U&"pg_\\0072ead_file"(\'x\')',            # \XXXX hides the r
                'select u&"pg_\\0072ead_file"(\'x\')',            # lowercase prefix
                'select U&"pg_\\+000072ead_file"(\'x\')',         # \+XXXXXX hides it too
                'select U&"PG_\\0052EAD_FILE"(\'x\')',            # mixed case sneaks neither
                "select U&\"pg_r#0065ad_file\" uescape '#'('x')",     # custom escape char (clause sits
                "select U&\"pg_r#0065ad_file\"uescape'#'('x')",       # between ident and args, glued)
                "select U&\"pg_r#0065ad_file\" /* gap */ uescape '#'('x')"):  # comment-separated clause
        assert find_side_effect_calls(sql, surface="sql") == ["pg_read_file"], sql
        assert find_side_effect_calls(sql, surface="analyze") == ["pg_read_file"], sql
    for sql in ('select U&"lo_\\0069mport"(\'/tmp/x\')',          # red-team repro: persistent write
                'select U&"\\0070g_terminate_backend"(1)',        # whole name escaped
                'select U&"pg_\\0072ead_file"(\'/etc/hostname\')'):
        with pytest.raises(ValueError, match="副作用"):
            diagnose_mod._validate([sql])
    # surface differential survives the decode: sleep family stays sql-allowed
    assert find_side_effect_calls('select U&"pg_\\0053leep"(1)', surface="analyze") == ["pg_sleep"]
    assert find_side_effect_calls('select U&"pg_\\0053leep"(1)', surface="sql") == []
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate(['select U&"pg_\\0053leep"(1)'])
    # doubled escape character is a LITERAL backslash, not an escape: the name is
    # pg_\0072ead_file — no blacklist hit, and the statement stays legal
    assert find_side_effect_calls('select U&"pg_\\\\0072ead_file"(\'x\')', surface="analyze") == []
    diagnose_mod._validate(['select U&"pg_\\\\0072ead_file"(\'x\')'])   # does not raise
    # benign decoded identifiers (count) are not collateral damage
    diagnose_mod._validate(['select U&"\\0063ount"(*) from t'])         # does not raise


def test_unicode_escaped_identifier_malformed_refused():
    """D-F1 fail-safe direction: anything we cannot decode EXACTLY like the
    server must be refused (ValueError), never silently decoded with the wrong
    escape character — that would re-open the hole for UESCAPE-carrying
    payloads. PG itself rejects each of these at parse time (live-verified:
    invalid Unicode escape / invalid Unicode escape character)."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import mask_literals
    for bad in ('select U&"a\\z00"',              # escape not followed by hex digits
                'select U&"a\\00"',               # 4-digit form needs exactly 4 digits
                'select U&"a\\+00006"',           # 6-digit form needs exactly 6 digits
                'select U&"a\\D800"',             # surrogate: PG pairs them, we refuse
                'select U&"a\\+110000"',          # above U+10FFFF
                'select U&"a" uescape',           # clause truncated (no string)
                'select U&"a" uescape 5',         # clause without a string constant
                'select U&"a" uescape \'ab\'',    # multi-char escape
                'select U&"a" uescape \'7\'',     # hex digit as escape (PG rejects)
                'select U&"a" uescape \'+\''):    # '+' would collide with the \+XXXXXX form
        with pytest.raises(ValueError, match="Unicode|UESCAPE"):
            mask_literals(bad)
    # the refusal propagates to the surface gate, not just the low-level mask
    with pytest.raises(ValueError, match="Unicode"):
        diagnose_mod._validate(['select U&"pg_\\zread_file"(\'x\')'])


def test_unicode_escaped_string_literal_stays_data_masked():
    """U&'...' is a Unicode STRING literal: data semantics — quotes kept, body
    blanked, harmless prefix characters. It must neither leak blacklist names
    into the guards nor false-positive on them."""
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls, mask_literals
    m = mask_literals("select U&'pg_\\0072ead_file' as v")
    assert "\\0072" not in m and "read_file" not in m      # body blanked as data
    assert find_side_effect_calls("select U&'pg_\\0072ead_file'", surface="analyze") == []
    diagnose_mod._validate(["select U&'a\\0062c' uescape '#'"])   # clause after a string: ignored, no error
    diagnose_mod._validate(["select U&'\\\\'"])                    # doubled escape inside data


def test_unicode_prefix_glued_to_identifier_is_not_a_prefix():
    """aU&"x" lexes as identifier aU + operator & + quoted identifier x — the U&
    is NOT a prefix when glued to an identifier character, so the quote falls to
    the plain branch (body verbatim, escapes are literal characters of a
    differently-named identifier: no hit, no crash). split_statements consumes
    U&"…" bodies inside quotes exactly like plain "…" (semicolons do not split)."""
    from pgtuner.sqlsplit import find_side_effect_calls, split_statements
    sql = 'select aU&"pg_\\0072ead_file"(\'x\')'
    assert find_side_effect_calls(sql, surface="analyze") == []
    assert find_side_effect_calls(sql, surface="sql") == []
    assert split_statements('select U&"a;b"') == ['select U&"a;b"']
    assert split_statements('select aU&"a;b"') == ['select aU&"a;b"']


def test_long_qualified_update_cte_rejected():
    """C-F1: the {0,64} window on the update…set bridge was shorter than a legal
    schema-qualified target (63 chars per name part, arbitrarily deep in raw
    text) — a gap>64 data-modifying CTE pierced the read-only surfaces. The
    bridge is unbounded now; head whitelist + literal/comment masking keep it
    precise (bare update…set in a body is either a CTE or the documented
    quoted-identifier tradeoff)."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    long_name = "s" + "234567890" * 12               # 109 chars — far past the old 64 window
    schema, table = "a" * 50, "b" * 50               # gap ≈ 102, past the old window too
    for q in (f"WITH u AS (UPDATE {schema}.{table} SET y=1 RETURNING *) SELECT * FROM u",
              f"WITH u AS (UPDATE public.{long_name} SET y=1 RETURNING *) SELECT * FROM u",
              f'WITH u AS (UPDATE "my {long_name} table" SET y=1 RETURNING *) SELECT * FROM u'):
        with pytest.raises(ValueError, match="只读"):
            diagnose_mod._validate([q])


def test_sequence_advancing_functions_blocked_and_currval_kept():
    """D-F6: nextval/setval/pg_nextoid advances are non-transactional (they
    survive rollback — live-verified advancing through advise --query --analyze
    despite the hypo rollback), so the analyze surface refuses them. The sql
    surface carries them too: PG read-only transactions already refuse
    nextval/setval with SQLSTATE 25001 (sequence.c:
    "read-only transactions may only modify temp sequences"), so the tool-level
    entry only front-runs the identical server refusal with a friendlier
    message. currval/lastval are true read-only probes and stay allowed."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in ("nextval", "setval", "pg_nextoid"):
        sql = f"select {fn}('s')"
        assert find_side_effect_calls(sql, surface="analyze") == [fn]
        assert find_side_effect_calls(sql, surface="sql") == [fn]
        with pytest.raises(ValueError, match="副作用"):
            diagnose_mod._validate([sql])
    assert find_side_effect_calls('select U&"\\006eextval"(\'s\')', surface="analyze") \
        == ["nextval"]                                # U& cannot hide a sequence advance either
    for ok in ("select currval('s')", "select lastval()"):
        assert find_side_effect_calls(ok, surface="analyze") == []
        assert find_side_effect_calls(ok, surface="sql") == []
        diagnose_mod._validate([ok])                  # does not raise


# ---------------- round-5 adversarial review: lo_* write family (D5-F1), ops
# side-effect family (D5-F2), unbounded update…set O(n²) bridge (C5-F2),
# U& \0000 code point (D5-F9) ----------------

_LO_WRITE_FNS = ("lo_create", "lo_open", "lowrite", "lo_truncate", "lo_unlink",
                 "lo_from_bytea")
_OPS_FNS = ("pg_stat_statements_reset", "pg_replication_origin_create",
            "pg_replication_origin_drop", "pg_replication_origin_advance",
            "pg_create_restore_point", "pg_switch_wal", "pg_wal_replay_pause",
            "pg_wal_replay_resume", "pg_promote")


def test_lo_write_family_blocked_on_both_surfaces():
    """D5-F1 (blocker, live-verified): select lowrite(lo_open(lo_create(0),
    131072),'x') passed the head whitelist, the write-smuggle check and the
    side-effect blacklists (none of lo_create/lo_open/lowrite listed) —
    diagnose --analyze really executed and committed it on the rw autocommit
    connection (pg_largeobject_metadata 0→1). The whole lo_* write family joins
    BOTH blacklists; lo_open is refused in all modes on purpose (INV_WRITE is
    the dangerous mode, but INV_READ-only use is rare and the blanket reject
    also kills wrap-smuggles)."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    chain = "select lowrite(lo_open(lo_create(0),131072), 'x')"
    for surface in ("sql", "analyze"):
        assert find_side_effect_calls(chain, surface=surface) \
            == ["lo_create", "lo_open", "lowrite"]
    for fn in _LO_WRITE_FNS:
        for surface in ("sql", "analyze"):
            assert find_side_effect_calls(f"select {fn}(0)", surface=surface) == [fn]
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate([chain])


def test_lo_write_family_u_escape_wrap_caught():
    """D5-F1 composed with the round-4 D-F1 decoder: U&-wrapped lo_* names
    (red-team corpus: U&\"low\\0072ite\") decode to the same visible text, so
    the completed blacklists catch them too."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    assert find_side_effect_calls('select U&"low\\0072ite"(1, \'x\')',
                                  surface="sql") == ["lowrite"]
    assert find_side_effect_calls('select U&"lo_\\0063reate"(0)',
                                  surface="analyze") == ["lo_create"]
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate(
            ['select U&"low\\0072ite"(lo_open(lo_create(0),131072), \'x\')'])


def test_ops_side_effect_family_blocked_on_both_surfaces():
    """D5-F2 (major, live-verified on the sql surface): read-only transactions
    stop none of the ops family — pg_replication_origin_create('x') really
    created an origin, pg_create_restore_point/pg_switch_wal really took
    effect, and pg_stat_statements_reset(0,0,0) cleared the pgss stats of ALL
    instances. All nine names join both blacklists (surviving a read-only
    transaction is exactly what disqualifies them here)."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in _OPS_FNS:
        for surface in ("sql", "analyze"):
            assert find_side_effect_calls(f"select {fn}(0)", surface=surface) == [fn]
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate(["select pg_stat_statements_reset(0,0,0)"])
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate(["select pg_replication_origin_create('x')"])


def test_write_smuggle_search_bounded_and_leftmost_equivalent():
    """C5-F2: the unbounded update…set bridge moved out of _WRITE_SMUGGLE behind
    write_smuggle_search's keyword gate — 'update '×20000 (140KB, no set) used
    to cost 20.6s of O(n²) lazy rescans; the gate must return None fast (<2s
    even with CI jitter), and the both-keywords path stays single-pass cheap.
    Equivalence: the gated search must return the SAME leftmost match as the
    historical combined pattern on a mixed corpus (legit / CTE smuggles / FOR
    locks / literal-masked data / update-earlier-than-bounded orderings)."""
    import re
    import time
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import mask_literals
    t0 = time.perf_counter()
    assert diagnose_mod.write_smuggle_search("update " * 20000) is None
    assert time.perf_counter() - t0 < 2.0
    t1 = time.perf_counter()
    assert diagnose_mod.write_smuggle_search("update " * 20000 + "set") is not None
    assert time.perf_counter() - t1 < 2.0
    # _validate keeps rejecting update CTEs through the gated path, and legit
    # update-adjacent identifiers keep passing (gate is substring-necessary only)
    with pytest.raises(ValueError, match="只读"):
        diagnose_mod._validate(["WITH u AS (UPDATE t SET y=1 RETURNING *) SELECT * FROM u"])
    diagnose_mod._validate(["SELECT last_update, update_hint, offset_col FROM t"])
    historical = re.compile(     # _WRITE_SMUGGLE exactly as it was before the split
        r"\b(delete\s+from|insert\s+into|merge\s+into|into\b|truncate\b"
        r"|for\s+(no\s+key\s+)?(update|share)\b|for\s+key\s+share\b"
        r"|lock\s+table\b|lock\s+in\s+share\b|grant\b|revoke\b"
        r"|update\b[\s\S]*?\bset\b"
        r"|create\b|alter\b|drop\b|copy\b|vacuum\b|reindex\b|call\b|do\b)\b", re.I)
    corpus = [
        "select 1",
        "with x as (select 1) select * from x",
        "select last_update, update_hint, offset_col from t",
        "select update_fn('x')",
        "select do_work('x'), drop_off from t",
        "select * from logs where msg = 'update x set y=1'",
        "select 'create table x; update t set y=1' as smuggle_text",
        "with u as (update t set x=1 returning *) select * from u",
        'with u as (update "my table" set y=1 returning *) select * from u',
        "with u as (update " + "a" * 50 + "." + "b" * 50
        + " set y=1 returning *) select * from u",
        "select update t set y=1 into z",       # update bridge starts EARLIER than the bounded hit
        "with d as (delete from t returning *) select * from d",
        "with i as (insert into t select 1 returning *) select * from i",
        "select * from t for update",
        "select * from t for no key update",
        "select * from t for share",
        "select 1 into cur from x",
        "select 1 into t from x where note = 'update t set k=1'",
        "grant select on t to u",
        "revoke all on t from u",
        "lock table t in share mode",
    ]
    for q in corpus:
        masked = mask_literals(q)
        h = historical.search(masked)
        g = diagnose_mod.write_smuggle_search(masked)
        assert (h is None) == (g is None), q
        if h is not None:
            assert (h.start(), h.group(0)) == (g.start(), g.group(0)), q


def test_unicode_escape_codepoint_zero_refused():
    """D5-F9: \\0000 (code point 0) — the server rejects it too, but the tool
    layer must fail closed instead of decoding chr(0) into the masked text
    (both the \\XXXX and \\+XXXXXX forms; refusal propagates to _validate)."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import mask_literals
    with pytest.raises(ValueError, match="Unicode|码点"):
        mask_literals('select U&"a\\0000"')
    with pytest.raises(ValueError, match="Unicode|码点"):
        mask_literals('select U&"a\\+000000"')
    with pytest.raises(ValueError, match="Unicode|码点"):
        diagnose_mod._validate(['select U&"pg_\\0000read_file"(\'x\')'])


# ---------------- round-6 adversarial review: blacklist alias/variant gaps
# (D6-F1 lo_creat, D6-F2 pg_stat_reset trio, D6-F4/F5/F6 family completions),
# substring-gate residual O(n²) (C6-F2) ----------------

_ALIAS_FNS = ("lo_creat", "lo_truncate64")
_STAT_RESET_FNS = ("pg_stat_reset", "pg_stat_reset_shared", "pg_stat_reset_slru")
_LOCK_FNS = ("pg_try_advisory_lock", "pg_try_advisory_xact_lock",
             "pg_advisory_unlock", "pg_advisory_unlock_all")


def test_lo_creat_alias_blocked_on_both_surfaces():
    """D6-F1 (blocker, live-verified): lo_creat is the OLD ALIAS of lo_create and
    a real pg_catalog function (pronargs=1) — \\blo_create\\b word boundaries
    never matched it, so select lo_creat(-1) slipped through the blacklists and
    really persisted large objects on the four rw autocommit surfaces
    (pg_largeobject_metadata 0→14). Explicit name on both lists; quoted and
    U&-wrapped aliases stay equally visible (mask keeps/decodes ident bodies)."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for surface in ("sql", "analyze"):
        assert find_side_effect_calls("select lo_creat(-1)", surface=surface) \
            == ["lo_creat"]
        assert find_side_effect_calls('select "lo_creat"(-1)', surface=surface) \
            == ["lo_creat"]
        assert find_side_effect_calls('select U&"lo_c\\0072eat"(-1)',
                                      surface=surface) == ["lo_creat"]
    chain = "select lowrite(lo_open(lo_creat(-1), 131072), 'x')"
    for surface in ("sql", "analyze"):
        assert find_side_effect_calls(chain, surface=surface) \
            == ["lo_creat", "lo_open", "lowrite"]
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate(["select lo_creat(-1)"])


def test_pg_stat_reset_trio_blocked_on_both_surfaces():
    """D6-F2 (major, live-verified on the sql surface): stats reset has NO
    read-only-transaction check (pg_stat_reset_shared('bgwriter') is
    CLUSTER-level) — and \\bpg_stat_reset\\b cannot cover _shared/_slru because
    '_' is a word character, so all three names must be listed verbatim; each
    must hit EXACTLY itself (no shorter-entry shadowing). Benign pg_stat
    probes/columns stay readable."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in _STAT_RESET_FNS:
        for surface in ("sql", "analyze"):
            assert find_side_effect_calls(f"select {fn}(0)", surface=surface) == [fn]
    # dollar-quoted argument (mask blanks the body; the name stays visible)
    assert find_side_effect_calls("select pg_stat_reset_shared($$bgwriter$$)",
                                  surface="sql") == ["pg_stat_reset_shared"]
    for q in ("select pg_stat_reset()",
              "select pg_stat_reset_shared('bgwriter')",
              "select pg_stat_reset_slru()"):
        with pytest.raises(ValueError, match="副作用"):
            diagnose_mod._validate([q])
    for ok in ("select * from pg_stat_database",
               "select stats_reset from pg_stat_database",
               "select * from pg_stat_activity"):
        assert find_side_effect_calls(ok, surface="sql") == []


def test_blacklist_variant_families_consistent():
    """D6-F4/F5/F6: family completions a \\b boundary cannot reach from a
    shorter entry — lo_truncate64 (64-bit variant), the try/unlock halves of
    the advisory-lock family (consistent with the existing lock entries), and
    pg_replication_origin_session_assign (present through PG≤16, cross-version
    completeness). Benign lookalike identifiers are NOT collateral damage."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in _ALIAS_FNS + _LOCK_FNS + ("pg_replication_origin_session_assign",):
        for surface in ("sql", "analyze"):
            assert find_side_effect_calls(f"select {fn}(0)", surface=surface) == [fn]
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate(["select pg_advisory_unlock_all()"])
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate(["select lo_truncate64(0, 1)"])
    for ok in ("select * from pg_advisory_locks",       # the view: trailing 's' breaks \b
               "select lo_created_at from t",           # column: lo_creat + 'e' tail
               "select last_lock_hint from t"):
        assert find_side_effect_calls(ok, surface="analyze") == []


def test_write_smuggle_word_gate_perf_and_precision():
    """C6-F2 (major): the substring gate ("set" in low) still opened on word
    TAILS — "update "×20000 + settle/offset/upset (measured 20.1s) made every
    update start lazily rescan its tail while a \\bset\\b WORD never
    materialized. The gate is exact now (first \\bupdate\\b start vs last
    \\bset\\b start, both O(n)): tail-only corpora must return None in <2s,
    real word pairs must still hit in one pass, and ordering is precise (a set
    word BEFORE the update word keeps the gate closed)."""
    import re
    import time
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import mask_literals
    for tail in ("settle", " offset", " upset"):
        t0 = time.perf_counter()
        assert diagnose_mod.write_smuggle_search("update " * 20000 + tail) is None, tail
        assert time.perf_counter() - t0 < 2.0, tail
    t0 = time.perf_counter()
    assert diagnose_mod.write_smuggle_search("update " * 20000 + "set") is not None
    assert time.perf_counter() - t0 < 2.0
    # gate precision: no \bset\b word after any \bupdate\b word → bridge stays off
    for s in ("update settle", "update offset", "update upset", "update t settle",
              "set update t", "select offset_x, settle_y, upset_z from t",
              "select 'update t settle' as v"):
        assert diagnose_mod.write_smuggle_search(mask_literals(s)) is None, s
    # real word pairs still hit, leftmost start of the update word
    for s, start in (("update upset set", 0), ("upset update set", 6),
                     ("update t set y=1", 0)):
        m = diagnose_mod.write_smuggle_search(s)
        assert m is not None and m.start() == start, s
    m = diagnose_mod.write_smuggle_search(
        "with x as (update t set y=1 returning *) select 1")      # task smoke: raw text path
    assert m is not None and m.start() == 11
    # leftmost equivalence vs the historical combined pattern on gate-edge corpus
    historical = re.compile(
        r"\b(delete\s+from|insert\s+into|merge\s+into|into\b|truncate\b"
        r"|for\s+(no\s+key\s+)?(update|share)\b|for\s+key\s+share\b"
        r"|lock\s+table\b|lock\s+in\s+share\b|grant\b|revoke\b"
        r"|update\b[\s\S]*?\bset\b"
        r"|create\b|alter\b|drop\b|copy\b|vacuum\b|reindex\b|call\b|do\b)\b", re.I)
    corpus = [
        "update t settle", "update t offset", "update t upset",
        "select offset_x, settle_y from t",
        "set update t", "upset update set", "update upset set",
        "select 'update t set y=1' as smuggle_text",
        "select msg from logs where msg = 'offset settle upset'",
        "x " * 200 + "update t set y=1",                      # word pair deep in the tail
        "update t set y=1 " + "offset " * 200,                # word pair early, tails after
        "select 1 set from t",                                # bare set word, no update
    ]
    for q in corpus:
        masked = mask_literals(q)
        h = historical.search(masked)
        g = diagnose_mod.write_smuggle_search(masked)
        assert (h is None) == (g is None), q
        if h is not None:
            assert (h.start(), h.group(0)) == (g.start(), g.group(0)), q


# ---------------- round-7 adversarial review: BRIN/GIN index-maintenance,
# replication-slot, WAL-message, shared-advisory, PG17-reset, ls-dir variant and
# backup families (D7-F2/F3/F4/F7/F8/F9/F10), ſ casefold gate boundary (C7-F4) ----------------

_R7_FAMILIES = (
    "brin_summarize_new_values", "brin_summarize_range", "brin_desummarize_range",
    "gin_clean_pending_list",
    "pg_create_physical_replication_slot", "pg_create_logical_replication_slot",
    "pg_drop_replication_slot", "pg_replication_slot_advance",
    "pg_logical_emit_message",
    "pg_advisory_lock_shared", "pg_try_advisory_lock_shared",
    "pg_try_advisory_xact_lock_shared", "pg_advisory_unlock_shared",
    "pg_stat_reset_replication_slot", "pg_stat_reset_subscription_stats",
    "pg_ls_waldir", "pg_ls_archive_statusdir", "pg_ls_logdir", "pg_ls_tmpdir",
    "pg_backup_start", "pg_backup_stop",
)


def test_r7_side_effect_families_blocked_on_both_surfaces():
    """D7-F2 (blocker, live-verified in review round 7): select
    brin_summarize_new_values('t') returned 44 on a READ-ONLY session and
    re-ran to 0 — the summarized pages were COMMITTED (index maintenance
    functions carry no PreventCommandIfReadOnly); the GIN pending-list cleaner
    is the same class of committed index write. D7-F3: a persistent
    physical/logical replication slot pins WAL forever and drop/advance break
    replication. D7-F4/F7/F8/F9/F10 complete the families (WAL message write,
    the four _shared advisory variants, the PG17+ per-object stats resets, the
    named pg_ls_* filesystem listings, the backup gate). Every name joins BOTH
    blacklists verbatim ('_' is a word char: \\bpg_ls_dir\\b can never cover
    pg_ls_waldir), survives dollar-quote argument masking, stays visible as a
    quoted-identifier call, and is refused by diagnose._validate."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in _R7_FAMILIES:
        for surface in ("sql", "analyze"):
            assert find_side_effect_calls(f"select {fn}(0)", surface=surface) == [fn], fn
        # dollar-quoted argument: the mask blanks the body, the NAME stays visible
        assert find_side_effect_calls(f"select {fn}($$arg$$)", surface="sql") == [fn], fn
        # quoted-identifier call syntax is equally visible (round-3 F1 unquoting)
        assert find_side_effect_calls(f'select "{fn}"(0)', surface="sql") == [fn], fn
        with pytest.raises(ValueError, match="副作用"):
            diagnose_mod._validate([f"select {fn}(0)"])
    # the live-verified repro shape from review round 7 (read-only session, committed)
    assert find_side_effect_calls("select brin_summarize_new_values('t')",
                                  surface="sql") == ["brin_summarize_new_values"]
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate(["select brin_summarize_new_values('t')"])


_R7_U_WRAPS = (
    ('select U&"bri\\006e_summarize_new_values"(\'t\')', "brin_summarize_new_values"),
    ('select U&"brin_summarize_\\0072ange"(\'t\')', "brin_summarize_range"),
    ('select U&"gin_c\\006cean_pending_list"(\'i\')', "gin_clean_pending_list"),
    ('select U&"pg_create_\\0070hysical_replication_slot"(\'x\')',
     "pg_create_physical_replication_slot"),
    ('select U&"pg_\\0064rop_replication_slot"(\'x\')', "pg_drop_replication_slot"),
    ('select U&"pg_logica\\006c_emit_message"(true, \'\', \'\')',
     "pg_logical_emit_message"),
    ('select U&"pg_adviso\\0072y_lock_shared"(1)', "pg_advisory_lock_shared"),
    ('select U&"pg_try_advisory_\\0078act_lock_shared"(1)',
     "pg_try_advisory_xact_lock_shared"),
    ('select U&"pg_stat_reset_\\0072eplication_slot"(NULL)',
     "pg_stat_reset_replication_slot"),
    ('select U&"pg_stat_reset_\\0073ubscription_stats"()',
     "pg_stat_reset_subscription_stats"),
    ('select U&"pg_ls_wa\\006cdir"()', "pg_ls_waldir"),
    ('select U&"pg_ls_archive_\\0073tatusdir"()', "pg_ls_archive_statusdir"),
    ('select U&"pg_backup_sta\\0072t"(\'x\', false)', "pg_backup_start"),
    ('select U&"pg_backup_\\0073top"(false)', "pg_backup_stop"),
)


def test_r7_u_escape_wrapped_family_names_caught():
    """Spot-check the round-4 D-F1 decoder composed with the round-7 families:
    U&-escaped fragments of each new family name decode to the same visible
    text, so the completed blacklists catch them on both surfaces."""
    from pgtuner.sqlsplit import find_side_effect_calls
    for sql, fn in _R7_U_WRAPS:
        assert find_side_effect_calls(sql, surface="sql") == [fn], sql
        assert find_side_effect_calls(sql, surface="analyze") == [fn], sql


def test_r7_family_near_misses_not_collateral():
    """Zero friendly fire around the new families: views, read-only accessors
    and lookalike columns keep passing on both surfaces, and pg_ls_dir stays
    exactly itself beside its new siblings (each full name hits exactly itself
    — no shorter-entry shadowing in either direction)."""
    from pgtuner.sqlsplit import find_side_effect_calls
    for ok in ("select * from pg_replication_slots",          # the view: trailing 's' (like pg_advisory_locks)
               "select * from pg_stat_replication",           # stats view
               "select * from pg_stat_subscription",          # vs pg_stat_reset_subscription_stats
               "select pg_stat_get_replication_slot(NULL)",   # read-only accessor vs the reset name
               "select brin_desummarized, brin_summarizer from t",  # brin lookalike columns
               "select gin_clean_pending from t",             # prefix tail of gin_clean_pending_list
               "select gin_index, gin_pending_hint from t",   # gin lookalike words
               "select pg_backup_start_time()",               # REAL read-only fn: name + '_time' tail
               "select waldir_size, logdir_name, tmpdir_path from t",  # ls-dir lookalike columns
               "select * from pg_advisory_locks"):            # view stays readable (re-pin)
        assert find_side_effect_calls(ok, surface="sql") == [], ok
        assert find_side_effect_calls(ok, surface="analyze") == [], ok
    # new and existing ls entries hit exactly themselves side by side
    for surface in ("sql", "analyze"):
        assert find_side_effect_calls("select pg_ls_dir('.'), pg_ls_waldir()",
                                      surface=surface) == ["pg_ls_dir", "pg_ls_waldir"]


def test_write_smuggle_gate_casefold_long_s_boundary():
    """C7-F4: re.I folds ſ (U+017F) onto s — the historical combined pattern
    matched 'update t ſet k=1' — while the gate's lower() kept the ſ, closing
    the gate (returned None) and breaking write_smuggle_search's
    full-equivalence claim. The server lexes ſet as an identifier (not
    exploitable), but the equivalence claim itself must hold: the gate
    casefolds now, so BOTH sides hit at the SAME leftmost position; ſ word
    TAILS stay closed; and casefold's expansion cases ('İ' → 'i̇') are
    conservative-only (the gate may open, the re.I bridge then simply fails)."""
    import re
    import time
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import mask_literals
    sql = "update t ſet k=1"
    bridge = diagnose_mod._UPDATE_SMUGGLE.search(sql)      # the re.I side really folds ſ→s
    assert bridge is not None and bridge.start() == 0
    m = diagnose_mod.write_smuggle_search(sql)             # the gate side must fold it too
    assert m is not None
    assert (m.start(), m.group(0)) == (bridge.start(), bridge.group(0)) == (0, "update t ſet")
    with pytest.raises(ValueError, match="只读"):
        diagnose_mod._validate(["WITH u AS (UPDATE t ſet k=1 RETURNING *) SELECT * FROM u"])
    # ſ tails and reversed order keep the gate closed exactly like \bset\b rejects them
    for s in ("update t ſettle", "update t oſet", "ſet update t"):
        assert diagnose_mod.write_smuggle_search(mask_literals(s)) is None, s
    # equivalence vs the historical combined pattern on the ſ corpus (the
    # 'İ' expansion case included: fold-fabricated words only ever waste one
    # bounded bridge scan, the result stays identical)
    historical = re.compile(
        r"\b(delete\s+from|insert\s+into|merge\s+into|into\b|truncate\b"
        r"|for\s+(no\s+key\s+)?(update|share)\b|for\s+key\s+share\b"
        r"|lock\s+table\b|lock\s+in\s+share\b|grant\b|revoke\b"
        r"|update\b[\s\S]*?\bset\b"
        r"|create\b|alter\b|drop\b|copy\b|vacuum\b|reindex\b|call\b|do\b)\b", re.I)
    corpus = [
        "update t ſet k=1",
        "UPDATE T ſET K=1",
        "with u as (update t ſet y=1 returning *) select 1",
        "update ſet",
        "update t ſettle",
        "update t oſet",
        "ſet update t",
        "select 'update t ſet k=1' as smuggle_text",
        "update t İset k=1",                    # casefold expansion: bridge cannot match
        "x " * 200 + "update t ſet y=1",        # ſ pair deep in the tail
    ]
    for q in corpus:
        masked = mask_literals(q)
        h = historical.search(masked)
        g = diagnose_mod.write_smuggle_search(masked)
        assert (h is None) == (g is None), q
        if h is not None:
            assert (h.start(), h.group(0)) == (g.start(), g.group(0)), q
    # the C5-F2/C6-F2 perf contract is untouched by the casefold switch
    t0 = time.perf_counter()
    assert diagnose_mod.write_smuggle_search("update " * 20000 + "ſettle") is None
    assert time.perf_counter() - t0 < 2.0


# ---------------- round-8 adversarial review: PG18+ statistics import/clear
# family (D8-F1/F1b/F3), variant completions (D8-F5/F6, C8-F5) and the
# systematic family sweep over the live rel19 (PG19devel) pg_catalog surface,
# then round-9: red-team confirmation gaps (R9-F1..F12) plus the INVERTED
# full sweep (complete pg_catalog dump − blacklists − allow groups −
# documented read-only pattern groups = zero manual-review remainder) ----------------

_R8_BOTH_FNS = (
    # D8-F1 (BLOCKER): PG18+ statistics import/clear + PG17-dev old names
    "pg_restore_relation_stats", "pg_restore_attribute_stats", "pg_restore_extended_stats",
    "pg_clear_relation_stats", "pg_clear_attribute_stats", "pg_clear_extended_stats",
    "set_relation_stats", "set_attribute_stats",
    # D8-F1b / D8-F3: collation import, per-backend and legacy per-object resets
    "pg_import_system_collations", "pg_stat_reset_backend_stats",
    "pg_stat_reset_single_table_counters", "pg_stat_reset_single_function_counters",
    # D8-F5 / D8-F6: the blocking shared xact lock, slot-CONSUME variants
    "pg_advisory_xact_lock_shared",
    "pg_logical_slot_get_changes", "pg_logical_slot_get_binary_changes",
    # C8-F5: PG<10 old aliases
    "pg_switch_xlog", "pg_xlog_replay_pause", "pg_xlog_replay_resume",
    # sweep: snapshot/origin resets, stats-snapshot clear, WAL-summary trio
    "pg_export_snapshot", "pg_import_snapshot",
    "pg_replication_origin_session_reset", "pg_replication_origin_xact_reset",
    "pg_stat_clear_snapshot",
    "pg_ls_summariesdir", "pg_available_wal_summaries", "pg_wal_summary_contents",
    # sweep: D7-F9 family fan-out misses (dir listings PG17+)
    "pg_ls_logicalmapdir", "pg_ls_logicalsnapdir", "pg_ls_replslotdir",
    # sweep: the binary_upgrade hijack family (server-gated outside upgrade
    # mode — the entries front-run the identical server refusal)
    "binary_upgrade_create_empty_extension", "binary_upgrade_create_conflict_detection_slot",
    "binary_upgrade_replorigin_advance", "binary_upgrade_add_sub_rel_state",
    "binary_upgrade_set_missing_value", "binary_upgrade_set_record_init_privs",
    "binary_upgrade_set_next_pg_type_oid", "binary_upgrade_set_next_array_pg_type_oid",
    "binary_upgrade_set_next_heap_pg_class_oid", "binary_upgrade_set_next_heap_relfilenode",
    "binary_upgrade_set_next_index_pg_class_oid", "binary_upgrade_set_next_index_relfilenode",
    "binary_upgrade_set_next_multirange_pg_type_oid",
    "binary_upgrade_set_next_multirange_array_pg_type_oid",
    "binary_upgrade_set_next_pg_authid_oid", "binary_upgrade_set_next_pg_enum_oid",
    "binary_upgrade_set_next_pg_tablespace_oid",
    "binary_upgrade_set_next_toast_pg_class_oid", "binary_upgrade_set_next_toast_relfilenode",
    "pg_stop_making_pinned_objects",
)


def test_r8_stats_import_and_sweep_families_blocked_on_both_surfaces():
    """D8-F1 (blocker, live-verified in review round 8): the PG18+ statistics
    import family really persists (pg_restore_relation_stats moved reltuples
    10→4242 on a read-only surface) — every name joins BOTH blacklists verbatim
    ('_' is a word char, so no shorter entry can cover a longer sibling), hits
    exactly itself, stays visible as a quoted call, and is refused by
    diagnose._validate."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in _R8_BOTH_FNS:
        for surface in ("sql", "analyze"):
            assert find_side_effect_calls(f"select {fn}(0)", surface=surface) == [fn], fn
        assert find_side_effect_calls(f'select "{fn}"(0)', surface="sql") == [fn], fn
        with pytest.raises(ValueError, match="副作用"):
            diagnose_mod._validate([f"select {fn}(0)"])
    # the live-verified repro shapes and the smoke trio from the round-8 report
    for sql, want in (("select pg_restore_relation_stats('t'::regclass, 4242)",
                       ["pg_restore_relation_stats"]),
                      ("select pg_import_system_collations('libc')",
                       ["pg_import_system_collations"]),
                      ("select pg_advisory_xact_lock_shared(1)",
                       ["pg_advisory_xact_lock_shared"])):
        assert find_side_effect_calls(sql, surface="sql") == want, sql
        assert find_side_effect_calls(sql, surface="analyze") == want, sql
    # U&-wrapped new family names decode to the same visible text
    assert find_side_effect_calls(
        'select U&"pg_\\0072estore_relation_stats"(\'t\', 1)', surface="sql") \
        == ["pg_restore_relation_stats"]
    assert find_side_effect_calls(
        'select U&"pg_logical_slot_get_ch\\0061nges"(\'s\', NULL)', surface="sql") \
        == ["pg_logical_slot_get_changes"]


def test_r8_slot_consume_vs_peek_and_sleep_differential():
    """D8-F6 boundary: the get variants consume the logical slot and durably
    advance confirmed_flush_lsn (blocked), while the peek twins decode without
    consuming (allowed on both surfaces). The sleep differential keeps its
    documented shape with the third family member pg_sleep_for: blocked where
    EXPLAIN ANALYZE would really run it, allowed on the statement_timeout-capped
    sql surface."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in ("pg_logical_slot_get_changes", "pg_logical_slot_get_binary_changes"):
        for surface in ("sql", "analyze"):
            assert find_side_effect_calls(f"select {fn}('s', NULL)", surface=surface) == [fn]
    for ok in ("select pg_logical_slot_peek_changes('s', NULL)",
               "select pg_logical_slot_peek_binary_changes('s', NULL)"):
        assert find_side_effect_calls(ok, surface="sql") == [], ok
        assert find_side_effect_calls(ok, surface="analyze") == [], ok
        diagnose_mod._validate([ok])                    # does not raise
    assert find_side_effect_calls("select pg_sleep_for('1 min')",
                                  surface="analyze") == ["pg_sleep_for"]
    assert find_side_effect_calls("select pg_sleep_for('1 min')",
                                  surface="sql") == []   # capped by statement_timeout
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate(["select pg_sleep_for('1 min')"])


_R8_ALLOWED_FNS = (
    # pg_stat_get_* accessor tail (reads; only the RESETS are blocked)
    "pg_stat_get_stat_reset_time", "pg_stat_get_db_stat_reset_time",
    "pg_stat_get_function_stat_reset_time", "pg_stat_get_bgwriter_stat_reset_time",
    "pg_stat_get_checkpointer_stat_reset_time", "pg_stat_get_blocks_fetched",
    "pg_stat_get_blocks_hit", "pg_stat_get_db_blocks_fetched", "pg_stat_get_db_blocks_hit",
    "pg_stat_get_xact_blocks_fetched", "pg_stat_get_xact_blocks_hit",
    "pg_stat_get_checkpointer_write_time", "pg_stat_get_db_blk_write_time",
    "pg_stat_get_checkpointer_restartpoints_performed",
    "pg_stat_get_checkpointer_restartpoints_requested",
    "pg_stat_get_checkpointer_restartpoints_timed",
    "pg_stat_get_db_conflict_lock", "pg_stat_get_db_conflict_startup_deadlock",
    "pg_stat_get_db_deadlocks", "pg_stat_get_db_sessions_killed",
    "pg_stat_get_backend_activity_start", "pg_stat_get_backend_start",
    "pg_stat_get_backend_xact_start", "pg_stat_get_bgwriter_buf_written_clean",
    "pg_stat_get_bgwriter_maxwritten_clean", "pg_stat_get_lock",
    # wait-graph / state / validator reads
    "pg_blocking_pids", "pg_safe_snapshot_blocking_pids",
    "pg_current_xact_id_if_assigned", "txid_current_if_assigned",
    "pg_postmaster_start_time", "pg_get_wal_summarizer_state", "pg_lock_status",
    "pg_event_trigger_dropped_objects", "pg_event_trigger_table_rewrite_oid",
    "pg_event_trigger_table_rewrite_reason", "pg_input_is_valid", "pg_input_error_info",
    "pg_isolation_test_session_is_blocked",
    # pure value-level functions the semantic roots drag in
    "aclremove", "array_remove", "ts_rewrite", "starts_with", "text_starts_with_support",
    "tid_block", "unicode_assigned", "clock_timestamp", "prsd_start",
    "brin_bloom_summary_in", "brin_bloom_summary_out",
    "brin_bloom_summary_recv", "brin_bloom_summary_send",
    "brin_minmax_multi_summary_in", "brin_minmax_multi_summary_out",
    "brin_minmax_multi_summary_recv", "brin_minmax_multi_summary_send",
    # descriptor-only lo_* readers (lo_open itself is refused in ALL modes)
    "lo_get", "lo_close", "lo_lseek", "lo_lseek64", "lo_tell", "lo_tell64",
    # read-only pg_upgrade check helper + example read-only probes from the task
    "binary_upgrade_check_logical_slot_pending_wal", "pg_backup_start_time",
    "pg_size_pretty",
)


def test_r8_allowed_group_zero_false_positives():
    """The sweep's deliberately-allowed group must stay exact zero-hit on BOTH
    surfaces (each name was audited read-only against the live rel19 catalog);
    lookalike columns around the new families are not collateral damage
    either."""
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in _R8_ALLOWED_FNS:
        assert find_side_effect_calls(f"select {fn}(0)", surface="sql") == [], fn
        assert find_side_effect_calls(f"select {fn}(0)", surface="analyze") == [], fn
    for ok in ("select restored_at, imported_from, exported_path from t",
               "select summaries_total, logicalmap_hint, replslot_name from t",
               "select clear_hint, reset_counter, switch_state from t",
               "select * from pg_stat_subscription",
               "select next_oid, set_at, binary_upgrade_mode from t"):
        assert find_side_effect_calls(ok, surface="sql") == [], ok
        assert find_side_effect_calls(ok, surface="analyze") == [], ok


# ---------------- round-9: confirmation-gap families (R9-F1..F12) and the
# three mutating names the inverted sweep itself surfaced ----------------

_R9_FNS = (
    # R9-F1 (BLOCKER): persistent cluster-level checksum flip + rewrite scan;
    # R9-F2: persistent slot creation (measured landing on disk from analyze)
    "pg_disable_data_checksums", "pg_enable_data_checksums",
    "pg_copy_physical_replication_slot", "pg_copy_logical_replication_slot",
    # R9-F3: any-session memory-context dump into the server log (loopable);
    # R9-F4: writes directly into WAL
    "pg_log_backend_memory_contexts", "pg_log_standby_snapshot",
    # R9-F7: the origin-setup twins (reset halves were already blocked);
    # R9-F8: real slot changes on a standby
    "pg_replication_origin_session_setup", "pg_replication_origin_xact_setup",
    "pg_sync_replication_slots",
    # R9-F9: real xid allocation (the *_if_assigned twins allocate nothing)
    "txid_current", "pg_current_xact_id",
    # R9-F11: benign flush (consistency with pg_stat_clear_snapshot) and the
    # server-refused extension helper (front-run, the nextval precedent)
    "pg_stat_force_next_flush", "pg_extension_config_dump",
    # R9-F12: adminpack legacy pg_catalog file I/O (absent from rel19 —
    # blocked by policy on old instances, the same stance as lo_creat)
    "pg_file_read", "pg_file_write", "pg_file_rename", "pg_file_unlink",
    "pg_file_ls",
    # round-9 inverted-sweep finds — live-verified on the read-only sql
    # surface (each call RETURNED, piercing the read-only transaction):
    # set_config = sticky session GUC hijack (pg_advisory_lock class),
    # pg_notify = cross-session queue write, setseed = benign-but-mutating
    # backend PRNG state (pg_stat_force_next_flush class)
    "set_config", "pg_notify", "setseed",
)


def test_r9_confirmation_gaps_and_sweep_finds_blocked_on_both_surfaces():
    """R9-F1..F12 plus the sweep finds: every name joins BOTH blacklists
    verbatim, hits exactly itself ('_' is a word char; \\btxid_current\\b can
    never cover txid_current_if_assigned or txid_current_snapshot), stays
    visible as a quoted call, and is refused by diagnose._validate."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in _R9_FNS:
        for surface in ("sql", "analyze"):
            assert find_side_effect_calls(f"select {fn}(0)", surface=surface) == [fn], fn
        assert find_side_effect_calls(f'select "{fn}"(0)', surface="sql") == [fn], fn
        with pytest.raises(ValueError, match="副作用"):
            diagnose_mod._validate([f"select {fn}(0)"])
    # the three live-verified repro shapes from the round-9 report
    assert find_side_effect_calls("select set_config('search_path','x',false)",
                                  surface="sql") == ["set_config"]
    assert find_side_effect_calls("select pg_notify('ch','x')",
                                  surface="analyze") == ["pg_notify"]
    assert find_side_effect_calls("select pg_disable_data_checksums()",
                                  surface="sql") == ["pg_disable_data_checksums"]
    # U&-wrapped new names decode to the same visible text (D-F1 composition)
    assert find_side_effect_calls(
        'select U&"set_\\0063onfig"(\'a\',\'b\',false)', surface="sql") == ["set_config"]
    assert find_side_effect_calls(
        'select U&"pg_copy_logical_replication_\\0073lot"(\'a\',\'b\')',
        surface="sql") == ["pg_copy_logical_replication_slot"]
    assert find_side_effect_calls(
        'select U&"txid_\\0063urrent"()', surface="analyze") == ["txid_current"]


def test_r9_xid_snapshot_and_value_setters_not_collateral():
    """Zero friendly fire around the round-9 additions: the *_if_assigned
    twins and snapshot readers stay allowed (R9-F9 boundary) and value-level
    set_*/convert/seed-adjacent names are copy-in/copy-out. The round-9
    ALLOWANCE for the query-string executors ts_stat / query_to_xml is
    FLIPPED to rejection (R10 evidence: red-team round 10 measured
    ts_stat embedding pg_stat_reset_single_table_counters genuinely
    resetting statistics through BOTH the read-only sql surface and
    diagnose --analyze — the inner SQL rides in a masked string literal
    and SPI read_only blocks only DML/DDL, not function side effects)."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for ok in ("select pg_current_xact_id_if_assigned()",
               "select txid_current_if_assigned()",
               "select txid_current_snapshot()", "select pg_current_snapshot()",
               "select currval('s')", "select lastval()",
               "select set_bit(b, 1, 1)", "select set_byte(b, 1, 1)",
               "select set_masklen(i, 24)", "select setweight(v, 'a')",
               "select pg_stat_have_stats('relation', 0, 0)",
               "select pg_replication_origin_session_is_setup()",
               "select * from pg_replication_origin_status"):
        assert find_side_effect_calls(ok, surface="sql") == [], ok
        assert find_side_effect_calls(ok, surface="analyze") == [], ok
    diagnose_mod._validate(["select txid_current_snapshot()"])   # does not raise
    # R10 FLIP (these two were in the allowed loop in round 9; rejected
    # since round 10 with the live-verified ts_stat/pg_stat_reset evidence)
    for bad, hit in (("select query_to_xml('select 1', true, false, '')",
                      "query_to_xml"),
                     ("select ts_stat('select to_tsvector(''a'')')", "ts_stat")):
        assert find_side_effect_calls(bad, surface="sql") == [hit], bad
        assert find_side_effect_calls(bad, surface="analyze") == [hit], bad
    with pytest.raises(ValueError, match="副作用"):
        diagnose_mod._validate(["select ts_stat('select to_tsvector(''a'')')"])
    # lookalike columns around the new families are not collateral damage
    for ok in ("select set_config_hint, setseeded_at, notifier_col from t",
               "select checksum_state, xid_current_hint, sync_hint from t",
               "select copy_hint, file_hint, setup_at, flush_at from t",
               "select log_hint, extension_dump_hint from t"):
        assert find_side_effect_calls(ok, surface="sql") == [], ok
        assert find_side_effect_calls(ok, surface="analyze") == [], ok


# ---------------- round-10: query-string executors (R10-F1) ----------------

_R10_FNS = (
    # R10-F1 (BLOCKER, live-verified): these execute an SQL statement handed
    # in as a STRING LITERAL — mask_literals blanks the inner query (every
    # blacklist is blind to calls inside it) and the executor's SPI
    # read_only=true shares the session read-only mechanism: it stops
    # DML/DDL only, never side effects of functions without a
    # PreventCommandIfReadOnly check (pg_stat_reset & co. have none). The
    # red-team round 10 measured ts_stat embedding
    # pg_stat_reset_single_table_counters genuinely resetting statistics
    # through BOTH the read-only sql surface and diagnose --analyze.
    "ts_stat", "query_to_xml", "query_to_xmlschema",
    "query_to_xml_and_xmlschema", "cursor_to_xml", "cursor_to_xmlschema",
)


def test_r10_query_string_executors_blocked_on_both_surfaces():
    """R10-F1: every query-string executor joins BOTH blacklists verbatim,
    stays visible as a quoted call, and is refused by diagnose._validate;
    the three live PoC shapes, the U&-wrapped name and the legitimate
    text-search probes round out the boundary."""
    import pytest
    from pgtuner import diagnose as diagnose_mod
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in _R10_FNS:
        for surface in ("sql", "analyze"):
            assert find_side_effect_calls(f"select {fn}('select 1')",
                                          surface=surface) == [fn], fn
        assert find_side_effect_calls(f'select "{fn}"(\'select 1\')',
                                      surface="sql") == [fn], fn
        with pytest.raises(ValueError, match="副作用"):
            diagnose_mod._validate([f"select {fn}('select 1')"])
    # the three R10 PoC shapes: the inner SQL is a masked string literal, so
    # the ONLY visible hit is the executor itself — exactly the name that
    # must carry the rejection for the blacklist to see the inner query
    for poc in ("select count(*) from ts_stat("
                "'select pg_stat_reset_single_table_counters(''t'')')",
                "select ts_stat('select pg_terminate_backend(0)')",
                "select ts_stat('select lo_import(''/tmp/poc'')')"):
        for surface in ("sql", "analyze"):
            assert find_side_effect_calls(poc, surface=surface) == ["ts_stat"], poc
        with pytest.raises(ValueError, match="副作用"):
            diagnose_mod._validate([poc])
    # U&-wrapped executor names decode to the same visible text (D-F1
    # composition: U&"ts_st\0061t" and U&"query_to_\0078ml" == the names)
    assert find_side_effect_calls(
        "select U&\"ts_st\\0061t\"('select 1')", surface="sql") == ["ts_stat"]
    assert find_side_effect_calls(
        "select U&\"query_to_\\0078ml\"('select 1', true, false, '')",
        surface="analyze") == ["query_to_xml"]
    # contrast: legitimate text-search PROBES stay callable — to_tsvector is
    # a value function and ts_debug's arguments are regconfig+text (a
    # DOCUMENT to tokenize, not SQL to execute)
    for ok in ("select to_tsvector('x')",
               "select to_tsvector('english', 'hello world')",
               "select ts_debug('x')",
               "select ts_debug('english', 'hello world')"):
        assert find_side_effect_calls(ok, surface="sql") == [], ok
        assert find_side_effect_calls(ok, surface="analyze") == [], ok
    diagnose_mod._validate(["select to_tsvector('x')"])   # does not raise
    diagnose_mod._validate(["select ts_debug('x')"])      # does not raise


# Round-9 inverted-sweep allow additions (audited read-only against the live
# rel19 catalog; frozen here so a future blacklist regression on any of them
# fails the snapshot contract). Grouped by the audit rationale:
_R9_ALLOWED_FNS = (
    # sequence reads (nextval/setval/pg_nextoid stay blocked) + lo descriptor reader
    "currval", "lastval", "loread",
    # snapshot / xid / WAL-position reads
    "pg_current_snapshot", "pg_current_wal_lsn", "pg_current_wal_flush_lsn",
    "pg_current_wal_insert_lsn", "pg_last_wal_receive_lsn", "pg_last_wal_replay_lsn",
    "pg_last_xact_replay_timestamp", "pg_is_in_recovery", "pg_is_wal_replay_paused",
    "pg_last_committed_xact", "pg_xact_commit_timestamp",
    "pg_xact_commit_timestamp_origin", "pg_xact_status", "txid_status",
    "pg_notification_queue_usage", "pg_listening_channels", "pg_wal_lsn_diff",
    "pg_walfile_name", "pg_walfile_name_offset", "pg_split_walfile_name",
    # replication-origin READS completing the blocked family
    "pg_replication_origin_oid", "pg_replication_origin_progress",
    "pg_replication_origin_session_progress", "pg_replication_origin_session_is_setup",
    # structured config-audit views (raw file primitives stay blocked)
    "pg_hba_file_rules", "pg_ident_file_mappings", "pg_show_all_settings",
    "pg_show_all_file_settings", "pg_show_replication_origin_status",
    "pg_settings_get_flags", "pg_config",
    # catalog / metadata probes
    "pg_describe_object", "pg_identify_object", "pg_identify_object_as_address",
    "pg_options_to_table", "pg_partition_root", "pg_prepared_statement",
    "pg_prepared_xact", "pg_cursor", "pg_stat_have_stats", "pg_numa_available",
    "pg_backend_pid", "pg_conf_load_time", "pg_jit_available", "pg_my_temp_schema",
    "pg_is_other_temp_schema", "pg_trigger_depth", "pg_typeof", "pg_basetype",
    "pg_filenode_relation", "pg_char_to_encoding", "pg_client_encoding",
    "getdatabaseencoding", "getpgusername", "get_current_ts_config",
    "pg_extension_update_paths", "pg_available_extensions",
    "pg_available_extension_versions", "pg_mcv_list_items", "obj_description",
    "col_description", "shobj_description", "pg_tablespace_location",
    "pg_indexam_has_property", "pg_indexam_progress_phasename",
    "row_security_active", "querytree", "currtid2",
    "pg_event_trigger_ddl_commands",
    # xml mappers over NAMED relations + the text-search/xml VALUE family
    # (arguments are relation names or documents, never a query string to
    # execute). R10 flipped the query-string executor halves of the round-9
    # group (ts_stat, query_to_xml*, cursor_to_xml*) into BOTH blacklists —
    # see _R10_FNS; the SPI-read-only source rationale is retired for any
    # function whose argument is arbitrary SQL to execute.
    "table_to_xml", "table_to_xmlschema",
    "table_to_xml_and_xmlschema", "schema_to_xml", "schema_to_xmlschema",
    "schema_to_xml_and_xmlschema", "database_to_xml", "database_to_xmlschema",
    "database_to_xml_and_xmlschema", "ts_debug", "ts_lexize",
    "ts_parse", "ts_token_type", "xmlvalidate", "xmlexists", "xpath",
    "xpath_exists",
    # value-level setters/generators (copy-in/copy-out; no server state)
    "set_bit", "set_byte", "set_masklen", "setweight", "strip", "numnode",
    "parse_ident", "get_bit", "get_byte", "convert", "convert_from",
    "convert_to", "random", "random_normal", "gen_random_uuid", "uuidv4",
    "uuidv7", "in_range", "any_value", "error_on_null",
    "satisfies_hash_partition", "num_nonnulls", "num_nulls",
    "phraseto_tsquery", "plainto_tsquery", "aclcontains", "acldefault",
    "aclexplode", "aclitemeq", "aclinsert", "makeaclitem", "amvalidate",
    "anytextcat", "session_user", "system", "system_user", "version",
    "timeofday", "icu_unicode_version", "unicode_version", "pg_lsn",
)


def test_r9_allowed_group_zero_false_positives():
    """The round-9 allow additions must stay exact zero-hit on BOTH surfaces
    (each name was classified read-only by the inverted sweep against the
    live rel19 catalog)."""
    from pgtuner.sqlsplit import find_side_effect_calls
    for fn in _R9_ALLOWED_FNS:
        assert find_side_effect_calls(f"select {fn}(0)", surface="sql") == [], fn
        assert find_side_effect_calls(f"select {fn}(0)", surface="analyze") == [], fn


def _pattern_names(rx):
    """Entries of a \\b(alternation)\\b blacklist pattern, as a set."""
    body = rx.pattern
    body = body[3:] if body.startswith("\\b(") else body
    return set(body[:body.rfind(")")].split("|"))


# Frozen snapshot of the round-8 root sweep, updated by the round-9 INVERTED
# sweep (audit target: live rel19, PG19devel, the COMPLETE pg_catalog proname
# dump — 2803 distinct names via read-only `pgtuner sql` — classified as
# blacklist ∪ explicit allow ∪ documented read-only pattern groups until the
# manual-review remainder hit zero: 2803 = 123 blacklist + 66 round-8 allow
# + 136 round-9 allow + 2478 pattern-group hits). Round 9 added the R9-F1..F12
# confirmation gaps plus the sweep finds set_config / pg_notify / setseed
# (live-verified piercing the read-only sql surface) and the adminpack legacy
# pg_file_* seats (policy entries, absent from the rel19 dump). Round 10
# (R10-F1) MOVED the six query-string executors ts_stat / query_to_xml
# (_schema,_and_xmlschema) / cursor_to_xml(schema) from the round-9 allow
# group into BOTH blacklists — the reconciliation shifts to 129 blacklist +
# 66 round-8 + 130 round-9 allow + 2478 pattern = 2803, and the six names
# left _R9_ALLOWED_FNS into the blocked snapshot below. When a new PG
# release adds another name to any audited family — or on any PG major-version
# upgrade — rerun the INVERTED reconciliation (see the sweep comment block in
# pgtuner/sqlsplit.py for the pattern groups) and update BOTH blacklists
# together with this snapshot; this test failing is the reminder that the
# manual audit is due. No database is contacted at test time.
_SWEEP_BLOCKED_SNAPSHOT = frozenset("""
    binary_upgrade_add_sub_rel_state binary_upgrade_create_conflict_detection_slot
    binary_upgrade_create_empty_extension binary_upgrade_replorigin_advance
    binary_upgrade_set_missing_value binary_upgrade_set_next_array_pg_type_oid
    binary_upgrade_set_next_heap_pg_class_oid binary_upgrade_set_next_heap_relfilenode
    binary_upgrade_set_next_index_pg_class_oid binary_upgrade_set_next_index_relfilenode
    binary_upgrade_set_next_multirange_array_pg_type_oid
    binary_upgrade_set_next_multirange_pg_type_oid
    binary_upgrade_set_next_pg_authid_oid binary_upgrade_set_next_pg_enum_oid
    binary_upgrade_set_next_pg_tablespace_oid binary_upgrade_set_next_pg_type_oid
    binary_upgrade_set_next_toast_pg_class_oid binary_upgrade_set_next_toast_relfilenode
    binary_upgrade_set_record_init_privs brin_desummarize_range
    brin_summarize_new_values brin_summarize_range dblink dblink_exec
    gin_clean_pending_list largeobject lo_creat lo_create
    lo_export lo_from_bytea lo_import lo_open
    lo_put lo_truncate lo_truncate64 lo_unlink
    lowrite nextval pg_advisory_lock pg_advisory_lock_shared
    pg_advisory_unlock pg_advisory_unlock_all pg_advisory_unlock_shared pg_advisory_xact_lock
    pg_advisory_xact_lock_shared pg_available_wal_summaries pg_backup_start pg_backup_stop
    pg_cancel_backend pg_clear_attribute_stats pg_clear_extended_stats pg_clear_relation_stats
    pg_create_logical_replication_slot pg_create_physical_replication_slot pg_create_restore_point pg_current_logfile
    pg_drop_replication_slot pg_export_snapshot pg_import_snapshot pg_import_system_collations
    pg_logical_emit_message pg_logical_slot_get_binary_changes pg_logical_slot_get_changes pg_ls_archive_statusdir
    pg_ls_dir pg_ls_logdir pg_ls_logicalmapdir pg_ls_logicalsnapdir
    pg_ls_replslotdir pg_ls_summariesdir pg_ls_tmpdir pg_ls_waldir
    pg_nextoid pg_promote pg_read_binary_file pg_read_file
    pg_reload_conf pg_replication_origin_advance pg_replication_origin_create pg_replication_origin_drop
    pg_replication_origin_session_assign pg_replication_origin_session_reset pg_replication_origin_xact_reset pg_replication_slot_advance
    pg_restore_attribute_stats pg_restore_extended_stats pg_restore_relation_stats pg_rotate_logfile
    pg_stat_clear_snapshot pg_stat_file pg_stat_reset pg_stat_reset_backend_stats
    pg_stat_reset_replication_slot pg_stat_reset_shared pg_stat_reset_single_function_counters pg_stat_reset_single_table_counters
    pg_stat_reset_slru pg_stat_reset_subscription_stats pg_stat_statements_reset pg_stop_making_pinned_objects
    pg_switch_wal pg_switch_xlog pg_terminate_backend pg_try_advisory_lock
    pg_try_advisory_lock_shared pg_try_advisory_xact_lock pg_try_advisory_xact_lock_shared pg_wal_replay_pause
    pg_wal_replay_resume pg_wal_summary_contents pg_xlog_replay_pause pg_xlog_replay_resume
    postgres_fdw_connect set_attribute_stats set_relation_stats setval
""".split()) | frozenset("""
    pg_copy_logical_replication_slot pg_copy_physical_replication_slot
    pg_current_xact_id pg_disable_data_checksums pg_enable_data_checksums
    pg_extension_config_dump pg_file_ls pg_file_read pg_file_rename pg_file_unlink
    pg_file_write
    pg_log_backend_memory_contexts pg_log_standby_snapshot pg_notify
    pg_replication_origin_session_setup pg_replication_origin_xact_setup
    pg_stat_force_next_flush pg_sync_replication_slots set_config setseed
    txid_current
""".split()) | frozenset("""
    cursor_to_xml cursor_to_xmlschema query_to_xml query_to_xml_and_xmlschema
    query_to_xmlschema ts_stat
""".split())     # round-10 query-string executors (R10-F1), moved off the
                # round-9 allow group into both blacklists
_SWEEP_ANALYZE_ONLY_SNAPSHOT = frozenset(("pg_sleep", "pg_sleep_until", "pg_sleep_for"))
_SWEEP_ALLOWED_SNAPSHOT = (frozenset(_R8_ALLOWED_FNS) | frozenset(_R9_ALLOWED_FNS)
                           | frozenset(("pg_logical_slot_peek_changes",
                                       "pg_logical_slot_peek_binary_changes")))


def test_round9_sweep_snapshot_contract():
    """Reconciliation contract: the two blacklists must equal the frozen
    post-sweep snapshot EXACTLY — the sql list is the full blocked set, the
    analyze list adds precisely the sleep family (the documented surface
    differential), nothing else may appear or disappear silently, and no
    audited read-only name (the allowed groups) may ever be on a list. Hermetic:
    the snapshot is frozen data; a failing run means a family changed and a
    human must redo the live reconciliation."""
    from pgtuner.sqlsplit import SIDE_EFFECT_FUNCTIONS, SIDE_EFFECT_FUNCTIONS_SQL
    analyze = _pattern_names(SIDE_EFFECT_FUNCTIONS)
    sql = _pattern_names(SIDE_EFFECT_FUNCTIONS_SQL)
    assert sql == _SWEEP_BLOCKED_SNAPSHOT, (
        "sql blacklist diverged from the sweep snapshot: "
        f"added={sorted(sql - _SWEEP_BLOCKED_SNAPSHOT)} "
        f"removed={sorted(_SWEEP_BLOCKED_SNAPSHOT - sql)}")
    assert analyze == _SWEEP_BLOCKED_SNAPSHOT | _SWEEP_ANALYZE_ONLY_SNAPSHOT, (
        "analyze blacklist diverged from the sweep snapshot (blocked set + sleep family): "
        f"added={sorted(analyze - _SWEEP_BLOCKED_SNAPSHOT - _SWEEP_ANALYZE_ONLY_SNAPSHOT)} "
        f"removed={sorted((_SWEEP_BLOCKED_SNAPSHOT | _SWEEP_ANALYZE_ONLY_SNAPSHOT) - analyze)}")
    assert analyze - sql == _SWEEP_ANALYZE_ONLY_SNAPSHOT      # the differential is exactly the sleep family
    assert sql - analyze == set()
    friendly = (analyze | sql) & _SWEEP_ALLOWED_SNAPSHOT      # zero friendly fire on the allowed group
    assert not friendly, f"audited read-only names blacklisted: {sorted(friendly)}"
