"""advise — index advising (transparent funnel style, v0.5 design note §3.3, historical).

What separates it from a black-box DTA: candidate generation is a **conservative heuristic**
(recall only; each candidate carries which predicates from which query it came from, for
agent/human review); the evaluation layer only gives **estimated-metric** conclusions
(hypo's EXPLAIN cost); the verification exits for the final conclusion are handed out
as-is (planbench/rebench command recipes) — advise itself does not endorse performance.

Flow:
  1. extract_candidates(queries): single-table WHERE equality/range columns, qualified
     named columns on both sides of JOIN ON, ORDER BY prefixes → single-column equality /
     (equality, range) / (equality, equality) / JOIN column / sort prefix / covering
     (predicate keys + INCLUDE of query-referenced columns; not generated for SELECT *) /
     partial (categorical string-literal equality column as WHERE, e.g. status='new';
     traced to S5 — two psql agents independently hand-built this index shape; numeric
     literals are instance values, not categories, so not done), deduplicated, capped at 12.
     Normalized text ($n placeholders) is also extractable (the predicate structure is there).
  2. evaluate: concrete queries → hypo.eval_candidates estimation matrix; when
     --from-statements pulls a real workload the pgss text carries $n placeholders and
     cannot be executed by EXPLAIN directly → only candidates + evidence + recipe are
     given, guiding the user to ground them into concrete queries before verifying
     (honest metric; no speculative placeholder substitution).
  3. ranking: sorted by weighted benefit Σ w_i·(1-ratio_i) (default weight 1; pgss calls
     can be passed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_CANDIDATES = 12

# Reserved words in alias position: the WHERE of "FROM t WHERE …" is not an alias
_KEYWORDS = {"where", "group", "order", "limit", "offset", "on", "using", "left",
             "right", "inner", "outer", "full", "cross", "natural", "join",
             "lateral", "set", "window", "having", "union", "except", "intersect",
             "returning", "as", "for", "with", "when", "then", "and", "or", "not"}

# Reserved words for the alias-position negative lookahead: derived from _KEYWORDS
# (a single word list, no double manual sync; minus "as" — the AS branch is captured
# separately — and when/then, which are value-position words), plus tablesample.
# The \b-terminated alternation is order-independent ("order" is not mis-matched by the
# prefix word "or"; on failure it backtracks).
_ALIAS_STOP = _KEYWORDS - {"as", "when", "then"} | {"tablesample"}
# Alias capture carries a reserved-word negative lookahead: otherwise the alias group
# would swallow the JOIN of "FROM a JOIN b" as a's alias, and finditer could then never
# match "join b" again (missed table — the two-table regression in tests/test_advise caught this)
_RE_TABLES = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_][\w.]*)\s*"
    r"(?:as\s+(?P<as_alias>[A-Za-z_]\w*)"
    rf"|(?P<alias>(?!(?:{'|'.join(sorted(_ALIAS_STOP, key=len, reverse=True))})\b)"
    rf"[A-Za-z_]\w*))?", re.I)
_RE_QUAL = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)")
_RE_EQ = re.compile(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*=\s*(?!=)([^=<>!\s(][^)\s;,]*)")
# No mandatory whitespace after the range operator: "abalance>5000" (no space / at line
# end, a real benchmark query shape) must also be recalled; the operators themselves are
# already ordered multi-char-first, so >= is never bitten into >
_RE_RANGE = re.compile(
    r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*(?:>=|<=|<>|!=|>|<)|"
    r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s+between\b", re.I)
_RE_IN = re.compile(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s+in\s*\(", re.I)
_RE_ORDER = re.compile(r"\border\s+by\s+((?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?\s*,?\s*)+)", re.I)
# Covering-index candidates: SELECT * cannot enumerate referenced columns, so nothing is
# generated; the bare-column scan excludes function names (followed by '(') and keywords
_RE_SELECT_STAR = re.compile(r"\bselect\s+\*", re.I)
_RE_BARE_COL = re.compile(r"\b([A-Za-z_]\w*)\b(?!\s*\()")
_RE_STR = re.compile(r"'(?:[^']|'')*'")
_NONCOL = _KEYWORDS | {"select", "from", "by", "desc", "asc", "is", "null", "like",
                       "ilike", "group", "distinct", "all", "case", "end", "else",
                       "interval", "cast", "exists", "any", "some"}


def _is_user_table(table: str) -> bool:
    """Generate candidates only for user tables: system objects under pg_*/information_schema never enter recommendations (the tool's own queries often show up in pg_stat_statements, and pg_catalog columns should not be treated as index targets)."""
    t = table.split(".")[-1].lower()
    return not t.startswith("pg_") and "information_schema" not in table.lower()


def _add_once(lst: list, v) -> None:
    """Append with dedup, keeping first-seen order (shared by equality/range/literal/referenced-column/evidence collection)."""
    if v not in lst:
        lst.append(v)


@dataclass
class Candidate:
    table: str
    cols: tuple[str, ...]
    kind: str                                    # kind literal value: one of the zh-CN kind strings (see add() call sites)
    evidence: list[str] = field(default_factory=list)
    include: tuple[str, ...] = ()                # INCLUDE columns of a covering index (query-referenced but not predicates)
    where: str = ""                              # partial index predicate (categorical string-literal equality)

    @property
    def ddl(self) -> str:
        base = f"CREATE INDEX ON {self.table} ({', '.join(self.cols)})"
        if self.include:
            base += f" INCLUDE ({', '.join(self.include)})"
        return base + (f" WHERE {self.where}" if self.where else "")


def _tables_and_aliases(query: str) -> tuple[list[str], dict[str, str]]:
    """(table name list, alias→table name). An identifier after FROM/JOIN is treated as a table; the identifier immediately following is treated as an alias unless it is a reserved word."""
    tables: list[str] = []
    alias: dict[str, str] = {}
    for m in _RE_TABLES.finditer(query):
        tbl = m.group(1)
        tables.append(tbl)
        alias.setdefault(tbl, tbl)               # the full name also works as a qualifier
        a = m.group("as_alias") or m.group("alias")
        if a and a.lower() not in _KEYWORDS:
            alias.setdefault(a, tbl)
    return tables, alias


def _resolve(col_ref: str, tables: list[str], alias: dict[str, str]) -> tuple[str, str] | None:
    """Qualified/bare column reference → (table name, column name). Bare columns are resolvable only in single-table queries (conservative)."""
    if "." in col_ref:
        qual, col = col_ref.rsplit(".", 1)
        tbl = alias.get(qual)
        return (tbl, col) if tbl else None
    return (tables[0], col_ref) if len(tables) == 1 else None


def extract_candidates(queries: list[str]) -> list[Candidate]:
    """Conservative heuristic candidate generation: only simple predicates are recognized
    (bare/qualified columns with direct = / range / IN / BETWEEN, qualified columns on both
    sides of JOIN ON, ORDER BY prefixes); function expressions and OR branches are not
    parsed (prefer missing over wrong). Returns deduplicated candidates (<=MAX_CANDIDATES);
    evidence records where each candidate came from."""
    by_key: dict[tuple[str, tuple[str, ...]], Candidate] = {}

    def add(table: str, cols: tuple[str, ...], kind: str, ev: str,
            include: tuple[str, ...] = (), where: str = "") -> None:
        if not _is_user_table(table):
            return
        key = (table, cols, include, where)   # include/where join the key: covering/partial variants of the same column combination each become their own candidate
        if key not in by_key:
            by_key[key] = Candidate(table=table, cols=cols, kind=kind,
                                    include=include, where=where)
        c = by_key[key]
        _add_once(c.evidence, ev)

    for qi, q in enumerate(queries, 1):
        tables, alias = _tables_and_aliases(q)
        eq_by_tbl: dict[str, list[str]] = {}
        range_by_tbl: dict[str, list[str]] = {}
        lit_eq_by_tbl: dict[str, list[tuple[str, str]]] = {}   # table → [(column, 'literal')]
        join_cols: list[tuple[str, str]] = []

        for m in _RE_EQ.finditer(q):
            left, right = m.group(1), m.group(2)
            lm = _RE_QUAL.fullmatch(left)
            rm = _RE_QUAL.fullmatch(right or "")
            if lm and rm:                        # qualified col = qualified col → JOIN predicate, record both sides
                for ref in (left, right):
                    got = _resolve(ref, tables, alias)
                    if got:
                        _add_once(join_cols, got)
                continue
            got = _resolve(left, tables, alias)
            if got:
                _add_once(eq_by_tbl.setdefault(got[0], []), got[1])
                # categorical string-literal equality (status='new'): raw material for a
                # partial index WHERE (numeric literals are instance values, not categories; not collected)
                if right and _RE_STR.fullmatch(right):
                    _add_once(lit_eq_by_tbl.setdefault(got[0], []), (got[1], right))

        for pattern in (_RE_RANGE, _RE_IN):
            for m in pattern.finditer(q):
                ref = m.group(1) or m.group(2)
                got = _resolve(ref, tables, alias)
                if got:
                    _add_once(range_by_tbl.setdefault(got[0], []), got[1])

        for tbl, eqs in eq_by_tbl.items():
            for col in eqs:
                add(tbl, (col,), "等值", f"Q{qi}: {col}= 等值")
            rng = range_by_tbl.get(tbl, [])
            if eqs and rng:
                add(tbl, (eqs[0], rng[0]), "等值+范围", f"Q{qi}: {eqs[0]}= 等值, {rng[0]} 范围")
            if len(eqs) >= 2:
                add(tbl, (eqs[0], eqs[1]), "等值,等值", f"Q{qi}: {eqs[0]}=, {eqs[1]}= 等值对")
        for tbl, cols in range_by_tbl.items():
            if tbl not in eq_by_tbl:
                for col in cols:
                    add(tbl, (col,), "范围", f"Q{qi}: {col} 范围")
        for tbl, col in join_cols:
            add(tbl, (col,), "JOIN", f"Q{qi}: JOIN ON {col}")

        # referenced-column collection (shared by covering/partial candidates): qualified
        # columns are always resolvable; single-table bare columns with strings stripped,
        # excluding function names/keywords
        refs_by_tbl: dict[str, list[str]] = {}

        for m in _RE_QUAL.finditer(q):
            got = _resolve(m.group(0), tables, alias)
            if got:
                _add_once(refs_by_tbl.setdefault(got[0], []), got[1])
        if len(tables) == 1:            # single-table bare columns (sum(amount) etc.); strip strings, exclude function names/keywords
            bare = _RE_STR.sub("''", q)
            for m in _RE_BARE_COL.finditer(bare):
                tok = m.group(1)
                if tok in tables or tok in alias or tok.lower() in _NONCOL:
                    continue
                _add_once(refs_by_tbl.setdefault(tables[0], []), tok)

        # Covering-index candidates (S3/S4 benchmark gap: the optimal solution for
        # JOIN/aggregate queries is often predicate keys + INCLUDE of query-referenced
        # columns; the benchmark agent had to hand-build this index shape twice to cover
        # the join slow spots). Conservative stance: nothing generated for SELECT *
        # (incomplete bare-column enumeration is one reason; the main one is that *
        # queries routinely do heap fetches anyway);
        # referenced columns = the table's resolvable column references in the query −
        # equality/range predicate columns (at most 2); JOIN columns go into INCLUDE
        # (nested-loop probes / index-only scans avoid heap fetches); prefer missing over
        # wrong — only resolvable references count.
        if not _RE_SELECT_STAR.search(q):
            for tbl, eqs in eq_by_tbl.items():
                rng = range_by_tbl.get(tbl, [])
                key = ((eqs[0], rng[0]) if rng else
                       (eqs[0], eqs[1]) if len(eqs) >= 2 else (eqs[0],))
                preds = set(eqs) | set(rng)
                extra = tuple(c for c in refs_by_tbl.get(tbl, [])
                              if c not in preds and c not in key)[:2]
                if extra:
                    add(tbl, key, "覆盖", f"Q{qi}: {','.join(key)}+INCLUDE "
                        f"{','.join(extra)}(查询引用列,免回表)", include=extra)

        # Partial-index candidates (v0.7.1, traced to S5: the two psql agents B2/B3
        # independently hand-built this index shape — a plain index on a 25%-selectivity
        # status column gets dropped by the planner, while partial+covering turns the
        # join inner side into an index-only-scan probe). Stance: categorical
        # string-literal equality column as WHERE (numeric literals are instance values,
        # not done); key = the table's other predicate columns (equality→JOIN→range,
        # take the first); INCLUDE = referenced columns (<=2).
        # Competition is left to the evaluation layer (hypo estimation / greedy measured
        # re-check); the generation layer only handles recall.
        for tbl, lits in lit_eq_by_tbl.items():
            wcol, lit = lits[0]
            others = ([c for c in eq_by_tbl.get(tbl, []) if c != wcol]
                      + [c for t, c in join_cols if t == tbl]
                      + list(range_by_tbl.get(tbl, [])))
            if not others:
                continue
            key = (others[0],)
            extra = tuple(c for c in refs_by_tbl.get(tbl, [])
                          if c != key[0] and c != wcol)[:2]
            add(tbl, key, "部分覆盖" if extra else "部分",
                f"Q{qi}: WHERE {wcol}={lit} 部分索引 + {key[0]}"
                + (f" + INCLUDE {','.join(extra)}" if extra else ""),
                include=extra, where=f"{wcol}={lit}")

        mo = _RE_ORDER.search(q)
        if mo:
            for ref in [c.strip() for c in mo.group(1).split(",") if c.strip()][:2]:
                ref = ref.split()[0]                   # take the column name only: strip DESC/ASC/NULLS/LIMIT suffixes
                got = _resolve(ref, tables, alias)
                if got:
                    add(got[0], (got[1],), "排序", f"Q{qi}: ORDER BY {got[1]}")

    # cap truncation: composite candidates first (more information), then by appearance order
    out = sorted(by_key.values(),
                 key=lambda c: (0 if "," in c.kind
                                or c.kind in ("等值+范围", "JOIN", "覆盖")
                                or c.kind.startswith("部分")
                                else 1))
    return out[:MAX_CANDIDATES]


# ---------------------------- Evaluation & ranking ----------------------------

def has_placeholders(queries: list[str]) -> bool:
    """Whether the query text carries $n placeholders (pg_stat_statements normalized form; cannot be executed by EXPLAIN directly)."""
    return any(re.search(r"\$\d+", q) for q in queries)


def evaluate(dsn: str, queries: list[str], *, weights: list[float] | None = None,
             analyze: bool = False) -> dict:
    """Candidate generation + hypo evaluation + weighted ranking + verification recipe.

    - Queries with placeholders: candidates and recipe only, no EXPLAIN evaluation (no
      speculative placeholder substitution — the selectivity of NULL/pseudo-literals
      would yield misleading costs); the weight source (pgst calls) is passed through as-is
    - weights matches queries in length and counts as 1 when absent; benefit = Σ w·(1-ratio), negative = worse
    - The recipe commands give two verification exits: planbench (estimation + plan-level
      measurement) and rebench (TPS level, produces ledger evidence)
    """
    from . import hypo as hypo_mod

    cands = extract_candidates(queries)
    result: dict = {"queries": queries, "weights": weights,
                    "placeholders": has_placeholders(queries),
                    "candidates": cands, "eval": None, "ranking": [], "recipe": []}
    if not cands:
        return result
    if result["placeholders"]:
        return result                              # no evaluation; candidates + recipe are still given

    report = hypo_mod.eval_candidates(dsn, [c.ddl for c in cands], queries, analyze=analyze)
    result["eval"] = report
    w = weights or [1.0] * len(queries)

    ranking = []
    singles = report["sets"][:len(cands)]         # combination sets are at the tail; ranking lists single candidates only
    for se, cand in zip(singles, cands):
        benefit = 0.0
        for qi, qe in enumerate(se.queries):
            if qe.ratio is not None:
                benefit += w[qi] * (1.0 - qe.ratio)
        ranking.append({"ddl": cand.ddl, "benefit": round(benefit, 4),
                        "avg_ratio": (round(se.avg_ratio, 4)
                                      if se.avg_ratio is not None else None),
                        "evidence": cand.evidence, "error": se.error})
    ranking.sort(key=lambda r: r["benefit"], reverse=True)
    result["ranking"] = ranking

    best = next((r for r in ranking if r["benefit"] > 0 and not r["error"]), None)
    if best:
        label = best["ddl"].lower().replace('"', "")
        result["recipe"] = [
            f'# plan 级验证(估算+实测 plan 对比;在克隆库上跑):',
            f'pgtuner planbench <查询文件> --variant idx1="{best["ddl"]}" --label "索引实验"',
            f'# TPS 级验证(纯测量,不改参不重启,免确认;出账本证据):',
            f'pgtuner rebench --instance <名> --queries-file <查询文件> --runs 5',
            f'# 真要落地:lab clone 拿库 → 建索引 → rebench 对照,results diff 出结论',
        ]
    return result


# ---------------------------- Rendering ----------------------------

def render_text(result: dict) -> str:
    """Compact plain text (v0.6 data-plane slimming): one line per candidate/evidence + ranking rows + recipe; machines read the json."""
    from .render import align_table

    lines = [f"候选 {len(result['candidates'])} 条"
             + (";查询带 $n 占位(pg_stat_statements 归一化形态)——只出候选与处方,"
                "落地为具体查询后再验" if result["placeholders"] else
                ";评估口径:EXPLAIN 估算代价(非实测)")]
    if result["candidates"]:
        cands = [[c.table, ", ".join(c.cols)
                  + (f" INC({', '.join(c.include)})" if c.include else "")
                  + (f" W:{c.where}" if c.where else ""),
                  c.kind, "; ".join(c.evidence)[:48]]
                 for c in result["candidates"]]
        lines.append(align_table(["表", "列", "类型", "证据(出处)"], cands))
    if result["ranking"]:
        lines.append("加权排名(benefit=Σ w·(1-ratio);>0 有估算收益;TPS 结论需实测):")
        rows = [[f"{r['benefit']:+.3f}",
                 "-" if r["avg_ratio"] is None else f"{r['avg_ratio']:.3f}",
                 r["ddl"][:64], (r["error"] or "")[:32]] for r in result["ranking"]]
        lines.append(align_table(["benefit", "avg_ratio", "候选DDL", "错误"], rows))
    if result["recipe"]:
        lines.append("验证处方:")
        lines.extend("  " + ln for ln in result["recipe"])
    return "\n".join(lines)


def render_json(result: dict) -> str:
    import json
    return json.dumps(result, ensure_ascii=False, default=lambda o: o.__dict__)
