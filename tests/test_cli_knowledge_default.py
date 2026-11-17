"""Regression for the bench default knowledge path being cwd-dependent (tool-w3 fix): the --knowledge default
was a relative path "knowledge/guc_knowledge.json" resolved against the process cwd, so running
bench/recommend/list bare outside the repository always died at load_knowledge("知识库文件不存在").
After the fix the default is anchored to the repository root derived from the package location
(same approach as paths.LEDGER_FILE), usable from any cwd; relative paths explicitly passed by the
user are still resolved against cwd (explicit-argument semantics unchanged). Does not depend on a
running PG (bench uses a mock connection and stops at the stage after knowledge loading).
"""
import os

from click.testing import CliRunner

from pgtuner import cli, paths


def test_default_knowledge_anchored_at_repo_root():
    """The default is an absolute path, anchored at the repository root (the editable install location), and the file really exists."""
    assert os.path.isabs(cli.DEFAULT_KNOWLEDGE)
    assert cli.DEFAULT_KNOWLEDGE == str(paths.KNOWLEDGE_FILE)
    assert paths.REPO_ROOT in paths.KNOWLEDGE_FILE.parents      # does not fork depending on cwd
    assert os.path.isfile(cli.DEFAULT_KNOWLEDGE)


def test_list_loads_default_knowledge_from_foreign_cwd(monkeypatch, tmp_path):
    """list directly loads DEFAULT_KNOWLEDGE (no PG/no mocks): from a non-repository cwd it should still print the knowledge table."""
    monkeypatch.chdir(tmp_path)                                 # an arbitrary non-repository directory
    result = CliRunner().invoke(cli.cli, ["knowledge"])
    assert result.exit_code == 0, result.output
    assert "知识库文件不存在" not in result.output
    assert "GUC 知识库" in result.output


def test_bench_default_knowledge_from_foreign_cwd(monkeypatch, tmp_path):
    """tool-w3 field case: running bench bare from a non-repository cwd (without --knowledge) — before the fix it died at
    "知识库文件不存在"; after the fix knowledge loading succeeds and it advances to the connect stage (the mock connection raises the missing-db error)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.pg, "connect", lambda dsn=None: (_ for _ in ()).throw(
        ConnectionError('库不存在？目标 /tmp:5437/bench_new。创建：createdb -h /tmp -p 5437 bench_new，'
                        "或用 --dsn 指向已有库\n原始错误: FATAL:  database \"bench_new\" does not exist")))
    exe = tmp_path / "pgbench"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    result = CliRunner().invoke(cli.cli, [
        "bench", "--dsn", "host=/tmp port=5437 dbname=bench_new",
        "--pgbench", str(exe), "--duration", "1"])             # without --knowledge
    assert "知识库文件不存在" not in result.output             # the default knowledge loads from any cwd
    assert "库不存在" in result.output                          # advanced to the connect stage


def test_explicit_relative_knowledge_still_resolves_against_cwd(monkeypatch, tmp_path):
    """Explicit relative path semantics unchanged: still resolved against the process cwd (not silently changed to repository-root-relative)."""
    monkeypatch.chdir(tmp_path)                                 # tmp has no knowledge/
    result = CliRunner().invoke(cli.cli, ["recommend", "--knowledge", "knowledge/guc_knowledge.json"])
    assert result.exit_code != 0
    assert "知识库文件不存在" in result.output


def test_lab_list_alias_and_knowledge_rename():
    """v0.7.1 regression: the `lab list` alias works; the top-level knowledge command is renamed to knowledge.

    Traceability: the first lab list implementation called lab_stats() directly — after @command
    decoration it is already a Command object, and calling it bare re-parses the original argv →
    "Got unexpected extra arguments".
    """
    from click.testing import CliRunner
    result = CliRunner().invoke(cli.cli, ["lab", "list"])
    assert result.exit_code == 0
    assert not CliRunner().invoke(cli.cli, ["knowledge"]).exception
    # the top-level list has stepped aside (avoiding confusion with the sub-group's habitual name)
    assert "list" not in cli.cli.commands
