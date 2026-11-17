"""hygiene: portability scan (v0.11.0) — zero tolerance for hardcoded addresses /
known-machine usernames / machine-bound absolute paths.

Policy (violation list in _PATTERNS / _URL_ALLOW_HOSTS):
- Forbidden: home-directory literals, known machine usernames, file addresses,
  IP literals;  # hygiene:allow (the detector itself)
- URLs only pass a public-host allowlist (a network address appearing in code
  can only be a public endpoint like "fetch PG source");
- Loopback/broadcast constants (127.0.0.1 etc.) don't count as machine-bound — allowed;
- An inline `hygiene:allow` marker = explicit exemption (limited to "detector
  itself" style cases, e.g. violation strings inside guard assertions);
- Scan scope = code / scripts / tests / benchmarks / living docs / skills;
  exemptions = historical evidence archives (patch-trial, reports), knowledge
  data files (including formula source URLs), .zcode/plans session artifacts,
  runtime directories (.venv/.pgtuner). The docs/knowledge exemption entries
  are kept for historical checkouts compatibility — the current repo no
  longer carries those trees (they simply match nothing here).

Wiring: selftest fast gate portability-scan + tests/test_hygiene.py.
Machine-independent system interfaces (/proc, /sys, /tmp) match no forbidden
pattern and need no allowlist.
"""
from __future__ import annotations

import re
from pathlib import Path

from .paths import REPO_ROOT

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"/home/[A-Za-z0-9_.-]+/"), "家目录绝对路径(/home/<user>/)"),
    (re.compile(r"/Users/[A-Za-z0-9_.-]+/"), "macOS 家目录绝对路径(/Users/<user>/)"),
    (re.compile(r"\b(?:zxx|zengxiangxin)\b"), "已知机器用户名"),  # hygiene:allow (the detector itself)
    (re.compile(r"file://"), "file 地址"),  # hygiene:allow (the detector itself)
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "IP 字面量"),
]
_URL_RE = re.compile(r"https?://([^/\s\"')]+)")   # excludes the trailing paren of a markdown link
# URL allowlist = public references/public endpoints (source fetch, benchmark specs, papers), machine-independent; private gateways/intranet addresses never allowed
_URL_ALLOW_HOSTS = {"github.com", "ftp.postgresql.org", "www.postgresql.org", "postgresql.org",
                    "hammerdb.com", "www.hammerdb.com", "tpc.org", "www.tpc.org", "arxiv.org",
                    # README badges (v0.11.0 open-source facade): CI/license/python-version
                    "img.shields.io", "www.python.org"}
_UNIVERSAL_IPS = {"127.0.0.1", "0.0.0.0", "255.255.255.255"}   # loopback/broadcast constants, not machine-bound

SCOPE_GLOBS = ("pgtuner/**/*.py", "scripts/*", "tests/*.py", "benchmarks/*",
               "README.md", "AGENTS.md", "CLAUDE.md", "docs/*.md",
               ".zcode/skills/**/*", ".claude/skills/**/*")
# "docs/*.md" and the first three EXEMPT_PARTS entries below target trees the
# repo itself no longer carries — paths kept for historical checkouts
# compatibility (so scanning an old checkout neither crashes nor flags its
# archived evidence); on the current tree the globs simply match nothing.
EXEMPT_PARTS = ("docs/patch-trial", "docs/reports", "knowledge", ".zcode/plans",
                ".venv", ".pgtuner", "build")


def scan(repo_root: str | Path | None = None) -> list[dict]:
    """Scan the scope for portability violations, returning [{file, line, pattern, text}]; empty = clean. Read-only."""
    root = Path(repo_root).resolve() if repo_root else REPO_ROOT
    files: set[Path] = set()
    for g in SCOPE_GLOBS:
        files.update(p for p in root.glob(g) if p.is_file())
    violations: list[dict] = []
    for f in sorted(files):
        rel = f.relative_to(root).as_posix()
        if any(rel.startswith(p) for p in EXEMPT_PARTS):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                                  # binary/unreadable: skip, no false positives
        for i, line in enumerate(text.splitlines(), 1):
            if "hygiene:allow" in line:
                continue                              # explicit exemption (guard assertions themselves, etc.)
            for pat, why in _PATTERNS:
                if any(m.group(0) not in _UNIVERSAL_IPS for m in pat.finditer(line)):
                    violations.append({"file": rel, "line": i, "pattern": why,
                                       "text": line.strip()[:120]})
                    break
            for m in _URL_RE.finditer(line):
                host = m.group(1)
                if not any(host == h or host.endswith("." + h) for h in _URL_ALLOW_HOSTS):
                    violations.append({"file": rel, "line": i, "pattern": "非白名单 URL 主机",
                                       "text": line.strip()[:120]})
                    break
    return violations
