"""benchmark suite tests: task registry / mission symmetry / DDL parsing and reference hits / grading branches / CLI flows.

The symmetry invariant is the fairness cornerstone of this suite (S4 lesson): the cli/psql mission variants may differ only in the Tools section.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from pgtuner import benchmark as bm
from pgtuner import ledger as ledger_mod
from pgtuner.cli import cli


# ---------------------------- task registry ----------------------------

def test_registry_five_tasks_unique_ids():
    assert len(bm.TASKS) == 7
    assert set(bm.TASKS) == {"workload-index", "slow-query", "plan-accuracy",
                             "lock-incident", "xbuild-diff",
                             "migration-safety", "stats-drift"}
    for t in bm.TASKS.values():
        assert t.template and t.title and t.mission_steps
        assert t.difficulty in ("easy", "medium", "hard")
        assert t.profile in ("index", "plan", "incident", "xbuild",
                             "migration", "drift")
        # leanness budget explicitly given for every task (metric self-check: verification adequacy has a budget)
        assert t.max_ledger_entries is not None and t.max_ledger_entries >= 3


def test_get_task_unknown_id_lists_known():
    with pytest.raises(ValueError, match="workload-index"):
        bm.get_task("nope")


def test_flagship_has_reference_and_red_herring():
    t = bm.get_task("workload-index")
    assert t.red_herring is not None and t.red_herring.table == "orders"
    assert t.writes_workload and t.queries is None
    assert t.max_indexes == 3
    # reference criterion (outcome-first): any customer_id-leading key on orders + customers(region)
    assert [r.table for r in t.reference] == ["orders", "customers"]


# ---------------------------- mission symmetry (fairness rule) ----------------------------

def _strip_tools(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.startswith("Tools:"))


@pytest.mark.parametrize("task_id", sorted(bm.TASKS))
def test_mission_symmetric_except_tools(task_id):
    t = bm.get_task(task_id)
    ctx = {"port": "5433", "run_dir": "/x", "db": "d", "repo_dir": "/r"}
    cli_text = bm.mission_text(t, "cli", **ctx)
    psql_text = bm.mission_text(t, "psql", **ctx)
    assert _strip_tools(cli_text) == _strip_tools(psql_text)
    # Tools section: the cli variant points to the workbench; the psql variant explicitly bans pgtuner (S4 contamination guard)
    assert "pgtuner CLI workbench" in cli_text
    assert "do not execute any pgtuner command" in psql_text.lower()


def test_mission_rejects_unknown_variant():
    with pytest.raises(ValueError, match="cli|psql"):
        bm.mission_text(bm.get_task("slow-query"), "http")


def test_mission_placeholders_default_and_filled():
    t = bm.get_task("slow-query")
    default = bm.mission_text(t, "cli")
    assert "<port>" in default and "<db>" in default
    filled = bm.mission_text(t, "cli", port=5433, db="bench_x")
    assert "5433" in filled and "bench_x" in filled and "<port>" not in filled


def test_workload_py_no_red_herring_comment():
    """Handouts must not carry red-herring labels — identifying it is part of the task."""
    text = bm.workload_py_text(5433)
    for banned in ("红鲱鱼", "red herring", "red-herring", "already fast"):
        assert banned.lower() not in text.lower()
    assert "port=5433" in text
    assert text.count("customer_id") >= 2 and "id=123456" in text


# ---------------------------- DDL parsing and reference hits ----------------------------

@pytest.mark.parametrize("ddl,table,cols", [
    ("CREATE INDEX ON orders (customer_id, status)", "orders", ("customer_id", "status")),
    ("CREATE INDEX idx1 ON public.orders USING btree (created_at)",
     "orders", ("created_at",)),
    ("CREATE UNIQUE INDEX IF NOT EXISTS i2 ON customers (region, id)",
     "customers", ("region", "id")),
    ("CREATE INDEX CONCURRENTLY ON orders (customer_id, status) INCLUDE (amount)",
     "orders", ("customer_id", "status")),          # INCLUDE columns not part of the key
    ("CREATE INDEX orders_c_idx ON public.orders USING btree (customer_id, status) INCLUDE (amount)",
     "orders", ("customer_id", "status")),          # pg_indexes.indexdef form
    ('CREATE INDEX ON "Orders" ("Customer_Id")', "orders", ("customer_id",)),
])
def test_parse_index_ddl_variants(ddl, table, cols):
    assert bm.parse_index_ddl(ddl) == (table, cols)


def test_parse_index_ddl_garbage_returns_none():
    assert bm.parse_index_ddl("DROP TABLE t") is None
    assert bm.parse_index_ddl("") is None


def test_covers_prefix_and_alternatives():
    ref = bm.RefIndex("orders", (("customer_id", "status"), ("customer_id",)))
    assert bm.covers(ref, [("orders", ("customer_id", "status"))])
    assert bm.covers(ref, [("orders", ("customer_id", "status", "created_at"))])  # prefix hit
    assert bm.covers(ref, [("orders", ("customer_id",))])                          # alternative prefix
    assert bm.covers(ref, [("orders", ("customer_id", "status"))])
    assert not bm.covers(ref, [("orders", ("status", "customer_id"))])             # wrong column order
    assert not bm.covers(ref, [("customers", ("customer_id",))])                   # wrong table
    # INCLUDE context: the parsing layer already drops INCLUDE, key only reaches (customer_id,status) → still a hit


def test_created_ddls_dedup_and_sources():
    e1 = _fake_exp(payload={"verify": {"indexes": [
        {"ddl": "CREATE INDEX ON orders (customer_id, status)"},
        {"ddl": 'CREATE INDEX  ON orders ( customer_id , status ) INCLUDE (amount)'}]}})
    report = "final plan:\nCREATE INDEX ON customers (region);\nCREATE INDEX ON customers (region);"
    out = bm.created_ddls([e1], db_indexes=["CREATE INDEX x ON public.orders USING btree (created_at)"],
                          report_text=report)
    parsed = sorted(bm.parse_index_ddl(d) for d in out)
    # after dedup on the three keys exactly 3 entries: orders(customer_id,status) / orders(created_at) / customers(region)
    assert parsed == [("customers", ("region",)), ("orders", ("created_at",)),
                      ("orders", ("customer_id", "status"))]


def test_created_ddls_dropped_not_counted():
    """verify.dropped holds plans vetoed by the actual re-check; they do not count as deliverables."""
    e = _fake_exp(payload={"verify": {"indexes": [{"ddl": "CREATE INDEX ON orders (created_at)"}],
                                       "dropped": [{"ddl": "CREATE INDEX ON orders (id)"}]}})
    parsed = [bm.parse_index_ddl(d) for d in bm.created_ddls([e], None, None)]
    assert ("orders", ("id",)) not in parsed


# ---------------------------- grade_run branches ----------------------------

def _fake_exp(kind="diagnose", payload=None, conclusion="ok", exp_id="exp-x-1"):
    class E:                                    # lightweight stand-in: grade only uses kind/conclusion/payload
        pass
    e = E()
    e.kind, e.payload, e.conclusion, e.id = kind, payload, conclusion, exp_id
    return e


def _run(task_id="workload-index", db="bench_run1"):
    return bm.RunState(run_id=f"{task_id}-20261113-000000", task=task_id, instance="rel19",
                       db=db, template="biz_tpl", variant="cli",
                       ledger_exp="exp-stamp", run_dir="/tmp/x")


_GOOD_DDL = 'CREATE INDEX ON orders (customer_id, status) INCLUDE (amount)'


def _full_pass_exps():
    return [_fake_exp(payload={"verify": {
        "baseline_id": "exp-a", "after_id": "exp-b",
        "indexes": [{"ddl": _GOOD_DDL}, {"ddl": "CREATE INDEX ON orders (created_at)"},
                    {"ddl": "CREATE INDEX ON customers (region)"}]}})]


def test_grade_full_pass():
    checks = bm.grade_run(bm.get_task("workload-index"), _run(), _full_pass_exps(), db_state="gone")
    by = {c.name: c for c in checks}
    assert all(c.passed for c in checks), bm.scorecard(checks)
    assert set(by) == {"hygiene", "evidence", "ab-pair", "reference", "red-herring", "sprawl", "leanness"}


def test_grade_hygiene_fail_leftover_db():
    checks = bm.grade_run(bm.get_task("workload-index"), _run(), _full_pass_exps(), db_state="present")
    h = {c.name: c for c in checks}["hygiene"]
    assert h.passed is False and "leftover" in h.detail


def test_grade_hygiene_skip_when_instance_unreachable():
    checks = bm.grade_run(bm.get_task("workload-index"), _run(), _full_pass_exps(), db_state="unknown")
    h = {c.name: c for c in checks}["hygiene"]
    assert h.passed is None and h.verdict == "SKIP"
    assert "score:" in bm.scorecard(checks) and "skipped" in bm.scorecard(checks)


def test_grade_no_evidence_no_ddl():
    checks = bm.grade_run(bm.get_task("workload-index"), _run(), [], db_state="gone")
    by = {c.name: c for c in checks}
    assert by["evidence"].passed is False
    assert by["ab-pair"].passed is False
    assert "no CREATE INDEX DDL" in by["reference"].detail
    assert by["red-herring"].passed is True     # no indexes built means no red-herring hit (but reference already FAIL)


def test_grade_red_herring_hit():
    exps = _full_pass_exps() + [_fake_exp(exp_id="exp-x-2", payload={"verify": {
        "baseline_id": "exp-a", "after_id": "exp-b",
        "indexes": [{"ddl": "CREATE INDEX ON orders (id)"}]}})]
    checks = bm.grade_run(bm.get_task("workload-index"), _run(), exps, db_state="gone")
    by = {c.name: c for c in checks}
    assert by["red-herring"].passed is False


def test_grade_partial_reference_reports_missing():
    exps = [_fake_exp(payload={"verify": {
        "baseline_id": "exp-a", "after_id": "exp-b",
        "indexes": [{"ddl": "CREATE INDEX ON orders (customer_id)"}]}})]
    checks = bm.grade_run(bm.get_task("workload-index"), _run(), exps, db_state="gone")
    by = {c.name: c for c in checks}
    assert by["reference"].passed is False
    # a (customer_id)-leading key already satisfies the orders side; only customers(region) is missing
    assert "customers (region)" in by["reference"].detail
    assert "orders" not in by["reference"].detail


def test_grade_sprawl_over_budget():
    ddls = [{"ddl": f"CREATE INDEX ON orders (col{i})"} for i in range(5)]
    ddls.append({"ddl": "CREATE INDEX ON orders (customer_id, status)"})
    exps = [_fake_exp(payload={"verify": {"baseline_id": "a", "after_id": "b",
                                          "indexes": ddls}})]
    checks = bm.grade_run(bm.get_task("workload-index"), _run(), exps, db_state="gone")
    by = {c.name: c for c in checks}
    assert by["sprawl"].passed is False and "budget 3" in by["sprawl"].detail


def test_grade_plan_profile_needs_two_conclusions():
    task = bm.get_task("plan-accuracy")
    one = [_fake_exp(kind="planbench", conclusion="before: estimates off")]
    checks = bm.grade_run(task, _run("plan-accuracy"), one, db_state="gone")
    by = {c.name: c for c in checks}
    assert set(by) == {"hygiene", "evidence", "leanness"}
    assert by["evidence"].passed is False and ">= 2" in by["evidence"].detail
    two = one + [_fake_exp(kind="planbench", conclusion="after: fixed", exp_id="exp-x-2")]
    checks = bm.grade_run(task, _run("plan-accuracy"), two, db_state="gone")
    assert all(c.passed for c in checks), bm.scorecard(checks)


def test_report_ddls_fenced_blocks_only():
    """With fenced code blocks only DDL inside the blocks counts; rejected candidates mentioned in prose are not deliverables (S5-A1 lesson)."""
    report = (
        "Candidate `CREATE INDEX ON orders (created_at)` was auto-dropped.\n"
        "```sql\nCREATE INDEX ON orders (customer_id, status);\n"
        "CREATE INDEX ON customers (region);\n```\n")
    parsed = sorted(bm.parse_index_ddl(d) for d in bm._report_ddls(report))
    assert parsed == [("customers", ("region",)), ("orders", ("customer_id", "status"))]
    # no fence → fall back to the full text
    plain = "plan: CREATE INDEX ON customers (region);"
    assert bm._report_ddls(plain) == ["CREATE INDEX ON customers (region)"]


def test_report_ddls_comment_lines_stripped():
    """Rejected candidates mentioned in `-- comment` lines inside the fence are not deliverables (S5-A3 lesson)."""
    report = ("```sql\n"
              "CREATE INDEX ON orders (customer_id, status);\n"
              "-- rejected by re-check: CREATE INDEX ON orders (created_at)\n"
              "CREATE INDEX ON customers (region);  -- Q5\n"
              "```\n")
    parsed = sorted(bm.parse_index_ddl(d) for d in bm._report_ddls(report))
    assert parsed == [("customers", ("region",)), ("orders", ("customer_id", "status"))]


def test_grade_zero_handout_snapshot_hygiene():
    """Zero-handout task (db=None): hygiene uses the snapshot criterion — current database set ⊆ setup snapshot."""
    run = _run()
    run.db = None
    run.dbs_at_setup = ["foreign_db"]
    checks = bm.grade_run(bm.get_task("workload-index"), run, _full_pass_exps(),
                          db_state="unknown", current_dbs=["foreign_db"])
    h = {c.name: c for c in checks}["hygiene"]
    assert h.passed is True and "snapshot" in h.detail
    # agent's own clone not returned → FAIL and the leftover database is named
    checks = bm.grade_run(bm.get_task("workload-index"), run, _full_pass_exps(),
                          db_state="unknown", current_dbs=["foreign_db", "biz_tpl_left1"])
    h = {c.name: c for c in checks}["hygiene"]
    assert h.passed is False and "biz_tpl_left1" in h.detail
    # instance unreachable → SKIP
    checks = bm.grade_run(bm.get_task("workload-index"), run, _full_pass_exps(),
                          db_state="unknown", current_dbs=None)
    assert {c.name: c for c in checks}["hygiene"].passed is None


def test_grade_report_file_as_ddl_source():
    """psql-style subject under test (no ledger evidence): DDL in the report still participates in reference hits (the evidence check still FAIL)."""
    report = "## final\nCREATE INDEX ON orders (customer_id, status);\n" \
             "CREATE INDEX ON orders (created_at);\nCREATE INDEX ON customers (region);\n"
    checks = bm.grade_run(bm.get_task("workload-index"), _run(), [], db_state="gone",
                          report_text=report)
    by = {c.name: c for c in checks}
    assert by["reference"].passed is True
    assert by["evidence"].passed is False and by["ab-pair"].passed is False


# ---------------------------- run state ----------------------------

def test_run_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "RUNS_DIR", tmp_path)
    st = _run()
    p = bm.save_run(st)
    assert json.loads(p.read_text(encoding="utf-8"))["db"] == st.db
    assert bm.load_run(st.run_id).ledger_exp == st.ledger_exp


def test_latest_run_picks_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "RUNS_DIR", tmp_path)
    for rid in ("workload-index-20261113-010101", "workload-index-20261113-020202"):
        bm.save_run(bm.RunState(run_id=rid, task="workload-index", instance="rel19",
                                db="d", template="biz_tpl", variant="cli",
                                ledger_exp="e", run_dir="/tmp"))
    assert bm.latest_run("workload-index").run_id == "workload-index-20261113-020202"
    assert bm.latest_run("slow-query") is None
    with pytest.raises(ValueError, match="no benchmark run"):
        bm.load_run("nope-1")


# ---------------------------- CLI ----------------------------

def _runner():
    return CliRunner()


def test_cli_list_shows_three_tasks():
    res = _runner().invoke(cli, ["benchmark", "list"])
    assert res.exit_code == 0
    for tid in ("workload-index", "slow-query", "plan-accuracy"):
        assert tid in res.output


def test_cli_show_variant_and_plan():
    res = _runner().invoke(cli, ["benchmark", "show", "workload-index", "--variant", "psql"])
    assert res.exit_code == 0
    assert "do not execute any pgtuner command" in res.output.lower()
    # the mission discloses the red-herring "concept" (symmetry), but does not name which query it is (that is the grading answer)
    assert "red herring" in res.output.lower()
    assert "id=123456" not in res.output
    res = _runner().invoke(cli, ["benchmark", "show", "workload-index", "--plan"])
    assert "red herring" in res.output and "orders (id)" in res.output
    res = _runner().invoke(cli, ["benchmark", "show", "nope"])
    assert res.exit_code != 0


def test_cli_setup_full_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.delenv("PGTUNER_INSTANCE", raising=False)
    monkeypatch.setattr("pgtuner.cli.config_mod.default_instance", lambda: "rel19")

    class Inst:
        name, pg_version, build_type, port = "rel19", "PostgreSQL 19.1", "release", 5433
    monkeypatch.setattr("pgtuner.cli.instance_mod.ensure_running", lambda n: Inst())
    monkeypatch.setattr("pgtuner.cli.instance_mod.clone",
                        lambda inst, tpl: "bench_clone_1")
    monkeypatch.setattr("pgtuner.cli.pg.connect_rw", lambda dsn: _FakeConn())

    # flagship zero-handout task: setup does not clone on the agent's behalf (S5-A1 lesson), only writes workload.py + a ledger stamp
    res = _runner().invoke(cli, ["benchmark", "setup", "workload-index"])
    assert res.exit_code == 0, res.output
    assert "run-id: workload-index-" in res.output
    assert "zero-handout" in res.output
    assert "workload.py" in res.output                     # flagship task hands out the workload driver
    assert "Tools: the pgtuner CLI workbench" in res.output
    wf = tmp_path / "runs" / res.output.split("run-id: ")[1].splitlines()[0] / "workload.py"
    assert "port=5433" in wf.read_text(encoding="utf-8")
    # ledger stamp recorded: zero-handout db=None, snapshot empty (FakeConn.fetchall=[])
    stamps = ledger_mod.list_experiments(kind="benchmark")
    assert len(stamps) == 1 and stamps[0].payload["db"] is None
    assert stamps[0].payload["dbs_at_setup"] == []

    # slow-query: pre-fed task clone + queries.sql, no workload.py handed out
    res = _runner().invoke(cli, ["benchmark", "setup", "slow-query", "--variant", "psql"])
    assert res.exit_code == 0, res.output
    assert "bench_clone_1" in res.output
    assert "queries.sql" in res.output and "workload.py" not in res.output
    assert "psql only" in res.output.lower()


def test_cli_setup_requires_instance():
    from pgtuner import config as config_mod
    import pgtuner.cli as cli_mod
    orig = cli_mod.config_mod.default_instance
    cli_mod.config_mod.default_instance = lambda: None
    try:
        res = _runner().invoke(cli, ["benchmark", "setup", "slow-query"])
        assert res.exit_code != 0
        assert "default-instance" in res.output
    finally:
        cli_mod.config_mod.default_instance = orig


def test_cli_grade_latest_run(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    stamp = ledger_mod.new_experiment("benchmark", "benchmark:slow-query",
                                      payload={"task": "slow-query"})
    bm.save_run(bm.RunState(run_id="slow-query-20261113-010101", task="slow-query",
                            instance="rel19", db="bench_gone", template="biz_tpl",
                            variant="cli", ledger_exp=stamp.id, run_dir="/tmp"))
    # evidence row after the stamp: ledger shape of diagnose --verify
    ev = ledger_mod.new_experiment("diagnose", "诊断:...", payload={
        "verify": {"baseline_id": "exp-1", "after_id": "exp-2",
                   "indexes": [{"ddl": "CREATE INDEX ON orders (customer_id, created_at)"},
                               {"ddl": "CREATE INDEX ON orders (status)"},
                               {"ddl": "CREATE INDEX ON customers (region)"}]}})
    ledger_mod.update(ev.id, conclusion="auto: improved")
    monkeypatch.setattr("pgtuner.cli.instance_mod.dsn_of", lambda n, d: "host=/tmp")
    monkeypatch.setattr("pgtuner.cli.pg.connect_rw", lambda dsn: _FakeConn())

    res = _runner().invoke(cli, ["benchmark", "grade", "slow-query"])
    assert res.exit_code == 0, res.output
    assert "reference" in res.output and "PASS" in res.output
    assert "score: 6/6" in res.output          # slow-query has no red-herring check, includes leanness: 6 checks
    # the conclusion backfills the stamp experiment
    got = ledger_mod.get(stamp.id)
    assert "[benchmark grade]" in got.conclusion
    assert len(got.payload["grade"]) == 6
    _ = ev  # noqa: F841  the evidence row is inside the window


def test_cli_grade_exit_code_on_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    stamp = ledger_mod.new_experiment("benchmark", "benchmark:slow-query")
    bm.save_run(bm.RunState(run_id="slow-query-20261113-010102", task="slow-query",
                            instance="rel19", db="bench_left", template="biz_tpl",
                            variant="psql", ledger_exp=stamp.id, run_dir="/tmp"))
    monkeypatch.setattr("pgtuner.cli.instance_mod.dsn_of", lambda n, d: "host=/tmp")
    monkeypatch.setattr("pgtuner.cli.pg.connect_rw", lambda dsn: _FakeConn(exists=True))
    res = _runner().invoke(cli, ["benchmark", "grade", "slow-query"])
    assert res.exit_code == 1                       # any FAIL → non-zero exit (orchestration gate)
    assert "FAIL" in res.output


class _FakeConn:
    """Stand-in for the pg_database existence probe: fetchone returns None when exists=False."""

    def __init__(self, exists: bool = False):
        self._exists = exists

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.last_sql = sql

    def fetchone(self):
        return (1,) if self._exists else None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


# ---------------------------- v0.8.0:leanness / incident / xbuild ----------------------------

def _aug(kind="rebench", payload=None, conclusion="", exp_id="exp-x", instance=None,
         fingerprint=None):
    e = _fake_exp(kind=kind, payload=payload, conclusion=conclusion, exp_id=exp_id)
    e.instance, e.fingerprint = instance, fingerprint
    return e


def test_grade_leanness_budget():
    t = bm.get_task("workload-index")
    within = [_fake_exp(kind="rebench", payload={"metrics": {"x": 1}}, exp_id=f"e{i}")
              for i in range(3)]
    over = [_fake_exp(kind="rebench", payload={"metrics": {"x": 1}}, exp_id=f"f{i}")
            for i in range(6)]
    ok = {c.name: c for c in bm.grade_run(t, _run(), _full_pass_exps() + within, db_state="gone")}
    assert ok["leanness"].passed                       # 1+3=4 ≤ 6
    bad = {c.name: c for c in bm.grade_run(t, _run(), _full_pass_exps() + over, db_state="gone")}
    assert bad["leanness"].passed is False and "budget 6" in bad["leanness"].detail


def _incident_run():
    return bm.RunState(run_id="lock-incident-20261114-000000", task="lock-incident",
                       instance="rel19", db="biz_tpl_ccafe", template="biz_tpl",
                       variant="cli", ledger_exp="exp-stamp", run_dir="/tmp/x",
                       staged={"holder_backend_pid": 4242, "victim_backend_pid": 4243,
                               "decoy_db": "biz_tpl_cdeadbeef"})

_INCIDENT_REPORT = "diagnosis: waiting pid 4243 (wait_event Lock); blocker pid 4242 idle in transaction"


def test_grade_incident_full_pass():
    checks = bm.grade_run(
        bm.get_task("lock-incident"), _incident_run(),
        [_fake_exp(kind="rebench", payload={"metrics": {"x": 1}}, conclusion="re-run ok")],
        db_state="gone", report_text=_INCIDENT_REPORT,
        audit_rows=[{"event": "release-denied", "db": "biz_tpl_ccafe", "ts": "t0"},
                    {"event": "release", "db": "biz_tpl_ccafe", "forced": True, "ts": "t1"}],
        decoy_state="present")
    by = {c.name: c for c in checks}
    assert set(by) == {"hygiene", "evidence", "targeting", "response", "collateral", "leanness"}
    assert all(c.passed for c in checks), [c.detail for c in checks]
    assert "refused attempt(s) first" in by["response"].detail
    assert "waiting pid 4243" in by["targeting"].detail    # victim pid referenced along the way


def test_grade_incident_targeting_response_collateral_fail():
    by = {c.name: c for c in bm.grade_run(
        bm.get_task("lock-incident"), _incident_run(),
        [_fake_exp(kind="rebench", conclusion="x")],
        db_state="gone", report_text="guess: probably some stuck session",
        audit_rows=[], decoy_state="gone")}
    assert by["targeting"].passed is False                 # true culprit backend pid not referenced
    assert by["response"].passed is False                  # no audit release event
    assert by["collateral"].passed is False                # bystander database harmed
    skip = {c.name: c.verdict for c in bm.grade_run(
        bm.get_task("lock-incident"), _incident_run(), [], db_state="gone",
        audit_rows=[], decoy_state="present")}
    assert skip["targeting"] == "SKIP"                     # no report → attribution undecidable


def _xb_run():
    return bm.RunState(run_id="xbuild-diff-20261114-000000", task="xbuild-diff",
                       instance="rel19", db="xb_a", template="xbuild_tpl", variant="cli",
                       ledger_exp="exp-stamp", run_dir="/tmp/x",
                       staged={"peer_instance": "base2", "peer_db": "xb_b", "peer_port": 5437})


def _xb_exps(a_ms=100.0, b_ms=150.0, verdict="slower"):
    concl = f"cross-build compare, ids cited; VERDICT: A->B {verdict}"
    ea = _aug(payload={"metrics": {"baseline|Q0_ms": a_ms, "baseline|Q1_ms": a_ms * 2}},
              conclusion=concl, exp_id="ea",
              instance={"name": "rel19", "build_type": "release"})
    eb = _aug(payload={"metrics": {"baseline|Q0_ms": b_ms, "baseline|Q1_ms": b_ms * 2}},
              conclusion=concl, exp_id="eb",
              instance={"name": "base2", "build_type": "release"})
    return [ea, eb]


def test_grade_xbuild_full_pass():
    checks = bm.grade_run(bm.get_task("xbuild-diff"), _xb_run(), _xb_exps(),
                          db_state="gone", peer_db_state="gone",
                          report_text="final: VERDICT: A->B slower")
    by = {c.name: c for c in checks}
    assert set(by) == {"hygiene", "evidence", "peer-hygiene", "release-only",
                       "direction", "leanness"}
    assert all(c.passed for c in checks), [c.detail for c in checks]


def test_grade_xbuild_direction_mismatch_parity_ambiguity():
    t = bm.get_task("xbuild-diff")
    bad = {c.name: c for c in bm.grade_run(
        t, _xb_run(), _xb_exps(verdict="faster"), db_state="gone", peer_db_state="gone",
        report_text="final: VERDICT: A->B faster")}
    assert bad["direction"].passed is False and "slower" in bad["direction"].detail
    par = {c.name: c for c in bm.grade_run(
        t, _xb_run(), _xb_exps(b_ms=102.0, verdict="parity"), db_state="gone",
        peer_db_state="gone", report_text="final: VERDICT: A->B parity")}
    assert par["direction"].passed                       # within ±5% = parity
    amb = {c.name: c for c in bm.grade_run(              # conclusion and report disagree → undecidable
        t, _xb_run(), _xb_exps(), db_state="gone", peer_db_state="gone",
        report_text="final: VERDICT: A->B faster")}
    assert amb["direction"].verdict == "SKIP"


def test_grade_xbuild_release_only_and_peer_hygiene():
    t = bm.get_task("xbuild-diff")
    dbg = _aug(payload={"metrics": {"x": 1}}, conclusion="measured on dbg20", exp_id="edbg",
               instance={"name": "dbg20", "build_type": "debug"},
               fingerprint={"build_type": "debug"})
    checks = {c.name: c for c in bm.grade_run(
        t, _xb_run(), _xb_exps() + [dbg], db_state="gone", peer_db_state="gone",
        report_text="final: VERDICT: A->B slower")}
    assert checks["release-only"].passed is False and "edbg" in checks["release-only"].detail
    peer = {c.name: c for c in bm.grade_run(
        t, _xb_run(), _xb_exps(), db_state="gone", peer_db_state="present",
        report_text="final: VERDICT: A->B slower")}
    assert peer["peer-hygiene"].passed is False


def test_xbuild_verdict_helpers_edges():
    one = _aug(payload={"metrics": {"a_ms": 1}}, exp_id="e1",
               instance={"name": "rel19", "build_type": "release"})
    assert bm._xbuild_verdict([one], _xb_run()) is None   # missing peer → undecidable
    assert bm._claimed_verdict([], None) is None
    assert bm._claimed_verdict([], "VERDICT: A->B parity") == "parity"
    assert bm._claimed_verdict([], "VERDICT: parity") == "parity"   # natural phrasing seen in S7 (A->B omitted)
    a = _fake_exp(kind="rebench", conclusion="VERDICT: A->B slower")
    assert bm._claimed_verdict([a], "VERDICT: A->B faster") is None


def test_runstate_staged_roundtrip():
    st = _incident_run()
    loaded = bm.RunState(**json.loads(st.to_json()))
    assert loaded.staged == st.staged and loaded.staged["holder_backend_pid"] == 4242


def test_holder_script_shapes():
    txt = bm.holder_py_text("d1", "d2")
    for app in ("payments-worker", "orders-api", "reporting-job"):
        assert app in txt
    assert "FOR UPDATE" in txt
    assert "UPDATE orders SET amount = amount + 1 WHERE id = 123456" in txt
    for key in ("holder_backend_pid", "victim_backend_pid", "decoy_backend_pid"):
        assert f'pids["{key}"]' in txt
    assert "time.sleep(7200)" in txt                     # bystanders stay alive until the operator cleans up


def test_cli_setup_lock_incident_stages(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.delenv("PGTUNER_INSTANCE", raising=False)
    monkeypatch.setattr("pgtuner.cli.config_mod.default_instance", lambda: "rel19")

    class Inst:
        name, pg_version, build_type, port = "rel19", "PostgreSQL 19.1", "release", 5433
    monkeypatch.setattr("pgtuner.cli.instance_mod.ensure_running", lambda n: Inst())
    monkeypatch.setattr("pgtuner.cli.instance_mod.get", lambda n: Inst())
    clone_calls = []

    def fake_clone(inst, tpl, owner=None):
        clone_calls.append((inst, tpl, owner))
        return "biz_tpl_cdeadbeef" if owner else "biz_tpl_ccafe"
    monkeypatch.setattr("pgtuner.cli.instance_mod.clone", fake_clone)
    monkeypatch.setattr("pgtuner.cli.pg.connect_rw", lambda dsn: _FakeConn())

    def fake_popen(args, **kw):
        from pathlib import Path as P
        out = P(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"holder_backend_pid": 4242, "victim_backend_pid": 4243,
                                   "decoy_backend_pid": 9999}), encoding="utf-8")

        class P:
            pid = 999
        return P()
    monkeypatch.setattr("pgtuner.cli.subprocess.Popen", fake_popen)

    res = _runner().invoke(cli, ["benchmark", "setup", "lock-incident"])
    assert res.exit_code == 0, res.output
    assert ("rel19", "biz_tpl", None) in clone_calls                 # incident database (no owner = self-use)
    assert ("rel19", "biz_tpl", "app-team-batch") in clone_calls     # bystander database (another team's owner)
    state = bm.latest_run("lock-incident")
    assert state.db == "biz_tpl_ccafe"
    assert state.staged["holder_backend_pid"] == 4242
    assert state.staged["decoy_db"] == "biz_tpl_cdeadbeef"
    assert "queries.sql" in res.output                               # victim statement handed out


def test_cli_setup_xbuild_stages(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.delenv("PGTUNER_INSTANCE", raising=False)
    monkeypatch.setattr("pgtuner.cli.config_mod.default_instance", lambda: "rel19")

    class Inst:
        def __init__(self, name):
            self.name = name
            self.pg_version, self.build_type = "PostgreSQL 20devel", "release"
            self.port = {"rel19": 5433, "base2": 5437}[name]
            self.socket_dir = "/tmp"
    made, analyzed = [], []

    def fake_ensure(n):
        return Inst(n)
    monkeypatch.setattr("pgtuner.cli.instance_mod.ensure_running", fake_ensure)
    monkeypatch.setattr("pgtuner.cli.instance_mod.get", lambda n, registry_file=None: Inst(n))

    def fake_clone(inst, tpl, owner=None):
        return "xb_a" if inst == "rel19" else "xb_b"
    monkeypatch.setattr("pgtuner.cli.instance_mod.clone", fake_clone)
    monkeypatch.setattr("pgtuner.cli.instance_mod.make_template",
                        lambda n, tpl, sql: made.append((n, tpl)))
    monkeypatch.setattr("pgtuner.cli.pg.connect_rw", lambda dsn: _FakeConn())

    def fake_execute(conn, sql):
        if sql.strip().upper().startswith("ANALYZE"):
            analyzed.append(sql)
    monkeypatch.setattr("pgtuner.cli.pg.execute", fake_execute)

    res = _runner().invoke(cli, ["benchmark", "setup", "xbuild-diff"])
    assert res.exit_code == 0, res.output
    assert made == [("rel19", "xbuild_tpl"), ("base2", "xbuild_tpl")]  # both sides' templates freshly created
    assert len(analyzed) == 2                                          # both sides' clones ANALYZEd
    state = bm.latest_run("xbuild-diff")
    assert state.staged["peer_instance"] == "base2"
    assert state.staged["peer_db"] == "xb_b" and state.staged["peer_port"] == 5437
    assert "instance rel19" in res.output and "instance base2" in res.output
    assert "VERDICT: A->B" in res.output                               # the mission carries a machine-checkable criterion


# ---------------------------- migration-safety / stats-drift (v0.9.0 S8) ----------------------------

def _rebench_exp(queries_ms, exp_id="exp-r1", conclusion="ok"):
    """Stand-in for the rebench custom ledger shape: cycles[].queries + metric candidate|Q<i>_ms."""
    cycles = [{"name": "baseline", "queries": [
        {"query": q, "execution_ms": ms} for q, ms in queries_ms]}]
    metrics = {f"baseline|Q{i}_ms": ms for i, (q, ms) in enumerate(queries_ms)}
    return _fake_exp(kind="rebench", payload={"cycles": cycles, "metrics": metrics},
                     conclusion=conclusion, exp_id=exp_id)


_MIGRATIONS = [
    ("ALTER TABLE orders ADD COLUMN note text", 12.0),
    ("ALTER TABLE orders ALTER COLUMN amount TYPE bigint", 4200.0),
    ("CREATE INDEX ON orders (status)", 3100.0),
    ("ALTER TABLE orders ADD CONSTRAINT orders_amount_check "
     "CHECK (amount >= 0) NOT VALID", 8.0),
    ("ALTER TABLE orders ADD COLUMN tag text DEFAULT md5(random()::text)", 5100.0),
]
_MIG_REPORT_OK = ("## classes\nM1: instant\nM2: rewrite\nM3: lock_risk\n"
                  "M4: instant\nM5: rewrite\n")


def test_migration_grade_full_pass():
    checks = bm.grade_run(bm.get_task("migration-safety"), _run("migration-safety"),
                          [_rebench_exp(_MIGRATIONS)], db_state="gone",
                          report_text=_MIG_REPORT_OK)
    assert {c.name: c.verdict for c in checks} == {
        "hygiene": "PASS", "evidence": "PASS", "classify": "PASS",
        "ddl-timed": "PASS", "consistency": "PASS", "leanness": "PASS"}
    cons = next(c for c in checks if c.name == "consistency")
    assert "x350" in cons.detail                      # measured ratio of 12ms vs 4200ms recorded


def test_migration_classify_wrong_missing_conflicting():
    task = bm.get_task("migration-safety")
    run = _run("migration-safety")
    exps = [_rebench_exp(_MIGRATIONS)]
    # M1 misclassified, M3 misclassified, M5 line missing
    bad = "M1: rewrite\nM2: rewrite\nM3: instant\nM4: instant\n"
    checks = bm.grade_run(task, run, exps, db_state="gone", report_text=bad)
    c = next(c for c in checks if c.name == "classify")
    assert c.verdict == "FAIL" and "missing M5" in c.detail and "M1" in c.detail
    # the same migration contradicts itself (instant and rewrite)
    conflict = _MIG_REPORT_OK + "later correction: M2: instant\n"
    checks = bm.grade_run(task, run, exps, db_state="gone", report_text=conflict)
    c = next(c for c in checks if c.name == "classify")
    assert c.verdict == "FAIL" and "conflicting" in c.detail


def test_migration_classify_accepts_table_and_compact_forms():
    """Both table cells `M1 | instant` and the compact `M1:instant` must be accepted (S7 lesson: formatting must not punish correct claims)."""
    report = ("| M1 | instant |\n| M2 | rewrite |\n| M3 | lock_risk |\n"
              "M4:instant\nM5 = rewrite\n")
    checks = bm.grade_run(bm.get_task("migration-safety"), _run("migration-safety"),
                          [_rebench_exp(_MIGRATIONS)], db_state="gone", report_text=report)
    assert next(c for c in checks if c.name == "classify").verdict == "PASS"


def test_migration_ddl_timed_and_consistency_degrade():
    task = bm.get_task("migration-safety")
    run = _run("migration-safety")
    # no utility timing: ddl-timed FAIL, consistency SKIP (nothing to base it on, so it does not count)
    checks = bm.grade_run(task, run, [_fake_exp(kind="bench")], db_state="gone",
                          report_text=_MIG_REPORT_OK)
    by = {c.name: c.verdict for c in checks}
    assert by["ddl-timed"] == "FAIL" and by["consistency"] == "SKIP"
    # split run times only 3 fast ones: ddl-timed passes, but under the degraded consistency criterion the fast/slow gap is insufficient → FAIL
    partial = _rebench_exp([_MIGRATIONS[0], _MIGRATIONS[3],
                            ("ALTER TABLE orders SET (autovacuum_enabled = true)", 9.0)])
    checks = bm.grade_run(task, run, [partial], db_state="gone", report_text=_MIG_REPORT_OK)
    by = {c.name: c.verdict for c in checks}
    assert by["ddl-timed"] == "PASS"
    c = next(c for c in checks if c.name == "consistency")
    assert c.verdict == "FAIL" and "partial run" in c.detail


_DRIFT_VICTIM = ("SELECT count(*), max(u.region) FROM events e "
                   "JOIN users u ON u.id = e.user_id WHERE e.kind = 'rare'")


def test_drift_grade_full_pass():
    exps = [
        _rebench_exp([(_DRIFT_VICTIM, 420.0)], exp_id="exp-before"),
        _rebench_exp([("ANALYZE events", 300.0)], exp_id="exp-analyze"),
        _rebench_exp([(_DRIFT_VICTIM, 38.0)], exp_id="exp-after"),
    ]
    report = ("root cause: pg_stat_user_tables shows n_mod_since_analyze=500000, "
              "last_analyze froze at 500k rows; planner estimates 5000 vs 505000 actual.\n"
              "fix: ANALYZE events; before 420 ms after 38 ms\n")
    checks = bm.grade_run(bm.get_task("stats-drift"), _run("stats-drift"), exps,
                          db_state="gone", report_text=report)
    assert {c.name: c.verdict for c in checks} == {
        "hygiene": "PASS", "evidence": "PASS", "rootcause": "PASS",
        "analyzed": "PASS", "improvement": "PASS", "leanness": "PASS"}
    imp = next(c for c in checks if c.name == "improvement")
    assert "420.0 ms -> 38.0 ms" in imp.detail


def test_drift_checks_fail_structurally_without_ledger_discipline():
    """No ANALYZE ledgered / root cause unsupported / only a single timing of the victim query → the three checks each fail."""
    exps = [_rebench_exp([(_DRIFT_VICTIM, 420.0)], exp_id="exp-only")]
    checks = bm.grade_run(bm.get_task("stats-drift"), _run("stats-drift"), exps,
                          db_state="gone", report_text="the plan changed, restarted app")
    by = {c.name: c.verdict for c in checks}
    assert by["rootcause"] == "FAIL" and by["analyzed"] == "FAIL"
    assert by["improvement"] == "FAIL" and "found 1" in next(
        c for c in checks if c.name == "improvement").detail


def test_drift_improvement_threshold():
    """380/420 (-9.6%) is not enough to prove a fix; the measured threshold for a drift fix is ≤70%."""
    exps = [_rebench_exp([(_DRIFT_VICTIM, 420.0)], exp_id="exp-b"),
            _rebench_exp([("ANALYZE events", 1.0)], exp_id="exp-a"),
            _rebench_exp([(_DRIFT_VICTIM, 380.0)], exp_id="exp-f")]
    checks = bm.grade_run(bm.get_task("stats-drift"), _run("stats-drift"), exps,
                          db_state="gone", report_text="n_mod_since_analyze cited\n")
    assert next(c for c in checks if c.name == "improvement").verdict == "FAIL"


def test_drift_full_pass_via_setup_single_experiment():
    """v0.11.0 rebench --setup shape: before/setup/after, three cycles living in one experiment.
    improvement must take metric keys per cycle — the old globally flattened index made the after
    phase look for |Q2_ms and miss (coincidentally equivalent with a single cycle, a real defect
    with multiple cycles); analyzed uses setup-cycle criterion #1;
    evidence counts by phase (one --setup entry = two phases, before and after; the intent "both ends ledgered" is kept)."""
    cycles = [
        {"name": "before", "queries": [{"query": _DRIFT_VICTIM, "execution_ms": 552.6}]},
        {"name": "setup", "queries": [{"query": "ANALYZE events", "execution_ms": 115.6}]},
        {"name": "after", "queries": [{"query": _DRIFT_VICTIM, "execution_ms": 81.1}]},
    ]
    metrics = {"before|Q0_ms": 552.6, "setup|Q0_ms": 115.6, "after|Q0_ms": 81.1}
    exps = [_fake_exp(kind="rebench", payload={"cycles": cycles, "metrics": metrics},
                      conclusion="ANALYZE 前后:552.6ms→81.1ms(-85%)", exp_id="exp-setup")]
    report = ("root cause: pg_stats MCV rare freq 0.0009 vs actual 0.25, reltuples stale\n"
              "fix: ANALYZE events (rebench --setup); 552.6 ms -> 81.1 ms\n")
    checks = bm.grade_run(bm.get_task("stats-drift"), _run("stats-drift"), exps,
                          db_state="gone", report_text=report)
    by = {c.name: c.verdict for c in checks}
    assert by["analyzed"] == "PASS" and by["improvement"] == "PASS"
    assert by["evidence"] == "PASS" and by["leanness"] == "PASS"
    imp = next(c for c in checks if c.name == "improvement")
    assert "552.6 ms -> 81.1 ms" in imp.detail
    evd = next(c for c in checks if c.name == "evidence")
    assert "3 ledgered phase(s) across 1 experiment(s)" in evd.detail


def test_is_utility_stmt_grader_side_classification():
    """Grader-side standalone utility classification: everything except EXPLAIN-wrappable leading words counts (DDL/ANALYZE/...)."""
    for s in ("SELECT 1", "  (SELECT 1)", "WITH x AS (SELECT 1) SELECT * FROM x",
              "insert into t values (1)", "UPDATE t SET a=1", "TABLE t", "delete from t"):
        assert not bm._is_utility_stmt(s), s
    for s in ("ALTER TABLE t ADD COLUMN a int", "CREATE INDEX ON t(k)", "ANALYZE t",
              "VACUUM t", "  DROP TABLE t"):
        assert bm._is_utility_stmt(s), s


def test_cli_setup_writes_extra_files(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(ledger_mod, "LEDGER_FILE", tmp_path / "ledger.jsonl")
    monkeypatch.delenv("PGTUNER_INSTANCE", raising=False)
    monkeypatch.setattr("pgtuner.cli.config_mod.default_instance", lambda: "rel19")

    class Inst:
        name, pg_version, build_type, port = "rel19", "PostgreSQL 19.1", "release", 5433
    monkeypatch.setattr("pgtuner.cli.instance_mod.ensure_running", lambda n: Inst())
    monkeypatch.setattr("pgtuner.cli.instance_mod.clone",
                        lambda inst, tpl: "bench_clone_1")
    monkeypatch.setattr("pgtuner.cli.pg.connect_rw", lambda dsn: _FakeConn())

    res = _runner().invoke(cli, ["benchmark", "setup", "migration-safety"])
    assert res.exit_code == 0, res.output
    assert "migrations.sql" in res.output                       # extra material handed out and listed
    run_id = res.output.split("run-id: ")[1].splitlines()[0]
    mig = (tmp_path / "runs" / run_id / "migrations.sql").read_text(encoding="utf-8")
    assert mig.count("ALTER TABLE orders") == 4 and "CREATE INDEX ON orders (status)" in mig
    assert "M1" in mig and "M5" in mig                           # numbered handles handed out with the file


def test_drift_analyzed_accepts_planbench_variant_channel():
    """Observed in S8-G1: ANALYZE via a planbench variant's setup SQL is also legitimate ledger evidence (not only rebench script)."""
    exps = [
        _rebench_exp([(_DRIFT_VICTIM, 552.6)], exp_id="exp-before"),
        _fake_exp(kind="planbench", payload={
            "workload": "plan", "variants": {"variants": [
                {"name": "baseline", "ddl": "", "queries": [{"median_ms": 500.0}]},
                {"name": "fix_analyze", "ddl": "ANALYZE events",
                 "queries": [{"median_ms": 92.3}]}]}},
            exp_id="exp-fix"),
        _rebench_exp([(_DRIFT_VICTIM, 81.1)], exp_id="exp-after"),
    ]
    checks = bm.grade_run(bm.get_task("stats-drift"), _run("stats-drift"), exps,
                          db_state="gone", report_text="reltuples stale, pg_stats cited")
    by = {c.name: c.verdict for c in checks}
    assert by["analyzed"] == "PASS" and by["improvement"] == "PASS"


def test_drift_analyzed_ddl_value_not_ending_in_analyze():
    """Regression observed in S9-4: the old tier-2 regex `[^"]*\\banalyze\\b"` only matched ddl
    values ending in analyze — "ANALYZE events" did not match, and S8-G1 actually slipped through
    via the old-name fallback; after the fix, ANALYZE at any position in the value counts as positive evidence."""
    hit = _fake_exp(kind="planbench", payload={
        "variants": {"variants": [
            {"name": "baseline", "ddl": "", "queries": [{"median_ms": 500.0}]},
            {"name": "fix_analyze", "ddl": "ANALYZE events",
             "queries": [{"median_ms": 82.0}]}]}}, exp_id="exp-ddl")
    assert bm._ledgered_analyze([hit]) == ["exp-ddl"]


def test_drift_analyzed_no_false_positive_on_variant_name():
    """A variant named analyze whose ddl creates an index (new ledger carries a ddl field) → not ANALYZE evidence;
    an old ledger (no ddl field) falls back to full-text matching → the variant name is accepted (compatibility clause)."""
    task = bm.get_task("stats-drift")
    run = _run("stats-drift")
    liar = _fake_exp(kind="planbench", payload={
        "variants": {"variants": [
            {"name": "baseline", "ddl": "", "queries": [{"median_ms": 500.0}]},
            {"name": "analyze", "ddl": "CREATE INDEX ON events(kind, payload)",
             "queries": [{"median_ms": 90.0}]}]}}, exp_id="exp-liar")
    checks = bm.grade_run(task, run, [liar], db_state="gone", report_text="pg_stats cited")
    assert next(c for c in checks if c.name == "analyzed").verdict == "FAIL"
    legacy = _fake_exp(kind="planbench", payload={
        "variants": {"variants": [
            {"name": "baseline", "queries": [{"median_ms": 500.0}]},
            {"name": "analyze", "queries": [{"median_ms": 90.0}]}]}}, exp_id="exp-old")
    checks = bm.grade_run(task, run, [legacy], db_state="gone", report_text="pg_stats cited")
    assert next(c for c in checks if c.name == "analyzed").verdict == "PASS"
