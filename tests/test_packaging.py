"""Packaging consistency: __version__ must match the pyproject.toml declaration (R2 fix regression)."""
import re
from pathlib import Path

import pgtuner


def test_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"),
                         re.MULTILINE).group(1)
    assert pgtuner.__version__ == declared, (
        f"版本漂移: __init__.py={pgtuner.__version__} vs pyproject={declared}")


def test_console_script_entry_point_declared():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert 'pgtuner = "pgtuner.cli:cli"' in pyproject.read_text(encoding="utf-8")


def test_setup_script_present_executable_and_idempotent_markers():
    """One-shot dependency script (v0.9.0): exists, executable, carries idempotent probes and the pgss-must-install marker."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "setup.sh"
    text = script.read_text(encoding="utf-8")
    assert script.exists() and (script.stat().st_mode & 0o111), "setup.sh 须存在且可执行"
    # idempotent probes: each of the three stages has its own "already satisfied → skip" branch
    assert "pg_config 就位" in text and "apt 包全部就位" in text and "pip install -e 幂等" in text
    # pgss must be installed (without it statements/doctor degrade across the board)
    assert "contrib/pg_stat_statements" in text
    # header version stamp stays in sync with the package version
    assert f"v{pgtuner.__version__}" in text


def test_packaged_agents_is_concat_of_root_en_and_zh():
    """init 装进用户项目的 AGENTS.md 是双语合并版(特性):资产必须精确等于
    strip(AGENTS.md) + "\\n\\n---\\n\\n" + strip(AGENTS.zh-CN.md)(构造性校验,
    非字节拷贝)。strip 规则:每个根文件恰有一行语言切换行(EN 是
    "**English** | [简体中文](AGENTS.zh-CN.md)",ZH 是 "**简体中文** |
    [English](AGENTS.md)"),连同前导空行一起剥掉——该行在 assets 目录与 init
    落地的用户项目里都是死链(那里没有另一语言的文件可指),合并版不带它。
    可直接粘贴的再生成命令(python 一行)见 CONTRIBUTING.md 的 "Packaged
    assets must not drift" 条目,与本断言的公式逐字一致。"""
    root = Path(__file__).resolve().parents[1]
    en = root / "AGENTS.md"
    zh = root / "AGENTS.zh-CN.md"
    shipped = root / "pgtuner" / "assets" / "AGENTS.md"
    for f in (en, zh, shipped):
        assert f.is_file(), f"文件缺失: {f}"

    def _strip_switch(text: str, switch_line: str) -> str:
        # exactly one such line per root file, sandwiched between blank lines;
        # removing "\n\n<line>\n" -> "\n" keeps the title/quote spacing intact
        return text.replace("\n\n" + switch_line + "\n", "\n")

    expected = (
        _strip_switch(en.read_text(encoding="utf-8"),
                      "**English** | [简体中文](AGENTS.zh-CN.md)")
        + "\n\n---\n\n"
        + _strip_switch(zh.read_text(encoding="utf-8"),
                        "**简体中文** | [English](AGENTS.md)"))
    actual = shipped.read_text(encoding="utf-8")
    assert actual == expected, (
        f"打包 AGENTS.md 与拼接公式漂移: {shipped}"
        "(应为 剥离语言切换行后的 AGENTS.md + 空行---空行 + 剥离后的"
        " AGENTS.zh-CN.md,重跑 CONTRIBUTING.md 里的再生成命令)")


def test_packaged_skill_in_sync_with_repo_original():
    """v0.11.0 packaging fix: init distributes the packaged copy (wheel installs work);
    the repo-root original remains the live contract — this test is the drift guard."""
    root = Path(__file__).resolve().parents[1]
    src = root / ".zcode" / "skills" / "pg-experiment" / "SKILL.md"
    shipped = root / "pgtuner" / "assets" / "SKILL.md"
    assert shipped.is_file(), f"打包资产缺失: {shipped}"
    assert src.read_text(encoding="utf-8") == shipped.read_text(encoding="utf-8"), (
        f"打包资产与仓库原件漂移: {shipped}(重新拷贝保持一致)")


def test_shipped_data_lives_inside_the_package():
    """knowledge JSON + bootstrap template SQL are package members (non-editable
    installs must resolve them; the R1-C wheel-content blocker)."""
    pkg = Path(pgtuner.__file__).resolve().parent
    assert (pkg / "data" / "guc_knowledge.json").is_file()
    assert (pkg / "assets" / "template_setup.sql").is_file()


def test_pyproject_discovers_subpackages_and_data():
    """packages.find with pgtuner* + package-data for assets/data (the help_en omission
    once broke PGTUNER_LANG=en on wheel installs — same class of bug stays pinned)."""
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'include = ["pgtuner*"]' in text
    assert '"*.md"' in text and '"*.json"' in text and '"*.sql"' in text
