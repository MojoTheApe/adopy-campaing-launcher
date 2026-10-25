"""ACL-1819: pre-flight audit билдера — одна таблица правил, гейт Publish, паритет.

Ни Meta, ни Multilogin здесь не трогаются: Graph — `FakeMeta` в памяти, страница —
Node на живом коде шаблона (`tests/preflight_harness.cjs`, `tests/preflight_gate_harness.cjs`).
Проверяется:
* таблица правил одна: JSON рядом с модулем равен экспорту, страница получает её же,
  у каждого правила есть всё нужное для строки карточки;
* фикстуры на каждую проверку (ok / warning / error) и паритет: Python
  (`evaluate_client`) и JS (`cbPreflight.evaluate`) дают одинаковые вердикты;
* серверные вердикты через создателя (страница, пиксель, форма, страна, язык,
  интересы, файл, лимиты, вертикаль, бюджет, существующая кампания, кабинет);
* аудит ничего не пишет в Meta (клиент без права записи, ни одного POST);
* гейт Publish: клиентская ошибка блокирует, серверная — после обновления проверок,
  предупреждения не блокируют; RPA — только клиентские; мутационная проверка гейта.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import graph as g  # noqa: E402
from src.fb_api import preflight_rules as pf  # noqa: E402
from tests.test_acl1761_api_create import ACT, _draft  # noqa: E402
from tests.test_acl1761_plan_check import _read_session  # noqa: E402
from tests.test_acl1775_instant_forms import ARCHIVED, FORM, FormMeta  # noqa: E402

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    return node


def _harness(mode: str, payload: dict, template: Path = TPL) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(payload, fh)
        path = fh.name
    try:
        r = subprocess.run([_node(), str(ROOT / "tests" / "preflight_harness.cjs"), str(template), mode, path],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ───────────────────────────── одна таблица правил ─────────────────────────────

def test_the_json_next_to_the_module_is_the_export():
    assert pf.JSON_PATH.read_text(encoding="utf-8") == pf.export_json(), \
        "preflight_rules.json отстал от таблицы: пересоздать pf.export_json()"


def test_every_rule_has_what_a_card_line_needs():
    ids = [r["id"] for r in pf.RULES]
    assert len(ids) == len(set(ids))
    for r in pf.RULES:
        assert r["label"] and r["hint"] and r["severity"] in (pf.ERROR, pf.WARNING)
        assert r["side"] in ("client", "server") and r["applies"] in ("always", "website", "instant_form")
        assert set(r["field"]) == {"sel", "level"} and r["field"]["level"] in ("campaign", "adset", "ad")
        assert r["label"].isascii() and r["hint"].isascii(), "UI на английском"


def test_the_page_gets_the_same_table():
    page = TPL.read_text(encoding="utf-8")
    assert "window.CB_PREFLIGHT_RULES = {{ (preflight_rules_json or {}) | tojson }}" in page
    from src.web import app as web

    assert web._preflight_rules() == pf.export_rules()


def test_every_server_rule_is_produced_by_the_server_and_every_client_rule_by_the_client(tmp_path):
    meta = AuditMeta()
    d = _draft(tmp_path, ads=1)
    _vertical(d)
    out = _audit(meta, d)
    produced = {v["rule"] for v in out.audit}
    assert produced == {r["id"] for r in pf.RULES if r["side"] == "server" and r["applies"] != "instant_form"}
    inp = _ok_input()
    inp_i = {**inp, "destination": "instant_form", "ads": [{**inp["ads"][0], "form_id": "7"}]}
    got = {v["rule"] for v in pf.evaluate_client(inp)} | {v["rule"] for v in pf.evaluate_client(inp_i)}
    assert got == {r["id"] for r in pf.RULES if r["side"] == "client"}


# ───────────────────────────── клиентские правила: фикстуры и паритет ─────────────────────────────

def _ok_input() -> dict:
    return {"destination": "website", "objective": "leads", "link": "https://track.example.com/c?sub=1",
            "country": "Mexico", "budget": "20", "age_min": 35, "age_max": 35,
            "ads": [{"name": "1000 - Text0", "file": "1000_img.png", "form_id": "", "body": "Body text",
                     "headline": "Short headline", "cta": "LEARN_MORE"},
                    {"name": "1001 - Text1", "file": "1001_img.png", "form_id": "", "body": "Another body",
                     "headline": "Other headline", "cta": "LEARN_MORE"}]}


def _with(**over) -> dict:
    inp = _ok_input()
    for k, v in over.items():
        inp[k] = v
    return inp


def _ad(i: int, **over) -> list:
    ads = copy.deepcopy(_ok_input()["ads"])
    ads[i].update(over)
    return ads


#: имя фикстуры -> (вход, правило, ожидаемый статус, ожидаемые объявления)
CLIENT_CASES: dict[str, tuple[dict, str, str, list[int]]] = {
    "country_ok": (_with(), "country_set", "ok", []),
    "country_empty": (_with(country=" "), "country_set", "error", []),
    "link_ok": (_with(), "link_https", "ok", []),
    "link_empty": (_with(link=""), "link_https", "error", []),
    "link_http": (_with(link="http://track.example.com/c?sub=1"), "link_https", "error", []),
    "link_malformed": (_with(link="https://nodot"), "link_https", "error", []),
    "link_spaces": (_with(link="https://track.example.com/a b"), "link_https", "error", []),
    "tracking_ok": (_with(), "link_tracking", "ok", []),
    "tracking_missing": (_with(link="https://track.example.com/c"), "link_tracking", "warning", []),
    "tracking_unknown_when_link_broken": (_with(link="nope"), "link_tracking", "unknown", []),
    "media_ok": (_with(), "media_chosen", "ok", []),
    "media_missing": (_with(ads=_ad(1, file="")), "media_chosen", "error", [2]),
    "primary_ok": (_with(), "primary_text", "ok", []),
    "primary_missing": (_with(ads=_ad(0, body="  ")), "primary_text", "error", [1]),
    "headline_ok": (_with(), "headline", "ok", []),
    "headline_missing": (_with(ads=_ad(0, headline="")), "headline", "error", [1]),
    "headline_40_ok": (_with(ads=_ad(0, headline="x" * 40)), "headline_len", "ok", []),
    "headline_41_warns": (_with(ads=_ad(0, headline="x" * 41)), "headline_len", "warning", [1]),
    "primary_125_ok": (_with(ads=_ad(0, body="y" * 125)), "primary_len", "ok", []),
    "primary_126_warns": (_with(ads=_ad(1, body="y" * 126)), "primary_len", "warning", [2]),
    "cta_ok": (_with(), "cta_allowed", "ok", []),
    "cta_wrong": (_with(ads=_ad(0, cta="SHOP_NOW")), "cta_allowed", "error", [1]),
    # ACL-1767: список кнопок по месту конверсии, устройства и старт набора.
    "cta_new_website_ok": (_with(ads=_ad(0, cta="BOOK_NOW")), "cta_allowed", "ok", []),
    "cta_order_now_not_for_forms": (_with(destination="instant_form",
                                          ads=[{**a, "form_id": "7", "cta": "ORDER_NOW"} for a in _ok_input()["ads"]]),
                                    "cta_allowed", "error", [1, 2]),
    "cta_order_now_on_website_ok": (_with(ads=_ad(1, cta="ORDER_NOW")), "cta_allowed", "ok", []),
    "devices_ok": (_with(), "devices_consistent", "ok", []),
    "devices_ios_version_ok": (_with(os="iOS", os_min="14.0", device="mobile"), "devices_consistent", "ok", []),
    "devices_ios_desktop": (_with(os="iOS", device="desktop"), "devices_consistent", "error", []),
    "devices_version_needs_os": (_with(os="All", os_min="14"), "devices_consistent", "error", []),
    "devices_version_bad": (_with(os="Android", os_min="14.x"), "devices_consistent", "error", []),
    "start_empty_ok": (_with(), "start_time_valid", "ok", []),
    "start_future_ok": (_with(start_time="2026-10-20T10:00", now="2026-10-12T12:00"), "start_time_valid", "ok", []),
    "start_today_ok": (_with(start_time="2026-10-12T08:00", now="2026-10-12T12:00"), "start_time_valid", "ok", []),
    "start_bad_format": (_with(start_time="20.10.2026 10:00", now="2026-10-12T12:00"), "start_time_valid", "error", []),
    "start_in_the_past": (_with(start_time="2026-10-11T23:00", now="2026-10-12T12:00"), "start_time_valid", "error", []),
    "budget_ok": (_with(), "budget_set", "ok", []),
    "budget_missing": (_with(budget=None), "budget_set", "error", []),
    "budget_zero": (_with(budget="0"), "budget_set", "error", []),
    "budget_text": (_with(budget="abc"), "budget_set", "error", []),
    "age_ok": (_with(), "age_range", "ok", []),
    "age_18_65_ok": (_with(age_min=18, age_max=65), "age_range", "ok", []),
    "age_17": (_with(age_min=17, age_max=40), "age_range", "error", []),
    "age_66": (_with(age_min=30, age_max=66), "age_range", "error", []),
    "age_min_over_max": (_with(age_min=50, age_max=40), "age_range", "error", []),
    "age_nan": (_with(age_min=None, age_max=None), "age_range", "error", []),
    "names_ok": (_with(), "ad_names_unique", "ok", []),
    "names_dup": (_with(ads=_ad(1, name="1000 - Text0")), "ad_names_unique", "error", [1, 2]),
    "form_not_needed_for_website": (_with(), "form_chosen", None, []),
    "form_partly_missing": (_with(destination="instant_form", ads=_ad(0, form_id="7")), "form_chosen", "error", [2]),
    "form_all_ok": (_with(destination="instant_form",
                          ads=[{**a, "form_id": "7"} for a in _ok_input()["ads"]]), "form_chosen", "ok", []),
    "tpl_counts_as_a_chosen_form": (_with(destination="instant_form", ads=[
        {**a, "form_id": "", "template": {"privacy_url": "https://x.example.com/p", "contact_fields": ["email"]}}
        for a in _ok_input()["ads"]]), "form_chosen", "ok", []),
    "tpl_privacy_ok": (_with(destination="instant_form", ads=[
        {**a, "template": {"privacy_url": "https://x.example.com/p", "contact_fields": ["email"]}}
        for a in _ok_input()["ads"]]), "form_template_privacy", "ok", []),
    "tpl_privacy_http": (_with(destination="instant_form", ads=_ad(
        0, template={"privacy_url": "http://x.example.com/p", "contact_fields": ["email"]})),
        "form_template_privacy", "error", [1]),
    "tpl_privacy_missing": (_with(destination="instant_form", ads=_ad(
        1, template={"privacy_url": "", "contact_fields": ["email"]})), "form_template_privacy", "error", [2]),
    "tpl_contacts_ok": (_with(destination="instant_form", ads=[
        {**a, "template": {"privacy_url": "https://x.example.com/p", "contact_fields": ["phone"]}}
        for a in _ok_input()["ads"]]), "form_template_contacts", "ok", []),
    "tpl_contacts_none": (_with(destination="instant_form", ads=_ad(
        0, template={"privacy_url": "https://x.example.com/p", "contact_fields": []})),
        "form_template_contacts", "error", [1]),
    "instant_has_no_link_rules": (_with(destination="instant_form", link="",
                                        ads=[{**a, "form_id": "7"} for a in _ok_input()["ads"]]),
                                  "link_https", None, []),
}


@pytest.mark.parametrize("name", sorted(CLIENT_CASES))
def test_client_fixture_verdict(name):
    inp, rule, want, ads = CLIENT_CASES[name]
    got = {v["rule"]: v for v in pf.evaluate_client(inp)}
    if want is None:
        assert rule not in got, "правило не должно применяться"
        return
    assert got[rule]["status"] == want, got[rule]
    assert got[rule]["ads"] == ads


def test_every_client_rule_has_an_ok_a_bad_fixture():
    covered = {}
    for inp, rule, want, _ in CLIENT_CASES.values():
        covered.setdefault(rule, set()).add(want)
    for r in pf.RULES:
        if r["side"] != "client":
            continue
        assert "ok" in covered[r["id"]], r["id"]
        worst = "warning" if r["severity"] == pf.WARNING else "error"
        assert worst in covered[r["id"]], (r["id"], covered[r["id"]])


def test_python_and_the_page_give_the_same_client_verdicts():
    fixtures = {name: case[0] for name, case in CLIENT_CASES.items()}
    js = _harness("--evaluate", fixtures)
    for name, inp in fixtures.items():
        assert js[name] == pf.evaluate_client(inp), name


def test_the_creator_agrees_with_the_client_about_the_shared_errors(tmp_path):
    """Те же ошибки создатель выдаёт сам: link, media, budget, имена объявлений."""
    d = _draft(tmp_path, ads=2)
    cr.plan_from_draft(d)                                            # чистая: без ошибок
    d.link = ""
    with pytest.raises(cr.PlanError, match="Tracking link is empty"):
        cr.plan_from_draft(d)
    assert [v["status"] for v in pf.evaluate_client(_with(link="")) if v["rule"] == "link_https"] == ["error"]
    d = _draft(tmp_path, ads=2)
    d.ad_sets[0].ads[1].creative_path = ""
    with pytest.raises(cr.PlanError, match="no creative file"):
        cr.plan_from_draft(d)
    d = _draft(tmp_path, ads=2)
    d.ad_sets[0].daily_budget = 0
    with pytest.raises(cr.PlanError, match="Daily budget missing"):
        cr.plan_from_draft(d)
    d = _draft(tmp_path, ads=2)
    d.ad_sets[0].ads[1].copy_name = d.ad_sets[0].ads[0].copy_name
    d.ad_sets[0].ads[1].creative_id = d.ad_sets[0].ads[0].creative_id
    d.ad_sets[0].ads[1].creative_filename = d.ad_sets[0].ads[0].creative_filename
    with pytest.raises(cr.PlanError, match="would all be named"):
        cr.plan_from_draft(d)
    # Instant Form без выбранной формы
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].destination = "instant_form"
    with pytest.raises(cr.PlanError, match="no Instant Form selected"):
        cr.plan_from_draft(d)


# ───────────────────────────── серверные вердикты через создателя ─────────────────────────────

def _png(w: int, h: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00" + b"0" * 32


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _mp4(seconds: int, w: int = 1080, h: int = 1920, rotate: bool = False) -> bytes:
    mvhd = _box(b"mvhd", b"\x00\x00\x00\x00" + b"\x00" * 8 + struct.pack(">II", 1000, seconds * 1000) + b"\x00" * 80)
    # Матрица tkhd (36 байт с 40-го): поворот на 90 градусов — a = 0, b = 1.0, c = -1.0, d = 0.
    matrix = struct.pack(">9i", 0, 0x10000, 0, -0x10000, 0, 0, 0, 0, 0x40000000) if rotate else b"\x00" * 36
    tkhd = _box(b"tkhd", b"\x00" * 40 + matrix + struct.pack(">II", w << 16, h << 16))
    return _box(b"ftyp", b"isom\x00\x00\x02\x00") + _box(b"moov", mvhd + _box(b"trak", tkhd)) + b"\x00" * 64


def _vertical(d, w: int = 1080, h: int = 1920) -> None:
    for s in d.ad_sets:
        for ad in s.ads:
            if not str(ad.creative_path).endswith(".mp4"):
                Path(ad.creative_path).write_bytes(_png(w, h))


class AuditMeta(FormMeta):
    """FormMeta + то, что можно подкрутить в проверках кабинета."""

    def __init__(self, *, pages=("111",), pixels=None, currency="USD", status=1, bad_interests=False,
                 geo=True, locale=True, **kw):
        super().__init__(**kw)
        self.pages, self.currency, self.status = list(pages), currency, status
        self.pixels = pixels
        self.bad_interests, self.geo, self.locale = bad_interests, geo, locale

    def _get(self, path, params):
        if path == "/me/adaccounts":
            return {"data": [{"account_id": ACT[4:], "account_status": self.status}]}
        parts = path.strip("/").split("/")
        if parts == [ACT]:
            return {"account_id": ACT[4:], "currency": self.currency, "account_status": self.status}
        if parts == [ACT, "promote_pages"]:
            return {"data": [{"id": p} for p in self.pages]}
        if path == "/search":
            t = params["type"]
            if t == "adgeolocation" and not self.geo:
                return {"data": []}
            if t == "adlocale" and not self.locale:
                return {"data": []}
            if t == "adinterestvalid" and self.bad_interests:
                return {"data": [{"name": n, "valid": False} for n in params["interest_list"]]}
        return super()._get(path, params)


def _audit(meta, draft, *, creator_kw=None):
    client = g.GraphClient(meta, allow_writes=False, sleep=lambda s: None, min_interval_s=0.0, max_calls=60)
    facts = cr.preflight_account(client, ACT)
    creator = cr.CampaignCreator(client, facts, cr.TargetingResolver(client), sleep=lambda s: None,
                                 **(creator_kw or {}))
    out = creator.plan_only(cr.plan_from_draft(draft))
    assert client.writes == 0 and meta.posts == [], "аудит не пишет в Meta"
    return out


def _by(out) -> dict:
    return {v["rule"]: v for v in out.audit}


def test_a_clean_campaign_has_no_server_error(tmp_path):
    d = _draft(tmp_path, ads=2)
    _vertical(d)
    out = _audit(AuditMeta(), d)
    assert out.error == "" and not pf.blocks(out.audit)
    assert {v["rule"]: v["status"] for v in out.audit} == {
        "account_active": "ok", "page_ok": "ok", "instagram_identity": "ok", "pixel_ok": "ok", "country_resolves": "ok",
        "language_resolves": "ok", "interests_resolve": "ok", "media_readable": "ok", "media_limits": "ok",
        "media_vertical": "ok", "budget_min": "ok", "existing_diffs": "ok"}


def test_page_not_on_the_account(tmp_path):
    d = _draft(tmp_path, ads=1)
    d.page_id = "999"
    out = _audit(AuditMeta(), d)
    assert _by(out)["page_ok"]["status"] == "error" and "is not among the pages this ad account can advertise from" in _by(out)["page_ok"]["detail"]
    assert _by(out)["pixel_ok"]["status"] == "ok", "остальные проверки независимы"


def test_page_automatic_uses_first_available_and_warns_for_manual_shell(tmp_path):
    d = _draft(tmp_path, ads=1)
    d.page_id = ""
    assert _by(_audit(AuditMeta(pages=("111", "222")), d))["page_ok"]["status"] == "ok"
    assert _by(_audit(AuditMeta(pages=()), d))["page_ok"]["status"] == "warning"
    assert _by(_audit(AuditMeta(pages=("111",)), d))["page_ok"]["status"] == "ok"   # единственная страница


def test_page_not_chosen_takes_the_first_page(tmp_path):
    # ACL-1897: пустой выбор берёт первую Страницу кабинета, одна она или несколько.
    d = _draft(tmp_path, ads=1)
    d.page_id = ""
    out = _audit(AuditMeta(pages=("111", "222")), d)
    assert _by(out)["page_ok"]["status"] == "ok"
    assert any("the first available page" in w for w in out.warnings)
    d.page_id = ""
    assert _by(_audit(AuditMeta(pages=("111",)), d))["page_ok"]["status"] == "ok"
    d.page_id = ""
    assert _by(_audit(AuditMeta(pages=()), d))["page_ok"]["status"] == "warning"  # ACL-1880: website manual completion


def test_page_chosen_must_be_among_pages_even_with_several(tmp_path):
    d = _draft(tmp_path, ads=1)
    d.page_id = "222"
    assert _by(_audit(AuditMeta(pages=("111", "222")), d))["page_ok"]["status"] == "ok"
    d.page_id = "333"
    assert _by(_audit(AuditMeta(pages=("111", "222")), d))["page_ok"]["status"] == "error"


def test_pixel_missing_for_website_conversions(tmp_path):
    d = _draft(tmp_path, ads=1)
    out = _audit(AuditMeta(pixels=[]), d)
    assert _by(out)["pixel_ok"]["status"] == "error" and "pixels/datasets" in _by(out)["pixel_ok"]["detail"]


def test_instant_form_needs_an_active_form_of_the_page(tmp_path):
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].destination = "instant_form"
    d.ad_sets[0].ads[0].lead_form_id = ARCHIVED
    out = _audit(AuditMeta(), d)
    assert _by(out)["form_active"]["status"] == "error" and "ACTIVE" in _by(out)["form_active"]["detail"]
    assert "pixel_ok" not in _by(out), "у Instant Form пикселя нет"
    d.ad_sets[0].ads[0].lead_form_id = FORM
    assert _by(_audit(AuditMeta(), d))["form_active"]["status"] == "ok"
    d.ad_sets[0].ads[0].lead_form_id = "7000009"            # форма другой Страницы
    assert _by(_audit(AuditMeta(), d))["form_active"]["status"] == "error"


def test_country_language_and_interests_need_exact_matches(tmp_path):
    d = _draft(tmp_path, ads=1)
    got = _by(_audit(AuditMeta(geo=False), d))
    assert got["country_resolves"]["status"] == "error" and "not found" in got["country_resolves"]["detail"]
    assert _by(_audit(AuditMeta(locale=False), d))["language_resolves"]["status"] == "error"
    got = _by(_audit(AuditMeta(bad_interests=True), d))
    assert got["interests_resolve"]["status"] == "error" and "Detailed targeting" in got["interests_resolve"]["detail"]
    d2 = _draft(tmp_path, ads=1)
    d2.language = ""
    assert _by(_audit(AuditMeta(locale=False), d2))["language_resolves"]["status"] == "ok", "язык не задан — нечего искать"


def test_media_missing_oversized_and_wrong_type(tmp_path, monkeypatch):
    d = _draft(tmp_path, ads=2)
    Path(d.ad_sets[0].ads[1].creative_path).unlink()
    got = _by(_audit(AuditMeta(), d))
    assert got["media_readable"]["status"] == "error" and got["media_readable"]["ads"] == [2]
    d = _draft(tmp_path, ads=1)
    monkeypatch.setattr(cr, "IMAGE_MAX_BYTES", 5)
    got = _by(_audit(AuditMeta(), d))
    assert got["media_limits"]["status"] == "error" and "larger than" in got["media_limits"]["detail"]
    monkeypatch.undo()
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].ads[0].creative_path = str(tmp_path / "doc.pdf")
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 not media")
    got = _by(_audit(AuditMeta(), d))
    assert got["media_limits"]["status"] == "error" and "not an image or video" in got["media_limits"]["detail"]


def test_video_limits_and_the_vertical_crop(tmp_path):
    d = _draft(tmp_path, video=True, ads=2)
    _vertical(d)
    video = Path(d.ad_sets[0].ads[1].creative_path)
    video.write_bytes(_mp4(30))
    got = _by(_audit(AuditMeta(), d))
    assert got["media_limits"]["status"] == "ok" and got["media_vertical"]["status"] == "ok"
    video.write_bytes(_mp4(90))                          # дольше минуты — Stories/Reels обрежут
    got = _by(_audit(AuditMeta(), d))
    assert got["media_limits"]["status"] == "warning" and got["media_limits"]["ads"] == [2]
    video.write_bytes(_mp4(242 * 60))
    assert _by(_audit(AuditMeta(), d))["media_limits"]["status"] == "error"
    video.write_bytes(_mp4(20, 1080, 1080))              # квадрат: вертикального файла нет
    got = _by(_audit(AuditMeta(), d))
    assert got["media_vertical"]["status"] == "warning" and got["media_vertical"]["ads"] == [2]
    assert not pf.blocks(_audit(AuditMeta(), d).audit), "отсутствие вертикали не блокирует"


def test_image_crops_warn_when_square_and_are_unknown_when_unreadable(tmp_path):
    d = _draft(tmp_path, ads=2)
    _vertical(d, 1080, 1080)
    got = _by(_audit(AuditMeta(), d))
    assert got["media_vertical"]["status"] == "warning" and got["media_vertical"]["ads"] == [1, 2]
    d = _draft(tmp_path, ads=1)           # b"creative-0": размеры не прочесть
    got = _by(_audit(AuditMeta(), d))
    assert got["media_vertical"]["status"] == "unknown"


def test_the_budget_minimum_by_currency_is_documented_and_unknown_when_unlisted(tmp_path):
    assert pf.MIN_DAILY_BUDGET == {"USD": 1.0} and "not listed" in pf.MIN_DAILY_BUDGET_SOURCE
    d = _draft(tmp_path, ads=1)
    d.ad_sets[0].daily_budget = 0.5
    got = _by(_audit(AuditMeta(), d))["budget_min"]
    assert got["status"] == "error" and "below the minimum" in got["detail"]
    d.ad_sets[0].daily_budget = 1.0
    assert _by(_audit(AuditMeta(), d))["budget_min"]["status"] == "ok"
    got = _by(_audit(AuditMeta(currency="BRL"), d))["budget_min"]
    assert got["status"] == "unknown" and "BRL" in got["detail"]
    assert not pf.blocks([got]), "неизвестный минимум не блокирует"


def test_an_existing_campaign_with_other_settings_is_a_rename_warning(tmp_path):
    from tests.test_acl1761_api_create import _run

    meta = AuditMeta()
    d = _draft(tmp_path, ads=1)
    _vertical(d)
    first, _ = _run(meta, d)
    assert first.error == ""
    meta.posts.clear()
    same = _by(_audit(meta, _draft(tmp_path, ads=1)))["existing_diffs"]
    assert same["status"] == "ok"
    d2 = _draft(tmp_path, ads=1)
    d2.ad_sets[0].daily_budget = 35.0
    got = _by(_audit(meta, d2))["existing_diffs"]
    # ACL-1902: другая кампания с этим именем - не ошибка, а предупреждение: робот возьмёт NAME_2.
    assert got["status"] == "warning" and "Will be published as" in got["detail"]
    assert "daily budget" in got["detail"] and not pf.blocks([got])


def test_existing_check_is_unknown_when_an_earlier_check_failed(tmp_path):
    d = _draft(tmp_path, ads=1)
    d.page_id = "999"
    got = _by(_audit(AuditMeta(), d))["existing_diffs"]
    assert got["status"] == "unknown" and "Not checked" in got["detail"]


def test_an_inactive_account_is_the_account_rule(tmp_path, monkeypatch):
    from src.web import publish_api

    meta = AuditMeta(status=2)
    monkeypatch.setattr(publish_api, "build_drafts", lambda slots, **kw: [_draft(tmp_path, ads=1)])
    _read_session(monkeypatch, meta)
    out = publish_api.check_plan([{"name": "x"}], account_label="Test", country="MX")
    assert out["results"] == [] and meta.posts == []
    assert out["account_audit"]["rule"] == "account_active" and out["account_audit"]["status"] == "error"
    assert "not active" in out["account_audit"]["detail"]


def test_check_plan_returns_the_audit_and_still_writes_nothing(tmp_path, monkeypatch):
    from src.web import publish_api

    meta = AuditMeta()
    d = _draft(tmp_path, ads=2)
    _vertical(d)
    monkeypatch.setattr(publish_api, "build_drafts", lambda slots, **kw: [d])
    opened = _read_session(monkeypatch, meta)
    out = publish_api.check_plan([{"name": "x"}], account_label="Test", country="MX")
    (res,) = out["results"]
    assert meta.posts == [] and opened[0].writes == 0 and opened[0].allow_writes is False
    assert out["account_audit"] is None and [v["rule"] for v in res["audit"]][0] == "account_active"
    assert not pf.blocks(res["audit"])


def test_plan_errors_come_without_a_server_audit(tmp_path, monkeypatch):
    from src.web import publish_api

    d = _draft(tmp_path, ads=1)
    d.link = ""
    monkeypatch.setattr(publish_api, "build_drafts", lambda slots, **kw: [d])
    _read_session(monkeypatch, AuditMeta())
    (res,) = publish_api.check_plan([{"name": "x"}], account_label="Test", country="MX")["results"]
    assert res.get("audit", []) == [] and "Tracking link is empty" in res["error"]


# ───────────────────────────── карточка и гейт (страница, Node) ─────────────────────────────

def _lines(case: dict) -> dict:
    return _harness("--lines", {"x": case})["x"]


def _status(lines: list, rule: str) -> str:
    return next(x["status"] for x in lines if x["rule"] == rule)


def test_the_card_under_rpa_has_client_lines_only():
    got = _lines({"input": _ok_input(), "opts": {"backend": "rpa"}})
    assert {x["side"] for x in got["lines"]} == {"client"} and got["blocks"] is False
    broken = _lines({"input": _with(link=""), "opts": {"backend": "rpa"}})
    assert broken["blocks"] is True and broken["count"]["error"] == 1


def test_the_card_under_api_waits_for_the_check_and_shows_the_server_verdicts(tmp_path):
    api = _lines({"input": _ok_input(), "opts": {"backend": "api", "server": None}})
    assert _status(api["lines"], "page_ok") == "unknown" and api["blocks"] is False
    d = _draft(tmp_path, ads=1)
    d.page_id = "999"
    out = _audit(AuditMeta(), d)
    shown = _lines({"input": _ok_input(), "opts": {"backend": "api", "server": out.audit}})
    assert _status(shown["lines"], "page_ok") == "error" and shown["blocks"] is True
    stale = _lines({"input": _ok_input(), "opts": {"backend": "api", "server": out.audit, "stale": True}})
    assert _status(stale["lines"], "page_ok") == "unknown" and stale["blocks"] is False
    assert "press Check" in next(x for x in stale["lines"] if x["rule"] == "page_ok")["detail"]


def test_warnings_never_block_and_a_server_warning_rule_cannot_become_an_error():
    warn = _with(link="https://track.example.com/c", ads=_ad(0, headline="h" * 50))
    got = _lines({"input": warn, "opts": {"backend": "rpa"}})
    assert got["count"]["warning"] == 2 and got["blocks"] is False
    forged = [{"rule": "media_vertical", "status": "error", "detail": "x", "ads": [1]}]
    shown = _lines({"input": _ok_input(), "opts": {"backend": "api", "server": forged}})
    assert _status(shown["lines"], "media_vertical") == "warning" and shown["blocks"] is False


def test_a_plan_error_without_verdicts_still_blocks():
    shown = _lines({"input": _ok_input(), "opts": {"backend": "api", "server": [], "planError": "Ad 1: boom"}})
    assert shown["blocks"] is True and shown["lines"][-1]["rule"] == "plan_error"


def test_the_account_verdict_is_shown_on_the_account_line():
    acc = {"rule": "account_active", "status": "error", "detail": "not active", "ads": []}
    shown = _lines({"input": _ok_input(), "opts": {"backend": "api", "server": [], "accountAudit": acc}})
    assert _status(shown["lines"], "account_active") == "error" and shown["blocks"] is True


def _gate(template: Path) -> subprocess.CompletedProcess:
    return subprocess.run([_node(), str(ROOT / "tests" / "preflight_gate_harness.cjs"), str(template)],
                          capture_output=True, text=True, encoding="utf-8", timeout=90)


def test_the_publish_gate_blocks_on_errors_and_lists_warnings():
    r = _gate(TPL)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().endswith("ok")


@pytest.mark.parametrize("old,new,label", [
    ("  if (bad.length) { stop(bad); return; }\n  function done() {",
     "  if (false) { stop(bad); return; }\n  function done() {", "client errors no longer block"),
    ("    if (again.length) { stop(again); return; }\n    window.CB_PF_WARN",
     "    if (false) { stop(again); return; }\n    window.CB_PF_WARN", "server errors no longer block"),
    ("function blocks(ls) { return ls.some(function (x) { return x.status === 'error'; }); }",
     "function blocks(ls) { return false; }", "blocks() never fires"),
    ("    var stale = it.slots.filter(function (slot) { return !cbPfFresh(slot); });",
     "    var stale = [];", "server checks are never refreshed before Publish"),
    ("  if (!tree || !tree.auditLines || !window.cbPreflight || !rules.length) { cbPfUnavailable(); return; }",
     "  if (false) { cbPfUnavailable(); return; }", "a missing tree or rule table lets Publish through"),
    ("      return null;                         // состояние аудита не читается — тоже запрет",
     "      return [];                           // состояние аудита не читается — тоже запрет",
     "an audit crash lets Publish through"),
    ("  if (!cbApiMode()) { done(); return; }",
     "  if (false) { done(); return; }", "RPA also runs the account checks"),
])
def test_mutating_the_gate_makes_its_test_fail(tmp_path, old, new, label):
    text = TPL.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"точка мутации не найдена: {label}"
    mutated = tmp_path / "report_mutated.html"
    mutated.write_text(text.replace(old, new), encoding="utf-8")
    r = _gate(mutated)
    assert r.returncode != 0, f"мутация не поймана: {label}"


def test_the_publish_buttons_are_wrapped_by_the_gate_and_the_originals_are_untouched():
    page = TPL.read_text(encoding="utf-8")
    assert "window.cbPreflightGate([{ form: form, slots: slots }], function () { origOne(form); });" in page
    assert "window.cbPreflightGate(items, function () { origAll(onProgress, picked, onlyKeys); });" in page
    assert "origAll = window.cbPublishAll, origOne = window.cbPublish" in page
    assert page.index("window.cbPublish = function(form) {") < page.index("origAll = window.cbPublishAll")
    # серверные проверки идут только по кнопке и перед Publish: ни таймера, ни события ввода
    run = page[page.index("window.cbPreflightRun = function"):page.index("window.cbPreflightRun = function") + 1200]
    assert "setTimeout" not in run and "setInterval" not in run and "addEventListener" not in run
    block = page[page.index("/* ── Pre-flight audit (ACL-1819)"):page.index("  function paintEditor() {")]
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)       # в комментариях эти слова упомянуты
    assert "setInterval" not in block and "/publish-plan" not in block, "карточка сама в сеть не ходит"
    assert "setTimeout" not in block


# ───────────────────────────── ревью #821: имена, поворот, ворота наглухо ─────────────────────────────

NAME_CASES = [
    ("2046 1.jpg", "Text A"), ("2046 1.mp4", "Text A"), ("1_es_2403_post.mp4", "Text B"),
    ("1000_img.png", "Text0"), ("coffee.mp4", "Text C"), ("1080x1080-17.png", "Text D"),
    (r"C:\files\2403_square.JPG", "Text E"), ("", "Only text"), ("2046.png", ""), ("", ""),  # linux-ok: Windows path is only data for the name function, which must take the basename of backslash paths too
    ("12_2403.png", "T"), ("2403x1080_3011.png", "T"),
    # ACL-2090: вариант в конце имени, размеры и соотношения сторон — не вариант.
    ("2175_ARM22.png", "T"), ("2175_ADN3.png", "T"), ("2175_ARM07_1080x1920.png", "T"),
    ("2175_post_9x16.mp4", "T"), ("2175_post_9:16.mp4", "T"), ("2175_post_1080х1920.png", "T"),
    ("12_2175_ARM22.png", "T"), ("2175_ARM22_Story.png", "T"),
]


def test_the_client_names_ads_exactly_like_the_server():
    from src.fb_ads_rpa.draft import AdDraft

    js = _harness("--names", [list(c) for c in NAME_CASES])["names"]
    server = [AdDraft(creative_filename=f, copy_name=t).ad_name() for f, t in NAME_CASES]
    assert js == server


def test_a_jpg_and_an_mp4_of_the_same_creative_with_the_same_text_are_duplicates():
    names = _harness("--names", [["2046 1.jpg", "Text A"], ["2046 1.mp4", "Text A"]])["names"]
    assert names[0] == names[1] == "2046 1 - Text A"  # ACL-2090: «1» в конце — вариант
    ads = [{"name": n, "file": f, "form_id": "", "body": "b", "headline": "h", "cta": "LEARN_MORE"}
           for n, f in zip(names, ["2046 1.jpg", "2046 1.mp4"])]
    inp = _with(ads=ads)
    got = {v["rule"]: v for v in pf.evaluate_client(inp)}["ad_names_unique"]
    assert got["status"] == "error" and got["ads"] == [1, 2]
    # Те же объявления создатель тоже отвергает.
    from src.fb_ads_rpa.draft import AdDraft
    assert AdDraft(creative_filename="2046 1.jpg", copy_name="Text A").ad_name() ==         AdDraft(creative_filename="2046 1.mp4", copy_name="Text A").ad_name()


def test_the_vertical_check_follows_the_tkhd_rotation(tmp_path):
    assert pf.video_info(_mp4(10, 1920, 1080, rotate=True))["width"] == 1080
    assert pf.video_info(_mp4(10, 1920, 1080, rotate=True))["height"] == 1920
    assert pf.video_info(_mp4(10, 1080, 1920))["height"] == 1920
    d = _draft(tmp_path, video=True, ads=2)
    _vertical(d)
    video = Path(d.ad_sets[0].ads[1].creative_path)
    video.write_bytes(_mp4(20, 1920, 1080, rotate=True))     # хранится лёжа, показывается стоя
    assert _by(_audit(AuditMeta(), d))["media_vertical"]["status"] == "ok"
    video.write_bytes(_mp4(20, 1080, 1920, rotate=True))     # хранится стоя, показывается лёжа
    got = _by(_audit(AuditMeta(), d))["media_vertical"]
    assert got["status"] == "warning" and got["ads"] == [2]


def test_the_retry_of_an_unanswered_submission_goes_through_the_gate():
    page = TPL.read_text(encoding="utf-8")
    i = page.index("    e.resend = function () {")
    body = page[i:page.index("    e.dismiss = function () {", i)]
    assert "window.cbPreflightGate([{ form: e.form, slots: slots }]" in body
    assert body.index("cbPreflightGate") < body.index("post(e, fd, skipped, sentNames)")
    assert "typeof window.cbPreflightGate !== 'function'" in body and "Pre-flight audit unavailable" in body


def test_resolve_page_sets_the_account_default_source():
    # ACL-1897: прямая проверка источника; кабинет с несколькими Страницами не стопорит запуск.
    plan = cr.CampaignPlan.__new__(cr.CampaignPlan)
    plan.page_id, plan.page_source = "", ""
    facts = cr.AccountFacts(act=ACT, currency="USD", pages=["111", "222"], pixels=[])
    cr.resolve_page(plan, facts)
    assert (plan.page_id, plan.page_source) == ("111", "account default")
