"""resolve_project_root anchor-resolution regression (v0.10.0): env override > upward search from cwd for .pgtuner/.git >
cwd fallback. Only the resolution function itself is tested (the constant PROJECT_ROOT is fixed at import time and never recomputed in-process)."""
import tempfile
from pathlib import Path

import pytest

from pgtuner import paths


def _require_bare_ancestors(tmp_path):
    """Skip when some directory above the pytest sandbox carries a .pgtuner/.git marker
    (e.g. a stray /tmp/.pgtuner from running the CLI in /tmp): the bare-cwd fallback
    below needs genuinely bare parents and cannot control the machine's tree."""
    if any((p / ".pgtuner").exists() or (p / ".git").exists() for p in tmp_path.parents):
        pytest.skip("ancestor of the pytest sandbox carries a .pgtuner/.git marker — environment not bare")


def test_env_override_wins(monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("PGTUNER_PROJECT_ROOT", str(proj))
    monkeypatch.chdir(tmp_path)                 # cwd being elsewhere does not matter
    assert paths.resolve_project_root() == proj.resolve()


def test_git_root_found_upward(monkeypatch, tmp_path):
    """From a subdirectory the git root is found upward (equivalent to running inside the pgtuner repo)."""
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.delenv("PGTUNER_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(sub)
    assert paths.resolve_project_root() == tmp_path.resolve()


def test_pgtuner_marker_found_upward(monkeypatch, tmp_path):
    """Non-git project: the .pgtuner directory itself is the marker (the shape after pgtuner init)."""
    (tmp_path / ".pgtuner").mkdir()
    sub = tmp_path / "work"
    sub.mkdir()
    monkeypatch.delenv("PGTUNER_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(sub)
    assert paths.resolve_project_root() == tmp_path.resolve()


def test_nearest_marker_wins(monkeypatch, tmp_path):
    """Nested projects: the nearest marker wins (an outer .git does not hijack the inner project)."""
    (tmp_path / ".git").mkdir()
    inner = tmp_path / "inner"
    (inner / ".pgtuner").mkdir(parents=True)
    monkeypatch.delenv("PGTUNER_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(inner)
    assert paths.resolve_project_root() == inner.resolve()


def test_bare_dir_falls_back_to_cwd(monkeypatch, tmp_path):
    """Bare directory (no .git, no .pgtuner): falls back to cwd; doctor hints that "state lands in the bare cwd"."""
    _require_bare_ancestors(tmp_path)
    monkeypatch.delenv("PGTUNER_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert paths.resolve_project_root() == tmp_path.resolve()


def test_temp_root_marker_is_skipped(monkeypatch, tmp_path):
    """B5: a system temp root never anchors. Merely running a pgtuner command in /tmp
    plants /tmp/.pgtuner (ensure_dirs); without the skip that marker captures every
    bare directory under /tmp (test_bare_dir_falls_back_to_cwd flips red the moment
    anyone has ever run the CLI there). The root set is injected via monkeypatch —
    the real /tmp is never created/deleted/touched by this test."""
    _require_bare_ancestors(tmp_path)
    troot = tmp_path / "tmproot"                     # stand-in for /tmp carrying a stray marker
    work = troot / "work"
    work.mkdir(parents=True)
    (troot / ".pgtuner").mkdir()
    monkeypatch.setattr(paths, "TEMP_ROOTS",
                        paths.TEMP_ROOTS | {troot.resolve(), tmp_path.resolve()})
    monkeypatch.delenv("PGTUNER_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(work)
    assert paths.resolve_project_root() == work.resolve()   # marker ignored → cwd fallback


def test_temp_root_subdirectory_marker_still_anchors(monkeypatch, tmp_path):
    """B5 companion: the skip applies to the temp root itself only — a real project
    living directly under a temp root (a clone in /tmp/proj, say) still anchors."""
    proj = tmp_path / "proj"
    sub = proj / "inner"
    sub.mkdir(parents=True)
    (proj / ".git").mkdir()
    monkeypatch.setattr(paths, "TEMP_ROOTS", paths.TEMP_ROOTS | {tmp_path.resolve()})
    monkeypatch.delenv("PGTUNER_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(sub)
    assert paths.resolve_project_root() == proj.resolve()


def test_default_temp_roots_cover_conventional_paths():
    """The default set always contains the conventional temp roots regardless of TMPDIR."""
    resolved = {str(p) for p in paths.TEMP_ROOTS}
    assert str(Path("/tmp").resolve()) in resolved
    assert str(Path(tempfile.gettempdir()).resolve()) in resolved


def test_repo_root_constants_unchanged():
    """Shipped-asset anchors (v0.11.0 packaging fix): knowledge + init assets live INSIDE
    the package (wheel/sdist installs work); REPO_ROOT keeps anchoring repo-only assets."""
    pkg_dir = Path(paths.__file__).resolve().parent
    assert paths.REPO_ROOT == Path(__file__).resolve().parents[1]
    assert paths.KNOWLEDGE_FILE == pkg_dir / "data" / "guc_knowledge.json"
    assert paths.KNOWLEDGE_FILE.is_file()
