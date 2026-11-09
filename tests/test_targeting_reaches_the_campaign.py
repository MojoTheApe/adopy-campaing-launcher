"""Выбранный таргет доезжает до кампании.

Детальный таргетинг не ставился **никогда** — не иногда. Найдено 14.09 на
живом прогоне: телеметрия всех четырёх кампаний сообщала `skip_targeting` со
словами «в черновике нет интересов», при том что таргет в билдере выбран и его
код стоит в имени кампании.

Скрытое поле формы несло строку интересов, а сервер по этому полю искал таргет
в библиотеке, ожидая код:

    lookup('Foreign exchange market, Investor, …')  ->  ''
    lookup('PF')  ->  'Foreign exchange market, Investor, Investment, …'

Не нашлось — `targeting_label` очищался, и кампания поднималась без детального
таргетинга.

Пряталось это за тремя слоями молчания, и каждый по-своему выглядел
убедительно: имя кампании берётся из ВИДИМОГО поля поиска, поэтому `_PF_` в
имени казалось доказательством; сторож проверял «напечатано, а скрытое пусто»
— а скрытое было не пусто, просто не тем; обогащение черновика писало
предупреждение, а не ошибку, и публикацию не останавливало.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _picker() -> str:
    """Кусок шаблона с обоими путями выбора таргета."""
    page = TPL.read_text(encoding="utf-8")
    start = page.index("window.cbTargetingSearch = function(input)")
    end = page.index("window.cbTargetingCommit", start)
    return page[start:end]


def _click() -> str:
    """Выбор мышью: обработчик mousedown обязан звать cbTargetingPick (ACL-2393),
    а тело выбора живёт в нём; для проверок берём оба куска."""
    page = TPL.read_text(encoding="utf-8")
    picker = _picker()
    handler = picker[picker.index("opt.addEventListener('mousedown'"):]
    assert "cbTargetingPick(wrap, opt.dataset.code" in handler, (
        "мышь больше не идёт через общий выбор cbTargetingPick"
    )
    start = page.index("window.cbTargetingPick = function(")
    body = page[start: page.index("\n  };", start)]
    return body + "\n" + handler


def _commit() -> str:
    page = TPL.read_text(encoding="utf-8")
    start = page.index("window.cbTargetingCommit = function(input)")
    return page[start: page.index("\n  };", start)]


# ── ради чего всё ─────────────────────────────────────────────────────

def test_the_mouse_pick_puts_the_code_in_the_form():
    click = _click()
    assert "hidden.value = code" in click, (
        "в отправляемое поле снова уходит строка интересов — сервер по ней "
        "ничего не найдёт, и детальный таргетинг молча очистится"
    )
    assert "hidden.value = interest" not in click


def test_typing_and_committing_puts_the_code_too():
    """Второй путь — набрать и уйти из поля. Он обязан класть то же самое:
    два похожих места, кладущих разное, — это та же поломка, только реже."""
    commit = _commit()
    assert "hidden.value = hit[0].code" in commit
    assert "hidden.value = hit[0].fb_interest" not in commit


def test_the_hint_still_shows_the_interests():
    """Человеку интересы показывать надо — он по ним и узнаёт таргет. Но
    показывать и отправлять — разные вещи."""
    click = _click()
    assert "hint.textContent = interest" in click
    assert "hint.textContent = hit[0].fb_interest" in _commit()


def test_both_paths_agree():
    """Разойтись им нельзя: выбор мышью и набор — один и тот же выбор."""
    click = _click()
    commit = _commit()
    for src in (click, commit):
        sets = re.findall(r"hidden\.value = ([^;]+);", src)
        assert sets, "поле больше не заполняется"
        for value in sets:
            assert "interest" not in value, f"в поле уходит {value.strip()}"


# ── и это доходит до библиотеки ───────────────────────────────────────

def test_the_library_resolves_a_code_and_not_an_interest_string(monkeypatch, tmp_path):
    from src.analysis_agent import targeting_registry as tr

    monkeypatch.setattr(tr, "_store", lambda: tmp_path / "targeting.json")
    monkeypatch.setattr(tr, "pull", lambda **kwargs: {})
    target = tr.add("PF", "Foreign exchange market, Investor")
    assert tr.resolve_interests(target.name) == target.rpa_interests()
    assert tr.resolve_interests(target.rpa_interests()) == ""
