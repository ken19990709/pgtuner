"""render regression: dual-width alignment / truncation / aligned tables (the foundation of the v0.6 data-plane slimming, pure functions)."""
from pgtuner.render import align_table, display_width, pad, truncate


def test_display_width_and_pad_truncate():
    assert display_width("abc") == 3
    assert display_width("中文") == 4                       # CJK full-width
    assert pad("ab", 5) == "ab   " and pad("ab", 5, right=True) == "   ab"
    assert pad("中", 4) == "中  "                           # padded by display width, not character count
    assert truncate("abcdef", 4) == "abc…" and truncate("中中文", 5) == "中中…"
    assert truncate("abc", 5) == "abc"                      # within width, kept as-is


def test_align_table_numeric_right_and_cjk():
    out = align_table(["调用", "总ms", "查询"],
                      [["1,000", "25.5", "SELECT 中文查询"],
                       ["9", "3,025.125", "SELECT 1"]])
    lines = out.splitlines()
    assert lines[0].startswith("调用") and "总ms" in lines[0]
    assert set(lines[1]) <= {"-"} | {" "}                    # the separator line contains only -
    assert lines[3].startswith("    9")                      # numeric right-aligned (flush right within the column)
    assert "SELECT 中文查询" in lines[2]


def test_align_table_squeezes_wide_columns():
    wide = [["x" * 200, "y"], ["z", "w"]]
    out = align_table(["a", "b"], wide, total_width=40)
    assert max(len(ln) for ln in out.splitlines()) <= 40
    assert out.count("…") >= 1                               # squeezed columns carry a truncation marker
