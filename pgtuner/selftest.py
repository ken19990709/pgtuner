"""selftest: acceptance gate collection (the executable form of the historical
acceptance checklist, layer 2 / layer 1).

The fast part: pure software assertions, no PG needed (imports / ledger roundtrip /
guard rules / plan diff / rendering).
The with-db part: temporarily creates a release instance (PG comes from
release_bindir(): env var PGTUNER_RELEASE_BINDIR > the first release instance's bindir
in the registry) — clone-pool stress loop, watch snapshot, rebench no-trace restore
end to end. The instance is destroyed automatically when done, zero residue.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from . import hygiene as hygiene_mod
from . import instance as instance_mod
from . import ledger as ledger_mod
from . import pg as pg_mod
from . import planbench as planbench_mod
from . import restartbench as rebench_mod
from . import watch as watch_mod

RELEASE_BINDIR = None    # no machine-hardcoded default since v0.9.0; fallback chain in release_bindir()


def release_bindir() -> str:
    """Fallback chain for the release install directory (machine binding removed in v0.9.0):
    env var PGTUNER_RELEASE_BINDIR > the bindir of the first release instance in the
    registry > error if neither exists (first build and install with scripts/setup.sh --pg
    then instance create/register, or set the env var)."""
    env = os.environ.get("PGTUNER_RELEASE_BINDIR")
    if env:
        return env
    for inst in instance_mod.list_instances():
        if inst.build_type == "release":
            return inst.bindir
    raise RuntimeError(
        "找不到 release 安装:设 PGTUNER_RELEASE_BINDIR,或注册/创建一个 release 实例"
        "(pgtuner instance list 查看;编译安装可跑 bash scripts/setup.sh --pg <prefix>)")


# ---------------------------- fast gates (no PG) ----------------------------

def _g_imports() -> None:
    from . import instance, ledger, planbench, restartbench, watch  # noqa: F401


def _fixture_plans() -> tuple[dict, dict]:
    """Inner plan nodes (same shape as [0]["Plan"] returned by planbench.explain())."""
    a = {"Node Type": "Seq Scan", "Relation Name": "b2_t", "Plan Rows": 10000,
         "Actual Rows": 10000, "Actual Loops": 1, "Actual Total Time": 12.0}
    b = {"Node Type": "Index Only Scan", "Relation Name": "b2_t", "Index Name": "b2_t_pkey",
         "Plan Rows": 10000, "Actual Rows": 9998, "Actual Loops": 1, "Actual Total Time": 3.0}
    return a, b


def _g_planbench() -> None:
    a, b = _fixture_plans()
    diffs = planbench_mod.diff_plans(a, b)
    changes = {d.change for d in diffs}
    assert "removed" in changes and "added" in changes, f"节点类型变化应判 added/removed: {changes}"
    acc = planbench_mod.accuracy_report(b)
    assert acc, "accuracy_report 不应为空"
    txt = planbench_mod.render_diff(diffs)
    assert 0 < len(txt.splitlines()) <= 40, "render_diff 输出异常"
    assert max(len(line) for line in planbench_mod.render_accuracy(acc).splitlines()) <= 100, "渲染超宽"


def _g_ledger_roundtrip(tmp: Path) -> None:
    lf = tmp / "l.jsonl"
    e = ledger_mod.new_experiment("bench", "roundtrip", payload={"metrics": {"a": 100.0}},
                                  instance={"build_type": "release"},
                                  fingerprint={"build_type": "release", "pg_major": 19},
                                  ledger_file=lf)
    ledger_mod.update(e.id, conclusion="测试结论", ledger_file=lf)
    got = ledger_mod.get(e.id[:12], ledger_file=lf)          # prefix match
    assert got.conclusion == "测试结论"
    assert len(ledger_mod.list_experiments(last=5, ledger_file=lf)) == 1


def _g_ledger_guard(tmp: Path) -> None:
    lf = tmp / "guard.jsonl"
    a = ledger_mod.new_experiment("bench", "rel", instance={"build_type": "release"},
                                  fingerprint={"build_type": "release", "pg_major": 19},
                                  payload={"metrics": {"tps": 100.0}}, ledger_file=lf)
    ledger_mod.update(a.id, noise_floor={"tps_cv_pct": 5.0}, ledger_file=lf)
    b = ledger_mod.new_experiment("bench", "dbg", instance={"build_type": "debug"},
                                  fingerprint={"build_type": "debug", "pg_major": 19},
                                  payload={"metrics": {"tps": 90.0}}, ledger_file=lf)
    d1 = ledger_mod.diff(a.id, b.id, ledger_file=lf)
    assert d1["compatible"] is False and d1["verdict"] == "incomparable", "debug×release 必须拒绝"
    c = ledger_mod.new_experiment("bench", "rel2", instance={"build_type": "release"},
                                  fingerprint={"build_type": "release", "pg_major": 19},
                                  payload={"metrics": {"tps": 101.5}}, ledger_file=lf)
    ledger_mod.update(c.id, noise_floor={"tps_cv_pct": 5.0}, ledger_file=lf)
    d2 = ledger_mod.diff(a.id, c.id, ledger_file=lf)
    assert d2["metrics"][0]["significant"] is False, "+1.5% 低于 5% 噪声底必须判不显著"


def _g_instance_pure() -> None:
    inst = instance_mod.Instance(name="x", bindir="/b", pg_version="19devel", build_type="release",
                                 data_dir="/d", port=1)
    assert instance_mod.Instance.from_dict(inst.to_dict()) == inst, "注册表序列化往返"
    c = rebench_mod.RestartCandidate("baseline", {})
    assert c.gucs == {}, "空 baseline 候选可构造"


def _g_hygiene() -> None:
    """Portability gate (v0.11.0): zero tolerance for hard-coded addresses / machine usernames / home-directory paths; see pgtuner/hygiene.py."""
    bad = hygiene_mod.scan()
    assert not bad, "可移植性违例: " + "; ".join(
        f"{v['file']}:{v['line']}({v['pattern']})" for v in bad[:5])


def run_fast() -> list[tuple[str, str, str]]:
    """Returns [(gate, PASS/FAIL, detail)]."""
    gates: list[tuple[str, str, str]] = []

    def gate(name: str, fn) -> None:
        try:
            fn()
            gates.append((name, "PASS", ""))
        except Exception as e:                                    # noqa: BLE001
            gates.append((name, "FAIL", f"{type(e).__name__}: {e}"[:200]))

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        gate("imports", _g_imports)
        gate("planbench-diff+render", _g_planbench)
        gate("ledger-roundtrip", lambda: _g_ledger_roundtrip(tmp))
        gate("ledger-guard(diff守门+显著性)", lambda: _g_ledger_guard(tmp))
        gate("instance-pure", _g_instance_pure)
        gate("portability-scan", _g_hygiene)
    return gates


# ---------------------------- with-db gates (requires a release install) ----------------------------

def _with_db_instance():
    name = f"st{os.getpid() % 10000}"
    # reuse or create the temporary instance (selftest may be invoked multiple times)
    try:
        return instance_mod.get(name), name, False
    except ValueError:
        return instance_mod.create(name, release_bindir()), name, True


def run_with_db(cycles: int = 100) -> list[tuple[str, str, str]]:
    gates: list[tuple[str, str, str]] = []

    def gate(name, fn):
        try:
            fn()
            gates.append((name, "PASS", ""))
        except Exception as e:                                    # noqa: BLE001
            gates.append((name, "FAIL", f"{type(e).__name__}: {e}"[:200]))

    inst, name, created = _with_db_instance()
    try:
        instance_mod.ensure_running(name)
        tmp = Path(tempfile.mkdtemp(prefix="pgtuner-selftest-"))

        def clone_pool() -> None:
            tpl_sql = tmp / "tpl.sql"
            tpl_sql.write_text("CREATE TABLE probe AS SELECT g, repeat('x', 64) FROM generate_series(1, 100000) g;")
            instance_mod.make_template(name, "st_tpl", tpl_sql)
            worst = 0.0
            for i in range(cycles):
                t0 = time.perf_counter()
                db = instance_mod.clone(name, "st_tpl")
                dt = time.perf_counter() - t0
                assert dt < 2.0, f"第 {i} 次克隆 {dt:.2f}s 超过 2s 门槛"
                instance_mod.drop_db(name, db)
                worst = max(worst, dt)
            residue = instance_mod.pool_stats().get(name, [])
            assert residue == [], f"克隆残留: {residue}"

        def watch_snap() -> None:
            txt = watch_mod.snapshot(instance_mod.dsn_of(name, "postgres"), frames=2, interval=0.3)
            assert "实例" in txt and len(txt.splitlines()) >= 6, "watch 快照渲染异常"

        def rebench_restore() -> None:
            # Minimal slow-channel end-to-end: touch one postmaster parameter → the restore
            # must match exactly.
            # B1 acceptance lesson: must assert the candidate actually succeeded — before
            # this, the gate was useless against "ALTER SYSTEM keeps failing".
            cands = [rebench_mod.RestartCandidate("sb128", {"shared_buffers": "128MB"})]
            res = rebench_mod.run_restart_bench(
                name, cands, dbname="postgres", workload="custom",
                custom_queries=["SELECT 1"], duration=1, warmup=0)
            assert len(res) == 2, "应自动补 baseline 候选"
            errs = [(r.candidate.name, r.error) for r in res if r.error is not None]
            assert not errs, f"存在失败周期(B1 曾漏检): {errs}"
            assert all(r.tps is not None for r in res), "测量值缺失"
            # a successful module return means the restore passed (failures raise); re-check independently once more
            dsn = instance_mod.dsn_of(name, "postgres")
            conn = pg_mod.connect_rw(dsn)
            try:
                cur = {r[0]: r[1] for r in pg_mod.fetch_all(conn, "SELECT name, setting FROM pg_settings")}
            finally:
                conn.close()
            assert cur.get("shared_buffers") is not None, "还原后无法读取 shared_buffers"

        def sql_schema() -> None:
            # sql read-only entry (query / write rejection / truncation / timeout) +
            # schema introspection (structural assertions) + DDL rebuild roundtrip.
            # All done on a one-shot cloned database, returned when done. Added in v0.4
            # (a permanent gate for the sql/schema commands).
            from . import introspect, sqlcmd
            tpl_sql = tmp / "sql_tpl.sql"
            tpl_sql.write_text(
                "CREATE TABLE acc(id int PRIMARY KEY, v int NOT NULL DEFAULT 0 CHECK (v >= 0), note text);\n"
                "CREATE INDEX acc_v_idx ON acc(v);\n"
                "CREATE FUNCTION acc_log() RETURNS trigger AS $f$ BEGIN RETURN NEW; END; $f$ LANGUAGE plpgsql;\n"
                "CREATE TRIGGER trg_acc BEFORE INSERT ON acc FOR EACH ROW EXECUTE FUNCTION acc_log();\n"
                "INSERT INTO acc SELECT g, g % 100, 'n' FROM generate_series(1, 1000) g;\n"
                "ANALYZE;", encoding="utf-8")
            instance_mod.make_template(name, "st_sql_tpl", tpl_sql)
            db = instance_mod.clone(name, "st_sql_tpl")
            dsn = instance_mod.dsn_of(name, db)
            try:
                res = sqlcmd.run_query(dsn, "SELECT count(*) FROM acc", max_rows=10)
                assert res.rows and res.rows[0][0] == 1000, f"计数不符: {res.rows}"
                try:
                    sqlcmd.run_query(dsn, "INSERT INTO acc VALUES (-1, 0, 'x')")
                    raise AssertionError("只读会话竟然放行了 INSERT")
                except RuntimeError as e:
                    assert "只读会话拒绝写操作" in str(e), f"写拒绝语义不对: {e}"
                res = sqlcmd.run_query(dsn, "SELECT g FROM generate_series(1, 50) g", max_rows=10)
                assert len(res.rows) == 10 and res.truncated, "截断探测失效"
                try:
                    sqlcmd.run_query(dsn, "SELECT pg_sleep(5)", timeout_s=0.5)
                    raise AssertionError("超时查询竟然放行")
                except RuntimeError as e:
                    assert "超时" in str(e), f"超时语义不对: {e}"

                conn = pg_mod.connect(dsn)
                try:
                    desc = introspect.describe_table(conn, "acc")
                finally:
                    conn.close()
                assert [c.name for c in desc.columns] == ["id", "v", "note"], "列不符"
                # PG18+/19 also lands column NOT NULL as a catalog constraint
                # (contype='n', rebuilt with column attributes); the structural assertion
                # counts only non-'n' constraints; 'n' entries are naturally restored via
                # column attributes, and the roundtrip side filters the same way
                structural = [c for c in desc.constraints if c.kind != "n"]
                assert len(desc.indexes) == 2 and len(structural) == 2 \
                    and len(desc.triggers) == 1, \
                    f"结构不符: idx={len(desc.indexes)} con={len(structural)} trg={len(desc.triggers)}"
                assert desc.row_est and 900 < desc.row_est < 1100, f"估算行数异常: {desc.row_est}"
                ddl = introspect.synthesize_ddl(desc)
                # correct roundtrip procedure: constraint/index names are database-wide
                # unique — capture the original structure snapshot first, DROP the original
                # table to free the names, then run the rebuild DDL and compare against the
                # snapshot (coexisting with the original objects would collide on names,
                # as warned in the DDL header)
                rw = pg_mod.connect_rw(dsn)
                try:
                    pg_mod.execute(rw, "DROP TABLE acc CASCADE")
                    pg_mod.execute(rw, ddl)          # the header is a SQL comment; execute the whole block
                finally:
                    rw.close()
                conn = pg_mod.connect(dsn)
                try:
                    new = introspect.describe_table(conn, "acc")
                finally:
                    conn.close()
                assert [(c.name, c.type) for c in new.columns] == \
                    [(c.name, c.type) for c in desc.columns], "roundtrip 列不一致"
                assert sorted(i.definition for i in new.indexes) == \
                    sorted(i.definition for i in desc.indexes), "roundtrip 索引不一致"
                assert sorted((c.kind, c.definition) for c in new.constraints if c.kind != "n") == \
                    sorted((c.kind, c.definition) for c in desc.constraints if c.kind != "n"), \
                    "roundtrip 约束不一致"
                assert sorted(t.definition for t in new.triggers) == \
                    sorted(t.definition for t in desc.triggers), "roundtrip 触发器不一致"
            finally:
                instance_mod.drop_db(name, db)

        def statements_hypo_advise() -> None:
            # permanent gate for v0.5 entries: statements (one-shot pgss enable +
            # workload queryable) / hypo (transaction-rollback mechanism: estimation
            # evaluation + zero-residue rollback) / advise (candidate extraction +
            # ranking + verification recipe)
            from . import advise as advise_mod
            from . import hypo as hypo_mod
            from . import statements as st_mod

            rep = st_mod.setup(name, "postgres")          # preload + restart + create extension (idempotent when already ready)
            dsn0 = instance_mod.dsn_of(name, "postgres")
            conn = pg_mod.connect_rw(dsn0)
            try:
                pg_mod.execute(conn, "SELECT count(*) FROM pg_class")   # generate a bit of load
                stats = st_mod.top_queries(conn, sort="total", limit=10, min_calls=1)
            finally:
                conn.close()
            assert any("pg_class" in s.query for s in stats), "pgss 未记录到负载"

            adv_sql = tmp / "adv_tpl.sql"
            adv_sql.write_text(
                "CREATE TABLE adv_t(a int, b int);\n"
                "INSERT INTO adv_t SELECT g, g % 50 FROM generate_series(1, 50000) g;\n"
                "ANALYZE adv_t;\n", encoding="utf-8")
            instance_mod.make_template(name, "st_adv_tpl", adv_sql)
            db = instance_mod.clone(name, "st_adv_tpl")
            dsn = instance_mod.dsn_of(name, db)
            try:
                q = "SELECT count(*) FROM adv_t WHERE a = 12345"
                hrep = hypo_mod.eval_candidates(dsn, ["CREATE INDEX ON adv_t(a)"], [q])
                assert hrep["mechanism"] == "rollback", f"机制应为事务回滚: {hrep['mechanism']}"
                assert hrep["index_count_before"] == hrep["index_count_after"], "残留自检失败"
                qe = hrep["sets"][0].queries[0]
                assert qe.base_cost and qe.ratio is not None and qe.ratio < 0.5, \
                    f"假设索引应显著降估算代价: ratio={qe.ratio}"
                assert qe.hypo_index and "adv_t_a" in qe.hypo_index, \
                    f"计划应真正用上假设索引: {qe.hypo_index}"
                conn2 = pg_mod.connect(dsn)               # independent zero-residue re-check (don't trust the self-check's single view)
                try:
                    n_left = pg_mod.fetch_one(
                        conn2, "SELECT count(*) FROM pg_indexes WHERE tablename = 'adv_t'")[0]
                finally:
                    conn2.close()
                assert n_left == 0, f"回滚后残留 {n_left} 个索引"

                res = advise_mod.evaluate(dsn, [q])
                cands = {(c.table, c.cols) for c in res["candidates"]}
                assert ("adv_t", ("a",)) in cands, f"候选应含无索引等值列: {cands}"
                assert res["ranking"] and res["ranking"][0]["benefit"] > 0, "排名收益为正"
                assert res["recipe"] and any("planbench" in ln for ln in res["recipe"]), \
                    "advise 应给可复制的验证处方"

                # v0.6 diagnose: one-shot closed loop (facts / clues / ranking / recipe
                # with real names substituted) + narrowed verification surface
                from . import diagnose as diag_mod
                diag = diag_mod.run(dsn, [q], instance_name=name, db=db,
                                    qfile=str(tmp / "diag_q.sql"))
                assert diag["best"] == "CREATE INDEX ON adv_t (a)", f"最优候选不符: {diag['best']}"
                pq0 = diag["per_query"][0]
                assert pq0["facts"]["seq"] and pq0["facts"]["seq"][0]["rel"] == "adv_t", \
                    "基线事实应含全表扫描"
                assert any("不被任何现有索引覆盖" in c for c in pq0["clues"]), \
                    f"线索应指向无索引覆盖: {pq0['clues']}"
                assert diag["recipe"] and all(
                    f"--instance {name}" in ln and f"--db {db}" in ln
                    for ln in diag["recipe"][:2]), f"处方应代入实名: {diag['recipe']}"
                n_left2 = pg_mod.fetch_one(pg_mod.connect(dsn),
                                           "SELECT count(*) FROM pg_indexes WHERE tablename = 'adv_t'")[0]
                assert n_left2 == 0, f"diagnose 后残留 {n_left2} 个索引"
                for bad in ("DELETE FROM adv_t", "UPDATE adv_t SET a=1"):
                    try:
                        diag_mod.run(dsn, [bad])
                        raise AssertionError(f"diagnose 应拒绝写语句: {bad}")
                    except ValueError:
                        pass
            finally:
                instance_mod.drop_db(name, db)

        gate(f"clone-pool-{cycles}次(单次<2s+零残留)", clone_pool)
        gate("watch-snapshot", watch_snap)
        gate("rebench-无痕还原端到端", rebench_restore)
        gate("sql-只读强制+截断+超时 & schema-内省+DDL-roundtrip", sql_schema)
        gate("statements-pgss启用+计数 & hypo-假设索引+零残留 & advise-候选+处方"
             " & diagnose-一键诊断+实名处方+拒写语句",
             statements_hypo_advise)
    finally:
        try:
            instance_mod.destroy(name)          # clean up template databases / clones along with it
        except Exception:                       # noqa: BLE001
            pass
    return gates


def run(*, with_db: bool = False, cycles: int = 100) -> list[tuple[str, str, str]]:
    gates = run_fast()
    if with_db:
        gates += run_with_db(cycles)
    return gates
