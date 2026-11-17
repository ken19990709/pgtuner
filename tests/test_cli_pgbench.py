"""pgbench path resolution regression (tool-w3 de-machine-binding; v0.9.0 removed the machine-hardcoded
default DEFAULT_PGBENCH, resolution consolidated into bench.resolve_pgbench): explicit path respected
verbatim > instance bindir (version-matched, both <bindir>/pgbench and <bindir>/bin/pgbench forms)
> PATH lookup; if all fail, return None and the caller reports the repair guidance
(scripts/setup.sh --pg). Does not depend on a running PG, nor on pgbench being installed locally."""
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from pgtuner import bench, cli


def _make_exe(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)


def test_explicit_pgbench_path_respected(tmp_path):
    """An explicit path gets no fallback (even if it does not exist; the caller reports the error); only None enters the resolution chain."""
    mine = tmp_path / "my-pgbench"
    _make_exe(mine)
    assert bench.resolve_pgbench(str(mine)) == str(mine)
    assert bench.resolve_pgbench(str(tmp_path / "nope")) == str(tmp_path / "nope")
    assert cli._fallback_pgbench(str(mine)) == str(mine)      # CLI delegation semantics identical


def test_default_falls_back_to_instance_bindir(tmp_path):
    """By default the instance bindir wins (version-matched), accepting both the bin/ and --prefix forms."""
    bindir = tmp_path / "inst"
    _make_exe(bindir / "bin" / "pgbench")
    assert bench.resolve_pgbench(None, bindir=str(bindir)) == str(bindir / "bin" / "pgbench")
    assert cli._fallback_pgbench(None, bindir=str(bindir)) == str(bindir / "bin" / "pgbench")
    _make_exe(bindir / "pgbench")                             # directly under bindir takes precedence over bin/
    assert bench.resolve_pgbench(None, bindir=str(bindir)) == str(bindir / "pgbench")


def test_default_falls_back_to_which(monkeypatch):
    """Without an instance bindir it falls back to PATH lookup (shutil.which)."""
    monkeypatch.setattr(bench.shutil, "which", lambda name: "/usr/bin/pgbench")
    assert bench.resolve_pgbench(None) == "/usr/bin/pgbench"


def test_default_no_candidate_returns_none(tmp_path, monkeypatch):
    """bindir/PATH both exhausted: returns None, the caller reports the repair guidance (no longer falls back to a machine-hardcoded path)."""
    monkeypatch.setattr(bench.shutil, "which", lambda name: None)
    assert bench.resolve_pgbench(None) is None
    assert bench.resolve_pgbench(None, bindir=str(tmp_path / "empty-inst")) is None


def test_bench_default_instance_supplies_bindir(monkeypatch, tmp_path):
    """bench given neither --instance/--dsn but a default instance is set: use the default instance's dsn, and its bindir enters the pgbench
    resolution chain — a fresh machine without pgbench on PATH no longer falsely reports "not found" (a gap caught during pgtest migration field testing)."""
    bindir = tmp_path / "pgb"
    inst = SimpleNamespace(name="rel18", pg_version="18.7", build_type="release",
                           bindir=str(bindir), port=5439, socket_dir="/tmp")
    monkeypatch.setattr(cli.config_mod, "default_instance", lambda: "rel18")
    monkeypatch.setattr(cli.instance_mod, "get", lambda n: inst)
    monkeypatch.setattr(cli.instance_mod, "ensure_running", lambda n: None)
    monkeypatch.setattr(cli.instance_mod, "dsn_of", lambda n, db: f"dsn:{n}/{db}")
    seen = {}

    def _spy(explicit=None, bindir=None):
        seen["bindir"] = bindir
        return None                                   # returning None → the command errors out here

    monkeypatch.setattr(cli, "_fallback_pgbench", _spy)
    res = CliRunner().invoke(cli.cli, ["bench", "--duration", "1"])
    assert res.exit_code != 0
    assert seen["bindir"] == str(bindir)              # the default instance's bindir has entered the resolution chain
