"""diagnose — one-shot slow-query diagnosis (v0.6 design note D1, historical).

Why: measurement showed (agent-ab [exp-261113-095210-48291ce4]) that the CLI group was
slow mainly due to **round trips** (22 vs 11 commands; the 1.84× token gap is almost
entirely explained by round trips). The minimal information loop for diagnosis is:
baseline plan → root-cause clues → candidate estimation comparison → directly executable
verification recipe → ledger evidence — this command completes it **in one process**,
cutting the many intermediate round trips of instance list/schema/sql/hypo/planbench.

Honesty boundaries (same stance as hypo/advise):
  - Estimated metrics (EXPLAIN/hypo) are used only to **screen candidates and find
    clues**, not to endorse performance;
  - The exit for performance conclusions is the ledger evidence of the recipe commands
    (planbench/rebench); the recipe already has the instance/db/query file substituted in;
  - Only SELECT/WITH queries are accepted (--analyze really executes statements, so the
    narrowed read-only surface is the safety boundary);
  - The hypo part really creates indexes on the target database (rolled back right
    after, zero residue) — the table is briefly locked during index creation; run on a clone.
"""
from __future__ import annotations

import re
from typing import Optional

from . import advise as advise_mod
from . import pg
from . import planbench as planbench_mod
from .planbench import _walk

_MAX_QUERIES = 8                       # query-count cap for one-shot diagnosis (multi-query workloads go to advise)
_READ_ONLY_HEAD = re.compile(r"^\s*(select|with|table|values|explain)\b", re.I)
# Data-modifying CTEs, locked reads and utility statements: a statement can START
# with SELECT/WITH/EXPLAIN and still write — WITH d AS (DELETE FROM t RETURNING *)
# SELECT … passes the head regex but really deletes under --analyze (self-caught
# while hardening R1-D). Matching runs on literal-masked text (sqlsplit.mask_literals),
# so keywords inside 'string literals' are data, not SQL (R2-C) — while double-quoted
# identifier BODIES stay visible (round-3 F1: "pg_read_file"(…) is a call, not data).
# The update…set bridge stays deliberately UNBOUNDED (round-4 C-F1) instead of the
# old update\s+\S{1,63}\s+set (round-3 F2) and its later {0,64}? window: PG
# identifiers run to 63 chars per part and qualified names chain arbitrarily in
# raw text, so any finite window is just the next bypass (red-team pierced three
# read-only surfaces with a gap>64 schema-qualified target). Widening is safe:
# the head whitelist already proves the statement HEAD is read-only, and string
# and comment content is masked away, so any bare update…set pair left in the
# body is either a data-modifying CTE or a quoted-identifier collision (documented
# tradeoff) — both belong to the "refuse" side.
# Round-5 C5-F2 however: that unbounded lazy alternative is O(n²) on adversarial
# input — "update " repeated 140KB with no "set" made every regex start position
# lazily rescan the whole tail (measured 20.6s). The bridge therefore lives in its
# own _UPDATE_SMUGGLE behind write_smuggle_search's keyword gate; _WRITE_SMUGGLE
# keeps the bounded alternatives only (still referenced by older call sites —
# cli's two .search callers switch to write_smuggle_search in lockstep).
# Round-6 C6-F2: the substring gate ("set" in low) still opened on word TAILS —
# "update "×20000 + "settle"/" offset"/" upset" paid one bounded-by-update-count
# rescan per update start (measured 20.1s) because the gate substring existed
# while the \bset\b WORD never did. The gate is now exact: it opens iff an
# \bupdate\b word exists before an \bset\b WORD (first update start vs last set
# start, both found in O(n)), which is precisely the bridge's match condition.
# CREATE/ALTER/DROP/etc. are blocked here too: EXPLAIN wraps them only accidentally
# (EXPLAIN CREATE … is a syntax error today, but that safety net is not a contract).
_WRITE_SMUGGLE = re.compile(
    r"\b(delete\s+from|insert\s+into|merge\s+into|into\b|truncate\b"
    r"|for\s+(no\s+key\s+)?(update|share)\b|for\s+key\s+share\b"
    r"|lock\s+table\b|lock\s+in\s+share\b|grant\b|revoke\b"
    r"|create\b|alter\b|drop\b|copy\b|vacuum\b|reindex\b|call\b|do\b)\b", re.I)
_UPDATE_SMUGGLE = re.compile(r"\bupdate\b[\s\S]*?\bset\b", re.I)
# gate probes run on the pre-CASEFOLDED text (round-7 C7-F4): re.I folds
# ſ (U+017F, Latin small letter long s) to plain s, so the historical combined
# pattern matched "update t ſet k=1" while lower() kept the ſ — the gate closed
# and write_smuggle_search returned None, breaking the full-equivalence claim.
# casefold() applies the same full-folding classes re.I uses (ſ→s). casefold
# may EXPAND characters ('İ' → 'i̇', 'ß' → 'ss'): the su/ss coordinates are only
# compared with each other (ss > su), never used as anchors into the raw text,
# so the length shift is harmless — and expansion can only open the gate MORE
# (a fold-fabricated "set" word makes the re.I bridge run and simply fail to
# match: the conservative direction), never miss a real re.I pair (every
# character re.I folds onto 's' — s/S/ſ — casefolds to exactly 's', and folding
# never turns a non-word char into a word char, so raw-text word boundaries
# survive into the folded text).
_UPDATE_WORD = re.compile(r"\bupdate\b")
_SET_WORD = "set"


def _is_word_char(ch: str) -> bool:
    r"""Regex \w semantics for the rfind boundary check: alphanumeric (str.isalnum
    covers the same Unicode classes the re module's \w uses) or underscore."""
    return ch == "_" or ch.isalnum()


def _last_word_start(low: str, word: str) -> Optional[int]:
    """Start of the LAST \\bword\\b occurrence in the casefolded text, or None.

    Reverse rfind walk: rfind returns the rightmost substring candidate, the
    word-boundary check rejects tails like the "set" inside settle/offset/
    upset, and the next search end moves down to the candidate start. The
    scanned windows tile the text without overlap, so the whole walk stays
    O(n) even when every candidate fails the boundary check (e.g. "settle"
    repeated 140KB → one pass, no regex rescan)."""
    end = len(low)
    while end:
        p = low.rfind(word, 0, end)
        if p == -1:
            return None
        before = low[p - 1] if p else ""
        after = low[p + len(word)] if p + len(word) < len(low) else ""
        if (not before or not _is_word_char(before)) and \
                (not after or not _is_word_char(after)):
            return p
        end = p
    return None


def write_smuggle_search(masked: str) -> Optional[re.Match]:
    """Bounded search: the unbounded update…set alternative runs only when an
    \\bupdate\\b WORD is followed somewhere by a \\bset\\b WORD; the rest of
    _WRITE_SMUGGLE (bounded alternatives) always runs.

    Full semantic equivalent of the historical combined _WRITE_SMUGGLE.search
    (the variant that still carried the update branch): the bounded pattern is
    always tried, the update bridge only under the gate, and the EARLIER of the
    two candidates wins — distinct keywords cannot tie at one start position,
    so leftmost-match semantics are preserved exactly.

    The gate (round-6 C6-F2) is EXACT, not substring-necessary: first update
    word start su (one regex search) vs last set word start ss (reverse rfind
    walk); a set word exists after an update word iff su and ss both exist and
    ss > su — which is precisely the bridge's match condition, so the O(n²)
    lazy rescan starts exactly when it will succeed (first start position wins
    in one O(n) pass) and never otherwise ("update "×20000 + "settle" /
    " offset" / " upset" → None in milliseconds instead of 20.1s: the word
    never materializes, so the bridge never runs).

    Round-7 C7-F4: the gate probes CASEFOLD, not lower — re.I folds ſ (U+017F)
    onto s, so "update t ſet k=1" matched the historical combined pattern while
    the lower()-based gate stayed closed (returned None). casefold keeps the
    gate exactly as fold-aware as the re.I bridge; expansion side effects
    ('İ' → 'i̇', 'ß' → 'ss') can only open the gate more (bridge then simply
    fails to match), never less — see the _UPDATE_WORD comment."""
    m = _WRITE_SMUGGLE.search(masked)
    low = masked.casefold()          # not lower(): re.I folds ſ→s, lower() does not (C7-F4)
    su = _UPDATE_WORD.search(low)
    if su is None:
        return m
    ss = _last_word_start(low, _SET_WORD)
    if ss is None or ss <= su.start():
        return m
    u = _UPDATE_SMUGGLE.search(masked)
    if u is not None and (m is None or u.start() < m.start()):
        return u
    return m
# row-count misestimate worth reporting (8×) and the minimum actual rows (small bases are noisy, not reported)
_MIS_FLOOR_ROWS, _MIS_RATIO = 50, 8.0


def _validate(queries: list[str]) -> None:
    if not queries:
        raise ValueError("至少一条查询(位置参数 SQL 或 --file)")
    if len(queries) > _MAX_QUERIES:
        raise ValueError(f"一次最多 {_MAX_QUERIES} 条(多查询负载场景走 advise --from-statements)")
    from .sqlsplit import find_side_effect_calls, mask_literals
    for q in queries:
        if not _READ_ONLY_HEAD.match(q):
            raise ValueError(f"diagnose 只吃 SELECT/WITH 查询(安全边界:--analyze 会真执行):{q[:60]!r}")
        masked = mask_literals(q)
        m = write_smuggle_search(masked)
        if m:
            raise ValueError(
                f"diagnose 只吃只读查询:检测到疑似写/锁定关键字 {m.group(0)!r}"
                f"(数据修改 CTE 与 FOR UPDATE 也会真改/真锁——写需求请走 lab 克隆库):{q[:60]!r}")
        calls = find_side_effect_calls(q, surface="analyze")
        if calls:
            raise ValueError(
                f"diagnose 拒绝副作用函数:{', '.join(calls)}"
                f"(EXPLAIN ANALYZE 会真执行,函数副作用不可回滚):{q[:60]!r}")


def plan_facts(plan: dict, *, analyze: bool) -> dict:
    """Plan tree → structured facts (seq scans / sorts / JOINs / row misestimates; facts only, no conclusions)."""
    seq: list[dict] = []
    sorts: list[dict] = []
    joins: list[str] = []
    materialize = 0
    mis: list[dict] = []
    for _, node in _walk(plan):
        nt = node.get("Node Type", "")
        if "Seq Scan" in nt:
            seq.append({"rel": node.get("Relation Name") or "?",
                        "est_rows": node.get("Plan Rows"),
                        "actual_rows": node.get("Actual Rows") if analyze else None,
                        "removed": node.get("Rows Removed by Filter") if analyze else None})
        elif nt == "Sort":
            sorts.append({"est_rows": node.get("Plan Rows"),
                          "method": node.get("Sort Method"),
                          "disk": node.get("Sort Space Used") if node.get("Sort Space Type") == "Disk" else None})
        elif "Join" in nt:
            joins.append(nt)
        elif nt == "Materialize":
            materialize += 1
        if analyze and node.get("Actual Rows") is not None and node.get("Plan Rows"):
            r = node["Actual Rows"] / node["Plan Rows"]
            if node["Actual Rows"] >= _MIS_FLOOR_ROWS and (r >= _MIS_RATIO or r <= 1 / _MIS_RATIO):
                mis.append({"node": nt, "est": node["Plan Rows"],
                            "actual": node["Actual Rows"], "ratio": round(r, 1)})
    return {"top": plan.get("Node Type"), "cost": plan.get("Total Cost"),
            "est_rows": plan.get("Plan Rows"),
            "actual_ms": (plan.get("Actual Total Time") if analyze else None),
            "seq": seq, "sorts": sorts, "joins": joins,
            "materialize": materialize, "misestimates": mis}


def _covered_cols(indexdefs: list[str]) -> set[str]:
    """Bare column names covered by existing index definitions (word-boundary match; heuristic, clue-level)."""
    cols: set[str] = set()
    for d in indexdefs:
        cols.update(re.findall(r"[A-Za-z_]\w*", d.split("(", 1)[-1]))
    return cols


def clues(fact: dict, index_by_rel: dict[str, list[str]],
          cand_cols_by_rel: dict[str, list[tuple]]) -> list[str]:
    """Facts → root-cause clues (conservative wording: state only visible facts and directions, no performance claims)."""
    out: list[str] = []
    for s in fact["seq"]:
        rel, idxs = s["rel"], index_by_rel.get(s["rel"], [])
        est = f"(估算输出 {s['est_rows']:,} 行)" if s["est_rows"] is not None else ""
        out.append(f"表 {rel} 全表扫描{est};现有索引: "
                   + ("; ".join(_short_idx(d) for d in idxs) if idxs else "无"))
        want = [c for cols in cand_cols_by_rel.get(rel, []) for c in cols]
        uncovered = [c for c in dict.fromkeys(want)
                     if not any(re.search(rf"\b{re.escape(c)}\b", d) for d in idxs)]
        if uncovered:
            out.append(f"线索: 谓词列 {','.join(uncovered)} 不被任何现有索引覆盖(索引方向)")
        elif idxs:
            out.append("线索: 谓词列已有相关索引但规划器仍全表扫描(选择性/统计口径值得核对)")
        if s.get("removed"):
            out.append(f"线索: 过滤淘汰 {s['removed']:,} 行(扫描宽、输出窄——索引收益方向)")
    for so in fact["sorts"]:
        if so.get("disk"):
            out.append(f"线索: 排序落盘 {so['disk']:,}KB(Sort Method={so['method']})——work_mem 方向")
    for m in fact["misestimates"][:2]:
        out.append(f"线索: {m['node']} 行数估算偏差大 est={m['est']:,} vs actual={m['actual']:,}"
                   f"(×{m['ratio']})——ANALYZE/统计口径")
    if fact["joins"]:
        out.append("JOIN: " + " + ".join(dict.fromkeys(fact["joins"])))
    return out[:6]


def _short_idx(definition: str) -> str:
    """pg_indexes.indexdef → short form 'name btree(aid)'."""
    m = re.match(r'CREATE (?:UNIQUE )?INDEX\s+(\S+)\s+ON\s+\S+\s+USING\s+(.*)$', definition or "")
    return f"{m.group(1)} {m.group(2)}" if m else (definition or "")[:60]


def _existing_indexes(dsn: str, rels: list[str]) -> dict[str, list[str]]:
    """Tables hit by seq scans → existing index definitions (read-only; reltuples fetched along the way as the row-count magnitude)."""
    uniq = sorted(set(rels))
    if not uniq:
        return {}
    conn = pg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT i.tablename, i.indexdef, c.reltuples::bigint FROM pg_indexes i "
                "JOIN pg_class c ON c.relname = i.tablename "
                "WHERE i.tablename = ANY(%s)", (uniq,))
            out: dict[str, list[str]] = {r: [] for r in uniq}
            for tab, idxdef, _tuples in cur.fetchall():
                out.setdefault(tab, []).append(idxdef)
            return out
    finally:
        conn.close()


def weighted_gain(qevals, weights: list[float]) -> float:
    """hypo single-candidate query-evaluation sequence → weighted average estimated
    reduction Σ w·(1-ratio) / Σ w (same numerator as advise's benefit, normalized to a
    0-1 mean for comparison across query counts / weight magnitudes; the stopping-line
    criterion of the --verify greedy). Queries with a missing ratio still count their
    weight in the denominator (conservative: failed evaluations don't get free gains)."""
    n = len(qevals)
    if not n:
        return 0.0
    w = weights or [1.0] * n
    denom = sum(w)
    if denom <= 0:
        denom = n
    total = 0.0
    for wi, qe in zip(w, qevals):
        if qe.ratio is not None:
            total += wi * (1.0 - qe.ratio)
    return total / denom


def weighted_ms(ms_list: list[float | None], weights: list[float]) -> float:
    """Measured per-query times (ms, None when missing) → weighted mean Σ w·ms / Σ w (the
    comparison quantity of the --verify measured re-check). Missing queries count 0 in the
    numerator while their weights still count in the denominator (conservative: queries
    that cannot be measured don't inflate the reduction)."""
    w = weights or [1.0] * len(ms_list)
    denom = sum(w)
    if denom <= 0:
        return 0.0
    total = 0.0
    for wi, ms in zip(w, ms_list):
        if ms is not None:
            total += wi * ms
    return total / denom


def run(dsn: str, queries: list[str], *, analyze: bool = False,
        weights: list[float] | None = None,
        instance_name: str | None = None, db: str | None = None,
        qfile: str | None = None) -> dict:
    """One-shot diagnosis: baseline plan facts → clues → advise/hypo candidate estimation
    ranking → recipe with real names substituted.

    The query-file path in the recipe is written by the caller (CLI) beforehand and passed
    in (qfile); this function only assembles command lines, avoiding module side effects.
    advise.evaluate internally includes the hypo transaction-rollback evaluation (zero
    residue). weights maps one-to-one to queries (the pgss calls metric is recommended) and
    affects the benefit ranking; default is all 1s.
    """
    _validate(queries)

    facts = []
    for q in queries:
        try:
            plan = planbench_mod.explain(dsn, q, analyze=analyze, buffers=analyze,
                                         settings=False, wal=analyze)
        except RuntimeError as e:
            raise RuntimeError(f"基线 EXPLAIN 失败: {e}") from None
        facts.append(plan_facts(plan, analyze=analyze))

    index_by_rel = _existing_indexes(dsn, [s["rel"] for f in facts for s in f["seq"]])

    adv = advise_mod.evaluate(dsn, queries, weights=weights, analyze=analyze)
    cand_cols: dict[str, list[tuple]] = {}
    for c in adv["candidates"]:
        cand_cols.setdefault(c.table, []).append(c.cols)

    per_query = []
    for q, f in zip(queries, facts):
        per_query.append({"query": q, "facts": f,
                          "clues": clues(f, index_by_rel, cand_cols)})

    ranking = adv["ranking"]
    best = next((r for r in ranking if r["benefit"] > 0 and not r["error"]), None)

    recipe: list[str] = []
    if best and qfile and (instance_name or db):
        tgt = [p for p in (f"--instance {instance_name}" if instance_name else "",
                           f"--db {db}" if db else "") if p]
        recipe = [
            f'pgtuner planbench {qfile} {" ".join(tgt)} '
            f'--variant idx1="{best["ddl"]}" --label "索引实验"',
            f'pgtuner rebench {" ".join(tgt)} --queries-file {qfile} --runs 5',
        ]
    elif adv["placeholders"]:
        recipe = ["查询带 $n 占位:先落地为具体参数,再跑 diagnose / planbench 验证"]
    elif not ranking:
        recipe = ["无简单谓词候选(启发式召回为空):核对函数表达式/OR 分支,或手写 --index 走 hypo"]

    return {"queries": queries, "analyze": analyze, "per_query": per_query,
            "index_by_rel": {k: [_short_idx(d) for d in v] for k, v in index_by_rel.items()},
            "placeholders": adv["placeholders"], "ranking": ranking,
            "best": best["ddl"] if best else None,
            "mechanism": (adv.get("eval") or {}).get("mechanism"),
            "warning": (adv.get("eval") or {}).get("warning"),
            "recipe": recipe}


# ---------------------------- Rendering ----------------------------

def render_text(result: dict) -> str:
    """Compact plain text (target <=1.5KB): one mechanism line + per-query facts/clues + ranking + recipe."""
    from .render import align_table, oneline

    lines = ["机制: EXPLAIN 基线 + hypo 事务回滚评估(零残留);估算口径只筛候选,不背书性能"
             + (";--analyze 已执行语句" if result["analyze"] else "")]
    for i, pq in enumerate(result["per_query"], 1):
        f = pq["facts"]
        head = f"Q{i}: {oneline(pq['query'], 64)}"
        if f["cost"] is not None:
            base = f"  基线: {f['top']} cost={f['cost']:.1f} est_rows={f['est_rows']}"
        else:
            base = f"  基线: {f['top']}"
        if f.get("actual_ms") is not None:
            base += f" actual={f['actual_ms']:.1f}ms"
        lines += [head, base]
        lines.extend(f"  {c}" for c in pq["clues"])
    if result["ranking"]:
        rows = [[f"×{r['avg_ratio']:.3f}" if r["avg_ratio"] is not None else "-",
                 f"{r['benefit']:+.2f}", r["ddl"][:64]] for r in result["ranking"][:5]]
        lines.append("候选排名(EXPLAIN 估算,benefit=Σ w·(1-ratio);TPS 结论需实测):")
        lines.append(align_table(["ratio", "benefit", "候选DDL"], rows))
    elif result["placeholders"]:
        lines.append("查询带 $n 占位:只出线索,未做 EXPLAIN 候选评估")
    else:
        lines.append("无候选(启发式召回为空)")
    if result["best"]:
        lines.append(f"建议: 估算最优候选 = {result['best']}(估算≠实测,验证走处方)")
    if result["recipe"]:
        lines.append("验证(命令已代入实例/库/查询文件,直接执行):")
        lines.extend("  " + ln for ln in result["recipe"])
    if result.get("warning"):
        lines.append(f"⚠ {result['warning']}")
    return "\n".join(lines)


def render_json(result: dict) -> str:
    import json
    return json.dumps(result, ensure_ascii=False, default=lambda o: o.__dict__)
