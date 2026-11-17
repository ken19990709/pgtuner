"""check command -o option tests: mocked DB/hardware probes, verifying the conf snippet written to disk (restart-required group first, with provenance annotations).

Includes post-validation tests for the 2026-11 application-verification fix: invalid_enum
(enum values checked against the target build) and unknown_guc (parameter absent from
pg_settings) — both with a mocked live pg_settings.
"""
import os

from click.testing import CliRunner

from pgtuner import cli, hardware, signals
from pgtuner.engine import recommend_all
from pgtuner.knowledge import load_knowledge
from pgtuner.signals import Signals

KNOWLEDGE_PATH = os.path.join(os.path.dirname(__file__), "..", "pgtuner", "data", "guc_knowledge.json")


def _fake_hw():
    return hardware.HardwareInfo(total_ram_gb=16, cpu_cores=8, os_type="linux",
                                 is_wsl2=False, disk_type="ssd", disk_confidence="high")


def _fake_sig():
    sig = Signals()
    sig.settings = {
        "server_version": {"setting": "18.2", "unit": None, "context": "internal"},
        "server_version_num": {"setting": "180002", "unit": None, "context": "internal"},
        "max_connections": {"setting": "100", "unit": None, "context": "user"},
        "shared_buffers": {"setting": "16384", "unit": "8kB", "context": "postmaster"},
        "work_mem": {"setting": "4096", "unit": "kB", "context": "user"},
    }
    sig.db_size_gb = 0.2
    sig.cache_hit_ratio = 0.999
    sig.wal_rate_mbps = 3.5
    sig.observations = {"buffer_cache_hit_ratio": 0.999, "wal_rate_mbps": 3.5,
                        "n_live_tup": 1000000, "biggest_table_live_tuples": 1000000,
                        "target_dead_tuples": 10000}
    return sig


def test_check_writes_conf_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.pg, "connect", lambda dsn: object())
    monkeypatch.setattr(cli.hardware, "probe", lambda p, data_dir="/tmp": _fake_hw())
    monkeypatch.setattr(cli.signals, "collect", lambda conn, cpu_cores, wal_sample_seconds: _fake_sig())
    out = tmp_path / "tuned.conf"
    result = CliRunner().invoke(
        cli.cli, ["check", "--workload", "oltp", "--knowledge", KNOWLEDGE_PATH,
                  "-o", str(out)])
    assert result.exit_code == 0, result.output
    conf = out.read_text(encoding="utf-8")
    assert conf.startswith("# pgtuner generated")
    assert "# --- 需重启（postmaster） ---" in conf
    assert "# --- 无需重启（reload/会话级） ---" in conf
    # restart-required group first (shared_buffers is postmaster), each entry carries pain/provenance annotations
    assert conf.index("需重启") < conf.index("无需重启")
    assert conf.index("# pain:") < conf.index("shared_buffers =")
    assert "checkpoint_timeout" in conf          # rules unlocked by the wal_rate_mbps observation enter the conf


def test_check_without_output_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.pg, "connect", lambda dsn: object())
    monkeypatch.setattr(cli.hardware, "probe", lambda p, data_dir="/tmp": _fake_hw())
    monkeypatch.setattr(cli.signals, "collect", lambda conn, cpu_cores, wal_sample_seconds: _fake_sig())
    before = set(os.listdir(tmp_path))
    result = CliRunner().invoke(
        cli.cli, ["check", "--knowledge", KNOWLEDGE_PATH])
    assert result.exit_code == 0, result.output
    assert set(os.listdir(tmp_path)) == before   # without -o no file is written


# ------------------- WAL sampling window + shared_buffers clamp passthrough (heavy-load verification fix) -------------------

def test_check_wal_sample_seconds_passthrough(monkeypatch):
    """--wal-sample-seconds is passed through to signals.collect; default 6s (previously fixed 3s)."""
    captured = {}

    def _collect(conn, cpu_cores, wal_sample_seconds):
        captured["wal"] = wal_sample_seconds
        return _fake_sig()

    monkeypatch.setattr(cli.pg, "connect", lambda dsn: object())
    monkeypatch.setattr(cli.hardware, "probe", lambda p, data_dir="/tmp": _fake_hw())
    monkeypatch.setattr(cli.signals, "collect", _collect)
    result = CliRunner().invoke(
        cli.cli, ["check", "--knowledge", KNOWLEDGE_PATH, "--wal-sample-seconds", "8"])
    assert result.exit_code == 0, result.output
    assert captured["wal"] == 8.0
    CliRunner().invoke(cli.cli, ["check", "--knowledge", KNOWLEDGE_PATH])
    assert captured["wal"] == 6.0


def test_check_conf_shared_buffers_clamped(monkeypatch, tmp_path):
    """E2E: the MemAvailable probe value enters the ns → engine clamps → conf records the clamped value with an annotation."""
    monkeypatch.setattr(cli.pg, "connect", lambda dsn: object())
    monkeypatch.setattr(cli.hardware, "probe", lambda p, data_dir="/tmp": hardware.HardwareInfo(
        total_ram_gb=16, cpu_cores=8, os_type="linux", is_wsl2=False,
        disk_type="ssd", disk_confidence="high", mem_available_gb=2.0))  # 16GB total but only 2GB available
    monkeypatch.setattr(cli.signals, "collect", lambda conn, cpu_cores, wal_sample_seconds: _fake_sig())
    out = tmp_path / "clamped.conf"
    result = CliRunner().invoke(
        cli.cli, ["check", "--workload", "oltp", "--knowledge", KNOWLEDGE_PATH,
                  "-o", str(out)])
    assert result.exit_code == 0, result.output
    conf = out.read_text(encoding="utf-8")
    assert "shared_buffers = 1258291kB" in conf   # 2GB×60%, not the formula's 4194304kB (4GB)
    assert "可用内存钳制" in conf


# ------------------- post-validation (2026-11 application-verification fix): mocked live pg_settings -------------------

def _live_sig_no_lz4(pg_major=18, has_io_workers=False):
    """Simulates signals measured on a release build: the wal_compression enum only has off/on/pglz (lz4/zstd not compiled in),
    and pg_settings has no io_workers row (with has_io_workers=False, simulating 20devel where it was removed)."""
    sig = Signals()
    sig.settings = {
        "server_version": {"setting": f"{pg_major}.devel", "unit": None, "context": "internal"},
        "server_version_num": {"setting": f"{pg_major}0002", "unit": None, "context": "internal"},
        "max_connections": {"setting": "100", "unit": None, "context": "user"},
        "wal_compression": {"setting": "off", "unit": None, "context": "superuser"},
        "min_wal_size": {"setting": "80", "unit": "MB", "context": "sighup"},
        "max_wal_size": {"setting": "1024", "unit": "MB", "context": "sighup"},
    }
    if has_io_workers:
        sig.settings["io_workers"] = {"setting": "3", "unit": None, "context": "sighup"}
    sig.enumvals = {"wal_compression": ["off", "on", "pglz"],
                    "huge_pages": ["off", "on", "try"]}
    sig.db_size_gb = 0.2
    sig.wal_rate_mbps = 0.55
    sig.observations = {"wal_rate_mbps": 0.55, "n_live_tup": 1000000,
                        "biggest_table_live_tuples": 1000000, "target_dead_tuples": 10000}
    return sig


def _wal_recs():
    """Minimal namespace: only evaluates io_workers[1] / wal_compression[1] (neither depends on observations)."""
    knowledge = load_knowledge(KNOWLEDGE_PATH)
    return {r.name: r for r in recommend_all(knowledge, {
        "pg_version": 18, "cpu_cores": 8, "workload_type": "oltp",
        "disk_type": "ssd", "total_ram_gb": 16})}


def test_validate_invalid_enum_falls_back_to_current():
    """wal_compression='lz4' but the target build's enumvals lack lz4 → marked invalid_enum;
    with no legal alternative it falls back to the current pg_settings value and annotates "维持现值" (keep current value)."""
    sig = _live_sig_no_lz4()
    recs = _wal_recs()
    assert recs["wal_compression"].primary.value == "lz4"    # before the fix it would enter conf verbatim → FATAL
    signals.validate_against_instance(list(recs.values()), sig)
    lz4_rule = recs["wal_compression"].results[1]
    assert lz4_rule.status == "invalid_enum"
    assert "目标构建不支持" in lz4_rule.reason and "枚举值" in lz4_rule.reason
    p = recs["wal_compression"].primary                      # fallback result (appended at the end, promoted to primary recommendation)
    assert p.value == "off" and p.origin == "check 后置校验回退"
    assert "维持现值" in p.note


def test_validate_unknown_guc_excluded():
    """Even if io_workers slips past the formula gate (still emitted for pg_version=18), it is absent from pg_settings →
    unknown_guc, kept out of conf (double insurance for the 1st fix)."""
    sig = _live_sig_no_lz4(pg_major=18, has_io_workers=False)
    recs = _wal_recs()
    assert recs["io_workers"].primary is not None            # formula layer emits it normally on PG18
    signals.validate_against_instance(list(recs.values()), sig)
    io = recs["io_workers"]
    assert io.primary is None
    assert all(r.status == "unknown_guc" for r in io.results if r.status != "none_result")
    assert "pg_settings 查无此参数" in io.results[1].reason


def test_validate_promotes_legal_alternative():
    """Primary recommendation's enum illegal, alternative legal → the illegal one is marked invalid_enum, the legal alternative is promoted in order."""
    sig = _live_sig_no_lz4()
    rec = _wal_recs()["wal_compression"]
    # hand-built: rule[0]='lz4' (illegal primary), rule[1]='on' (legal alternative)
    rr_type = type(rec.results[0])
    rec.results = [
        rr_type(guc="wal_compression", index=0, formula="f0", origin="o0",
                caveats="", unit="none", value="lz4", status="ok"),
        rr_type(guc="wal_compression", index=1, formula="f1", origin="o1",
                caveats="", unit="none", value="on", status="ok"),
    ]
    signals.validate_against_instance([rec], sig)
    assert rec.results[0].status == "invalid_enum"
    assert rec.primary is rec.results[1] and rec.primary.value == "on"
    assert len(rec.results) == 2                             # with a legal alternative no fallback result is appended


def test_check_conf_validated_against_instance(monkeypatch, tmp_path):
    """E2E: in connected mode (mocked live instance = PG20 without lz4 compiled) the conf has no io_workers,
    wal_compression is a legal value (keep current), and min_wal_size < max_wal_size."""
    monkeypatch.setattr(cli.pg, "connect", lambda dsn: object())
    monkeypatch.setattr(cli.hardware, "probe", lambda p, data_dir="/tmp": _fake_hw())
    monkeypatch.setattr(cli.signals, "collect",
                        lambda conn, cpu_cores, wal_sample_seconds: _live_sig_no_lz4(pg_major=20))
    out = tmp_path / "fixed.conf"
    result = CliRunner().invoke(
        cli.cli, ["check", "--workload", "oltp", "--knowledge", KNOWLEDGE_PATH,
                  "-o", str(out)])
    assert result.exit_code == 0, result.output
    conf = out.read_text(encoding="utf-8")
    body = "\n".join(l for l in conf.splitlines() if not l.startswith("#") and l.strip())
    assert "io_workers" not in body                     # formula gate (PG20 → None) in effect
    assert "io_workers" in conf.split("# 未输出")[1]     # and listed under the "未输出" (not emitted) marker
    assert "wal_compression = 'off'" in conf            # invalid_enum → falls back to the current value (legal)
    assert "维持现值" in conf
    assert "min_wal_size = 990MB" in conf               # consistency adjustment: max=1980 → max/2
    assert "max_wal_size = 1980MB" in conf


def test_results_conclude_rejects_empty(monkeypatch, tmp_path):
    """v0.7.1 regression (S5-A4): an empty conclusion would silently wipe the diagnose auto-conclusion — must be rejected."""
    from click.testing import CliRunner
    from pgtuner import ledger as ledger_mod, cli as cli_mod
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "l.jsonl")
    exp = ledger_mod.new_experiment("bench", "t")
    r = CliRunner().invoke(cli_mod.cli, ["results", "conclude", exp.id, ""])
    assert r.exit_code != 0 and "不能为空" in r.output
    r = CliRunner().invoke(cli_mod.cli, ["results", "conclude", exp.id, "真结论"])
    assert r.exit_code == 0
    assert ledger_mod.get(exp.id).conclusion == "真结论"


def test_rebench_db_guard_accepts_attached_form(tmp_path, monkeypatch):
    """The attached forms --db=x/--dbname=x also count as "gave --db" (gap found: a bare "--db" in sys.argv check
    misses the attached forms; --queries-file --db=mydb was once wrongly rejected by the guard; the guard reads
    sys.argv, which CliRunner does not forward, so an explicit patch is needed). Giving --db should let it
    through — the later instance-not-found error is irrelevant to this assertion."""
    import sys
    qf = tmp_path / "q.sql"
    qf.write_text("SELECT 1;\n", encoding="utf-8")
    for extra in (["--db", "mydb"], ["--db=mydb"], ["--dbname=mydb"]):
        argv = ["pgtuner", "rebench", "--instance", "nope", "--queries-file", str(qf), *extra]
        monkeypatch.setattr(sys, "argv", argv)
        r = CliRunner().invoke(cli.cli, argv[1:])
        assert "请显式 --db" not in r.output, extra
    monkeypatch.setattr(sys, "argv",
                        ["pgtuner", "rebench", "--instance", "nope", "--queries-file", str(qf)])
    r = CliRunner().invoke(cli.cli, ["rebench", "--instance", "nope", "--queries-file", str(qf)])
    assert "请显式 --db" in r.output                     # --db entirely absent: guard still blocks


# ---------------------------- v0.11.0 measurement surface: --format json / --setup ----------------------------

def test_rebench_format_json_setup_pure_stdout(tmp_path, monkeypatch):
    """rebench --format json: stdout must be pure JSON (ledger payload verbatim + envelope), human-readable hints go to
    stderr; --setup is passed through to the engine and the setup cycle enters cycles. Smoke test for the countermeasure to S8 finding #4."""
    import json
    import sys
    from types import SimpleNamespace
    from pgtuner import ledger as ledger_mod, restartbench

    qf = tmp_path / "q.sql"
    qf.write_text("SELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "l.jsonl")

    def fake_run(instance_name, candidates, **kw):
        assert kw.get("setup_statements") == ["ANALYZE events"]

        def mk(name, qs, tps=1.0):
            return restartbench.CycleResult(
                restartbench.RestartCandidate(name, {}), tps=tps, warmup_s=0, error=None,
                payload={"queries": [{"query": q, "execution_ms": ms, "times_ms": [ms]}
                                     for q, ms in qs]})
        return [mk("before", [("SELECT 1", 552.6)]),
                mk("setup", [("ANALYZE events", 115.6)]),
                mk("after", [("SELECT 1", 81.1)])]

    monkeypatch.setattr(restartbench, "run_restart_bench", fake_run)
    monkeypatch.setattr(cli.instance_mod, "get",
                        lambda n, registry_file=None: SimpleNamespace(
                            name="t9", pg_version="PostgreSQL 19.1",
                            build_type="release", port=5433, bindir="/fake/bin"))
    monkeypatch.setattr(cli.instance_mod, "dsn_of",
                        lambda name, dbname: f"host=/tmp port=5433 dbname={dbname}")
    monkeypatch.setattr(cli, "_fallback_pgbench", lambda p, bindir=None: "/fake/pgbench")
    monkeypatch.setattr(cli, "_safe_fp", lambda *a, **k: None)
    argv = ["pgtuner", "rebench", "--instance", "t9", "--queries-file", str(qf),
            "--db=d1", "--setup", "ANALYZE events", "--format", "json", "--yes"]   # --yes: setup onto a non-pool db needs consent (R2-D gate)
    monkeypatch.setattr(sys, "argv", argv)
    r = CliRunner().invoke(cli.cli, argv[1:])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)                                # stdout is pure JSON, directly parseable
    assert doc["id"].startswith("exp-") and doc["kind"] == "rebench"
    assert doc["setup"] == ["ANALYZE events"]
    assert [c["name"] for c in doc["cycles"]] == ["before", "setup", "after"]
    assert doc["metrics"]["after|Q0_ms"] == 81.1
    assert "已落账本" in r.stderr and "已落账本" not in r.stdout   # human-readable receipt only on stderr
    exp = ledger_mod.get(doc["id"])
    assert [c["name"] for c in exp.payload["cycles"]] == ["before", "setup", "after"]


def test_results_diff_format_json(tmp_path, monkeypatch):
    """results diff --format json: verdict/metrics emitted to stdout fully faithfully; text-mode behavior unchanged."""
    import json
    from pgtuner import ledger as ledger_mod
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "l.jsonl")
    a = ledger_mod.new_experiment("rebench", "a", fingerprint={"inst": "t9"},
                                  payload={"metrics": {"baseline|Q0_ms": 100.0}})
    b = ledger_mod.new_experiment("rebench", "b", fingerprint={"inst": "t9"},
                                  payload={"metrics": {"baseline|Q0_ms": 50.0}})
    r = CliRunner().invoke(cli.cli, ["results", "diff", a.id, b.id, "--format", "json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["a"] == a.id and doc["b"] == b.id
    assert "verdict" in doc and doc["metrics"][0]["name"] == "baseline|Q0_ms"
    assert doc["metrics"][0]["delta_pct"] == -50.0
    r2 = CliRunner().invoke(cli.cli, ["results", "diff", a.id, b.id])
    assert r2.exit_code == 0 and "verdict" in r2.stdout


def test_planbench_format_json_pure_stdout(tmp_path, monkeypatch):
    """planbench --format json: the final ledger payload goes to stdout verbatim (including plain_plans); the receipt goes to stderr."""
    import json
    from pgtuner import ledger as ledger_mod, planbench as pb
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "l.jsonl")
    monkeypatch.setattr(cli, "_resolve_target",
                        lambda *a, **k: ("host=/tmp port=5 dbname=d", None))
    monkeypatch.setattr(cli, "_safe_fp", lambda *a, **k: None)
    monkeypatch.setattr(pb, "explain",
                        lambda dsn, q: {"Node Type": "Seq Scan", "Plan Rows": 10,
                                        "Actual Rows": 200, "Actual Total Time": 3.5,
                                        "Total Cost": 100.0})
    monkeypatch.setattr(cli, "PLANS_DIR", tmp_path / "plans")
    qf = tmp_path / "q.sql"
    qf.write_text("SELECT 1;\n", encoding="utf-8")
    r = CliRunner().invoke(cli.cli, ["planbench", str(qf), "--format", "json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["id"].startswith("exp-") and doc["kind"] == "planbench"
    assert doc["plain_plans"]["Q0"]["Node Type"] == "Seq Scan"
    assert "已落账本" in r.stderr and "已落账本" not in r.stdout
