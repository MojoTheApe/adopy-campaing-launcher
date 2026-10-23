"""Country-report regressions; all browser connections and storage are isolated."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest
from src.analysis_agent import country_split as cs
from src.analysis_agent.account_spend import ProfileNotAttachable
from src.fb_ads_rpa.operations import browser_operation

DAY = "2026-09-10"
HEADER = ["Country", "Amount spent"]
COMPLETE = [HEADER, ["Peru", "USD 2.000"], ["Chile", "3.125 USD"],
            ["Total results 2/2 rows displayed", "USD 5.125"]]


@pytest.mark.parametrize("raw,expected", [
    ("-12.34 USD", (-12.34, "USD")), ("USD -12.34", (-12.34, "USD")),
    ("-$1,234.56", (-1234.56, "$")), ("KWD 1.234", (1.234, "KWD")),
    ("1 234.123 KWD", (1234.123, "KWD")), ("0", (0.0, "")),
    ("$0.00", (0.0, "$")), ("€12.00 EUR", (12.0, "EUR")),
    ("12.34 USD extra", (None, "")), ("4.00 campaigns", (None, "")),
    ("USD 1.0000", (None, "")), ("USD 1,23", (None, "")),
    ("EUR 1.00 GBP", (None, "")), ("--12.00", (None, "")),
    ("USD nan", (None, "")), ("12,34 EUR", (None, "")),
])
def test_whole_money_cells(raw, expected):
    assert cs.money_and_currency(raw) == expected


@pytest.mark.parametrize("matrix", [
    [["Campaign name", "Amount spent"], ["KW_demo", "$5.00"]],
    [HEADER], [HEADER, ["Peru", "—"]], [HEADER, ["Peru"]],
    [HEADER, ["Peru", "$5.00"], ["Chile", "loading"]],
    [HEADER, ["Peru", "$1.00"], ["Peru", "$2.00"]],
    [["Country", "Day", "Amount spent"], ["Peru", DAY, "$5.00"]],
    [["Country", "Age", "Amount spent"], ["Peru", "18-24", "$5.00"],
     ["Total results 1/1 rows displayed", "", "$5.00"]],
    [["Country", "Amount spent", "Amount spent"], ["Peru", "1", "2"]],
    [HEADER, ["Peru", "$5.00"], ["Total results 1/2 rows displayed", "$5.00"]],
    [HEADER, ["Peru", "$5.00"], ["Total results 1/1 rows displayed", "$7.00"]],
    [HEADER, ["Peru", "KWD 5.00"], ["Total results 1/1 rows displayed", "$5.00"]],
    [HEADER, ["Peru", "$5.00"], ["Total results", "$5.00"]],
    [HEADER, ["Peru", "0"], ["No data"]],
])
def test_unverified_report_is_not_empty_or_partial_success(matrix):
    rows, how = cs._verified_rows(matrix)
    assert rows == [] and how.startswith("refused:")


def test_complete_report_reconciles_three_decimal_amounts():
    rows, how = cs._verified_rows(COMPLETE)
    assert len(rows) == 2 and "reconciled" in how
    assert rows[1]["currency"] == "USD"


def test_zero_is_a_real_row_but_no_data_requires_an_explicit_marker():
    assert cs._verified_rows([HEADER, ["No data"]])[1].startswith("empty:")
    rows, how = cs._verified_rows([HEADER, ["Peru", "$0.00"], ["Total results 1/1 rows displayed", "$0.00"]])
    assert len(rows) == 1 and not how.startswith("empty:")


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "_store_path", lambda: tmp_path / "country_splits.json")
    entry = SimpleNamespace(ad_account_id="act_123", profile_id="550e8400-e29b-41d4-a716-446655440000")
    page = Mock()
    # Страница отвечает по существу вопроса, а не одинаково на любой: вход в
    # Reporting спрашивает её, отрисовалась ли она, не открыт ли запрос о
    # несохранённых изменениях и выбран ли вид отчёта. Ответ «таблица» на все
    # эти вопросы делал бы проверки зависимыми от порядка вызовов.
    answers = SimpleNamespace(table={"matrix": COMPLETE, "error": ""})

    def _evaluate(script, *args):
        if script is cs._MOUNTED_JS:
            return 9
        if script is cs._UNSAVED_DIALOG_JS:
            return False
        if script is cs._PICK_PIVOT_JS:
            return {"ok": True, "already": True}
        if script is cs._CUSTOMIZE_OPEN_JS:
            return True
        if script is cs._SECTION_CHECKBOX_JS:
            return {"ok": True, "was": args[0]["wanted"] if args else True}
        if script is cs._DATE_CONTROLS_JS:
            return ["This month: Sep 1, 2026 - Sep 15, 2026"]
        if script is cs._MARK_DATE_FIELDS_JS:
            return 2
        if isinstance(script, str) and "data-acl-date" in script:
            return ["Sep 10, 2026", "Sep 10, 2026"]
        return answers.table

    page.evaluate.side_effect = _evaluate
    conn = Mock(page=page)
    attach = Mock(return_value=conn)
    monkeypatch.setattr(cs, "_entry_for", lambda _: entry)
    from src.analysis_agent import account_spend
    monkeypatch.setattr(account_spend, "_entry_for", lambda _: entry)
    monkeypatch.setattr(cs, "_attach_only", attach)
    monkeypatch.setattr(cs, "_sleep", lambda _: None)
    monkeypatch.setattr(cs, "_act_from_url", lambda *a, **k: "act_123")
    monkeypatch.setattr(cs, "_read_status", lambda _: ("clear", ""))
    monkeypatch.setattr(cs, "_click_label", lambda *a, **k: True)
    monkeypatch.setattr(cs, "_set_checkbox", lambda *a, **k: True)
    monkeypatch.setattr(cs, "_shown_period_text", lambda _: "Sep 10, 2026")
    monkeypatch.setattr(cs, "_shoot", lambda *a: "synthetic.png")
    return SimpleNamespace(entry=entry, page=page, conn=conn, attach=attach, answers=answers)


def read():
    return cs.read_country_split("Demo", DAY)


def assert_closed(h):
    h.page.close.assert_called_once_with()
    h.conn.close.assert_called_once_with(keep_browser_open=True)


def test_read_uses_numeric_url_and_owned_tab(harness):
    out = read()
    assert out.state == "ok" and len(out.rows) == 2
    assert harness.page.goto.call_args.args[0] == cs.REPORTING + "?act=123&date=2026-09-10_2026-09-11"
    assert cs.get_split("Demo", DAY, "")["state"] == "ok"
    assert_closed(harness)


@pytest.mark.parametrize("act,start,end", [
    ("act_", DAY, DAY), ("123&act=456", DAY, DAY), ("123junk", DAY, DAY),
    ("123", "2026-02-30", DAY), ("123", DAY, "2026-09-09"),
    ("123", "20260910", DAY), ("123", DAY, "2026-9-11"),
])
def test_invalid_inputs_never_attach(harness, act, start, end):
    harness.entry.ad_account_id = act
    out = cs.read_country_split("Demo", start, end)
    assert out.state == "failed" and out.error
    harness.attach.assert_not_called()


def test_loading_header_retries_before_accepting_rows(harness):
    """Пока видно только заголовок — отчёт ещё грузится, читать рано.

    Считаются обращения **за таблицей**, а не все подряд: вход в Reporting
    задаёт странице и другие вопросы (отрисовалась ли, открыт ли запрос о
    несохранённых изменениях), и они к этой проверке отношения не имеют.
    """
    answers = [{"matrix": [HEADER], "error": ""}, {"matrix": COMPLETE, "error": ""}]
    grabs = []

    fixture_answers = harness.page.evaluate.side_effect

    def _evaluate(script, *args):
        if script is cs._GRAB_CELLS_JS:
            grabs.append(script)
            return answers[min(len(grabs), len(answers)) - 1]
        # На всё остальное отвечает подделка из фикстуры: пусть у этой
        # проверки будет одна забота — счёт обращений за таблицей.
        return fixture_answers(script, *args)

    harness.page.evaluate.side_effect = _evaluate
    assert read().state == "ok"
    assert len(grabs) == 2, f"за таблицей обратились {len(grabs)} раз"


@pytest.mark.parametrize("matrix,expected", [([HEADER], "failed"), ([HEADER, ["No data"]], "empty")])
def test_empty_vs_loading(harness, matrix, expected):
    harness.answers.table = {"matrix": matrix, "error": ""}
    out = read()
    assert out.state == expected
    if expected == "failed":
        assert out.shot and cs.get_split("Demo", DAY, DAY)["shot"]
    assert_closed(harness)


@pytest.mark.parametrize("failure", ["goto", "evaluate", "checkbox", "fresh_report", "account", "late_period"])
def test_browser_failures_are_structured_and_close_only_owned_tab(harness, monkeypatch, failure):
    if failure in ("goto", "evaluate"):
        getattr(harness.page, failure).side_effect = RuntimeError("synthetic failure")
    elif failure == "checkbox":
        monkeypatch.setattr(cs, "_set_section_checkbox", lambda *a: (False, "синтетический отказ"))
    elif failure == "fresh_report":
        monkeypatch.setattr(cs, "_click_label", lambda *a, **k: False)
    elif failure == "account":
        monkeypatch.setattr(cs, "_act_from_url", Mock(side_effect=["act_123", "act_456"]))
    else:
        monkeypatch.setattr(cs, "_shown_period_text", Mock(side_effect=["Sep 10, 2026", "Sep 9, 2026"]))
    out = read()
    assert out.state == "failed" and out.rows == [] and out.error and out.shot
    assert_closed(harness)


def test_failed_retry_replaces_cached_success_even_without_connection(harness):
    assert read().state == "ok"
    harness.attach.side_effect = ProfileNotAttachable("profile unavailable")
    assert read().state == "failed"
    assert cs.get_split("Demo", DAY, DAY)["state"] == "failed"


@pytest.mark.parametrize("broken", ["{broken", "[]"])
def test_corrupt_store_is_preserved_and_read_reports_storage_failure(harness, broken):
    cs._store_path().write_text(broken)
    out = read()
    assert out.state == "failed" and "Could not save" in out.error
    assert cs._store_path().read_text() == broken
    assert cs.get_split("Demo", DAY, DAY) is None
    assert_closed(harness)


def test_read_waits_for_the_existing_browser_operation_lock(harness):
    entered, done = threading.Event(), threading.Event()
    def run():
        entered.set()
        try:
            read()
        finally:
            done.set()
    with browser_operation:
        worker = threading.Thread(target=run)
        worker.start()
        assert entered.wait(1)
        assert not done.wait(0.1)
        harness.attach.assert_not_called()
    worker.join(2)
    assert done.is_set() and harness.attach.call_count == 1
