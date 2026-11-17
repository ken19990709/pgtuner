"""D1 regression: SQL file splitting must strip comments first (semicolons inside comments must not produce garbage statements).

tool-w3 extension: semicolons inside strings / dollar-quotes / double-quoted identifiers are not split on (minimal lexical scanner state machine) —
trigger/PLpgSQL function bodies were once blindly split into garbage statements, which errored outright when fed to rebench --queries-file / planbench.
"""
from pgtuner.cli import _split_sql_file


def _split(tmp_path, sql: str) -> list[str]:
    f = tmp_path / "q.sql"
    f.write_text(sql, encoding="utf-8")
    return _split_sql_file(str(f))


def test_split_strips_comments(tmp_path):
    f = tmp_path / "q.sql"
    f.write_text("-- header with ; semicolon\nSELECT 1;\n/* block ; comment */\nSELECT 2;\n",
                 encoding="utf-8")
    assert _split_sql_file(str(f)) == ["SELECT 1", "SELECT 2"]


def test_split_plain(tmp_path):
    f = tmp_path / "q.sql"
    f.write_text("SELECT 1;\nSELECT 2;", encoding="utf-8")
    assert _split_sql_file(str(f)) == ["SELECT 1", "SELECT 2"]


# ---------------------------- tool-w3: semicolons outside bare regions are not split on ----------------------------

def test_split_dollar_quoted_function_body(tmp_path):
    """tool-w3 regression: semicolons inside a PLpgSQL function body were once blindly split into 5 statements (4 garbage)."""
    stmts = _split(tmp_path,
                   "CREATE FUNCTION f() RETURNS trigger AS $$\n"
                   "BEGIN\n"
                   "  UPDATE t SET x = 1; RETURN NEW;\n"
                   "END;\n"
                   "$$ LANGUAGE plpgsql;\n"
                   "SELECT count(*) FROM t;\n")
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE FUNCTION f()")
    assert stmts[0].endswith("LANGUAGE plpgsql")
    assert "UPDATE t SET x = 1; RETURN NEW;" in stmts[0]      # function body kept as one statement
    assert stmts[1] == "SELECT count(*) FROM t"


def test_split_semicolon_in_string_literal(tmp_path):
    """Semicolons inside string literals are not split on."""
    assert _split(tmp_path, "SELECT 'a;b' AS s;\nSELECT 2;\n") == ["SELECT 'a;b' AS s", "SELECT 2"]


def test_split_escaped_quote_keeps_string_open(tmp_path):
    """'' is a literal quote, not a terminator: the semicolon in the latter half is still inside the string, not split on."""
    assert _split(tmp_path, "SELECT 'it''s;fine';\nSELECT 2;\n") \
        == ["SELECT 'it''s;fine'", "SELECT 2"]


def test_split_semicolon_in_quoted_identifier(tmp_path):
    """Semicolons inside double-quoted identifiers are not split on (same for "" escaping)."""
    assert _split(tmp_path, 'SELECT 1 FROM "a;b";\nSELECT 2;\n') \
        == ['SELECT 1 FROM "a;b"', "SELECT 2"]
    assert _split(tmp_path, 'SELECT "a""b;c";\n') == ['SELECT "a""b;c"']


def test_split_dollar_tag_named_and_nested(tmp_path):
    """$tag$ named form + nesting of different tags: closing requires the exact same tag; the inner $$...$$ is preserved as content."""
    stmts = _split(tmp_path,
                   "SELECT $fn$ body; $$inner; still;$$ tail $fn$;\n"
                   "SELECT 2;\n")
    assert stmts == ["SELECT $fn$ body; $$inner; still;$$ tail $fn$", "SELECT 2"]


def test_split_dollar_placeholder_is_not_dollar_quote(tmp_path):
    """$1/$2 placeholders (starting with a digit) do not form a dollar-quote; subsequent semicolons split as usual."""
    assert _split(tmp_path, "EXECUTE p($1, $2);\nSELECT 2;\n") \
        == ["EXECUTE p($1, $2)", "SELECT 2"]


def test_split_comment_inside_dollar_quote_preserved(tmp_path):
    """Comments inside a dollar-quote body are function-body content, preserved verbatim (semicolons inside must certainly not be split on)."""
    assert _split(tmp_path, "SELECT $$x /* ; */ y$$;\nSELECT 3;\n") \
        == ["SELECT $$x /* ; */ y$$", "SELECT 3"]
