"""Bilingual CLI help: PGTUNER_LANG=en swaps --help texts to English at runtime.

The Chinese texts in cli.py remain the single source of truth; this module only
substitutes English strings from pgtuner.help_en at render time. Missing table
entries fall back to Chinese (fail-open), so translations can land piecemeal.
The swap remembers originals and is reversible, which keeps in-process
language-toggle tests honest.
"""
from __future__ import annotations

import os


def active_lang() -> str:
    """'en' when PGTUNER_LANG asks for English, else 'zh' (the default)."""
    return "en" if os.environ.get("PGTUNER_LANG", "").strip().lower().startswith("en") else "zh"


# (id(node), attr) -> (node, attr, original Chinese text); lets a later zh
# apply restore state mutated by an earlier en apply in the same process.
_ORIGINALS: dict = {}


def apply(root) -> None:
    """Swap the whole command tree's help texts per PGTUNER_LANG.

    No-op for zh on a pristine tree; restores originals for zh after an en
    pass has mutated them (in-process toggling).
    """
    lang = active_lang()
    if lang != "en" and not _ORIGINALS:
        return
    from .help_en import HELP
    _walk(root, (), HELP, to_en=(lang == "en"))


def _swap(node, attr: str, new_text: str, to_en: bool) -> None:
    key = (id(node), attr)
    if to_en:
        if key not in _ORIGINALS:
            _ORIGINALS[key] = (node, attr, getattr(node, attr))
        setattr(node, attr, new_text)
    else:
        saved = _ORIGINALS.pop(key, None)
        if saved is not None:
            setattr(saved[0], saved[1], saved[2])


def _walk(node, path: tuple, table: dict, to_en: bool) -> None:
    if path in table:
        _swap(node, "help", table[path], to_en)
    ep_key = path + ("__epilog__",)
    if getattr(node, "epilog", None) and ep_key in table:
        _swap(node, "epilog", table[ep_key], to_en)
    for p in node.params:
        if not getattr(p, "help", None):   # click.Argument carries no help
            continue
        decl = p.opts[0] if getattr(p, "opts", None) else p.name
        key = path + (decl,)
        if key in table:
            _swap(p, "help", table[key], to_en)
    for name, sub in list(getattr(node, "commands", {}).items()):
        _walk(sub, path + (name,), table, to_en)
