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


def test_packaged_assets_in_sync_with_repo_originals():
    """v0.11.0 packaging fix: init distributes the packaged copies (wheel installs work);
    the repo-root originals remain the live contract — this test is the drift guard."""
    root = Path(__file__).resolve().parents[1]
    pairs = [
        (root / "AGENTS.md", root / "pgtuner" / "assets" / "AGENTS.md"),
        (root / ".zcode" / "skills" / "pg-experiment" / "SKILL.md",
         root / "pgtuner" / "assets" / "SKILL.md"),
    ]
    for src, shipped in pairs:
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
