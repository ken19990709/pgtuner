"""Load and validate pgtuner/data/guc_knowledge.json (a versioned data file), returning structured objects.

Validation principle: missing required fields or an unparseable formula →
KnowledgeError (fail fast); optional fields (pain_level, restart_required_note)
may be absent.
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field


class KnowledgeError(Exception):
    """Knowledge base file has an invalid structure."""


@dataclass
class Rule:
    inputs: list          # list of formula input variable names
    formula: str          # pure Python expression (evaluated via eval)
    origin: str           # source reference
    caveats: str          # units and gating notes


@dataclass
class Guc:
    name: str
    description: str
    default_value: str    # free text with annotations; must not be used as a number
    context: str
    restart_required: bool
    pain_level: str | None = None            # optional: null in 2 of the 57 entries
    restart_required_note: str | None = None  # optional: present in only 2 entries
    why_hard: str = ""
    rules: list[Rule] = field(default_factory=list)
    version_notes: str = ""
    sources: list = field(default_factory=list)


@dataclass
class HardwareProbe:
    name: str
    description: str
    rules: list            # kept as-is: rules[].formula is a shell command string, shown for reference only, never executed
    sources: list


@dataclass
class Knowledge:
    generated_for: str
    pg_versions: str
    gucs: list[Guc]
    hardware_probes: list[HardwareProbe]
    path: str


# Function name set kept consistent with engine.FUNCS (used here only for load-time consistency checking)
_FUNC_NAMES = {"min", "max", "abs", "int", "round", "ceil", "floor", "ln", "log"}


def _require(obj: dict, key: str, where: str):
    if key not in obj:
        raise KnowledgeError(f"{where}: 缺必填字段 {key!r}")
    return obj[key]


def load_knowledge(path: str) -> Knowledge:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise KnowledgeError(f"知识库文件不存在: {path}") from None
    except json.JSONDecodeError as e:
        raise KnowledgeError(f"知识库 JSON 解析失败: {path}: {e}") from None

    gucs: list[Guc] = []
    for gi, g in enumerate(_require(raw, "gucs", "顶层")):
        where = f"gucs[{gi}]"
        name = _require(g, "name", where)
        guc = Guc(
            name=name,
            description=_require(g, "description", where),
            default_value=_require(g, "default_value", where),
            context=_require(g, "context", where),
            restart_required=bool(_require(g, "restart_required", where)),
            pain_level=g.get("pain_level"),
            restart_required_note=g.get("restart_required_note"),
            why_hard=g.get("why_hard", ""),
            version_notes=g.get("version_notes", ""),
            sources=g.get("sources", []),
        )
        for ri, r in enumerate(_require(g, "rules", where)):
            rwhere = f"{name}.rules[{ri}]"
            rule = Rule(
                inputs=list(_require(r, "inputs", rwhere)),
                formula=_require(r, "formula", rwhere),
                origin=_require(r, "origin", rwhere),
                caveats=_require(r, "caveats", rwhere),
            )
            _check_formula(rule, rwhere)
            guc.rules.append(rule)
        gucs.append(guc)

    probes = [
        HardwareProbe(
            name=p.get("name", ""),
            description=p.get("description", ""),
            rules=p.get("rules", []),
            sources=p.get("sources", []),
        )
        for p in raw.get("hardware_probes", [])
    ]
    return Knowledge(
        generated_for=raw.get("generated_for", ""),
        pg_versions=raw.get("pg_versions", ""),
        gucs=gucs,
        hardware_probes=probes,
        path=path,
    )


def _check_formula(rule: Rule, where: str) -> None:
    """Load-time validation: the formula must parse in eval mode and reference only declared inputs or agreed function names."""
    try:
        tree = ast.parse(rule.formula, mode="eval")
    except SyntaxError as e:
        raise KnowledgeError(f"{where}: 公式语法错误: {rule.formula!r}: {e}") from None
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    undeclared = names - set(rule.inputs) - _FUNC_NAMES
    if undeclared:
        raise KnowledgeError(f"{where}: 公式引用了 inputs 未声明的标识符: {sorted(undeclared)}")
