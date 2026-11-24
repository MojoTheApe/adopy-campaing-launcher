"""ACL-2622: флаги стран слева от названия — файлы, фильтр и разметка."""
from pathlib import Path

from src.analysis_agent.country_names import COUNTRY_CODES
from src.web import app as web_app

ROOT = Path(__file__).resolve().parents[1]
FLAGS = ROOT / "src" / "web" / "static" / "flags"


def test_every_mapped_country_has_a_flag_file():
    missing = sorted({c.lower() for c in COUNTRY_CODES.values()
                      if not (FLAGS / f"{c.lower()}.svg").is_file()})
    assert not missing, f"no flag file for: {missing}"


def test_flag_code_accepts_name_and_code():
    assert web_app._flag_code("Kuwait") == "kw"
    assert web_app._flag_code("  saudi arabia ") == "sa"
    assert web_app._flag_code("KW") == "kw"
    assert web_app._flag_code("United Arab Emirates") == "ae"


def test_flag_code_unknown_is_empty_not_a_guess():
    assert web_app._flag_code("Atlantis") == ""
    assert web_app._flag_code("") == ""
    assert web_app._flag_code(None) == ""


def test_flags_are_local_files_not_a_cdn():
    # CSP дешборда: img-src 'self' data: blob: — внешние картинки не грузятся.
    html = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    home = (ROOT / "src" / "web" / "templates" / "_home_accounts.html").read_text(encoding="utf-8")
    for text in (html, home):
        assert "flagcdn" not in text
        assert "/static/flags/" in text
