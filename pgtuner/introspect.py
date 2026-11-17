"""schema — in-database introspection: table listing and single-table full view, the
underlying piece of the cli `schema` subcommand.

All catalog queries are read-only (pg_class / pg_attribute / pg_index / pg_constraint /
pg_trigger / pg_stat_*) and always parameter-bound — object names go through nspname/relname
equality matching, not ::regclass string concatenation (odd names with quotes/special
characters are both an injection surface and a parsing trap).

Metric conventions (accuracy first; prefer labeling "unknown" over guessing values):
  - Row counts come in two metrics: estimated rows = reltuples (written by ANALYZE; PG14+'s
    -1 means unknown → None), statistics rows = pg_stat_user_tables.n_live_tup (real-time
    cumulative, affected by track_counts, can be NULL);
  - Sizes: data size = pg_table_size (data+toast, excluding indexes); total size =
    pg_total_relation_size (data+toast+indexes). For partitioned tables (relkind='p') the
    parent has no storage of its own; sizes are summed over all leaves of pg_partition_tree;
    views/foreign tables have no storage;
  - Index usage count idx_scan can be NULL (track_counts=off or a new object) → display "-",
    don't guess 0.

The DDL rebuild (synthesize_ddl) is a **semantically equivalent** rebuild: columns
(name/type/not-null/default), constraints, indexes, triggers, and the partition structure
recreate an isomorphic object; storage parameters/tablespaces/privileges/owner are not
included — the header comment states this honestly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import pg

# relkind → display label; S sequences / t toast are outside this tool's "table listing" scope
RELKIND_LABELS = {"r": "表", "p": "分区表", "v": "视图", "m": "物化视图", "f": "外部表"}
RELKINDS = tuple(RELKIND_LABELS)
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")
CONSTRAINT_KINDS = {"p": "主键", "u": "唯一", "f": "外键", "c": "检查", "x": "排除",
                    "t": "触发器(旧)", "n": "NOT NULL"}
TRIGGER_ENABLED = {"O": "原生", "A": "总是", "D": "禁用", "R": "仅副本"}
# Output order of CREATE TABLE inline constraints (structural keys before behavioral constraints; "n" is covered by column attributes, not emitted)
_INLINE_CONSTRAINT_ORDER = "pufcxt"

# List user objects in the database (optional schema filter). Both size columns of a
# partitioned table are summed over its leaves (see module docstring).
_SQL_TABLES = """
SELECT n.nspname,
       c.relname,
       c.relkind,
       CASE WHEN c.relkind = 'p'
            THEN COALESCE((SELECT sum(pg_table_size(l.relid)) FROM pg_partition_tree(c.oid) l
                           WHERE l.isleaf), 0)
            WHEN c.relkind IN ('v', 'f') THEN 0
            ELSE pg_table_size(c.oid) END,
       CASE WHEN c.relkind = 'p'
            THEN COALESCE((SELECT sum(pg_total_relation_size(l.relid)) FROM pg_partition_tree(c.oid) l
                           WHERE l.isleaf), 0)
            WHEN c.relkind IN ('v', 'f') THEN 0
            ELSE pg_total_relation_size(c.oid) END,
       c.reltuples::bigint,
       s.n_live_tup,
       s.n_dead_tup,
       obj_description(c.oid, 'pg_class')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
WHERE c.relkind = ANY(%s) AND n.nspname <> ALL(%s)
"""

_SQL_RESOLVE = """
SELECT c.oid, c.relkind, c.reltuples::bigint, c.relpersistence,
       pg_get_userbyid(c.relowner), obj_description(c.oid, 'pg_class')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s
"""

_SQL_SIZE_OF = """
SELECT CASE WHEN c.relkind = 'p'
            THEN COALESCE((SELECT sum(pg_table_size(l.relid)) FROM pg_partition_tree(c.oid) l
                           WHERE l.isleaf), 0)
            WHEN c.relkind IN ('v', 'f') THEN 0
            ELSE pg_table_size(c.oid) END,
       CASE WHEN c.relkind = 'p'
            THEN COALESCE((SELECT sum(pg_total_relation_size(l.relid)) FROM pg_partition_tree(c.oid) l
                           WHERE l.isleaf), 0)
            WHEN c.relkind IN ('v', 'f') THEN 0
            ELSE pg_total_relation_size(c.oid) END
FROM pg_class c WHERE c.oid = %s
"""

_SQL_COLUMNS = """
SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       pg_get_expr(ad.adbin, ad.adrelid, false), col_description(a.attrelid, a.attnum)
FROM pg_attribute a
LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""

_SQL_INDEXES = """
SELECT ic.relname, pg_get_indexdef(i.indexrelid), i.indisunique, i.indisprimary,
       i.indisvalid, pg_relation_size(i.indexrelid), s.idx_scan
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.indexrelid
WHERE i.indrelid = %s
ORDER BY i.indisprimary DESC, i.indisunique DESC, ic.relname
"""

_SQL_CONSTRAINTS = """
SELECT conname, contype, pg_get_constraintdef(oid, true), convalidated
FROM pg_constraint WHERE conrelid = %s
ORDER BY contype, conname
"""

_SQL_TRIGGERS = """
SELECT t.tgname, pg_get_triggerdef(t.oid, true), t.tgenabled
FROM pg_trigger t WHERE t.tgrelid = %s AND NOT t.tgisinternal
ORDER BY t.tgname
"""

_SQL_STATS = """
SELECT n_live_tup, n_dead_tup, seq_scan, idx_scan,
       last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
FROM pg_stat_user_tables WHERE relid = %s
"""

_SQL_VIEWDEF = "SELECT pg_get_viewdef(%s, true)"
_SQL_PARTKEY = "SELECT pg_get_partkeydef(%s)"

_SQL_PARTITIONS = """
SELECT n.nspname, c.relname, l.level, l.isleaf, pg_get_expr(c.relpartbound, c.oid)
FROM pg_partition_tree(%s) l
JOIN pg_class c ON c.oid = l.relid
JOIN pg_namespace n ON n.oid = c.relnamespace
ORDER BY l.level, n.nspname, c.relname
"""

_SQL_PARENT = """
SELECT pn.nspname, pc.relname
FROM pg_inherits i
JOIN pg_class pc ON pc.oid = i.inhparent
JOIN pg_namespace pn ON pn.oid = pc.relnamespace
WHERE i.inhrelid = %s
"""


def _fetch(conn, sql: str, params=None) -> list:
    """Parameterized read-only query (pg.fetch_all + error translation: failures become a uniform RuntimeError carrying the last error line and SQL context)."""
    try:
        return pg.fetch_all(conn, sql, params)
    except Exception as e:                                   # noqa: BLE001
        tail = str(e).strip().splitlines()
        raise RuntimeError(f"内省查询失败: {tail[-1] if tail else e}\nSQL: {sql.strip()[:120]}") from None


# ---------------------------- Data model ----------------------------

def _est_rows(value):
    """reltuples → int; None and PG14+'s -1 (unknown) both map to None (never pass a statistics value off as the estimate)."""
    return None if value is None or value < 0 else int(value)


@dataclass
class TableSummary:
    schema: str
    name: str
    kind: str                       # r/p/v/m/f
    data_bytes: int | None
    total_bytes: int | None
    row_est: int | None
    live_tup: int | None
    dead_tup: int | None
    comment: str | None

    @property
    def kind_label(self) -> str:
        return RELKIND_LABELS.get(self.kind, self.kind)


@dataclass
class ColumnInfo:
    name: str
    type: str
    not_null: bool
    default: str | None
    comment: str | None


@dataclass
class IndexInfo:
    name: str
    definition: str
    unique: bool
    primary: bool
    valid: bool
    size_bytes: int | None
    scans: int | None


@dataclass
class ConstraintInfo:
    name: str
    kind: str                       # p/u/f/c/x/t/n
    definition: str
    validated: bool

    @property
    def kind_label(self) -> str:
        return CONSTRAINT_KINDS.get(self.kind, self.kind)


@dataclass
class TriggerInfo:
    name: str
    definition: str
    enabled: str                    # O/A/D/R

    @property
    def enabled_label(self) -> str:
        return TRIGGER_ENABLED.get(self.enabled, self.enabled)


@dataclass
class PartitionInfo:
    schema: str
    name: str
    level: int
    is_leaf: bool
    bound: str | None


@dataclass
class TableDescription:
    schema: str
    name: str
    kind: str
    owner: str
    unlogged: bool                  # relpersistence='u'
    comment: str | None
    row_est: int | None
    data_bytes: int | None
    total_bytes: int | None
    columns: list[ColumnInfo] = field(default_factory=list)
    indexes: list[IndexInfo] = field(default_factory=list)
    constraints: list[ConstraintInfo] = field(default_factory=list)
    triggers: list[TriggerInfo] = field(default_factory=list)
    stats: dict = field(default_factory=dict)   # live/dead/seq_scan/idx_scan/last_*
    view_def: str | None = None
    partition_key: str | None = None            # partition key definition (partitioned table)
    partitions: list[PartitionInfo] = field(default_factory=list)
    parent: tuple[str, str] | None = None       # parent table when this table is a partition (schema, name)

    @property
    def kind_label(self) -> str:
        return RELKIND_LABELS.get(self.kind, self.kind)


# ---------------------------- Table listing ----------------------------

def list_tables(conn, schema: str | None = None) -> list[TableSummary]:
    """All user tables/views/materialized views in the database (only that schema when given), sorted by (schema, name)."""
    sql = _SQL_TABLES + ("AND n.nspname = %s " if schema else "") + "ORDER BY n.nspname, c.relname"
    params = [list(RELKINDS), list(SYSTEM_SCHEMAS)] + ([schema] if schema else [])
    return [TableSummary(schema=r[0], name=r[1], kind=r[2],
                         data_bytes=r[3], total_bytes=r[4], row_est=_est_rows(r[5]),
                         live_tup=r[6], dead_tup=r[7], comment=r[8])
            for r in _fetch(conn, sql, params)]


# ---------------------------- Single-table full view ----------------------------

def describe_table(conn, table: str, schema: str = "public") -> TableDescription:
    """Collect the full view of a single object. Raises ValueError when the object does not exist (with the schema's object count as a hint)."""
    rows = _fetch(conn, _SQL_RESOLVE, [schema, table])
    if not rows:
        n = len(list_tables(conn, schema=schema))
        raise ValueError(f"对象不存在: {schema}.{table}(该 schema 下共 {n} 个用户对象,"
                         f"清单见 pgtuner schema tables)")
    oid, kind, reltuples, persistence, owner, comment = rows[0]
    desc = TableDescription(schema=schema, name=table, kind=kind, owner=owner,
                            unlogged=(persistence == "u"), comment=comment,
                            row_est=_est_rows(reltuples),
                            data_bytes=None, total_bytes=None)

    size = _fetch(conn, _SQL_SIZE_OF, [oid])
    if size:
        desc.data_bytes, desc.total_bytes = size[0][0], size[0][1]

    desc.columns = [ColumnInfo(*row) for row in _fetch(conn, _SQL_COLUMNS, [oid])]
    desc.indexes = [IndexInfo(row[0], row[1], row[2], row[3], row[4], row[5], row[6])
                    for row in _fetch(conn, _SQL_INDEXES, [oid])]
    desc.constraints = [ConstraintInfo(row[0], row[1], row[2], row[3])
                        for row in _fetch(conn, _SQL_CONSTRAINTS, [oid])]
    desc.triggers = [TriggerInfo(row[0], row[1], row[2])
                     for row in _fetch(conn, _SQL_TRIGGERS, [oid])]

    stats = _fetch(conn, _SQL_STATS, [oid])
    if stats:
        live, dead, seq_scan, idx_scan, lv, lav, la, laa = stats[0]
        desc.stats = {"live_tup": live, "dead_tup": dead, "seq_scan": seq_scan,
                      "idx_scan": idx_scan, "last_vacuum": lv, "last_autovacuum": lav,
                      "last_analyze": la, "last_autoanalyze": laa}

    if kind in ("v", "m"):
        vd = _fetch(conn, _SQL_VIEWDEF, [oid])
        desc.view_def = vd[0][0] if vd else None
    if kind == "p":
        pk = _fetch(conn, _SQL_PARTKEY, [oid])
        desc.partition_key = pk[0][0] if pk else None
        desc.partitions = [PartitionInfo(*row) for row in _fetch(conn, _SQL_PARTITIONS, [oid])]
    else:
        parent = _fetch(conn, _SQL_PARENT, [oid])
        if parent:
            desc.parent = (parent[0][0], parent[0][1])
    return desc


# ---------------------------- DDL rebuild ----------------------------

def _qid(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _qstr(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def synthesize_ddl(desc: TableDescription) -> str:
    """Rebuild **semantically equivalent** DDL from the catalog (boundaries in the module docstring). Views/materialized views emit their SELECT."""
    full = f"{_qid(desc.schema)}.{_qid(desc.name)}"
    lines = ["-- 语义等价重建:列(名/类型/非空/默认)+约束+索引+触发器+分区结构;",
             "-- 不含存储参数/表空间/权限/属主;执行前请先建依赖对象(外键目标表/被引用函数等),",
             "-- 并移除/改名原对象——约束与索引名全库唯一,原对象在场会撞名"]

    if desc.kind in ("v", "m"):
        kw = "MATERIALIZED VIEW" if desc.kind == "m" else "VIEW"
        lines.append(f"CREATE {kw} {full} AS")
        lines.append(desc.view_def or "(获取视图定义失败)")
        if desc.comment:
            lines.append(f"COMMENT ON VIEW {full} IS {_qstr(desc.comment)};")
        return "\n".join(lines)

    body: list[str] = []
    for col in desc.columns:
        piece = f"  {_qid(col.name)} {col.type}"
        if col.not_null:
            piece += " NOT NULL"
        if col.default is not None:
            piece += f" DEFAULT {col.default}"
        body.append(piece)
    inline = [c for c in desc.constraints if c.kind in _INLINE_CONSTRAINT_ORDER]
    for c in sorted(inline, key=lambda c: (_INLINE_CONSTRAINT_ORDER.index(c.kind), c.name)):
        body.append(f"  CONSTRAINT {_qid(c.name)} {c.definition}")
    tail = f") PARTITION BY {desc.partition_key}" if desc.partition_key else ")"
    lines.append(f"CREATE TABLE {full} (")
    if body:
        lines.append(",\n".join(body))
    lines.append(tail + ";")

    for p in desc.partitions:
        if p.is_leaf:
            bound = f" {p.bound}" if p.bound else ""
            lines.append(f"CREATE TABLE {_qid(p.schema)}.{_qid(p.name)} PARTITION OF {full}{bound};")

    # standalone indexes: skip constraint-backed indexes (they share names with p/u
    # constraints and were already created by the CONSTRAINT clauses)
    constraint_indexes = {c.name for c in desc.constraints if c.kind in ("p", "u")}
    for idx in desc.indexes:
        if idx.name not in constraint_indexes:
            lines.append(idx.definition.rstrip(";") + ";")

    for trg in desc.triggers:
        lines.append(trg.definition.rstrip(";") + ";")
    if desc.comment:
        lines.append(f"COMMENT ON TABLE {full} IS {_qstr(desc.comment)};")
    for col in desc.columns:
        if col.comment:
            lines.append(f"COMMENT ON COLUMN {full}.{_qid(col.name)} IS {_qstr(col.comment)};")
    return "\n".join(lines)


# ---------------------------- Rendering (both text / json formats) ----------------------------

def _num(v) -> str:
    """Number → thousands separator; None → "-" (unknown/no stats, don't guess 0)."""
    return "-" if v is None else f"{v:,}"


def render_tables_text(summaries: list[TableSummary]) -> str:
    """Table listing → compact plain text (v0.6 data-plane slimming; returns str, caller bare-prints).

    The schema column is omitted for a single schema; the two row-count metrics merge into
    one "estimated/statistics" column. Metric notes in the module docstring.
    """
    from .render import align_table
    from .watch import _human_bytes

    schemas = {s.schema for s in summaries}
    one = len(schemas) == 1
    header = ([] if one else ["schema"]) + ["名称", "类型", "行数(估算/统计)", "死元组",
                                            "数据", "总大小", "注释"]
    rows = []
    for s in summaries:
        rows.append(([] if one else [s.schema]) + [
            s.name, s.kind_label, f"{_num(s.row_est)}/{_num(s.live_tup)}",
            _num(s.dead_tup), _human_bytes(s.data_bytes),
            _human_bytes(s.total_bytes) + ("(含分区)" if s.kind == "p" else ""),
            s.comment or ""])
    out = align_table(header, rows)
    return f"用户对象 {len(summaries)} 个(行数=估算(ANALYZE)/统计(实时);大写单位=1024进位)\n{out}"


def render_tables_json(summaries: list[TableSummary]) -> str:
    """Table listing → JSON (machine-readable; dataclasses via asdict)."""
    import json
    return json.dumps([asdict(s) | {"kind_label": s.kind_label} for s in summaries],
                      ensure_ascii=False, default=str, indent=1)


def _index_def_short(definition: str) -> str:
    """pg_get_indexdef → short form like "btree (aid)" (strips the CREATE ... ON <table> USING prefix; original on strip failure)."""
    import re
    m = re.search(r" USING (\w+ \(.*)$", definition or "")
    return m.group(1) if m else (definition or "")


def render_desc_text(desc: TableDescription) -> str:
    """Single-table full view → compact plain text (v0.6 data-plane slimming; \\d style,
    returns str for the caller to bare-print).

    p/u constraints are already expressed by the index rows' primary/unique attributes, so
    the constraint section lists only foreign keys/checks/exclusions (dedup without losing information).
    """
    from .render import truncate
    from .watch import _human_bytes

    lines: list[str] = []
    head = f"{desc.schema}.{desc.name} {desc.kind_label} · 属主 {desc.owner}" \
           + (" · UNLOGGED" if desc.unlogged else "") \
           + (f" · 父分区 {desc.parent[0]}.{desc.parent[1]}" if desc.parent else "")
    if desc.comment:
        head += f" · 注释: {desc.comment}"
    lines.append(head)

    st = desc.stats
    fresh = next((f"{k.replace('last_', '')}={str(v)[:19]}" for k in ("last_analyze", "last_autoanalyze",
                                                                      "last_vacuum", "last_autovacuum")
                  if (v := st.get(k))), "从未")
    lines.append(
        f"行数 估算 {_num(desc.row_est)}/统计 {_num(st.get('live_tup'))}(死 {_num(st.get('dead_tup'))})"
        f" · 大小 数据 {_human_bytes(desc.data_bytes)}/总 {_human_bytes(desc.total_bytes)}"
        + ("(含分区)" if desc.kind == "p" else "")
        + f" · 扫描 seq {_num(st.get('seq_scan'))}/idx {_num(st.get('idx_scan'))}"
        + f" · 统计 {fresh}")

    col_bits = [c.name + " " + c.type
                + (" NOT NULL" if c.not_null else "")
                + (f" DEFAULT {c.default}" if c.default is not None else "")
                for c in desc.columns]
    if len(" | ".join(col_bits)) <= 110:
        lines.append(f"列({len(col_bits)}): " + " | ".join(col_bits))
    else:
        lines.append(f"列({len(col_bits)}):")
        lines.extend("  " + b for b in col_bits)

    if desc.indexes:
        lines.append(f"索引({len(desc.indexes)}):")
        for i in desc.indexes:
            attr = ("主键 " if i.primary else ("唯一 " if i.unique else "")) \
                   + ("" if i.valid else "无效!")
            lines.append(f"  {i.name} {attr}{_index_def_short(i.definition)}"
                         f"  {_human_bytes(i.size_bytes)}  扫描{_num(i.scans)}")
    else:
        lines.append("索引: 无")

    others = [c for c in desc.constraints if c.kind not in ("p", "u", "n")]
    if others:
        lines.append(f"约束({len(others)}):")
        lines.extend(f"  {c.name} {c.kind_label}{' 未验证' if not c.validated else ''} {c.definition}"
                     for c in others)
    if desc.triggers:
        lines.append(f"触发器({len(desc.triggers)}):")
        lines.extend(f"  {t.name} [{t.enabled_label}] {truncate(t.definition, 80)}"
                     for t in desc.triggers)
    if desc.partition_key:
        leaves = [p for p in desc.partitions if p.is_leaf]
        lines.append(f"分区键: {desc.partition_key} · 叶分区 {len(leaves)} 个")
        lines.extend(f"  {truncate(p.name, 32)} {p.bound or ''}" for p in leaves[:8])
        if len(leaves) > 8:
            lines.append(f"  … 共 {len(leaves)} 个")
    if desc.view_def:
        lines.append("视图定义:")
        lines.extend("  " + ln for ln in desc.view_def.splitlines())
    return "\n".join(lines)


def render_desc_json(desc: TableDescription) -> str:
    """Single-table full view → JSON (machine-readable; the parent tuple becomes a list for serialization)."""
    import json
    d = asdict(desc)
    if d.get("parent"):
        d["parent"] = list(d["parent"])
    d["kind_label"] = desc.kind_label
    return json.dumps(d, ensure_ascii=False, default=str, indent=1)
