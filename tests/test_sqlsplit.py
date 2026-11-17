"""sqlsplit regression: lexically aware splitting (the shared low-level component of cli._split_sql_file and sqlcmd's single-statement validation).

tool-w3 merge note: cli once carried its own scanner; it now uniformly delegates to this module — the behaviors locked here
are finer than the cli-layer tests (E'' backslash escaping, $ inside identifiers, unclosed constructs raising, comment→single-space semantics).
"""
import pytest

from pgtuner.sqlsplit import split_statements


def test_function_body_single_statement():
    """R3 reproduction case: a PLpgSQL function body is kept as one statement, not torn apart by inner semicolons."""
    stmts = split_statements(
        "CREATE FUNCTION f() RETURNS trigger AS $$\n"
        "BEGIN\n"
        "  UPDATE t SET x = 1; RETURN NEW;\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;\n"
        "SELECT count(*) FROM t;\n")
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE FUNCTION f()")
    assert "UPDATE t SET x = 1; RETURN NEW;" in stmts[0]


def test_semicolon_in_string_and_identifier():
    assert split_statements("SELECT 'a;b'; SELECT 2;") == ["SELECT 'a;b'", "SELECT 2"]
    assert split_statements('SELECT "a;b";') == ['SELECT "a;b"']
    assert split_statements('SELECT "a""b;c";') == ['SELECT "a""b;c"']


def test_escaped_quote_keeps_string_open():
    assert split_statements("SELECT 'it''s;fine';") == ["SELECT 'it''s;fine'"]


def test_escape_string_prefix_backslash():
    """Backslash escaping inside E'...': \' does not end the string; the semicolon after it stays inside the string."""
    assert split_statements("SELECT E'a\\';b'; SELECT 2;") == ["SELECT E'a\\';b'", "SELECT 2"]
    # in plain '...' the backslash is a literal character (standard_conforming_strings=on): the string ends at the second quote
    assert split_statements("SELECT 'a\\'; SELECT 2;") == ["SELECT 'a\\'", "SELECT 2"]


def test_dollar_tag_forms_and_nesting():
    assert split_statements("SELECT $fn$ ; $fn$;") == ["SELECT $fn$ ; $fn$"]
    stmts = split_statements("SELECT $outer$ a $$b;c$$ d $outer$; SELECT 2;")
    assert len(stmts) == 2
    assert "$$b;c$$" in stmts[0]


def test_dollar_after_identifier_char_not_opener():
    """A $ adjacent to identifier characters (e.g. the column name price$total) does not open a dollar-quote (PG lexical behavior)."""
    assert split_statements("SELECT price$total FROM t; SELECT 2;") \
        == ["SELECT price$total FROM t", "SELECT 2"]


def test_dollar_placeholder_not_quote():
    assert split_statements("EXECUTE p($1, $2);\nSELECT 2;") == ["EXECUTE p($1, $2)", "SELECT 2"]


def test_comments_replaced_by_space():
    """Comments are replaced wholesale by a single space (established D1 semantics: statement text contains no comments); except inside dollar-quoted bodies."""
    assert split_statements("-- h ; h\nSELECT 1;/* b ; b */SELECT 2;") == ["SELECT 1", "SELECT 2"]
    assert split_statements("SELECT 1 /* c /* nested ; */ c */ , 2;") == ["SELECT 1   , 2"]
    assert split_statements("SELECT $$x /* ; */ y$$;") == ["SELECT $$x /* ; */ y$$"]


def test_tail_without_semicolon_and_empty():
    assert split_statements("SELECT 1") == ["SELECT 1"]
    assert split_statements("  ; \n-- only comment\n") == []


def test_unclosed_raises():
    with pytest.raises(ValueError):
        split_statements("SELECT 'abc")
    with pytest.raises(ValueError):
        split_statements("SELECT $$abc")
    with pytest.raises(ValueError, match="块注释"):
        split_statements("SELECT /* unclosed ; SELECT 2;")
