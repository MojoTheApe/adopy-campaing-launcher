# -*- coding: utf-8 -*-
"""acl-357: пострановая разбивка — всё, что останавливается до браузера."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis_agent.country_split import (
    CountrySplit,
    money_and_currency,
    rows_from_cells,
)


# ── money_and_currency ─────────────────────────────────────────────────

def test_dollar_amount_keeps_the_sign_meta_wrote():
    # $ пишут за десяток валют. Код не выдумываем, но и знак не теряем:
    # решение владельца 15.09 — показывать то, что стоит в кабинете.
    assert money_and_currency("$1,234.56") == (1234.56, "$")


def test_three_letter_code_wins():
    assert money_and_currency("KWD 12.00") == (12.0, "KWD")


def test_euro_sign_stays_a_sign():
    assert money_and_currency("€45.50") == (45.5, "€")


def test_a_code_written_next_to_the_sign_wins():
    """«€12.00 EUR» — одна валюта, записанная дважды; код точнее знака."""
    assert money_and_currency("€12.00 EUR") == (12.0, "EUR")


def test_a_sign_and_a_different_code_is_a_refusal():
    assert money_and_currency("€1.00 GBP") == (None, "")


def test_campaign_count_is_not_money():
    # Ловушка из _first_money: «4» из «4 campaigns» деньгами не считается.
    assert money_and_currency("Results from 4 campaigns")[0] is None


def test_bare_decimal_counts_as_money():
    assert money_and_currency("31.47")[0] == 31.47


def test_empty_and_dash_are_not_money():
    assert money_and_currency("")[0] is None
    assert money_and_currency("—")[0] is None


# ── rows_from_cells ────────────────────────────────────────────────────

HEADER = ["Country", "Reach", "Impressions", "Amount spent", "Cost per result"]


def test_rows_extracted_by_header_words():
    matrix = [
        ["Untitled report"],
        HEADER,
        ["Kuwait", "2,788", "3,162", "$23.89", "$0.04"],
        ["Saudi Arabia", "5,998", "6,740", "KWD 21.61", "$0.45"],
        ["Total results 2/2 rows displayed", "8,845", "9,902", "$45.50", ""],
    ]
    rows, how = rows_from_cells(matrix)
    assert rows == [
        {"country": "Kuwait", "spend": 23.89, "currency": "$"},
        {"country": "Saudi Arabia", "spend": 21.61, "currency": "KWD"},
    ]
    assert "header row 1" in how


def test_multiple_totals_are_ambiguous():
    matrix = [
        HEADER,
        ["Total results", "1", "2", "$9.99", ""],
        ["All", "1", "2", "$9.99", ""],
        ["Peru", "1", "2", "$9.99", ""],
    ]
    rows, how = rows_from_cells(matrix)
    assert rows == [] and "multiple totals" in how


def test_country_without_amount_is_skipped_not_zeroed():
    matrix = [HEADER, ["Chile", "0", "0", "—", "—"]]
    rows, _ = rows_from_cells(matrix)
    assert rows == []  # «—» — это не ноль, строки нет вовсе


def test_missing_header_is_a_named_failure():
    rows, how = rows_from_cells([["something"], ["else entirely"]])
    assert rows == []
    assert "not found" in how


def test_column_order_does_not_matter():
    matrix = [
        ["Amount spent", "Country"],
        ["$5.00", "Ecuador"],
    ]
    rows, _ = rows_from_cells(matrix)
    assert rows == [{"country": "Ecuador", "spend": 5.0, "currency": "$"}]


def test_short_rows_do_not_crash():
    matrix = [HEADER, ["Kuwait"], []]
    rows, _ = rows_from_cells(matrix)
    assert rows == []


# ── CountrySplit shape ─────────────────────────────────────────────────

def test_split_to_dict_carries_reading_shape():
    s = CountrySplit(account="A", date_from="2026-09-01", date_to="2026-09-10")
    d = s.to_dict()
    # Форма как у SpendReading (acl-108b): кто зовёт, не отличает маршрут.
    for key in ("account", "date_from", "date_to", "source", "state", "how", "error"):
        assert key in d
    assert d["state"] == "failed" and d["rows"] == []


from src.analysis_agent import country_split as cs


# ── вход в Reporting: живой прогон 15.09 ──────────────────────────────────
#
# Переход по нашему адресу приводит не на список отчётов: Reporting сам
# открывает последний (`/reporting/manage` → `/reporting/view`), а «Create
# report» есть только на списке. Уход перехватывается диалогом «You have
# unsaved changes». Оба шага проверяются здесь без браузера: логика пути —
# обычный Python, и ей незачем ждать живого кабинета.


class _FakePage:
    """Страница, которая отвечает на те же три вопроса, что и настоящая."""

    def __init__(self, url, *, labels=(), dialog=False, lands=None, mounted=9):
        self.url = url
        self._labels = set(labels)
        self._dialog = dialog
        self._lands = lands
        self._mounted = mounted
        self.clicked: list[str] = []

    def evaluate(self, script, arg=None):
        if script is cs._MOUNTED_JS:
            return self._mounted
        if script is cs._UNSAVED_DIALOG_JS:
            return self._dialog
        if script is cs._CLICK_LABEL_JS:
            if arg not in self._labels:
                return False
            self.clicked.append(arg)
            if arg == "Don't save":
                self._dialog = False
            # Страница уходит на список, когда отвечать больше не на что:
            # диалог закрыт (или его и не было), а «All reports» нажата.
            if self._lands and not self._dialog and "All reports" in self.clicked:
                self.url = self._lands
            return True
        raise AssertionError("неожиданный скрипт")


def test_a_page_already_on_the_list_is_left_alone(monkeypatch):
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    page = _FakePage("https://adsmanager.facebook.com/adsmanager/reporting/manage?act=1")
    reached, note = cs._leave_open_report(page)
    assert reached is True
    assert page.clicked == [], "на списке отчётов нажимать нечего"


def test_an_open_report_is_left_without_saving(monkeypatch):
    """«Save» оставил бы на кабинете байера сохранённый отчёт."""
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    page = _FakePage(
        "https://adsmanager.facebook.com/adsmanager/reporting/view?act=1",
        labels={"All reports", "Don't save"},
        dialog=True,
        lands="https://adsmanager.facebook.com/adsmanager/reporting/manage?act=1",
    )
    reached, note = cs._leave_open_report(page)
    assert reached is True, note
    assert "All reports" in page.clicked and "Don't save" in page.clicked
    assert "Save" not in page.clicked, "сохранили чужой отчёт на кабинете"
    assert "changes not saved" in note


def test_a_report_we_cannot_leave_is_refused(monkeypatch):
    """Остаться в чужом отчёте и молча читать его — хуже отказа."""
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    page = _FakePage("https://adsmanager.facebook.com/adsmanager/reporting/view?act=1")
    reached, note = cs._leave_open_report(page)
    assert reached is False
    assert "All reports" in note


def test_an_unanswered_dialog_is_refused(monkeypatch):
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    page = _FakePage(
        "https://adsmanager.facebook.com/adsmanager/reporting/view?act=1",
        labels={"All reports"},
        dialog=True,
    )
    reached, note = cs._leave_open_report(page)
    assert reached is False
    assert "unsaved-changes" in note


def test_waiting_for_the_interface_not_for_seconds(monkeypatch):
    """Сразу после перехода на странице одна кнопка — «Ads Manager»."""
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    slow = _FakePage("https://x/reporting/view", mounted=1)
    assert cs._wait_mounted(slow, need=6, tries=3) == 1, "посчитали неотрисованную страницу готовой"
    ready = _FakePage("https://x/reporting/view", mounted=12)
    assert cs._wait_mounted(ready, need=6, tries=3) == 12


def test_an_open_customize_panel_is_not_toggled_shut(monkeypatch):
    """«Customize» — переключатель, и на свежем отчёте панель уже открыта.

    15.09 клик по ней панель **закрыл**, и галочки после этого искать было
    негде. Тот же класс ошибки, что клик по «Show more settings» на уже
    раскрытом разделе площадок: успехом считали нажатие, а не результат.
    """
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    clicks = []
    monkeypatch.setattr(cs, "_click_label", lambda *a, **k: clicks.append(a[1]) or True)

    page = _FakePage("https://x/reporting/view", mounted=9)
    page.evaluate = lambda script, arg=None: True if script is cs._CUSTOMIZE_OPEN_JS else None
    assert cs._ensure_customize_open(page) is True
    assert clicks == [], "закрыли уже открытую панель"


def test_a_closed_customize_panel_is_opened_and_checked(monkeypatch):
    """Закрытую — открываем, но успехом считаем открытую панель, а не клик."""
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    state = {"open": False}

    def _click(page, label, tries=10, **kw):
        if label == "Customize":
            state["open"] = True
        return True

    monkeypatch.setattr(cs, "_click_label", _click)
    page = _FakePage("https://x/reporting/view", mounted=9)
    page.evaluate = lambda script, arg=None: state["open"] if script is cs._CUSTOMIZE_OPEN_JS else None
    assert cs._ensure_customize_open(page) is True
    assert state["open"] is True


def test_a_panel_that_never_opens_is_refused(monkeypatch):
    """Не открылась — отказ, а не молчаливый проход к галочкам, которых нет."""
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    monkeypatch.setattr(cs, "_click_label", lambda *a, **k: True)
    page = _FakePage("https://x/reporting/view", mounted=9)
    page.evaluate = lambda script, arg=None: False if script is cs._CUSTOMIZE_OPEN_JS else None
    assert cs._ensure_customize_open(page, tries=2) is False


# ── выставление периода ───────────────────────────────────────────────────
#
# Через адрес период не задать: параметр `date` Meta выбрасывает. Панель
# открывается кнопкой, в ней два поля с маской и кнопка «Update».
#
# Эти проверки закрывают дыру, найденную 15.09 живым прогоном: `_set_period`
# не вызывался ни в одной проверке (в подделке период сразу совпадал), и
# опечатка в его коде — обращение к удалённой переменной — дошла до живого
# кабинета вместо того, чтобы упасть здесь.


class _DatePage:
    """Панель периода: кнопка, два поля с маской, «Update»."""

    def __init__(self, *, fields=2, mask=False, control="This month: Sep 1, 2026 - Sep 15, 2026"):
        self.url = "https://x/reporting/view"
        self.values = ["Sep 1, 2026", "Sep 15, 2026"]
        self.typed: list[str] = []
        self.clicked: list[str] = []
        self.updated = False
        self._fields = fields
        self._mask = mask
        self._control = control
        self.keyboard = self

    # --- страница ---
    def evaluate(self, script, arg=None):
        if script is cs._DATE_CONTROLS_JS:
            return [self._control, "Customize"]
        if script is cs._MARK_DATE_FIELDS_JS:
            return self._fields
        if script is cs._CLICK_LABEL_JS:
            label = arg["label"] if isinstance(arg, dict) else arg
            self.clicked.append(label)
            if label == "Update":
                self.updated = True
            return True
        if isinstance(script, str) and "data-acl-date" in script:
            return list(self.values)
        raise AssertionError(f"неожиданный скрипт: {script!r}")

    def click(self, selector):
        self._slot = 0 if 'from' in selector else 1

    # --- клавиатура ---
    def press(self, key):
        if key in ("Control+A", "Delete") and not self._mask:
            self.values[self._slot] = ""

    def type(self, text, delay=0):
        self.typed.append(text)
        # Поле с маской дописывает к прежнему значению — так и было живьём.
        self.values[self._slot] = (self.values[self._slot] + text) if self._mask else text


def test_the_period_is_typed_in_the_americanised_format(monkeypatch):
    """Поле показывает «Sep 1, 2026», а принимает `9/15/2026`."""
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    page = _DatePage()
    ok, note = cs._set_period(page, "2026-08-16", "2026-09-15")
    assert ok is True, note
    assert page.typed == ["8/16/2026", "9/15/2026"]
    assert page.updated is True, "период не подтверждён кнопкой «Update»"


def test_a_masked_field_that_appends_is_refused(monkeypatch):
    """`fill` дописывал к прежнему: «Sep 1, 2026» + «8/16/2026» → `Sep 1, 202688`.

    Молча принять такое — значит прочитать чужой период и выдать его за
    запрошенный.
    """
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    page = _DatePage(mask=True)
    ok, note = cs._set_period(page, "2026-08-16", "2026-09-15")
    assert ok is False
    assert "the period fields show" in note and page.updated is False


def test_a_period_panel_without_two_fields_is_refused(monkeypatch):
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    page = _DatePage(fields=1)
    ok, note = cs._set_period(page, "2026-08-16", "2026-09-15")
    assert ok is False and "found 1 period fields" in note


def test_no_period_button_is_refused(monkeypatch):
    monkeypatch.setattr(cs, "_sleep", lambda *a, **k: None)
    page = _DatePage(control="Customize")
    ok, note = cs._set_period(page, "2026-08-16", "2026-09-15")
    assert ok is False and "period button not found" in note
