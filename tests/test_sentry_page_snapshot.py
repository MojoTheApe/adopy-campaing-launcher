# -*- coding: utf-8 -*-
"""К событию Sentry о блокирующем отказе едет вычищенный раздел страницы (ACL-1325).

Решение владельца 26.09: к событиям о блокирующем отказе и об остановке
кампании прикладывается копия карточки упавшего шага (DOM раздела) — без
номеров кабинетов, токенов, адресов с id и личных данных. Скриншоты — нет:
на них имена кабинета, кампании и страницы.

Проверяется здесь:

* вычистка — на разметке из `tests/fixtures/ads_manager`, в которую
  подсажены имена прогона, номера, адреса, почта, телефон, токены, скрипты;
  ничего из подсаженного не должно доехать, а устройство карточки (роли,
  data-surface, подписи) — должно;
* потолок размера и закрытые теги при обрезке;
* выбор карточки на странице — в офлайн-Chromium, по той же разметке;
* вложение есть у блокирующего отказа и у остановки кампании, его нет у
  чистого прогона и при выключенной настройке `ACL_SENTRY_PAGE_SNAPSHOT`;
* до транспорта Sentry вложение доходит настоящим SDK.
"""
from __future__ import annotations

import json
import os
import re
import sys
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import page_capture as pc  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import sentry_signal  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "ads_manager"
SUGGEST = (FIXTURES / "audience_suggest_layout_2026_09_26.html").read_text(encoding="utf-8")
FRAG = {m.group(1): m.group(2).strip() for m in re.finditer(
    r"<!-- fragment: (\w+) \| source: [^>]*-->\n(.*?)(?=\n<!-- fragment: |\Z)", SUGGEST, re.S)}

CAMPAIGN = "CO_bnc_All_Video_FX_35_26.09"
ACCOUNT = "FacebookDoc6540 Ivanna Petrenko"
AD_SET = "CO_bnc adset Bogota"
AD_NAME = "2046 1 - Text6"

#: Всё, что не должно доехать до Sentry ни в каком виде.
SECRETS = [
    CAMPAIGN, "FacebookDoc6540", "Ivanna", "Petrenko", AD_SET, AD_NAME,
    "1234567890", "987654321", "555666777", "4455667788",
    "ivanna.petrenko@example.com", "123-4567", "(555)",
    "https://adsmanager.facebook.com", "scontent.xx.fbcdn", "fbcdn",
    "secret-token-value", "EAAB", "window.__data", "color:red", "x1y2z3",
    "typed secret value", "onclick", "data-testid", "leak-in-comment",
    "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
]


def _seeded(fragment: str) -> str:
    """Карточка из фикстуры с подсаженными именами и номерами прогона."""
    seeded = (
        f'<div class="x1y2z3" style="color:red" data-testid="card" onclick="steal()"'
        f' aria-label="Campaign {CAMPAIGN}">'
        f'<script>window.__data = {{"token": "secret-token-value", "act": "1234567890"}};</script>'
        f'<style>.x1y2z3 {{ color:red }}</style>'
        f'<!-- leak-in-comment -->'
        f'<svg viewBox="0 0 10 10"><path d="M0 0L10 10"/><title>{CAMPAIGN}</title></svg>'
        f'<img src="https://scontent.xx.fbcdn.net/v/t1/1234567890_n.jpg?token=EAAB123">'
        f'<span id="js_4f" role="heading" aria-level="4">{CAMPAIGN} / {ACCOUNT}</span>'
        f'<div aria-labelledby="js_4f">Ad set: {AD_SET}; ad: {AD_NAME}</div>'
        f'<a href="https://adsmanager.facebook.com/adsmanager?act=1234567890&business_id=987654321">'
        f'open https://adsmanager.facebook.com/adsmanager?act=1234567890&amp;business_id=987654321</a>'
        f'<div>Page id=555666777, asset_id=4455667788, uuid 3f2504e0-4f89-11d3-9a0c-0305e82c3301</div>'
        f'<div>Contact ivanna.petrenko@example.com or +1 (555) 123-4567</div>'
        f'<input type="text" value="typed secret value" placeholder="Search languages">'
        f'<input type="checkbox" checked aria-label="Use as a suggestion">'
        + fragment +
        '</div>'
    )
    return seeded


def _scrub(html: str, **kw) -> str:
    body, _cut = pc.scrub_section_html(
        html, campaign=CAMPAIGN, account=ACCOUNT, names=[AD_SET, AD_NAME], **kw)
    return body


def _assert_clean(body: str) -> None:
    # Свои комментарии вложения (заголовок и границы частей) — не из страницы.
    body = re.sub(r"<!-- (?:ACL section snapshot|part:)[^\n]*-->\n", "", body)
    lowered = body.lower()
    for secret in SECRETS:
        assert secret.lower() not in lowered, secret
    assert not re.search(r"\d{6,}", body)
    assert "<script" not in lowered and "<style" not in lowered and "<svg" not in lowered
    assert not re.search(r"\s(class|style|href|src|value|onclick|data-testid)=", body)
    assert "<!--" not in body


class _Balance(HTMLParser):
    """Открытые теги минус закрытые — у вычищенной разметки должно быть 0."""

    VOID = {"area", "br", "col", "hr", "img", "input", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.broken = False

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.broken = True


def _balanced(body: str) -> bool:
    parser = _Balance()
    parser.feed(body)
    parser.close()
    return not parser.broken and not parser.stack


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv(pc.SECTION_SETTING, raising=False)
    pc.forget_sections()
    rt.clear_current_run()
    yield
    pc.forget_sections()
    rt.clear_current_run()


# ── Вычистка ────────────────────────────────────────────────────────────────

def test_nothing_identifying_survives_the_scrub_of_a_real_audience_card():
    body = _scrub(_seeded(FRAG["suggest_fx"]))
    _assert_clean(body)
    # Устройство карточки на месте — по нему и пишется селектор.
    assert 'role="heading"' in body
    assert "Suggest an audience" in body
    assert "Further limit the reach of your ads" in body
    assert 'placeholder="Search languages"' in body
    assert "checked" in body
    # Имена прогона — заглушками, а не пустотой: видно, что там было имя.
    assert "&lt;campaign&gt;" in body and "&lt;account&gt;" in body and "&lt;name&gt;" in body
    assert "&lt;email&gt;" in body and "&lt;phone&gt;" in body and "&lt;url&gt;" in body
    # Служебный id рендера заменён, но ссылка на него осталась связанной.
    assert 'id="syn_1"' in body and 'aria-labelledby="syn_1"' in body
    assert "js_4f" not in body
    assert _balanced(body)


@pytest.mark.parametrize("fixture", sorted(p.name for p in FIXTURES.glob("*.html")))
def test_every_recorded_fixture_scrubs_clean(fixture):
    raw = (FIXTURES / fixture).read_text(encoding="utf-8")
    body = _scrub(_seeded(raw))
    _assert_clean(body)
    assert len(body.encode("utf-8")) <= pc.SECTION_MAX_BYTES
    assert _balanced(body)


def test_data_surface_keeps_its_path_but_not_its_ids():
    body = _scrub('<div data-surface="/am/editor_drawer/AYMTAMGlobalBannerContainer/1234567890123">x</div>')
    assert 'data-surface="/am/editor_drawer/AYMTAMGlobalBannerContainer/&lt;digits&gt;"' in body


def test_short_numbers_that_the_steps_depend_on_stay():
    # Возраст, бюджет, счётчики — не опознавательные знаки, по ним и чинят.
    body = _scrub("<div>Age 18 - 65+</div><div>$35.00</div><div>0/250</div><div>Included: 16</div>")
    for kept in ("18 - 65+", "$35.00", "0/250", "Included: 16"):
        assert kept in body


def test_the_snapshot_is_capped_and_says_so():
    big = _seeded(FRAG["suggest_fx"]) * 60
    assert len(big.encode("utf-8")) > pc.SECTION_MAX_BYTES
    body, cut = pc.scrub_section_html(big, campaign=CAMPAIGN, account=ACCOUNT)
    assert cut is True
    assert len(body.encode("utf-8")) <= pc.SECTION_MAX_BYTES
    assert _balanced(body)


def test_a_long_text_is_cut():
    body = _scrub("<div>" + "word " * 2000 + "</div>")
    assert len(body) < pc.SECTION_TEXT_MAX + 40


def test_the_step_names_its_card():
    assert pc.section_titles_for("further_limit_reach_radio") == ("audience",)
    assert pc.section_titles_for("advantage_plus_audience") == ("audience",)
    assert pc.section_titles_for("audience_location") == ("audience",)
    assert pc.section_titles_for("languages") == ("audience",)
    assert pc.section_titles_for("conversion_website") == ("conversion",)
    assert pc.section_titles_for("placements") == ("placements",)
    assert pc.section_titles_for("ad2_copy") == ("ad creative", "ad setup")
    assert pc.section_titles_for("ad3_creative_upload") == ("ad creative", "ad setup")
    assert pc.section_titles_for("destination_sweep") == ("destination",)
    assert pc.section_titles_for("ad2_destination") == ("destination",)
    assert pc.section_titles_for("mystery_step") == ()


# ── Выбор карточки на странице (офлайн-Chromium) ────────────────────────────

_PAGE = """
<div data-surface="/am/editor_drawer">
  <div style="width:700px">{audience}</div>
  <div><div role="heading" aria-level="3">Placements</div><div>PLACEMENTS BODY</div></div>
  <div><div role="heading" aria-level="3">Conversion</div><div>CONVERSION BODY</div></div>
</div>
<div role="dialog"><div role="heading" aria-level="1">Notifications</div><div>NOTIFICATIONS BODY</div></div>
<div role="dialog" aria-label="Reach your preferred audiences">
  <div role="heading" aria-level="2">Reach your preferred audiences</div>
  <span role="radio" aria-checked="false">Further limit the reach of your ads</span>
</div>
"""


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def test_the_failing_steps_card_and_the_open_dialog_are_taken(page):
    page.set_content(_PAGE.format(audience=_seeded(FRAG["suggest_fx"])))
    assert pc.remember_section(page, step="further_limit_reach_radio", run_id="r1", campaign=CAMPAIGN)
    att = pc.section_attachment("further_limit_reach_radio", "r1", account=ACCOUNT,
                                names=[AD_SET, AD_NAME])
    assert att is not None
    assert att["context"]["parts"] == ["audience", "dialog"]
    body = att["bytes"].decode("utf-8")
    _assert_clean(body)
    assert "Suggest an audience" in body and "Reach your preferred audiences" in body
    # Чужие карточки и панель уведомлений не едут.
    assert "PLACEMENTS BODY" not in body and "CONVERSION BODY" not in body
    assert "NOTIFICATIONS BODY" not in body
    assert "screenshots are never sent" in body


def test_an_unknown_step_falls_back_to_the_editor_under_the_cap(page):
    page.set_content(_PAGE.format(audience=FRAG["suggest_fx"]))
    assert pc.remember_section(page, step="mystery_step", run_id="r1", campaign=CAMPAIGN)
    att = pc.section_attachment("mystery_step", "r1")
    assert att["context"]["parts"][0] == "editor"
    assert len(att["bytes"]) <= pc.SECTION_MAX_BYTES
    assert "PLACEMENTS BODY" in att["bytes"].decode("utf-8")


# ── Вложение у события ──────────────────────────────────────────────────────

class _FakePage:
    """Страница, которая отдаёт карточку как `_SECTION_JS` — без браузера."""

    def __init__(self, html: str, name: str = "audience"):
        self.parts = [{"name": name, "html": html}]
        self.calls = 0

    def evaluate(self, _js, _arg):
        self.calls += 1
        return self.parts


@pytest.fixture
def events(monkeypatch):
    """Приёмник-двойник на месте `sentry_sdk`: событие и его вложения."""
    import sentry_sdk

    out: list[dict] = []

    class Scope:
        def __init__(self):
            self.fingerprint = None
            self.tags: dict = {}
            self.contexts: dict = {}
            self.attachments: list[dict] = []

        def set_tag(self, key, value):
            self.tags[key] = value

        def set_context(self, key, value):
            self.contexts[key] = value

        def add_attachment(self, **kw):
            self.attachments.append(kw)

    holder: dict = {}

    @contextmanager
    def new_scope():
        holder["scope"] = Scope()
        yield holder["scope"]

    def capture_message(message, **_kw):
        scope = holder["scope"]
        event = acl_sentry._before_send({
            "message": message, "fingerprint": scope.fingerprint,
            "tags": scope.tags, "contexts": scope.contexts,
        }, {})
        event["attachments"] = scope.attachments
        out.append(event)
        return f"event-{len(out)}"

    monkeypatch.setattr(acl_sentry, "init", lambda: True)
    monkeypatch.setattr(sentry_sdk, "new_scope", new_scope)
    monkeypatch.setattr(sentry_sdk, "capture_message", capture_message)
    return out


def _fail_step(step: str, run_id: str = "r1") -> None:
    rt.set_current_run(run_id, CAMPAIGN)
    assert pc.remember_section(_FakePage(_seeded(FRAG["suggest_fx"])), step=step,
                               run_id=run_id, campaign=CAMPAIGN)


def test_a_blocking_failure_event_carries_the_scrubbed_section(events):
    _fail_step("audience_location")
    sentry_signal.report_blocking_failures(
        ["audience_location"], campaign=CAMPAIGN, account=ACCOUNT, run_id="r1",
        names=[AD_SET, AD_NAME])
    (event,) = events
    (att,) = event["attachments"]
    assert att["filename"] == "section_audience_location.html"
    assert att["content_type"] == "text/html"
    assert len(att["bytes"]) <= pc.SECTION_MAX_BYTES
    _assert_clean(att["bytes"].decode("utf-8"))
    assert event["tags"]["page_snapshot"] == "section"
    snap = event["contexts"]["page_snapshot"]
    assert snap["parts"] == ["audience"] and snap["captured_at_step"] == "audience_location"
    assert snap["same_step"] is True
    # Вложение не меняет отпечаток: тикеты складываются как раньше.
    assert event["fingerprint"][:2] == ["acl-step", "audience_location"]


def test_a_campaign_stop_takes_the_section_of_the_step_that_explained_it(events):
    # `further_limit_reach_radio` молчит, причину и снимок оставил помощник.
    _fail_step("pick_further_limit_radio")
    sentry_signal.report_campaign_stop("further_limit_reach_radio", campaign=CAMPAIGN,
                                       account=ACCOUNT, run_id="r1", attempt=1,
                                       names=[AD_SET, AD_NAME])
    (event,) = events
    (att,) = event["attachments"]
    assert event["contexts"]["page_snapshot"]["captured_at_step"] == "pick_further_limit_radio"
    assert event["contexts"]["page_snapshot"]["same_step"] is True
    _assert_clean(att["bytes"].decode("utf-8"))


def test_a_section_from_another_step_is_labelled_as_such(events):
    _fail_step("audience_age")
    sentry_signal.report_blocking_failures(["ad2_copy:Ad has no text"], campaign=CAMPAIGN,
                                           account=ACCOUNT, run_id="r1")
    snap = events[0]["contexts"]["page_snapshot"]
    assert snap["captured_at_step"] == "audience_age" and snap["same_step"] is False


def test_a_clean_run_sends_no_event_and_no_attachment(events):
    # Шаг споткнулся и снимок снят, но кампания вышла чистой.
    _fail_step("placements")
    sentry_signal.report_blocking_failures([], campaign=CAMPAIGN, account=ACCOUNT, run_id="r1")
    assert events == []


def test_a_failure_in_another_run_does_not_lend_its_section(events):
    _fail_step("audience_location", run_id="other")
    sentry_signal.report_blocking_failures(["audience_location"], campaign=CAMPAIGN,
                                           account=ACCOUNT, run_id="r1")
    (event,) = events
    assert event["attachments"] == []
    assert "page_snapshot" not in event["tags"]


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_the_machine_setting_turns_the_snapshot_off(events, monkeypatch, value):
    monkeypatch.setenv(pc.SECTION_SETTING, value)
    fake = _FakePage(_seeded(FRAG["suggest_fx"]))
    assert pc.remember_section(fake, step="audience_location", run_id="r1") is False
    assert fake.calls == 0  # страницу даже не спрашивали
    # Снятое до выключения тоже не едет.
    monkeypatch.delenv(pc.SECTION_SETTING)
    _fail_step("audience_location")
    monkeypatch.setenv(pc.SECTION_SETTING, value)
    sentry_signal.report_blocking_failures(["audience_location"], campaign=CAMPAIGN,
                                           account=ACCOUNT, run_id="r1")
    (event,) = events
    assert event["attachments"] == []


def test_the_setting_is_on_by_default(monkeypatch):
    monkeypatch.delenv(pc.SECTION_SETTING, raising=False)
    assert pc.section_snapshots_enabled() is True


def test_an_oversized_attachment_is_refused_but_the_event_still_goes(events):
    acl_sentry.report_step_failure(
        "audience_location", "x", run_id="r1",
        attachment={"bytes": b"x" * (pc.SECTION_MAX_BYTES + 1), "filename": "s.html"})
    (event,) = events
    assert event["attachments"] == []


def test_a_broken_attachment_does_not_lose_the_event(events, monkeypatch):
    import sentry_sdk

    real = sentry_sdk.new_scope

    @contextmanager
    def no_attachments():
        with real() as scope:
            def boom(**_kw):
                raise RuntimeError("no attachments here")
            scope.add_attachment = boom
            yield scope

    monkeypatch.setattr(sentry_sdk, "new_scope", no_attachments)
    _fail_step("audience_location")
    sentry_signal.report_blocking_failures(["audience_location"], campaign=CAMPAIGN,
                                           account=ACCOUNT, run_id="r1")
    assert len(events) == 1


def test_capture_of_a_failed_step_remembers_its_section(tmp_path, monkeypatch):
    """`page_capture.capture` (его зовёт `_shot` на каждом отказе) снимает раздел."""
    monkeypatch.setattr(pc, "get_runtime_workspace", lambda: tmp_path)
    pc._taken.clear()

    class _Page(_FakePage):
        url = "https://x.example/p"

        def locator(self, _sel):
            return self

        def inner_html(self, *, timeout):
            return "<body>ok</body>"

    fake = _Page(_seeded(FRAG["suggest_fx"]))
    assert pc.capture(fake, step="audience_location", run_id="r9", campaign=CAMPAIGN)
    assert pc.section_attachment("audience_location", "r9") is not None
    # Файлы снимка на диске — прежние; раздел для Sentry на диск не пишется.
    assert not list(tmp_path.rglob("section_*"))


# ── Настоящий SDK: вложение доходит до транспорта ───────────────────────────

def test_the_attachment_reaches_the_sentry_transport(monkeypatch):
    import sentry_sdk
    from sentry_sdk.transport import Transport

    envelopes = []

    class _Transport(Transport):
        def capture_envelope(self, envelope):
            envelopes.append(envelope)

    client = sentry_sdk.Client(dsn="https://public@example.invalid/1", transport=_Transport,
                               default_integrations=False, before_send=acl_sentry._before_send)
    monkeypatch.setattr(acl_sentry, "init", lambda: True)
    _fail_step("audience_location")
    with sentry_sdk.isolation_scope() as iso:
        iso.set_client(client)
        sentry_signal.report_blocking_failures(["audience_location"], campaign=CAMPAIGN,
                                               account=ACCOUNT, run_id="r1",
                                               names=[AD_SET, AD_NAME])
    client.flush()
    items = [item for env in envelopes for item in env.items]
    attachments = [i for i in items if i.headers.get("type") == "attachment"]
    assert len(attachments) == 1
    assert attachments[0].headers["filename"] == "section_audience_location.html"
    payload = attachments[0].payload.get_bytes()
    assert len(payload) <= pc.SECTION_MAX_BYTES
    _assert_clean(payload.decode("utf-8"))
    event = next(i for i in items if i.headers.get("type") == "event").payload.json
    assert event["tags"]["page_snapshot"] == "section"
    assert CAMPAIGN not in json.dumps(event.get("contexts", {}).get("page_snapshot", {}))
