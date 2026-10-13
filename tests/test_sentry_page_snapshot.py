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
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import page_capture as pc  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import sentry_signal  # noqa: E402
from tests.profile_lease_fakes import fake_profile

FIXTURES = ROOT / "tests" / "fixtures" / "ads_manager"
SUGGEST = (FIXTURES / "audience_suggest_layout_2026_09_26.html").read_text(encoding="utf-8")
FRAG = {m.group(1): m.group(2).strip() for m in re.finditer(
    r"<!-- fragment: (\w+) \| source: [^>]*-->\n(.*?)(?=\n<!-- fragment: |\Z)", SUGGEST, re.S)}

CAMPAIGN = "CO_bnc_All_Video_FX_35_26.09"
ACCOUNT = "FacebookDoc6540 Ivanna Petrenko"
AD_SET = "CO_bnc adset Bogota"
AD_NAME = "2046 1 - Text6"
#: Синтетический uuid (как id профиля Multilogin). Собирается из частей, чтобы
#: сканер секретов не принимал сам тест за утечку.
PROFILE_UUID = "-".join(("3f2504e0", "4f89", "11d3", "9a0c", "0305e82c3301"))

#: Всё, что не должно доехать до Sentry ни в каком виде.
SECRETS = [
    CAMPAIGN, "FacebookDoc6540", "Ivanna", "Petrenko", AD_SET, AD_NAME,
    "1234567890", "987654321", "555666777", "4455667788",
    "ivanna.petrenko@example.com", "123-4567", "(555)",
    "https://adsmanager.facebook.com", "scontent.xx.fbcdn", "fbcdn",
    "secret-token-value", "EAAB", "window.__data", "color:red", "x1y2z3",
    "typed secret value", "onclick", "data-testid", "leak-in-comment",
    PROFILE_UUID,
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
        f'<div>Page id=555666777, asset_id=4455667788, uuid {PROFILE_UUID}</div>'
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


# Список кампаний (ревью PR #328, F1): кампания не открылась — шаги `create`,
# `objective_*`, `stall_*` снимают список, где карточек шага нет. Раньше
# запасной путь отдавал весь `document.body`. Уточнение владельца 26.09:
# внутри браузера Multilogin снимать можно, имена других кампаний остаются, а
# id, адреса, почта и токены — нет.
_LIST_ROW = (
    '<tr><td><a href="https://adsmanager.facebook.com/adsmanager/manage/campaigns'
    '?act=1234567890&amp;selected_campaign_ids=120212345678901">{name}</a></td>'
    '<td>Draft</td><td>act=1234567890 campaign_id=120212345678901</td></tr>'
)
_LIST_PAGE = """
<div role="banner">
  <div role="button" aria-label="Your profile, Ivan Sidorenko">Ivan Sidorenko</div>
  <div role="combobox" aria-label="Ad account">Acme Clinics LLC (Doc19)</div>
</div>
<div role="main">
  <div role="toolbar"><div role="button">Create</div><div role="button">Duplicate</div></div>
  <table role="table"><tbody>{rows}</tbody></table>
  <div>Support: ivanna.petrenko@example.com, +1 (555) 123-4567,
       access_token=EAABsecrettokenvalue, uuid {uuid}</div>
</div>
{dialog}
"""
_OBJECTIVE_DIALOG = """
<div role="dialog" aria-label="Create new campaign">
  <div role="heading" aria-level="2">Create new campaign</div>
  <span role="radio" aria-checked="false">Leads</span>
  <span role="radio" aria-checked="false">Sales</span>
</div>
"""


def _list_page(rows: int = 3, dialog: str = "") -> str:
    names = ["Other client Zeta Dental Promo", "Acme Clinics Spring Leads 2026", "Beta Launch"]
    body = "".join(_LIST_ROW.format(name=names[i % 3] + ("" if i < 3 else f" {i}")) for i in range(rows))
    return _LIST_PAGE.format(rows=body, uuid=PROFILE_UUID, dialog=dialog)


@pytest.mark.parametrize("step", ["stall_objective_wait", "create", "objective_leads"])
def test_a_campaigns_list_failure_takes_the_list_region_not_the_whole_page(page, step):
    page.set_content(_list_page())
    assert pc.remember_section(page, step=step, run_id="r1", campaign="Beta Launch")
    att = sentry_signal._section("objective_leads", "r1", campaign="Beta Launch",
                                 account="FacebookDoc19", names=[])
    assert att is not None
    assert att["context"]["parts"] == ["campaigns list"]
    body = att["bytes"].decode("utf-8")
    # Таблица списка на месте — по ней видно, что было на экране.
    assert 'role="table"' in body and "Duplicate" in body
    assert "Zeta Dental Promo" in body and "&lt;campaign&gt;" in body
    # Шапка профиля вне области списка — не едет.
    assert "Your profile" not in body and "Ad account" not in body
    # Номера, адреса, почта, телефон, токены и uuid — вычищены.
    lowered = body.lower()
    for leaked in ("1234567890", "120212345678901", "https://", "adsmanager.facebook.com",
                   "ivanna.petrenko@example.com", "123-4567", "eaabsecrettokenvalue", PROFILE_UUID):
        assert leaked.lower() not in lowered, leaked
    assert "act=&lt;hidden&gt;" in body
    assert "href=" not in body
    assert not re.search(r"\d{6,}", body)
    assert _balanced(body)


def test_an_open_dialog_on_the_campaigns_list_is_taken_alone(page):
    page.set_content(_list_page(dialog=_OBJECTIVE_DIALOG))
    assert pc.remember_section(page, step="stall_objective_wait", run_id="r1", campaign="Beta Launch")
    att = pc.section_attachment("objective_leads", "r1", campaign="Beta Launch")
    assert att["context"]["parts"] == ["dialog"]
    body = att["bytes"].decode("utf-8")
    assert "Create new campaign" in body and "Leads" in body
    assert "Zeta Dental Promo" not in body and "Your profile" not in body


def test_a_huge_campaigns_list_is_capped_in_the_page_and_in_the_file(page):
    page.set_content(_list_page(rows=6000))
    assert pc.remember_section(page, step="create", run_id="r1", campaign="Beta Launch")
    entry = pc._find_section("r1", ["create"])
    (part,) = entry["parts"]
    # Обход в странице кончился на потолке, а не после копии всего списка.
    assert len(part["html"]) <= pc.SECTION_LIST_RAW_MAX_CHARS
    att = pc.section_attachment("create", "r1", campaign="Beta Launch")
    assert len(att["bytes"]) <= pc.SECTION_MAX_BYTES
    assert att["context"]["truncated"] is True
    assert _balanced(att["bytes"].decode("utf-8"))


def test_nothing_is_taken_from_a_page_without_card_editor_list_or_dialog(page):
    page.set_content('<div role="banner"><div aria-label="Your profile, Ivan Sidorenko">Ivan</div></div>'
                     '<div>Loading…</div>')
    assert pc.remember_section(page, step="create", run_id="r1", campaign="Beta Launch") is False
    assert pc.section_attachment("create", "r1", campaign="Beta Launch") is None


# ── Вложение у события ──────────────────────────────────────────────────────

class _Handle:
    """Ответ `wait_for_function`: объект `{v: …}`, как у `_eval_bounded`."""

    def __init__(self, value):
        self.value = value

    def json_value(self):
        return {"v": self.value}

    def dispose(self):
        pass


class _FakePage:
    """Страница, которая отдаёт карточку как `_SECTION_JS` — без браузера.

    Раздел снимается ограниченным вызовом (`wait_for_function` с таймаутом,
    ревью PR #328, F5), поэтому и отвечает двойник на него."""

    def __init__(self, html: str, name: str = "audience"):
        self.parts = [{"name": name, "html": html}]
        self.calls = 0

    def wait_for_function(self, _expr, *, arg=None, timeout=None):
        self.calls += 1
        return _Handle(self.parts)


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


# ── Чужой прогон и чужая кампания (ревью PR #328, F2) ───────────────────────

class _SectionPage(_FakePage):
    """Страница, отдающая готовый раздел (как `_SECTION_JS`), — без браузера."""


_ALPHA = "Alpha Spring Promo"
_ALPHA_SECTION = f"<div><span>{_ALPHA}</span><span>Alpha adset Bogota</span></div>"


def _profile_stop_calls(monkeypatch) -> list:
    sent: list = []
    monkeypatch.setattr(acl_sentry, "report_step_failure",
                        lambda step, why, **kw: sent.append((step, kw)) or "e1")
    return sent


def test_an_attach_time_stop_of_the_next_campaign_carries_no_section_and_no_old_run(monkeypatch):
    """Сценарий ревью: кампания A оставила раздел, у кампании B профиль не
    подключился. Событие B — без раздела A и без номера прогона A."""
    from src.fb_ads_rpa import profile_trouble as pt
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    monkeypatch.setattr("src.fb_ads_rpa.rpa_publisher.load_ad_account_map", lambda _: {
        "FacebookDoc19": SimpleNamespace(profile_id=fake_profile("FacebookDoc19"))})
    sent = _profile_stop_calls(monkeypatch)
    rt.set_current_job("job-7")
    rt.set_current_run("runA", _ALPHA)
    assert pc.remember_section(_SectionPage(_ALPHA_SECTION), step="audience_location",
                               run_id="runA", campaign=_ALPHA)

    pub = RpaPublisher.__new__(RpaPublisher)
    pub.hand_off = pub.dry_run = False
    pub.selectors_path = None
    pub._conn = None
    pub._built_at = None

    def ensure(_label):
        raise pt.ProfileStop(pt.RESTART_FAILED, "x")

    monkeypatch.setattr(pub, "_ensure_browser", ensure)
    draft = CampaignDraft("Beta Launch", "MX", "FacebookDoc19",
                          ad_sets=[AdSetDraft("Set", ads=[AdDraft(ad_title="t", ad_body="b")])])
    pub.publish_draft(draft)

    (step, kw), = sent
    assert step == pt.RESTART_FAILED and kw["campaign"] == "Beta Launch"
    assert kw.get("attachment") is None
    assert kw["run_id"] == ""
    # Номер задания пережил смену кампании.
    assert rt.current_job_id() == "job-7" and rt.current_run_id() == ""


def test_a_profile_stop_never_carries_a_section_even_of_its_own_campaign(monkeypatch):
    """Вкладка упала посреди кампании, переподключение не удалось: раздел,
    снятый до падения, к остановке профиля не едет."""
    from src.fb_ads_rpa import profile_trouble as pt
    from src.fb_ads_rpa.rpa_publisher import RpaPublisher

    sent = _profile_stop_calls(monkeypatch)
    rt.set_current_run("runB", "Beta Launch")
    assert pc.remember_section(_SectionPage("<div>Beta section</div>"), step=pt.RESTART_FAILED,
                               run_id="runB", campaign="Beta Launch")
    RpaPublisher._report_profile_stop(pt.ProfileStop(pt.RESTART_FAILED, "x"), "Beta Launch",
                                      "FacebookDoc19", "reattach")
    (_step, kw), = sent
    assert kw.get("attachment") is None


def test_a_section_of_another_campaign_is_never_attached():
    assert pc.remember_section(_SectionPage(_ALPHA_SECTION), step="audience_location",
                               run_id="r1", campaign=_ALPHA)
    # Тот же прогон, тот же шаг, другая кампания — ни точного, ни запасного.
    assert pc.section_attachment("audience_location", "r1", campaign="Beta Launch") is None
    assert pc.section_attachment("objective_leads", "r1", campaign="Beta Launch") is None
    assert pc.section_attachment("audience_location", "r1", campaign=_ALPHA) is not None


def test_without_a_run_id_there_is_no_last_section_fallback():
    assert pc.remember_section(_SectionPage(_ALPHA_SECTION), step="audience_location",
                               run_id="", campaign=_ALPHA)
    assert pc.section_attachment("objective_leads", "", campaign=_ALPHA) is None
    # Без номера прогона — только точный шаг и только своя, названная кампания.
    assert pc.section_attachment("audience_location", "", campaign="") is None
    assert pc.section_attachment("audience_location", "", campaign="Beta Launch") is None
    assert pc.section_attachment("audience_location", "", campaign=_ALPHA) is not None


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


# ── Зависшая вкладка (ревью PR #328, F5) ────────────────────────────────────

class _HungPage:
    """Вкладка, у которой голый `evaluate` висит, а ограниченный вызов
    честно кончается таймаутом через свой `timeout`.

    ACL-1491: «висит» — `HANG_S`, а граница теста — его половина. Прежде
    висели 1.5 с при границе 1.0 с, и честный путь на нагруженном раннере
    шёл 1.02–1.14 с. Разрыв между «не ждали» и «ждали» теперь в секундах,
    а не в десятых; дольше `HANG_S` сломанный код тест не держит."""

    HANG_S = 10.0

    def __init__(self):
        self.evaluate_calls = 0
        self.timeouts: list = []

    def evaluate(self, *_a, **_k):
        import time

        self.evaluate_calls += 1
        time.sleep(self.HANG_S)  # «повисла»; дольше в тесте не ждём
        return [{"name": "audience", "html": "<div>too late</div>"}]

    def wait_for_function(self, _expr, *, arg=None, timeout=None):
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        self.timeouts.append(timeout)
        raise PlaywrightTimeout(f"Timeout {timeout}ms exceeded.")


def test_a_hung_tab_gives_no_section_within_the_bound():
    import time

    hung = _HungPage()
    started = time.monotonic()
    assert pc.remember_section(hung, step="audience_location", run_id="r1", campaign=CAMPAIGN) is False
    assert time.monotonic() - started < _HungPage.HANG_S / 2
    assert hung.evaluate_calls == 0, "раздел снимался неограниченным evaluate"
    assert hung.timeouts == [int(pc.SECTION_EVAL_TIMEOUT_S * 1000)] == [5000]
    assert pc.section_attachment("audience_location", "r1", campaign=CAMPAIGN) is None


def test_capture_on_a_tab_that_hangs_after_inner_html_does_not_hang(tmp_path, monkeypatch):
    """`capture` из `_stall_evidence`: `inner_html` ответил, а вкладка повисла."""
    import time

    monkeypatch.setattr(pc, "get_runtime_workspace", lambda: tmp_path)
    pc._taken.clear()

    class _Page(_HungPage):
        url = "https://x.example/p"

        def locator(self, _sel):
            return self

        def inner_html(self, *, timeout):
            return "<body>ok</body>"

    page = _Page()
    started = time.monotonic()
    assert pc.capture(page, step="stall_objective_wait", run_id="r9", campaign=CAMPAIGN)
    assert time.monotonic() - started < _HungPage.HANG_S / 2
    assert page.evaluate_calls == 0
    assert pc.section_attachment("stall_objective_wait", "r9", campaign=CAMPAIGN) is None


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


def test_a_table_directly_under_body_does_not_take_the_body(page):
    """Таблица прямо под <body>: «контейнер таблицы» — это сам body, и его брать
    нельзя — ушла бы вся страница (повторное ревью PR #328, N2)."""
    page.set_content('<div aria-label="Your profile, Ivan Sidorenko">Ivan</div>'
                     '<table role="table"><tr><td>Zeta</td></tr></table>')
    pc.remember_section(page, step="create", run_id="r1", campaign="X")
    att = pc.section_attachment("create", "r1", campaign="X")
    assert att is None or "Your profile" not in att["bytes"].decode()


def test_markup_that_reaches_the_text_never_carries_its_attributes():
    """Тег, дошедший до текста (экранированный на странице или не разобранный
    старым парсером Python 3.11 — CI PR #328), вырезается целиком: его
    `class`/`href`/`onclick` мимо белого списка не едут."""
    html = ('<div role="dialog"><span>Pick an event</span>'
            '&lt;div class="x1n2onr6" href="https://example.com/act=12345678" onclick="go()"&gt;'
            '<b>Leads</b> &lt;/div&gt; a &lt; b</div>')
    body, _cut = pc.scrub_section_html(html, campaign="", account="", names=())
    assert not re.search(r"\s(class|style|href|src|value|onclick|data-testid)=", body)
    assert "Pick an event" in body and "Leads" in body and "a &lt; b" in body


def test_the_tail_of_a_torn_tag_never_carries_its_attributes():
    """`>` внутри значения атрибута может разорвать тег, и хвост с атрибутами
    придёт текстом — пары `имя="значение"` из текста вырезаются."""
    scrubber = pc._SectionScrubber(None, {}, 200_000)
    scrubber.handle_starttag("div", [("role", "dialog")])
    scrubber.handle_data(' ad name" class="x1n2onr6" href="https://example.com/?act=1" data-testid=\'t\'> Leads')
    scrubber.handle_endtag("div")
    body = scrubber.result()
    assert not re.search(r"\s(class|style|href|src|value|onclick|data-testid)=", body)
    assert "Leads" in body


@pytest.mark.parametrize("tail", [
    '<input type="text" value="typed secret val',
    '<div class="x1n2onr6 x78zum5 xdt5',
    '<div aria-label="a > b" class=x1n2onr6 data-x=1',
])
def test_a_section_cut_inside_a_tag_drops_the_unfinished_tag(tail):
    """Раздел обрезан посреди тега и `>` дальше нет. Python 3.11 отдал бы
    недописанный тег текстом (атрибуты, набранное value); хвост отбрасывается
    на любой версии — поймает регрессию на CI 3.11 (ревью PR #328)."""
    scrubber = pc._SectionScrubber(None, {}, 200_000)
    scrubber.feed('<div role="dialog"><span>Leads</span>' + tail)
    scrubber.close()
    body = scrubber.result()
    assert "Leads" in body
    assert "secret" not in body and "x1n2onr6" not in body
    assert not re.search(r"\s(class|style|href|src|value|onclick|data-testid)=", body)
