"""ACL-2180: Instant Form locale "Auto" - язык формы из языка и страны набора.

Мета не вызывается: Graph - `FormsMeta` в памяти. Проверяется таблица соответствий,
отказ ДО любой записи, устойчивость хэша, старые шаблоны с явным языком и страница.
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from src.fb_api import form_locale as fl  # noqa: E402
from src.fb_api import form_templates as ft  # noqa: E402
from tests.test_acl1776_create_forms import (  # noqa: E402,F401
    TEMPLATE, FormsMeta, _creator, _library, _run, _saved, _tpl_draft)

HTML = ROOT / "src" / "web" / "templates" / "report.html"


@pytest.mark.parametrize("language,country,expected", [
    ("Spanish", "Colombia", "ES_LA"), ("Spanish", "MX", "ES_LA"), ("Spanish (All)", "Argentina", "ES_LA"),
    ("Spanish", "Spain", "ES_ES"), ("Spanish", "ES", "ES_ES"),
    ("Portuguese", "Brazil", "PT_BR"), ("Portuguese", "Portugal", "PT_PT"), ("Portuguese", "Angola", "PT_PT"),
    ("English", "United Kingdom", "EN_GB"), ("English", "GB", "EN_GB"), ("English", "United States", "EN_US"),
    ("English", "India", "EN_US"), ("English (All)", "Canada", "EN_US"),
    ("Arabic", "Egypt", "AR_AR"), ("French", "France", "FR_FR"), ("German", "Austria", "DE_DE"),
    ("Italian", "Italy", "IT_IT"), ("Turkish", "Turkey", "TR_TR"), ("Russian", "Kazakhstan", "RU_RU"),
    ("Polish", "Poland", "PL_PL"), ("Japanese", "Japan", "JA_JP"), ("Vietnamese", "Vietnam", "VI_VN"),
    ("Chinese", "Taiwan", "ZH_TW"), ("Chinese", "HK", "ZH_HK"), ("Chinese", "China", "ZH_CN"),
    ("English", "", "EN_US"), ("German", "Narnia", "DE_DE"),
    ("Spanish", "Colombia, Mexico", "ES_LA"),
])
def test_mapping_table_cases(language, country, expected):
    assert fl.resolve_locale(language, country) == expected


@pytest.mark.parametrize("language,country,text", [
    ("Ukrainian", "Ukraine", "no Instant Form language for language 'Ukrainian', country 'Ukraine'"),
    ("", "Mexico", "language '(empty)'"),
    ("Spanish", "United States", "language 'Spanish', country 'United States'"),
    ("Spanish", "", "the country is needed"),
    ("Spanish", "Spain, Mexico", "different form languages (ES_ES, ES_LA)"),
    ("Chinese", "Singapore", "language 'Chinese', country 'Singapore'"),
    ("English", "United Kingdom, Ireland", "different form languages (EN_GB, EN_US)"),
])
def test_unknown_or_ambiguous_pairs_are_refused_with_a_clear_message(language, country, text):
    with pytest.raises(fl.LocaleRefused) as e:
        fl.resolve_locale(language, country)
    assert text in str(e.value) and "Form locale Auto" in str(e.value)


def test_every_mapped_locale_is_in_the_allowed_list_and_named():
    codes = {c for rule in fl.RULES.values() for c in rule.values()}
    assert codes <= set(ft.LOCALES)
    assert set(fl.LOCALE_NAMES) == set(ft.LOCALES)
    assert fl.LOCALE_NAMES["ES_LA"] == "Spanish (Latin America)" and fl.LOCALE_NAMES["PT_BR"] == "Portuguese (Brazil)"


def _auto(**over):
    return _saved(locale="AUTO", **over)


def test_auto_is_a_valid_template_locale_and_cannot_be_sent_unresolved():
    clean, errors = ft.validate({**TEMPLATE, "locale": "auto"})
    assert errors == [] and clean["locale"] == "AUTO"
    with pytest.raises(ValueError, match="Auto must be resolved"):
        ft.form_params(clean)
    _c, bad = ft.validate({**TEMPLATE, "locale": "english"})
    assert bad and "Unknown locale" in bad[0]


def test_the_plan_resolves_auto_from_the_ad_set_language_and_country(tmp_path):
    tid = _auto()["id"]
    plan = cr.plan_from_draft(_tpl_draft(tmp_path, [tid]))      # черновик: Spanish + MX
    ad = plan.ad_sets[0].ads[0]
    assert ad.form_template["locale"] == "ES_LA"
    assert ad.form_locale_note == "Form locale: ES_LA (Spanish, MX)"
    assert ft.form_params(ad.form_template)["locale"] == "ES_LA"


def test_several_languages_the_primary_language_drives_the_form_locale(tmp_path):
    # ACL-1767: дополнительные языки набора на форму не влияют; главный - Language набора.
    tid = _auto()["id"]
    d = _tpl_draft(tmp_path, [tid])
    d.ad_sets[0].languages = ["English", "Portuguese"]
    plan = cr.plan_from_draft(d)
    assert plan.ad_sets[0].ads[0].form_template["locale"] == "ES_LA"


def test_an_unknown_pair_stops_the_plan_before_any_graph_call(tmp_path):
    tid = _auto()["id"]
    d = _tpl_draft(tmp_path, [tid])
    d.language = "Ukrainian"
    d.country = "UA"
    meta = FormsMeta()
    with pytest.raises(cr.PlanError, match="Form locale Auto: no Instant Form language for language 'Ukrainian'"):
        _run(meta, d)
    assert meta.posts == [] and meta.form_posts == []


def test_a_refused_pair_in_the_published_run_writes_nothing(tmp_path):
    tid = _auto()["id"]
    d = _tpl_draft(tmp_path, [tid])
    d.country = "US"
    meta = FormsMeta()
    with pytest.raises(cr.PlanError, match="country 'US'"):
        _run(meta, d)
    assert meta.posts == []


class _EnglishUsMeta(FormsMeta):
    """FormsMeta, который знает ещё и США и английский (исходный умеет лишь Мексику и испанский)."""

    def _get(self, path, params):
        if path == "/search" and params.get("type") == "adgeolocation" and params.get("q") in ("US", "United States"):
            return {"data": [{"key": "US", "country_code": "US", "name": "United States"}]}
        if path == "/search" and params.get("type") == "adlocale" and params.get("q") == "English":
            return {"data": [{"key": 6, "name": "English (All)"}]}
        return super()._get(path, params)


def test_the_same_template_makes_one_form_per_language_and_never_a_second_for_the_same_one(tmp_path):
    tid = _auto()["id"]
    meta = _EnglishUsMeta()
    es, _ = _run(meta, _tpl_draft(tmp_path, [tid]))
    assert es.error == "" and [p["locale"] for _, p in meta.form_posts] == ["ES_LA"]
    again, _ = _run(meta, _tpl_draft(tmp_path, [tid], name="MX Leads 2"))      # тот же язык и страна
    assert again.error == "" and len(meta.form_posts) == 1
    assert again.reused["forms"] == [es.created["forms"][0]]
    en_draft = _tpl_draft(tmp_path, [tid], name="US Leads")
    en_draft.language, en_draft.country = "English", "US"
    creator, _client = _creator(meta, writes=False)
    out = creator.plan_only(cr.plan_from_draft(en_draft))      # английский набор: форма ещё не создана
    assert out.error == "", out.error
    item = out.plan["summary"]["ad_sets"][0]["ads"][0]
    assert item["form_action"] == "create" and item["form_locale"] == "Form locale: EN_US (English, US)"
    assert item["form_name"] != meta.form_posts[0][1]["name"] and len(meta.form_posts) == 1


def test_check_plan_shows_the_resolved_locale_and_writes_nothing(tmp_path):
    tid = _auto()["id"]
    meta = FormsMeta()
    creator, _client = _creator(meta, writes=False)
    out = creator.plan_only(cr.plan_from_draft(_tpl_draft(tmp_path, [tid])))
    assert out.error == "" and meta.posts == []
    item = out.plan["summary"]["ad_sets"][0]["ads"][0]
    assert item["form_locale"] == "Form locale: ES_LA (Spanish, MX)"
    assert "ad.form_locale" in HTML.read_text(encoding="utf-8")


def test_an_auto_form_equals_the_manual_form_with_the_same_resolved_locale(tmp_path):
    auto = cr.plan_from_draft(_tpl_draft(tmp_path, [_auto()["id"]])).ad_sets[0].ads[0].form_template
    manual = ft.validate({**TEMPLATE, "locale": "ES_LA"})[0]
    assert ft.form_name(auto) == ft.form_name(manual) and ft.content_hash(auto) == ft.content_hash(manual)


def test_templates_with_a_manual_locale_behave_exactly_as_before(tmp_path):
    clean = ft.validate(TEMPLATE)[0]
    tid = _saved()["id"]                                  # EN_US вручную, черновик испанский
    plan = cr.plan_from_draft(_tpl_draft(tmp_path, [tid]))
    tpl = plan.ad_sets[0].ads[0].form_template
    assert tpl["locale"] == "EN_US" and tpl["id"] == tid and plan.ad_sets[0].ads[0].form_locale_note == ""
    assert ft.form_name(tpl) == ft.form_name(clean)
    legacy = {**copy.deepcopy(clean), "id": "bbbbbbbbbbbb", "locale": "ES_MX"}
    p2 = cr.plan_from_draft(_tpl_draft(tmp_path, ["bbbbbbbbbbbb"]), template_loader=lambda _i: legacy)
    assert p2.ad_sets[0].ads[0].form_template["locale"] == "ES_MX"
    # Без поля locale по-прежнему EN_US (старые файлы), Auto - только явный выбор.
    no_locale = {k: v for k, v in TEMPLATE.items() if k != "locale"}
    assert ft.validate(no_locale)[0]["locale"] == "EN_US"


# ───────────────────────── страница ─────────────────────────

def _js_object(name: str):
    page = HTML.read_text(encoding="utf-8")
    m = re.search(r"var %s = (\{.*?\});\n" % name, page, re.S)
    assert m, name
    return json.loads(m.group(1))


def test_the_page_mapping_is_the_server_mapping():
    assert _js_object("FORM_LOCALE_RULES") == fl.RULES
    assert _js_object("FORM_LOCALE_NAMES") == fl.LOCALE_NAMES
    label = re.search(r'var FORM_LOCALE_AUTO_LABEL = (".*?");', HTML.read_text(encoding="utf-8"))
    assert json.loads(label.group(1)) == fl.AUTO_LABEL


def test_both_dropdowns_use_the_shared_option_builder():
    page = HTML.read_text(encoding="utf-8")
    assert '<select id="fm-locale"' in page and 'id="fm-locale" type="text"' not in page
    assert "formLocaleOptions('AUTO')" in page and "panelValue(panel, '.cb-fc-locale') || 'AUTO'" in page
    assert "locale: el('fm-locale').value || 'AUTO'" in page
