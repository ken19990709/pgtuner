"""Bilingual help: PGTUNER_LANG=en swaps --help to English; zh stays the default.

Mechanism lives in pgtuner/i18n.py + pgtuner/help_en/ (translation table);
these tests pin the contract: full coverage (every CJK help text has a table
entry), no stale keys, rendered English help contains no CJK, and toggling
back to zh restores the originals.
"""
import re

import pytest
from click.testing import CliRunner

from pgtuner import cli, i18n
from pgtuner.help_en import HELP

CJK = re.compile(r"[\u4e00-\u9fff]")


@pytest.fixture()
def restore_zh():
    """Restore Chinese help texts after a test mutates the shared command tree."""
    yield
    i18n.apply(cli.cli)


def _walk_collect(node, path, want_cjk, sink):
    text = getattr(node, "help", None)
    if text and (not want_cjk or CJK.search(text)):
        sink.append(path)
    ep = getattr(node, "epilog", None)
    if ep and (not want_cjk or CJK.search(ep)):
        sink.append(path + ("__epilog__",))
    for p in node.params:
        ph = getattr(p, "help", None)   # click.Argument carries no help
        if ph and (not want_cjk or CJK.search(ph)):
            decl = p.opts[0] if getattr(p, "opts", None) else p.name
            sink.append(path + (decl,))
    for name, sub in getattr(node, "commands", {}).items():
        _walk_collect(sub, path + (name,), want_cjk, sink)


def _cjk_help_paths():
    sink = []
    _walk_collect(cli.cli, (), True, sink)
    return sink


def _all_live_paths():
    sink = []
    _walk_collect(cli.cli, (), False, sink)
    return set(sink)


def test_every_cjk_help_has_en_entry():
    missing = [p for p in _cjk_help_paths() if p not in HELP]
    assert not missing, f"help_en is missing keys: {missing}"


def test_no_stale_table_keys():
    live = _all_live_paths()
    stale = set(HELP) - live
    assert not stale, f"help_en has keys matching no live command/option: {stale}"


def test_root_help_english(monkeypatch, restore_zh):
    monkeypatch.setenv("PGTUNER_LANG", "en")
    r = CliRunner().invoke(cli.cli, ["--help"])
    assert r.exit_code == 0
    assert not CJK.search(r.output), r.output[:400]


def test_sample_subcommand_help_english(monkeypatch, restore_zh):
    monkeypatch.setenv("PGTUNER_LANG", "en")
    for argv in (["instance", "--help"], ["instance", "register", "--help"],
                 ["lab", "--help"], ["lab", "clone", "--help"],
                 ["rebench", "--help"], ["planbench", "--help"],
                 ["diagnose", "--help"], ["sql", "--help"],
                 ["results", "diff", "--help"], ["selftest", "--help"]):
        r = CliRunner().invoke(cli.cli, argv)
        assert r.exit_code == 0, argv
        assert not CJK.search(r.output), (argv, r.output[:400])


def test_zh_default_and_toggle_back(monkeypatch, restore_zh):
    r = CliRunner().invoke(cli.cli, ["--help"])
    assert CJK.search(r.output)                      # zh is the default
    monkeypatch.setenv("PGTUNER_LANG", "en")
    r = CliRunner().invoke(cli.cli, ["--help"])
    assert not CJK.search(r.output)
    monkeypatch.delenv("PGTUNER_LANG")
    r = CliRunner().invoke(cli.cli, ["--help"])
    assert CJK.search(r.output)                      # originals restored


def test_missing_entry_falls_back_to_zh(monkeypatch, restore_zh):
    import pgtuner.help_en as help_en_pkg
    monkeypatch.setattr(help_en_pkg, "HELP", {})
    monkeypatch.setenv("PGTUNER_LANG", "en")
    i18n.apply(cli.cli)
    assert CJK.search(cli.cli.commands["instance"].help)   # untouched → zh
