"""hygiene portability scan tests (v0.11.0): (1) the whole repo must have zero violations (zero tolerance for hardcoded addresses/machine usernames/home-directory
paths); (2) positive and negative cases for the scanner itself (planted violations are caught; allowlist/allow markers let lines through)."""
from pgtuner import hygiene


def test_repo_scan_clean():
    """The gate itself: whole-repo scan with zero violations (same assertion as selftest's portability-scan)."""
    bad = hygiene.scan()
    assert bad == [], "; ".join(f"{v['file']}:{v['line']}({v['pattern']})" for v in bad[:8])


def _plant(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_scanner_catches_planted_violations(tmp_path):
    """Negative cases: home-directory paths / machine usernames / intranet IPs / non-allowlisted URLs / private gateways, each caught."""
    _plant(tmp_path, "pgtuner/x.py", 'P = "/home/someone/bin/pgbench"\n')  # hygiene:allow (detector fixture)
    _plant(tmp_path, "tests/y.py", "OWNER = 'zxx'\n")  # hygiene:allow (detector fixture)
    _plant(tmp_path, "docs/z.md", "网关 https://open.bigmodel.cn/api/v1\n")  # hygiene:allow (detector fixture)
    _plant(tmp_path, "scripts/w.sh", "curl http://10.1.2.3/x\n")  # hygiene:allow (detector fixture)
    got = hygiene.scan(tmp_path)
    pats = {v["pattern"] for v in got}
    assert "家目录绝对路径(/home/<user>/)" in pats
    assert "已知机器用户名" in pats
    assert "非白名单 URL 主机" in pats
    assert "IP 字面量" in pats
    assert {v["file"] for v in got} == {"pgtuner/x.py", "tests/y.py", "docs/z.md", "scripts/w.sh"}


def test_scanner_allows_public_refs_and_markers(tmp_path):
    """Positive cases: allowlisted public hosts / loopback IPs / machine-independent system paths / hygiene:allow markers, all allowed through."""
    _plant(tmp_path, "pgtuner/ok.py", "\n".join([
        'SRC = "https://github.com/postgres/postgres.git"',
        'SPEC = "https://www.tpc.org/information/benchmarks5.asp"',
        'LOOP = "127.0.0.1"',
        'SOCK = "/tmp"',
        'PROC = open("/proc/version")',
        'GUARD = "/home/zxx"  # hygiene:allow(守卫断言本体)',
        "sub = 'www.postgresql.org/docs'",
    ]) + "\n")
    assert hygiene.scan(tmp_path) == []


def test_scanner_exempts_history_and_data(tmp_path):
    """Exempt zones: patch-trial/reports historical archives, knowledge data files, .zcode/plans session artifacts."""
    _plant(tmp_path, "docs/patch-trial/a.md", "旧路径 /home/whoever/x\n")  # hygiene:allow (detector fixture)
    _plant(tmp_path, "docs/reports/b.md", "zxx 的实验\n")  # hygiene:allow (detector fixture)
    _plant(tmp_path, "knowledge/c.json", '"url": "https://example.com/x"\n')  # hygiene:allow (detector fixture)
    _plant(tmp_path, ".zcode/plans/d.md", "计划 /home/whoever\n")  # hygiene:allow (detector fixture)
    assert hygiene.scan(tmp_path) == []
