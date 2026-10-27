"""В билдере больше нет кнопки «⏸ Pause RPA».

Публикация идёт через Graph API изнутри профиля, ставить браузерного робота
на паузу с дешборда незачем — владелец попросил кнопку убрать (20.10).
Бэкенд `/rpa-takeover` остаётся: флаг паузы использует режим обучения,
а GET отдаёт его состояние.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "src" / "web" / "templates" / "report.html"


def test_template_has_no_pause_rpa_button() -> None:
    html = TPL.read_text(encoding="utf-8")
    assert "rpa-takeover-btn" not in html
    assert "rpaTakeover" not in html
    assert "Pause RPA" not in html


def test_takeover_endpoint_stays_for_teach_mode() -> None:
    app = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/rpa-takeover")' in app
    assert '@app.post("/rpa-takeover")' in app
