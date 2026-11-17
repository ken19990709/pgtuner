"""lab bootstrap engine tests: bootstrap module dependency boundaries (instance/statements/
bench/config) are monkeypatched throughout; no real PG is contacted. Three things are locked
down: step idempotence (skip when already present), the template populate call order
(extension → pgbench -i → payload SQL — the extension must be created before sealing so
cloned databases inherit it), and the default-instance no-stealing semantics (env
PGTUNER_INSTANCE is not forcibly overridden).

Failure cleanup of the instance._build_sealed_db skeleton (populate raises → DROP the
half-built database) is directly regression-tested here (the make_template-level B1 behavior
is already covered in test_instance.py; this adds the regression after the skeleton extraction)."""
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from pgtuner import bootstrap as bp, cli
from pgtuner import instance as instance_mod


def _fake_inst(name="tst", build="release"):
    return SimpleNamespace(name=name, build_type=build, bindir="/fake/pg-bin",
                           pg_version="19devel", port=5433, socket_dir="/tmp")


def _patch_env(monkeypatch, *, tpls=(), setup_result=None, file_default=None,
               inst=None, env_instance=None):
    """Patch all bootstrap.bootstrap dependency boundaries, return the call log calls."""
    calls = {"exec": [], "run_init": [], "sealed": [], "save": [], "drop": []}
    inst = inst or _fake_inst()
    monkeypatch.setattr(instance_mod, "get", lambda n, rf=None: inst)
    monkeypatch.setattr(instance_mod, "ensure_running", lambda n: inst)
    monkeypatch.setattr(instance_mod, "templates", lambda n, rf=None: list(tpls))
    monkeypatch.setattr(instance_mod, "_build_sealed_db",
                        lambda n, db, populate, rf=None:
                        (calls["sealed"].append((n, db)), populate(n, db, rf)))
    monkeypatch.setattr(instance_mod, "drop_db",
                        lambda n, db, rf=None, **k: calls["drop"].append(db))
    monkeypatch.setattr(bp, "_exec", lambda n, db, sql, rf=None: calls["exec"].append((db, sql)))
    monkeypatch.setattr(bp.statements_mod, "setup",
                        lambda n, db, force=False, registry_file=None:
                        setup_result or {"changed": True, "restart": False, "note": "已启用"})
    monkeypatch.setattr(bp.config_mod, "load", lambda: {"default_instance": file_default})
    monkeypatch.setattr(bp.config_mod, "save",
                        lambda k, v: (calls["save"].append((k, v)), {"default_instance": v}))
    monkeypatch.setattr(bp.bench_mod, "resolve_pgbench",
                        lambda explicit=None, bindir=None: "/fake/pg-bin/pgbench")
    monkeypatch.setattr(bp.bench_mod, "run_init",
                        lambda p, h, port, db, scale=10:
                        calls["run_init"].append({"path": p, "db": db, "scale": scale})
                        or SimpleNamespace(returncode=0, stdout="", stderr=""))
    if env_instance is None:
        monkeypatch.delenv("PGTUNER_INSTANCE", raising=False)
    else:
        monkeypatch.setenv("PGTUNER_INSTANCE", env_instance)
    return calls


def test_fresh_instance_full_bootstrap(monkeypatch):
    """Fresh instance: pgss enabled → template three-stage order (extension → pgbench -i → payload SQL) → default instance auto-set."""
    calls = _patch_env(monkeypatch, tpls=[], file_default=None)
    result = bp.bootstrap("tst")
    assert [s["name"] for s in result["steps"]] == ["pg_stat_statements", "模板 bench_tpl", "默认实例"]
    assert [s["changed"] for s in result["steps"]] == [True, True, True]
    assert calls["sealed"] == [("tst", "bench_tpl")]
    assert calls["run_init"] == [{"path": "/fake/pg-bin/pgbench", "db": "bench_tpl", "scale": 10}]
    # populate order: CREATE EXTENSION must come first (extension created before sealing, clones inherit it via TEMPLATE)
    assert len(calls["exec"]) == 2
    assert "CREATE EXTENSION" in calls["exec"][0][1]
    assert calls["exec"][0][0] == calls["exec"][1][0] == "bench_tpl"
    assert calls["save"] == [("default_instance", "tst")]


def test_rerun_is_idempotent(monkeypatch):
    """Rerun: template already exists so skipped, pgss changed=False, default instance already set so no save."""
    calls = _patch_env(monkeypatch, tpls=["bench_tpl"], file_default="tst",
                       setup_result={"changed": False, "restart": False, "note": "已启用"})
    result = bp.bootstrap("tst")
    assert [s["changed"] for s in result["steps"]] == [False, False, False]
    assert calls["sealed"] == [] and calls["run_init"] == [] and calls["save"] == []


def test_default_instance_not_stolen_without_flag(monkeypatch):
    """Default instance already set to another instance: keep it by default (override only with --set-default)."""
    calls = _patch_env(monkeypatch, tpls=["bench_tpl"], file_default="rel19")
    result = bp.bootstrap("tst")
    assert calls["save"] == []
    assert "保持 rel19" in result["steps"][-1]["note"]
    calls = _patch_env(monkeypatch, tpls=["bench_tpl"], file_default="rel19")
    result = bp.bootstrap("tst", force_default=True)
    assert calls["save"] == [("default_instance", "tst")]
    assert "原 rel19 被覆盖" in result["steps"][-1]["note"]


def test_env_override_mentioned_not_fought(monkeypatch):
    """When env PGTUNER_INSTANCE overrides: still write the config file, the note states env remains temporarily in effect."""
    calls = _patch_env(monkeypatch, tpls=["bench_tpl"], file_default=None, env_instance="other")
    result = bp.bootstrap("tst")
    assert calls["save"] == [("default_instance", "tst")]
    assert "PGTUNER_INSTANCE=other" in result["steps"][-1]["note"]


def test_debug_build_warning_step(monkeypatch):
    _patch_env(monkeypatch, tpls=["bench_tpl"], inst=_fake_inst(build="debug"))
    result = bp.bootstrap("tst")
    assert result["steps"][0]["name"] == "构建警告"
    assert "铁律 5" in result["steps"][0]["note"]


def test_populate_failure_drops_halfbuilt(monkeypatch):
    """populate raises midway: the _build_sealed_db skeleton must DROP the half-built database (skeleton extraction regression)."""
    monkeypatch.setattr(instance_mod, "_run_sql", lambda *a, **k: None)
    dropped = []
    monkeypatch.setattr(instance_mod, "drop_db",
                        lambda n, db, rf=None, **k: dropped.append(db))

    def boom(n, db, rf):
        raise RuntimeError("pgbench -i 失败: boom")

    with pytest.raises(RuntimeError, match="boom"):
        instance_mod._build_sealed_db("tst", "bench_tpl", boom)
    assert dropped == ["bench_tpl"]


def test_cli_lab_bootstrap_invocation(monkeypatch):
    """CLI wiring: lab bootstrap tabular output exits 0; an engine error becomes ClickException (exit 1)."""
    _patch_env(monkeypatch, tpls=[])
    res = CliRunner().invoke(cli.cli, ["lab", "bootstrap", "--instance", "tst"])
    assert res.exit_code == 0, res.output
    assert "可开工" in res.output and "lab clone" in res.output

    def _boom(*a, **k):
        raise RuntimeError("pgbench 未找到")

    monkeypatch.setattr(bp, "bootstrap", _boom)
    res = CliRunner().invoke(cli.cli, ["lab", "bootstrap", "--instance", "tst"])
    assert res.exit_code == 1
    assert "pgbench 未找到" in res.output
