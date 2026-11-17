"""doctor environment preflight tests: doctor module dependency boundaries (instance/pg/statements/
bench/config) are monkeypatched throughout; the real registry is untouched and no real PG is contacted — doctor's rule is read-only with zero side effects, and the tests are equally side-effect-free.

A missing psycopg2 is simulated by injecting None into sys.modules (the import statement then raises ImportError)."""
import sys
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from pgtuner import cli, doctor


def _inst(name="rel19", build="release"):
    return SimpleNamespace(name=name, pg_version="19devel", build_type=build,
                           bindir="/fake/pg-bin", port=5433, socket_dir="/tmp")


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _patch_env(monkeypatch, *, insts=None, default=None, ready_names=None,
               tpls=(), pgss=None, pgbench="/fake/pg-bin/pgbench"):
    """Patch all doctor.run dependency boundaries in one shot; defaults to an all-green environment (ledger points to a creatable directory under /tmp)."""
    insts = [_inst()] if insts is None else insts
    ready_names = {i.name for i in insts} if ready_names is None else ready_names
    monkeypatch.setattr(doctor.instance_mod, "list_instances", lambda *a, **k: list(insts))
    monkeypatch.setattr(doctor.instance_mod, "is_ready", lambda n: n in ready_names)
    monkeypatch.setattr(doctor.instance_mod, "get",
                        lambda n, rf=None: next(i for i in insts if i.name == n))
    monkeypatch.setattr(doctor.instance_mod, "templates", lambda n, rf=None: list(tpls))
    monkeypatch.setattr(doctor.config_mod, "default_instance", lambda: default)
    monkeypatch.delenv("PGTUNER_INSTANCE", raising=False)
    conn = _FakeConn()
    monkeypatch.setattr(doctor.pg_mod, "connect_rw", lambda dsn: conn)
    if pgss is None:
        pgss = {"installed": True, "created": True, "preloaded": True}
    monkeypatch.setattr(doctor.statements_mod, "status", lambda c: dict(pgss))
    monkeypatch.setattr(doctor.bench_mod, "resolve_pgbench",
                        lambda explicit=None, bindir=None: pgbench)
    monkeypatch.setattr(doctor, "LEDGER_DIR", Path("/tmp/pgtuner-doctor-test-ledger"))
    return conn


def test_all_green(monkeypatch):
    """Standard ready environment: all 7 checks ok, no fail/warn."""
    _patch_env(monkeypatch, default="rel19", tpls=["bench_tpl", "biz_tpl"])
    rows = doctor.run()
    assert [r["level"] for r in rows] == ["ok"] * 8      # since v0.10.0 includes the "状态锚点" (status anchor) row
    assert rows[4]["detail"].startswith("bench_tpl")
    assert rows[7]["name"] == "状态锚点"


def test_psycopg2_missing_is_fail(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg2", None)   # import psycopg2 → ImportError
    _patch_env(monkeypatch, tpls=["bench_tpl"])
    rows = doctor.run()
    assert rows[0]["level"] == "fail"
    assert "scripts/setup.sh" in rows[0]["fix"]


def test_empty_registry_is_fail(monkeypatch):
    _patch_env(monkeypatch, insts=[], default=None)
    assert doctor.run()[1]["level"] == "fail"


def test_only_debug_instances_warn(monkeypatch):
    _patch_env(monkeypatch, insts=[_inst("dbg", build="debug")], default="dbg",
               ready_names={"dbg"}, tpls=["bench_tpl"])
    row = doctor.run()[1]
    assert row["level"] == "warn"
    assert "铁律 5" in row["detail"]


def test_no_default_instance_degrades_not_fails(monkeypatch):
    """No default instance set: warn + template/pgss/pgbench three checks skip, zero fail."""
    _patch_env(monkeypatch, default=None)
    rows = doctor.run()
    assert rows[3]["level"] == "warn"
    # templates/pgss depend on the target instance → skip; pgbench does not (still found via PATH) → ok
    assert [r["level"] for r in rows[4:7]] == ["skip", "skip", "ok"]
    assert not [r for r in rows if r["level"] == "fail"]


def test_missing_template_warns_with_bootstrap_fix(monkeypatch):
    _patch_env(monkeypatch, default="rel19", tpls=["other_tpl"])
    row = doctor.run()[4]
    assert row["level"] == "warn"
    assert "lab bootstrap" in row["fix"]


def test_pgss_states(monkeypatch):
    """Not installed → setup.sh --pg; not preloaded → lab bootstrap; preloaded but extension not created → ok without false alarm."""
    _patch_env(monkeypatch, default="rel19", tpls=["bench_tpl"],
               pgss={"installed": False, "created": False, "preloaded": False})
    row = doctor.run()[5]
    assert (row["level"], "setup.sh --pg" in row["fix"]) == ("warn", True)
    _patch_env(monkeypatch, default="rel19", tpls=["bench_tpl"],
               pgss={"installed": True, "created": False, "preloaded": False})
    row = doctor.run()[5]
    assert (row["level"], "lab bootstrap" in row["fix"]) == ("warn", True)
    _patch_env(monkeypatch, default="rel19", tpls=["bench_tpl"],
               pgss={"installed": True, "created": False, "preloaded": True})
    assert doctor.run()[5]["level"] == "ok"


def test_pgbench_missing_warns(monkeypatch):
    _patch_env(monkeypatch, default="rel19", tpls=["bench_tpl"], pgbench=None)
    row = doctor.run()[6]
    assert row["level"] == "warn"
    assert "setup.sh --pg" in row["fix"]


def test_ledger_unwritable_is_fail(monkeypatch):
    _patch_env(monkeypatch, default="rel19", tpls=["bench_tpl"])
    monkeypatch.setattr(doctor, "LEDGER_DIR", Path("/proc/definitely-not-writable"))
    assert doctor.run()[2]["level"] == "fail"


def test_ledger_fresh_checkout_ok(monkeypatch, tmp_path):
    """Fresh checkout: .pgtuner entirely absent, project root writable → ok (to be auto-created), no false blocking."""
    _patch_env(monkeypatch, default="rel19", tpls=["bench_tpl"])
    (tmp_path / "proj").mkdir()                    # only the project root, no .pgtuner
    monkeypatch.setattr(doctor, "LEDGER_DIR", tmp_path / "proj" / ".pgtuner" / "ledger")
    row = doctor.run()[2]
    assert (row["level"], "待自动创建" in row["detail"]) == ("ok", True)


def test_ledger_file_blocker_is_fail(monkeypatch, tmp_path):
    """A same-named file in the way: a definite fail, not sidestepped into a wrong writable verdict via ancestors."""
    _patch_env(monkeypatch, default="rel19", tpls=["bench_tpl"])
    blocker = tmp_path / "ledger-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(doctor, "LEDGER_DIR", blocker)
    assert doctor.run()[2]["level"] == "fail"


def test_cli_exit_code_and_summary(monkeypatch):
    """CLI: all green exits 0; any fail (e.g. psycopg2 missing) exits 1 and the output carries the fix column."""
    _patch_env(monkeypatch, default="rel19", tpls=["bench_tpl"])
    runner = CliRunner()
    res = runner.invoke(cli.cli, ["doctor"])
    assert res.exit_code == 0, res.output
    assert "8✅" in res.output.replace(" ", "")
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    res = runner.invoke(cli.cli, ["doctor"])
    assert res.exit_code == 1
    assert "阻断" in res.output


def test_cli_unknown_instance_fails_fast(monkeypatch):
    _patch_env(monkeypatch, default="rel19", tpls=["bench_tpl"])

    def _raise(n, rf=None):
        raise ValueError(f"实例不存在: {n}")

    monkeypatch.setattr(doctor.instance_mod, "get", _raise)
    res = CliRunner().invoke(cli.cli, ["doctor", "--instance", "ghost"])
    assert res.exit_code == 1
    assert "ghost" in res.output


def test_anchor_bare_cwd_warns(monkeypatch, tmp_path):
    """Bare cwd (no .git/.pgtuner): the status anchor row warns and offers the init fix (v0.10.0)."""
    _patch_env(monkeypatch, tpls=["bench_tpl"])
    monkeypatch.delenv("PGTUNER_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    row = doctor.run()[7]
    assert (row["level"], "pgtuner init" in row["fix"]) == ("warn", True)


def test_anchor_env_and_git_marker(monkeypatch, tmp_path):
    """Two ok shapes: env override and the git marker."""
    _patch_env(monkeypatch, tpls=["bench_tpl"])
    monkeypatch.setenv("PGTUNER_PROJECT_ROOT", str(tmp_path))
    row = doctor.run()[7]
    assert row["level"] == "ok" and "env PGTUNER_PROJECT_ROOT" in row["detail"]
    monkeypatch.delenv("PGTUNER_PROJECT_ROOT", raising=False)
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    row = doctor.run()[7]
    assert row["level"] == "ok" and row["detail"].endswith("(git)")
