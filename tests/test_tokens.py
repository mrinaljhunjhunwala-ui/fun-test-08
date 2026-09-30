"""Colour tokens: single source (dashboard/shared/tokens.py) consumed everywhere."""
import os
import re
import sqlite3

from dashboard.shared import tokens
from dashboard.shared.tokens import COLORS, hex_to_rgb

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_tokens_are_hex():
    for k, v in COLORS.items():
        assert re.fullmatch(r"#[0-9a-f]{6}", v), (k, v)


def test_hex_to_rgb_and_rgba():
    assert hex_to_rgb(COLORS["bull"]) == (111, 170, 132)
    assert tokens.rgba("bear", 0.5) == "rgba(199,112,112,0.5)"


def test_css_vars_cover_every_token():
    css = tokens.css_vars()
    for k, v in COLORS.items():
        assert f"--{k}: {v};" in css


def test_plot_colors_come_from_tokens():
    from dashboard.shared.chart_helpers import PLOT_COLORS, diverging_colors
    for k, v in PLOT_COLORS.items():
        assert v == COLORS[k]
    pos, neg = diverging_colors([1.0, -1.0])
    assert pos.startswith("rgba(%d,%d,%d," % hex_to_rgb(COLORS["bull"]))
    assert neg.startswith("rgba(%d,%d,%d," % hex_to_rgb(COLORS["bear"]))


def test_table_styles_use_tokens():
    from dashboard.shared import table_styles as ts
    assert ts._BULL == COLORS["bull"] and ts._BEAR == COLORS["bear"]
    assert ts._BULL_RGB == hex_to_rgb(COLORS["bull"])


def test_design_root_is_built_from_tokens():
    src = open(os.path.join(_ROOT, "dashboard", "shared", "design.py"), encoding="utf-8").read()
    assert "/*__COLOR_TOKENS__*/" in src
    for v in COLORS.values():
        assert re.search(r"--[\w-]+:\s+%s;" % v, src) is None, v


def test_tqs_page_has_no_raw_hex():
    src = open(os.path.join(_ROOT, "dashboard", "pages", "18_tqs_scanner.py"), encoding="utf-8").read()
    assert re.search(r"#[0-9a-fA-F]{6}\b", src) is None


def _page_hex_hook():
    """Load .claude/hooks/block_page_hex.py so the test shares its exemptions."""
    import importlib.util
    path = os.path.join(_ROOT, ".claude", "hooks", "block_page_hex.py")
    spec = importlib.util.spec_from_file_location("_block_page_hex", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_raw_hex_in_any_page():
    from pathlib import Path
    hook = _page_hex_hook()
    pages = sorted(Path(_ROOT, "dashboard", "pages").glob("*.py"))
    assert pages
    hits = {p.name: hook._find_hex_violations(p) for p in pages}
    hits = {k: v for k, v in hits.items() if v}
    assert not hits, hits


def test_page_hex_hook_exemptions():
    import tempfile
    from pathlib import Path
    hook = _page_hex_hook()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d, "x.py")
        p.write_text('# comment #16c784\nA = "#fff"  # noqa: hex\nB = "#ff4d4d"\n', encoding="utf-8")
        assert [h[0] for h in hook._find_hex_violations(p)] == [3]


def test_ordinal_ramp_distinct_and_anchored():
    r5, r6 = tokens.ordinal_ramp(5), tokens.ordinal_ramp(6)
    assert len(set(r5)) == 5 and len(set(r6)) == 6
    for r in (r5, r6):
        assert r[0] == COLORS["bull"] and r[-1] == COLORS["bear"]
        assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in r)
    assert r5[2] == COLORS["amber"]


def _load_tqs_ramps():
    """Pull SIGNAL_COLOUR / GRADE_COLOUR out of the page without running Streamlit."""
    import ast
    path = os.path.join(_ROOT, "dashboard", "pages", "18_tqs_scanner.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    wanted = {"_SIGNAL_ORDER", "_GRADE_ORDER", "SIGNAL_COLOUR", "GRADE_COLOUR"}
    body = [n for n in tree.body
            if isinstance(n, ast.Assign) and any(getattr(t, "id", None) in wanted for t in n.targets)]
    ns = {"ordinal_ramp": tokens.ordinal_ramp, "TOKENS": COLORS}
    exec(compile(ast.Module(body=body, type_ignores=[]), path, "exec"), ns)
    return ns["SIGNAL_COLOUR"], ns["GRADE_COLOUR"]


def test_tqs_ramps_all_distinct():
    signal, grade = _load_tqs_ramps()
    assert list(signal) == ["STRONG TREND", "TRENDING", "NEUTRAL", "WEAK", "AVOID"]
    assert list(grade) == ["A+", "A", "B", "C", "D", "F"]
    assert len(set(signal.values())) == len(signal)
    assert len(set(grade.values())) == len(grade)
    # Best/worst stay anchored on the signal tokens.
    assert signal["STRONG TREND"] == grade["A+"] == COLORS["bull"]
    assert signal["AVOID"] == grade["F"] == COLORS["bear"]
    # Readable on the dark ground: every shade clears 4.5:1 contrast.
    def _lum(h):
        def ch(c):
            c /= 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = hex_to_rgb(h)
        return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)
    bg = _lum(COLORS["surface"])
    for c in list(signal.values()) + list(grade.values()):
        assert (_lum(c) + 0.05) / (bg + 0.05) >= 4.5, c


def test_no_deprecated_components_html_in_shared():
    d = os.path.join(_ROOT, "dashboard", "shared")
    for f in os.listdir(d):
        if f.endswith(".py"):
            src = open(os.path.join(d, f), encoding="utf-8").read()
            assert "components.v1 as" not in src, f
            assert "components.html(" not in src, f


def test_read_sql_df_matches_shape():
    from trade_store import read_sql_df
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(1, "x"), (2, "y")])
    df = read_sql_df("SELECT * FROM t WHERE a >= ?", conn, params=(2,))
    assert list(df.columns) == ["a", "b"] and df.to_dict("records") == [{"a": 2, "b": "y"}]
    empty = read_sql_df("SELECT * FROM t WHERE a > 99", conn)
    assert list(empty.columns) == ["a", "b"] and empty.empty
