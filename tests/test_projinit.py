"""pgtuner init (release layer, v0.10.0) regression: five-piece suite landing, idempotence, marker-block merging, --global,
missing-asset errors. CliRunner + tmp projects; the real HOME is untouched (--global uses a monkeypatched HOME),
and the real .pgtuner is untouched."""
import shutil
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from pgtuner import cli, paths, projinit


def _launcher_target_exists() -> bool:
    """The --global PATH-wiring step needs a pgtuner executable for the launcher
    symlink (projinit._launcher_target priority: interpreter-adjacent console
    script > sys.argv[0] > repo .venv > which()). An unpacked sdist has no repo
    .venv — and these tests pin PATH to a system-only prefix, so a pgtuner
    sitting on the *user's* PATH (~/.local/bin) is invisible to them. Probe
    exactly the sources _launcher_target can see under that prefix."""
    if sys.executable and (Path(sys.executable).with_name("pgtuner")).is_file():
        return True
    if sys.argv and sys.argv[0] and Path(sys.argv[0]).name == "pgtuner" \
            and Path(sys.argv[0]).resolve().is_file():
        return True
    if (paths.REPO_ROOT / ".venv" / "bin" / "pgtuner").is_file():
        return True
    return shutil.which("pgtuner", path="/usr/bin:/bin") is not None


requires_pgtuner_executable = pytest.mark.skipif(
    not _launcher_target_exists(),
    reason="sdist checkout without repo venv: no pgtuner executable to wire PATH to")


def _run(*args):
    return CliRunner().invoke(cli.cli, ["init", *args])


def _files(proj: Path) -> dict:
    return {
        "agents": proj / "AGENTS.md",
        "claude": proj / "CLAUDE.md",
        "zcode": proj / ".zcode" / "skills" / "pg-experiment" / "SKILL.md",
        "claude_skill": proj / ".claude" / "skills" / "pg-experiment" / "SKILL.md",
        "gi": proj / ".gitignore",
        "ledger": proj / ".pgtuner" / "ledger",
    }


def test_fresh_project_full_init(tmp_path):
    """Fresh project: the five-piece suite lands in one go; contents identical to the tool-repo single-style sources."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    res = _run(str(proj))
    assert res.exit_code == 0, res.output
    f = _files(proj)
    assert all(p.exists() for p in f.values()), res.output
    assert (f["agents"].read_text(encoding="utf-8")
            == projinit.ASSET_AGENTS.read_text(encoding="utf-8"))       # the whole file = verbatim identical
    assert f["claude"].read_text(encoding="utf-8") == "@AGENTS.md\n"
    skill_src = projinit.ASSET_SKILL.read_text(encoding="utf-8")
    assert f["zcode"].read_text(encoding="utf-8") == skill_src
    assert f["claude_skill"].read_text(encoding="utf-8") == skill_src
    assert ".pgtuner/" in f["gi"].read_text(encoding="utf-8")


def test_rerun_is_idempotent(tmp_path):
    """Rerun: every step changed=False, file contents unchanged."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    assert _run(str(proj)).exit_code == 0
    watch = [proj / "AGENTS.md", proj / "CLAUDE.md", proj / ".gitignore",
             proj / ".zcode" / "skills" / "pg-experiment" / "SKILL.md"]
    before = {p: p.read_text(encoding="utf-8") for p in watch}
    result = projinit.init(proj)
    assert all(s["changed"] is False for s in result["steps"]), result
    assert {p: p.read_text(encoding="utf-8") for p in watch} == before


def test_existing_agents_marked_block_and_force_update(tmp_path, monkeypatch):
    """Existing AGENTS.md: the marker block is appended (original content preserved); rerunning does not duplicate; an asset upgrade replaces the block via --force."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("# 我的项目规则\n", encoding="utf-8")
    assert _run(str(proj)).exit_code == 0
    cur = (proj / "AGENTS.md").read_text(encoding="utf-8")
    assert cur.startswith("# 我的项目规则")
    assert cur.count(projinit.AGENTS_BEGIN) == 1 and cur.count(projinit.AGENTS_END) == 1

    # simulate a tool upgrade (asset content changes): by default untouched; --force replaces the whole block, without duplicating markers
    agents_v1 = projinit.ASSET_AGENTS.read_text(encoding="utf-8")
    agents_v2 = agents_v1 + "\n<!-- v2 新增铁律 -->\n"
    monkeypatch.setattr(projinit, "_assets",
                        lambda: (agents_v2, projinit.ASSET_SKILL.read_text(encoding="utf-8")))
    r2 = projinit.init(proj)
    step = next(s for s in r2["steps"] if s["name"] == "AGENTS.md")
    assert step["changed"] is False and "--force" in step["note"]
    assert (proj / "AGENTS.md").read_text(encoding="utf-8") == cur       # not overwritten
    r3 = projinit.init(proj, force=True)
    step = next(s for s in r3["steps"] if s["name"] == "AGENTS.md")
    assert step["changed"] is True
    new = (proj / "AGENTS.md").read_text(encoding="utf-8")
    assert new.startswith("# 我的项目规则")                               # original content still there
    assert new.count(projinit.AGENTS_BEGIN) == 1 and "v2 新增铁律" in new


def test_skill_conflict_needs_force(tmp_path):
    """Target skill already exists with different content: skipped with a hint by default; only --force overwrites."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    dest = proj / ".zcode" / "skills" / "pg-experiment" / "SKILL.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("name: 别的东西\n", encoding="utf-8")
    result = projinit.init(proj)
    step = next(s for s in result["steps"] if s["name"] == "skill→.zcode")
    assert step["changed"] is False and "--force" in step["note"]
    assert dest.read_text(encoding="utf-8") == "name: 别的东西\n"       # not overwritten
    result = projinit.init(proj, force=True)
    step = next(s for s in result["steps"] if s["name"] == "skill→.zcode")
    assert step["changed"] is True
    assert dest.read_text(encoding="utf-8") == projinit.ASSET_SKILL.read_text(encoding="utf-8")


def test_gitignore_merge_idempotent(tmp_path):
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    projinit.init(proj)
    gi = (proj / ".gitignore").read_text(encoding="utf-8")
    assert gi.startswith("node_modules/") and gi.count(".pgtuner/") == 1
    r2 = projinit.init(proj)
    assert next(s for s in r2["steps"] if s["name"] == ".gitignore")["changed"] is False
    assert (proj / ".gitignore").read_text(encoding="utf-8") == gi


@requires_pgtuner_executable
def test_init_global_full_suite(monkeypatch, tmp_path):
    """--global seven-piece suite (v0.11.0): skills×3 + rule marker blocks×3 (shims preserved) + PATH hookup (.bashrc fallback);
    no project assets landed (AGENTS.md/.pgtuner); rerun idempotent."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")        # triggers the fallback branch where ~/.local/bin is not on PATH
    # preset realistic shape: claude/codex rule files are single-line @import shims; the zcode side is missing
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("@RTK.md\n", encoding="utf-8")
    (home / ".codex").mkdir()
    (home / ".codex" / "AGENTS.md").write_text("@/some/where/RTK.md\n", encoding="utf-8")
    res = _run("--global")
    assert res.exit_code == 0, res.output

    skill = projinit.ASSET_SKILL.read_text(encoding="utf-8")
    for d in (".zcode", ".claude", ".codex"):
        p = home / d / "skills" / "pg-experiment" / "SKILL.md"
        assert p.is_file() and p.read_text(encoding="utf-8") == skill, d

    # rules×3: the marker block enters three user-level files; zcode is created fresh; claude/codex shims stay verbatim ahead of the block
    for d, name, head in ((".claude", "CLAUDE.md", "@RTK.md"),
                          (".codex", "AGENTS.md", "@/some/where/RTK.md"),
                          (".zcode", "AGENTS.md", None)):
        p = home / d / name
        cur = p.read_text(encoding="utf-8")
        assert projinit.AGENTS_BEGIN in cur and projinit.AGENTS_END in cur
        assert cur.count(projinit.AGENTS_BEGIN) == 1
        if head:
            assert cur.startswith(head)                # the @import shim is untouched
        assert "PG 实验铁律" in cur                     # the USER_RULES summary is inside the block

    # PATH hookup: ~/.local/bin/pgtuner symlink + .bashrc marker-block fallback
    launcher = home / ".local" / "bin" / "pgtuner"
    assert launcher.is_symlink()
    rc = (home / ".bashrc").read_text(encoding="utf-8")
    assert projinit.RC_BEGIN in rc and projinit.RC_PATH_LINE in rc and projinit.RC_END in rc

    # project assets do not go global; rerun idempotent (all steps changed=False)
    assert not (home / "AGENTS.md").exists()
    assert not (home / ".pgtuner").exists()
    r2 = projinit.init_global()
    assert all(s["changed"] is False for s in r2["steps"]), r2


@requires_pgtuner_executable
def test_init_global_path_branches(monkeypatch, tmp_path):
    """Two PATH-hookup branches: ~/.local/bin already on PATH → .bashrc untouched; launcher already present → skipped."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{home}/.local/bin:/usr/bin:/bin")
    projinit.init_global()
    assert not (home / ".bashrc").exists()             # on PATH: no fallback needed
    r2 = projinit.init_global()
    path_step = next(s for s in r2["steps"] if s["name"] == "PATH 接入")
    assert path_step["changed"] is False and "已存在" in path_step["note"]


def test_missing_target_dir_errors(tmp_path):
    res = _run(str(tmp_path / "nope"))
    assert res.exit_code == 1
    assert "目标目录不存在" in res.output


def test_missing_asset_errors(monkeypatch, tmp_path):
    """Release asset missing (the non-editable premise broken): a clear error, no silent degradation."""
    monkeypatch.setattr(projinit, "ASSET_AGENTS", tmp_path / "ghost.md")
    res = _run(str(tmp_path))
    assert res.exit_code == 1
    assert "发行资产缺失" in res.output


def test_write_failure_leaves_user_files_untouched(tmp_path, monkeypatch):
    """Atomic writes: a failure mid-write (disk full at fsync) must leave every pre-existing
    user file byte-identical and leave no tmp litter — a crash can never truncate the user's
    AGENTS.md/CLAUDE.md/.gitignore."""
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("# 用户心血\n", encoding="utf-8")
    (proj / "CLAUDE.md").write_text("# 用户 CLAUDE\n", encoding="utf-8")
    (proj / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    watch = [proj / "AGENTS.md", proj / "CLAUDE.md", proj / ".gitignore"]
    before = {p: p.read_text(encoding="utf-8") for p in watch}

    def boom(fd):
        raise OSError("disk full")

    monkeypatch.setattr("os.fsync", boom)
    with pytest.raises(OSError):
        projinit.init(proj)
    assert {p: p.read_text(encoding="utf-8") for p in watch} == before
    assert not [q for q in proj.rglob("*") if q.name.endswith(".tmp")]


@requires_pgtuner_executable
def test_bashrc_wire_atomic_on_failure(monkeypatch, tmp_path):
    """~/.bashrc is the highest-stakes write: on a mid-write failure the original bytes
    stay put, a not-yet-existing .bashrc is never half-created, and no tmp litter remains."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")     # forces the .bashrc fallback branch

    def boom(fd):
        raise OSError("disk full")

    monkeypatch.setattr("os.fsync", boom)
    rc = home / ".bashrc"
    rc.write_text("export EDITOR=vim\n", encoding="utf-8")
    with pytest.raises(OSError):
        projinit._wire_path(home)
    assert rc.read_text(encoding="utf-8") == "export EDITOR=vim\n"
    assert not [q for q in home.rglob("*") if q.name.endswith(".tmp")]

    home2 = tmp_path / "home2"                      # fresh home: failed create leaves no .bashrc behind
    home2.mkdir()
    with pytest.raises(OSError):
        projinit._wire_path(home2)
    assert not (home2 / ".bashrc").exists()


def test_marker_block_replace_preserves_backslashes():
    """R2-D: re.sub treated the replacement block as a template — backslash-t in
    the shipped AGENTS.md (`psql \timing`) became a TAB through the replace path."""
    from pgtuner import projinit as P
    TAB = chr(9)
    block = P.AGENTS_BEGIN + "\npsql " + chr(92) + "timing is banned\n" + P.AGENTS_END
    cur = "header\n" + block + "\ntrailer\n"
    new, mode = P._apply_block(cur, block)
    assert mode == "same" and new is None
    assert (chr(92) + "timing") in cur and (TAB + "iming") not in cur
    block2 = P.AGENTS_BEGIN + "\npsql " + chr(92) + "timing and " + chr(92) + "q banned\n" + P.AGENTS_END
    new2, mode2 = P._apply_block(cur, block2)
    assert mode2 == "replace"
    assert (chr(92) + "timing and " + chr(92) + "q") in new2 and (TAB + "iming") not in new2


# ---------------------------- R4 B-MINOR-2: launcher target priority ----------------------------

def test_wire_path_prefers_running_executable_over_stale_shim(monkeypatch, tmp_path):
    """R4 B-MINOR-2: the wiring must point at the executable that is ACTUALLY
    running. Priority ① is the console script next to sys.executable (venv pip
    install); a stale pgtuner first on PATH (the old which() fallback) and the
    repo .venv must BOTH lose against it."""
    home = tmp_path / "home"
    home.mkdir()
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (venv / "pgtuner").write_text("#!/bin/sh\n# the running one\n", encoding="utf-8")
    stale = tmp_path / "stale" / "pgtuner"
    stale.parent.mkdir(parents=True)
    stale.write_text("#!/bin/sh\n# an old install\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(stale.parent))            # old behavior would wire to this
    monkeypatch.setattr(sys, "executable", str(venv / "python"))
    step = projinit._wire_path(home)
    launcher = home / ".local" / "bin" / "pgtuner"
    assert step["changed"] is True, step
    import os
    assert os.path.realpath(launcher) == str(venv / "pgtuner")   # NOT the stale shim
    assert "pgtuner[db]" not in step["note"]


def test_wire_path_argv0_beats_repo_venv_and_which(monkeypatch, tmp_path):
    """Priority ②: when the pgtuner launcher itself was invoked (sys.argv[0]),
    it wins over both the repo .venv and which() — even with no interpreter-
    adjacent script (sys.executable sits in a driver-less venv)."""
    home = tmp_path / "home"
    home.mkdir()
    invoked = tmp_path / "bin" / "pgtuner"
    invoked.parent.mkdir(parents=True)
    invoked.write_text("#!/bin/sh\n# argv0\n", encoding="utf-8")
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "python3").write_text("#!/bin/sh\n", encoding="utf-8")   # no pgtuner beside it
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(sys, "executable", str(bare / "python3"))
    monkeypatch.setattr(sys, "argv", [str(invoked), "init", "--global"])
    step = projinit._wire_path(home)
    launcher = home / ".local" / "bin" / "pgtuner"
    assert step["changed"] is True, step
    import os
    assert os.path.realpath(launcher) == str(invoked)


def test_wire_path_no_target_hints_pip_install(monkeypatch, tmp_path):
    """No candidate at all → skipped with the pip-install wording (the wheel
    without [db] scenario; the old hint only mentioned the tool-repo setup.sh)."""
    home = tmp_path / "home"
    home.mkdir()
    empty_bin = tmp_path / "emptybin"                        # deterministic which(): nothing there
    empty_bin.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "none" / "python"))
    monkeypatch.setattr(sys, "argv", ["pytest"])             # not a pgtuner launcher
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path / "no-repo")   # no repo .venv either
    step = projinit._wire_path(home)
    assert step["changed"] is False
    assert 'pip install "pgtuner[db]"' in step["note"]
    assert not (home / ".local" / "bin" / "pgtuner").exists()  # nothing wired
