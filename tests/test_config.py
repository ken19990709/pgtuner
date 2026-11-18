"""Pure-logic tests for v0.6.1 deployment-level defaults and new diagnose parameters (no PG)."""
from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from pgtuner import cli, config as config_mod, diagnose as diagnose_mod


@pytest.fixture(autouse=True)
def _reset_corrupt_warned():
    """The corrupt-config warning is once-per-process global state; reset it around each
    test so the warning fires (or stays silent) deterministically regardless of order."""
    config_mod._corrupt_warned = False
    yield
    config_mod._corrupt_warned = False


class _QE:                                   # minimal stand-in for hypo QueryEval (only needs .ratio)
    def __init__(self, ratio):
        self.ratio = ratio


# ---------------------------- weighted_gain ----------------------------

def test_weighted_gain_equal_weights():
    # ratio 0.2 → reduction 0.8; ratio 1.0 → 0; mean criterion (0.8+0)/2
    assert diagnose_mod.weighted_gain([_QE(0.2), _QE(1.0)], [1, 1]) == pytest.approx(0.4)


def test_weighted_gain_weighted_by_calls():
    # calls criterion 150:50 → (150*0.8 + 50*0)/200 = 0.6
    assert diagnose_mod.weighted_gain([_QE(0.2), _QE(1.0)], [150, 50]) == pytest.approx(0.6)


def test_weighted_gain_missing_ratio_counts_denominator():
    # queries whose evaluation failed (ratio None) get no free gain: the denominator still counts → 0.8/2
    assert diagnose_mod.weighted_gain([_QE(0.2), _QE(None)], [1, 1]) == pytest.approx(0.4)


def test_weighted_gain_empty():
    assert diagnose_mod.weighted_gain([], None) == 0.0


# ---------------------------- config module ----------------------------

def test_config_roundtrip_and_env_precedence(tmp_path, monkeypatch):
    f = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", f)
    assert config_mod.load() == {}                       # missing → empty
    cfg = config_mod.save("default_instance", "rel19")
    assert cfg["default_instance"] == "rel19"
    monkeypatch.delenv("PGTUNER_INSTANCE", raising=False)
    assert config_mod.default_instance() == "rel19"
    monkeypatch.setenv("PGTUNER_INSTANCE", "base2")      # env overrides the file
    assert config_mod.default_instance() == "base2"


def test_config_corrupt_file_is_empty(tmp_path, monkeypatch):
    f = tmp_path / "config.json"
    f.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", f)
    monkeypatch.delenv("PGTUNER_INSTANCE", raising=False)
    assert config_mod.load() == {}
    assert config_mod.default_instance() is None


def test_config_show_corrupt_json_warns_on_stderr(tmp_path, monkeypatch, capsys):
    """Corrupt config.json: `config show` still exits 0 and shows 未设置; the warning
    (file path + 按空配置处理) goes to stderr only — stdout stays machine-readable —
    and fires at most once per process."""
    f = tmp_path / "config.json"
    f.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", f)
    monkeypatch.delenv("PGTUNER_INSTANCE", raising=False)
    from conftest import cli_runner
    res = cli_runner().invoke(cli.cli, ["config", "show"])
    assert res.exit_code == 0
    assert "未设置" in res.output
    err = res.stderr
    assert "警告" in err and "按空配置处理" in err and str(f) in err
    # once per process: a further load() in the same interpreter stays silent
    capsys.readouterr()
    assert config_mod.load() == {}
    assert capsys.readouterr().err == ""


def test_config_save_atomic_failure_keeps_file(tmp_path, monkeypatch):
    """save() is atomic: a mid-write failure (disk full at fsync) leaves the previous
    config byte-identical with its permissions intact and no tmp litter; a successful
    rewrite also preserves permissions of the existing file."""
    f = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", f)
    config_mod.save("default_instance", "rel19")
    assert f.stat().st_mode & 0o777 == 0o644                # new file: same mode write_text gave
    f.chmod(0o600)
    config_mod.save("default_instance", "rel20")            # successful atomic rewrite keeps the mode
    assert f.stat().st_mode & 0o777 == 0o600
    assert json.loads(f.read_text(encoding="utf-8"))["default_instance"] == "rel20"
    before = f.read_text(encoding="utf-8")

    def boom(fd):
        raise OSError("disk full")

    monkeypatch.setattr("os.fsync", boom)
    with pytest.raises(OSError):
        config_mod.save("default_instance", "base2")
    assert f.read_text(encoding="utf-8") == before          # previous config untouched
    assert f.stat().st_mode & 0o777 == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]   # no tmp litter


# ---------------------------- CLI wiring ----------------------------

def test_resolve_target_falls_back_to_config(monkeypatch):
    monkeypatch.setattr(cli.config_mod, "default_instance", lambda: "rel19")
    calls = {}

    def fake_get(name):
        calls["get"] = name
        return type("I", (), {"name": name, "pg_version": "19devel",
                              "build_type": "release", "port": 5433})()

    monkeypatch.setattr(cli.instance_mod, "get", fake_get)
    monkeypatch.setattr(cli.instance_mod, "ensure_running", lambda n: calls.setdefault("run", n))
    monkeypatch.setattr(cli.instance_mod, "dsn_of",
                        lambda n, db: f"host=/tmp port=5433 dbname={db}")
    dsn, meta = cli._resolve_target(None, "mydb", None)
    assert calls["get"] == "rel19" and calls["run"] == "rel19"
    assert dsn == "host=/tmp port=5433 dbname=mydb" and meta["name"] == "rel19"


def test_resolve_target_explicit_wins_over_config(monkeypatch):
    monkeypatch.setattr(cli.config_mod, "default_instance", lambda: "rel19")
    monkeypatch.setattr(cli.instance_mod, "get",
                        lambda n: type("I", (), {"name": n, "pg_version": "19",
                                                 "build_type": "release", "port": 1})())
    monkeypatch.setattr(cli.instance_mod, "ensure_running", lambda n: None)
    monkeypatch.setattr(cli.instance_mod, "dsn_of", lambda n, db: f"dbname={db}@{n}")
    dsn, meta = cli._resolve_target("base2", None, None)
    assert dsn == "dbname=postgres@base2" and meta["name"] == "base2"


def test_parse_weights_ok_and_rejects():
    assert cli._parse_weights("150,100, 25", 3) == [150.0, 100.0, 25.0]
    assert cli._parse_weights(None, 3) is None
    with pytest.raises(Exception):
        cli._parse_weights("150,abc", 2)
    with pytest.raises(Exception):
        cli._parse_weights("150", 2)                     # count does not match the number of queries


def test_lab_clone_uses_default_instance(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("PGTUNER_INSTANCE", raising=False)
    config_mod.save("default_instance", "rel19")
    cloned = {}

    class FakeInst:
        name = "rel19"

        def clone(self, inst, tpl):
            cloned["args"] = (inst, tpl)
            return "biz_tpl_cdeadbeef"

    monkeypatch.setattr(cli.instance_mod, "get", lambda n: FakeInst())
    monkeypatch.setattr(cli.instance_mod, "ensure_running", lambda n: None)
    monkeypatch.setattr(cli.instance_mod, "clone", FakeInst().clone)
    monkeypatch.setattr(cli.instance_mod, "dsn_of", lambda n, db: f"dbname={db}")
    res = CliRunner().invoke(cli.cli, ["lab", "clone", "--template", "biz_tpl"])
    assert res.exit_code == 0, res.output
    assert cloned["args"] == ("rel19", "biz_tpl")
    assert res.output.strip().endswith("biz_tpl_cdeadbeef")   # stdout carries only the database name


def test_config_set_validates_instance(monkeypatch):
    def boom(name):
        raise KeyError(name)

    monkeypatch.setattr(cli.instance_mod, "get", boom)
    res = CliRunner().invoke(cli.cli, ["config", "set", "default-instance", "nope"])
    assert res.exit_code != 0 and "不在注册表" in res.output


def test_foreign_holders_self_anchor_not_foreign():
    """bash exec optimization: the releasing process inherits the subshell pid (starttime unchanged) — a self-anchor in the claim chain does not count as a foreign holder
    (v0.6.3 smoke-test false-rejection postmortem: `cmd && pgtuner diagnose --release` as the subshell's last command)."""
    import os as _os
    from pgtuner import instance as I
    me = I._proc_stat(_os.getpid())
    assert me is not None, "非 Linux/无 /proc 环境"
    claim = {"ancestors": [[_os.getpid(), me["starttime"]]]}
    assert I._foreign_holders(claim) == []
    # a truly foreign pid (nonexistent) is not blocked; PID reuse (starttime mismatch) is not blocked
    assert I._foreign_holders({"ancestors": [[999999, "1"]]}) == []
