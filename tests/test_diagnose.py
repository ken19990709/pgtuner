"""diagnose regression: plan fact extraction / clue generation (pure functions) + run assembly flow (fake injection) + validation.

Same fake approach as test_hypo: monkeypatch diagnose.planbench_mod.explain,
diagnose.pg.connect, diagnose.advise_mod.evaluate; no real PG is touched.
"""
import json

import pytest

from pgtuner import advise as A
from pgtuner import diagnose as D
from pgtuner import hypo as H


# ---------------------------- validation ----------------------------

def test_validate_rejects_writes_and_empty():
    with pytest.raises(ValueError, match="至少一条"):
        D._validate([])
    with pytest.raises(ValueError, match="SELECT/WITH"):
        D._validate(["DELETE FROM t"])
    with pytest.raises(ValueError, match="最多"):
        D._validate([f"SELECT {i}" for i in range(9)])
    D._validate(["SELECT 1", "WITH x AS (SELECT 1) SELECT * FROM x"])   # does not raise


# ---------------------------- plan_facts ----------------------------

def _plan_tree():
    return {"Node Type": "Sort", "Total Cost": 5327.4, "Plan Rows": 200,
            "Actual Rows": 200, "Actual Total Time": 411.2,
            "Plans": [{"Node Type": "Seq Scan", "Relation Name": "accounts",
                       "Plan Rows": 1, "Actual Rows": 800,          # 800× deviation
                       "Rows Removed by Filter": 199200,
                       "Plans": []}]}


def test_plan_facts_seq_sort_misestimate():
    f = D.plan_facts(_plan_tree(), analyze=True)
    assert f["top"] == "Sort" and f["cost"] == 5327.4
    assert f["seq"][0]["rel"] == "accounts"
    assert f["seq"][0]["removed"] == 199200
    assert f["sorts"] and f["sorts"][0]["method"] is None      # no Sort Method key → None
    assert f["misestimates"] and f["misestimates"][0]["ratio"] == 800.0
    g = D.plan_facts(_plan_tree(), analyze=False)               # without analyze, actual values are not read
    assert g["seq"][0]["removed"] is None and not g["misestimates"] and g["actual_ms"] is None


def test_clues_coverage_and_direction():
    fact = {"top": "Seq Scan", "cost": 100.0, "est_rows": 200, "actual_ms": None,
            "seq": [{"rel": "accounts", "est_rows": 200, "actual_rows": None, "removed": None}],
            "sorts": [{"est_rows": 200, "method": "external merge", "disk": 4096}],
            "joins": [], "materialize": 0, "misestimates": []}
    idx = {"accounts": ['CREATE INDEX accounts_pkey ON public.accounts USING btree (aid)']}
    cands = {"accounts": [("bid", "abalance"), ("bid",)]}
    out = D.clues(fact, idx, cands)
    assert any("全表扫描" in c and "accounts_pkey" in c for c in out)
    assert any("bid,abalance" in c and "不被任何现有索引覆盖" in c for c in out)
    assert any("排序落盘" in c and "4,096" in c and "work_mem" in c for c in out)
    # columns already covered → switch to the statistics-criterion clue
    idx2 = {"accounts": ["CREATE INDEX i1 ON public.accounts USING btree (bid, abalance)"]}
    out2 = D.clues(fact, idx2, cands)
    assert any("已有相关索引但规划器仍全表扫描" in c for c in out2)


# ---------------------------- run (fake injection) ----------------------------

def test_run_assembles_facts_ranking_recipe(monkeypatch):
    monkeypatch.setattr(D.planbench_mod, "explain",
                        lambda dsn, q, **k: _plan_tree())
    monkeypatch.setattr(D.pg, "connect",
                        lambda dsn: _fake_idx_conn({"accounts": ["CREATE INDEX accounts_pkey "
                                                                 "ON public.accounts USING btree (aid)"]}))
    monkeypatch.setattr(D.advise_mod, "evaluate",
                        lambda dsn, queries, weights=None, analyze=False: _fake_adv())
    out = D.run("dsn", ["SELECT * FROM accounts WHERE bid=42 AND abalance>5000"],
                instance_name="rel19", db="cl1", qfile="/tmp/q.sql")
    assert out["per_query"][0]["facts"]["seq"][0]["rel"] == "accounts"
    assert out["ranking"][0]["benefit"] == 5.0
    assert out["best"] == "CREATE INDEX ON accounts (bid, abalance)"
    assert any("--instance rel19" in r and "--db cl1" in r for r in out["recipe"])
    txt = D.render_text(out)
    assert "零残留" in txt and "×0.500" in txt and "验证" in txt
    doc = json.loads(D.render_json(out))
    assert doc["best"].startswith("CREATE INDEX")


def test_run_placeholder_recipe(monkeypatch):
    monkeypatch.setattr(D.planbench_mod, "explain", lambda dsn, q, **k: _plan_tree())
    monkeypatch.setattr(D.pg, "connect", lambda dsn: _fake_idx_conn({}))
    monkeypatch.setattr(D.advise_mod, "evaluate",
                        lambda dsn, queries, weights=None, analyze=False: {
                            "queries": queries, "weights": None, "placeholders": True,
                            "candidates": [], "eval": None, "ranking": [], "recipe": []})
    out = D.run("dsn", ["SELECT * FROM t WHERE k=$1"])
    assert out["placeholders"] and not out["best"]
    assert any("$n 占位" in r for r in out["recipe"])


def _fake_idx_conn(by_table):
    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.rows = [(t, d, 200000) for t, ds in by_table.items() for d in ds]

        def fetchall(self):
            return self.rows

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass
    return Conn()


def _fake_adv():
    sets = []
    for i, ddl in enumerate(["CREATE INDEX ON accounts (bid, abalance)",
                             "CREATE INDEX ON accounts (bid)"]):
        se = H.SetEval(label=f"idx{i + 1}", ddls=[ddl])
        qe = H.QueryEval(sql="q", base_cost=2000.0, hypo_cost=1000.0 if i == 0 else 2000.0,
                         ratio=0.5 if i == 0 else 1.0)
        se.queries.append(qe)
        sets.append(se)
    ev = {"mechanism": "rollback", "analyze": False, "sets": sets,
          "index_count_before": 1, "index_count_after": 1, "warning": None}
    ranking = [{"ddl": "CREATE INDEX ON accounts (bid, abalance)", "benefit": 5.0,
                "avg_ratio": 0.5, "evidence": ["Q1: bid= 等值"], "error": None},
               {"ddl": "CREATE INDEX ON accounts (bid)", "benefit": 0.0,
                "avg_ratio": 1.0, "evidence": [], "error": None}]
    return {"queries": ["q"], "weights": None, "placeholders": False,
            "candidates": [A.Candidate("accounts", ("bid", "abalance"), "等值+范围"),
                           A.Candidate("accounts", ("bid",), "等值")],
            "eval": ev, "ranking": ranking, "recipe": []}
