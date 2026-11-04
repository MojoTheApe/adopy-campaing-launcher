"""ACL-2066: CPL виден в пресете Optimization (единственный набор колонок)."""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "_campaign_table.html").read_text(encoding="utf-8")


def test_optimization_preset_shows_cpl_before_cpa():
    m = re.search(r"optimization: CTRL\.concat\(DECIDE, \[([^\]]*)\]", SRC)
    assert m, "optimization preset not found"
    cols = re.findall(r"'([a-z_]+)'", m.group(1))
    assert "b_cpl" in cols
    assert cols.index("b_cpl") == cols.index("b_cpa") - 1


def test_saved_column_layout_is_not_read_any_more():
    """Выбор колонок убран (04.11): сохранённая раскладка не читается, CPL приходит из пресета Optimization."""
    assert "cpl_seen" not in SRC and "localStorage.getItem(STORE_KEY" not in SRC
