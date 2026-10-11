"""Шапка дешборда: что в ней стоит и чего в ней больше нет.

Раздел «Дизайн», задача «Поправляем Header». Проверки держат решения, которые
легко потерять при следующей правке шаблона.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _header() -> str:
    t = TPL.read_text(encoding="utf-8")
    return t[t.index('<header class="page-header">'): t.index("</header>")]


def test_the_two_values_are_labelled():
    """«Updated … · acl-release-…» читалось как одно смазанное сообщение.

    Обе величины нужны в один момент — когда обновление сломало работу и надо
    понять, что стоит и когда встало, чтобы откатиться.
    """
    h = _header()
    assert "Last updated:" in h, "исчезла подпись у времени обновления"
    assert "Version:" in h, "исчезла подпись у версии"


def test_the_manager_is_not_part_of_the_product_name():
    """Под логотипом стояло «CM Evgeni Sedin» — положение зрителя, а не свойство
    продукта. Осталось отдельным элементом сбоку."""
    h = _header()
    brand = h[h.index('class="head-meta"'): h.index("</dl>", h.index('class="brand-meta"'))]
    assert "my_cm" not in brand, "имя менеджера вернулось под логотип"
    assert "Logged as" in h, "исчезла подпись у выбора менеджера"
    assert ">Buyer<" not in h, "вернулась подпись Buyer"


def test_the_counters_are_gone():
    """`Trend 7d · 58 combos` — числа, за которыми никто не приходит в шапку."""
    h = _header()
    assert "trend_days" not in h, "счётчик тренда вернулся в шапку"
    assert "total_combos" not in h, "счётчик комбинаций вернулся в шапку"


def test_room_is_kept_for_the_theme_switch():
    """Место было занято заранее, и переключатель встал в него, ничего не сдвинув."""
    assert "theme-switch" in _header(), "исчез переключатель темы из шапки"


def test_the_routine_csv_line_is_not_a_warning():
    """Полоса «Added 13 account(s)…» висела всегда и приучала не читать это
    место. Настоящее предупреждение, встав туда же, тоже осталось бы
    непрочитанным."""
    src = (ROOT / "src" / "analysis_agent" / "morning_service.py").read_text(encoding="utf-8")
    body = src[src.index("if new_from_csv:"):]
    body = body[: body.index("assert enriched_df is not None")]
    assert "warnings.append" not in body, "обычная работа снова объявлена предупреждением"


def test_the_logo_is_the_blue_adopy_mark():
    """ACL-1705: знак — плоская синяя метка Adopy «A» (#2563eb), без плитки и текста.

    Прежний знак с `<text>`-буквами «ACL» зависел от шрифта Segoe UI; новый —
    чистая геометрия, поэтому проверяем, что текста в нём нет.
    """
    svg = (ROOT / "src" / "web" / "static" / "logo.svg").read_text(encoding="utf-8")
    assert "#2563eb" in svg and "<text" not in svg and "<rect" not in svg
    assert 'aria-label="Adopy Campaign Manager"' in svg


def test_the_header_shows_the_logo_only():
    """ACL-1723: под знаком нет видимой подписи; имя продукта — в alt у логотипа.

    Слова Launcher в шапке по-прежнему нет, шрифт подписи больше не грузится.
    """
    head = _header()
    assert 'brand-name' not in head, "под знаком вернулась подпись"
    assert "<h1" not in head, "в шапке появился заголовок-подпись"
    assert re.search(r'<img[^>]*class="brand-logo"[^>]*alt="Adopy Campaign Manager"|'
                     r'<img[^>]*alt="Adopy Campaign Manager"[^>]*class="brand-logo"', head)
    assert "Launcher" not in head
    tpl = TPL.read_text(encoding="utf-8")
    assert "ACM Inter" not in tpl, "неиспользуемый @font-face вернулся"


# ── ACL-1778: Linear toolbar, период в рамке ─────────────────────────────

def _render_header(**ctx):
    """Шапка, отрисованная настоящим окружением шаблонов, с минимумом данных."""
    from src.web.app import templates

    t = TPL.read_text(encoding="utf-8")
    frag = t[t.index("{%- set rel = installed_release or {} %}"): t.index("</header>") + 9]
    base = {"report": {"date": "2026-10-10", "source_excel": "x.xlsx", "my_cm": "Evgeni"},
            "installed_release": {"short": "abc123"}, "built_at": "2026-10-10 20:29:11"}
    base.update(ctx)
    return templates.env.from_string(frag).render(**base)


def _period_text(html: str) -> str:
    seg = html[html.index('class="period-text"'):]
    seg = seg[: seg.index("</button>")]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", seg.split(">", 1)[1])).strip()


def test_the_period_reads_as_full_english_dates():
    """«Oct 1, 2026 – Oct 9, 2026»: оба года, без пересчёта часовым поясом."""
    html = _render_header(period={"date_from": "2026-10-01", "date_to": "2026-10-09", "span": ""})
    assert _period_text(html) == "Oct 1, 2026 – Oct 9, 2026"
    assert 'aria-label="Date range: Oct 1, 2026 – Oct 9, 2026"' in html
    assert 'aria-expanded="false"' in html and "Date range</span>" in html


def test_the_period_across_a_year_and_a_single_day():
    cross = _render_header(period={"date_from": "2025-12-30", "date_to": "2026-01-02", "span": ""})
    assert _period_text(cross) == "Dec 30, 2025 – Jan 2, 2026"
    one = _render_header(period={"date_from": "2026-02-28", "date_to": "2026-02-28", "span": ""})
    assert _period_text(one) == "Feb 28, 2026"
    whole = _render_header(period={"date_from": "", "date_to": "", "span": "all"})
    assert _period_text(whole) == "All dates"
    nothing = _render_header()
    assert _period_text(nothing) == "All dates"


def test_last_updated_is_not_the_period():
    """Выбранный период и время обновления данных — разные величины."""
    html = _render_header(period={"date_from": "2026-10-01", "date_to": "2026-10-09", "span": ""})
    meta = html[html.index('class="head-meta"'):]
    assert "2026-10-10 20:29:11" in meta and "Oct 1, 2026" not in meta


def test_every_header_control_keeps_its_id():
    """Скрипты страницы находят кнопки шапки по id — новая раскладка их не меняет."""
    h = _header()
    for ident in ("period-btn", "amap-dates", "buyer-select", "buyer-msg", "robot-health-link",
                  "report-problem-btn", "acl-update-btn", "theme-toggle", "dash-refresh-btn",
                  "acl-version"):
        assert f'id="{ident}"' in h, ident
    assert 'onclick="amapCalendar(this)"' in h
    assert 'action="/refresh"' in h and 'onsubmit="return dashRefresh(event)"' in h
    assert 'href="/robot-health"' in h


def test_no_prototype_leftovers():
    """Из макета не переехали демо-данные, переключатель вариантов и заглушки."""
    t = TPL.read_text(encoding="utf-8")
    for junk in ("prototype-switcher", "study-bar", "design-linear", "Demo buyer", "demo-build",
                 "Preview only", "date-label"):
        assert junk not in t, junk


def test_the_picker_closes_through_one_path():
    """Ревью ACL-1778: Update тем же адресом — переход по якорю без
    перезагрузки; закрытие окна обязано сбросить aria-expanded кнопки."""
    t = TPL.read_text(encoding="utf-8")
    apply = t[t.index("function amapApply"):]
    apply = apply[: apply.index("\n}\n")]
    assert "_amapClose" in apply
    cal = t[t.index("function amapCalendar"): t.index("function amapApply")]
    assert "box.setAttribute('role', 'dialog')" in cal


def test_a_half_open_period_and_a_strange_value():
    """Ревью ACL-1778: только начало или только конец — «…» на месте второй
    даты; нечитаемое значение из адреса показывается как есть и экранируется."""
    only_from = _render_header(period={"date_from": "2026-10-01", "date_to": "", "span": ""})
    assert _period_text(only_from) == "Oct 1, 2026 – …"
    only_to = _render_header(period={"date_from": "", "date_to": "2026-10-09", "span": ""})
    assert _period_text(only_to) == "… – Oct 9, 2026"
    odd = _render_header(period={"date_from": '"><b>x', "date_to": '"><b>x', "span": ""})
    assert "<b>x" not in odd and "&#34;&gt;&lt;b&gt;x" in odd
