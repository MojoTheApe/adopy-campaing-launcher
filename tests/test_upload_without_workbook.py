"""Заливка перестаёт зависеть от `Upload FB.xlsx`.

Книга держалась в пути публикации на четырёх вещах. Разбор 9 сентября показал,
что две из них уже не нужны, а держались на убеждении, что нужны:

* **пиксель** заполнялся из книги, клался в черновик, печатался в превью
  строкой `Pixel: —` — и не использовался больше нигде: ни один шаг его не
  набирает и не выбирает;
* **креативы** давно ищутся `/api/creatives`, который обходит папку;
  экселевский поиск срабатывает, только если имя файла пустое, а у черновика
  из билдера оно заполнено всегда.

Оставшиеся две — решение пользователя:

* **интересы** больше не подставляются из книги. Не нашлось в библиотеке —
  сказать об этом, а кампанию поднять можно и без детального таргетинга;
* **язык** переехал в билдер. Умолчание считается по стране (GCC — арабский,
  LatAm — испанский), для прочих стран поле пустое и подсвечено: угадывать
  язык за человека мы не станем, но и промолчать нельзя.

И то, без чего книга не ушла бы совсем: **ссылка**. Она уже жила в машинной
карте, но книга её перебивала. Порядок перевёрнут.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import enrich_draft as en  # noqa: E402
from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft  # noqa: E402


@pytest.fixture
def enrich(monkeypatch, tmp_path):
    """Обогащение без книги, без карты и без сети — только проверяемое решение."""
    monkeypatch.setattr(en, "load_ad_account_map", lambda p: {})
    monkeypatch.setattr(
        en, "resolve_account_entry",
        lambda *a: SimpleNamespace(ad_account_id="act_test", page_id="page", profile_id="profile"),
    )

    def run(draft, ref=None):
        if ref is not None:
            monkeypatch.setattr(en, "_get_ref", lambda path: ref)
            monkeypatch.setattr(en.os.path, "isfile", lambda p: True)
        en.enrich_campaign_drafts(
            [draft], account_label="Test",
            upload_fb_path=str(tmp_path / "none.xlsx"),
            ad_account_map_path=str(tmp_path / "map.csv"),
            creatives_root=str(tmp_path),
            preview_mode=True,
        )
        return draft

    return run


def _draft(**kw):
    kw.setdefault("ad_sets", [AdSetDraft("Set", targeting_label="")])
    return CampaignDraft("Test", "SA", "Test", **kw)


# ── ссылка ────────────────────────────────────────────────────────────────

def test_an_empty_tracking_link_warns_and_does_not_block(enrich):
    """Договорённость 8 сентября. Черновик без адреса — это и есть черновик.

    Плеер печатает ссылку как `d.link or None`: пустую он просто не набирает.
    Запрещать публикацию из-за неё значило запрещать то, ради чего RPA и
    существует — и запрет стоял, пока форма рядом писала «не обязательно».
    """
    d = enrich(_draft())
    assert not any("link" in e.lower() for e in d.errors), d.errors
    assert any("tracking link not set" in w for w in d.warnings), d.warnings


def test_the_link_typed_in_the_builder_survives_enrichment(enrich):
    """Иначе человек вписывает ссылку и видит «Link empty» — искать нечего."""
    d = enrich(_draft(link="https://tkr.example.com/typed"))
    assert d.link == "https://tkr.example.com/typed"
    assert not any("tracking link" in w for w in d.warnings)


def test_the_workbook_fills_the_link_only_when_the_form_did_not(enrich):
    ref = {"accounts": {"Test": {"link": "https://tkr.example.com/book"}}}
    assert enrich(_draft(), ref=ref).link == "https://tkr.example.com/book"


# ── интересы ──────────────────────────────────────────────────────────────

def test_a_targeting_missing_from_the_library_is_named_not_substituted(enrich, monkeypatch):
    """Книга подставляла интересы, которых человек не выбирал.

    Делала она это предупреждением, а не вопросом — то есть кампания уходила с
    чужим набором, и узнать об этом можно было только вчитавшись в жёлтую
    строку под превью.
    """
    monkeypatch.setattr(
        "src.analysis_agent.gsheet_targeting.lookup_targeting_interests",
        lambda name: "",
    )
    ref = {"interests": {"Day": '["Swing trading"]'}}
    d = enrich(_draft(ad_sets=[AdSetDraft("Set", targeting_label="Day")]), ref=ref)
    assert d.ad_sets[0].targeting_label == "", "интересы подставлены из книги"
    assert any("not in the library" in w for w in d.warnings), d.warnings
    assert not d.errors, d.errors


def test_a_targeting_found_in_the_library_is_used(enrich, monkeypatch):
    monkeypatch.setattr(
        "src.analysis_agent.gsheet_targeting.lookup_targeting_interests",
        lambda name: "Investment, Trading",
    )
    d = enrich(_draft(ad_sets=[AdSetDraft("Set", targeting_label="FX")]))
    assert d.ad_sets[0].targeting_label == "Investment, Trading"


# ── язык ──────────────────────────────────────────────────────────────────

def test_the_language_chosen_in_the_builder_is_not_overwritten(enrich):
    """Правило по стране стало умолчанием формы, а не заменой ответа.

    Раньше строка обогащения перетирала язык безусловно: выбрать в билдере
    что-то своё было невозможно, поле бы ничего не решало.
    """
    d = enrich(_draft(language="Hebrew"))
    assert d.language == "Hebrew"


def test_an_empty_language_falls_back_to_the_country_rule(enrich):
    """Поле не трогали — правило работает, как и раньше."""
    assert enrich(_draft()).language == "Arabic"


def test_a_country_outside_the_two_families_stays_empty_and_says_so(enrich):
    """Решение пользователя 9 сентября: не угадывать, но и не молчать."""
    d = enrich(CampaignDraft("Test", "DE", "Test", ad_sets=[AdSetDraft("Set")]))
    assert d.language == ""
    assert any("audience language not set" in w for w in d.warnings), d.warnings
    assert not d.errors


def test_the_language_step_is_skippable_without_an_error(enrich):
    """Язык сняли намеренно — это выбор, а не недоделка."""
    d = enrich(CampaignDraft("Test", "DE", "Test", language="", ad_sets=[AdSetDraft("Set")]))
    assert not d.errors


# ── пиксель ───────────────────────────────────────────────────────────────

def test_the_pixel_is_not_carried_from_the_workbook(enrich):
    """Он не использовался ни одним шагом — только занимал строку в превью."""
    ref = {"accounts": {"Test": {"pixel": "999", "link": "https://x.example.com"}}}
    assert enrich(_draft(), ref=ref).pixel_id == ""


def test_no_preview_shows_a_pixel_row():
    """Строка `Pixel: —` держалась вечно и не отвечала ни на один вопрос."""
    for path in ("src/fb_ads_rpa/draft.py", "src/fb_ads_rpa/preview.py"):
        assert '"Pixel"' not in (ROOT / path).read_text(encoding="utf-8"), path


# ── текст объявления ──────────────────────────────────────────────────────

def test_the_ad_text_comes_from_the_library(enrich, monkeypatch, tmp_path):
    """В превью стояло «Body: —»: обогащение ходило в таблицу, а не в реестр."""
    from src.analysis_agent import ad_text_registry as ar

    monkeypatch.setattr(ar.paths, "get_local_config_dir", lambda: str(tmp_path))
    ar.add("AR Text 1", "للمزيد", "كم لديك من المال اليوم", "★★★★")
    monkeypatch.setattr(
        "src.web.ad_copy_gsheet.lookup_ad_copy_by_title",
        lambda name: pytest.fail("пошли в таблицу при полной библиотеке"),
    )
    d = enrich(_draft(ad_sets=[AdSetDraft("Set", ads=[AdDraft(copy_name="AR Text 1")])]))
    ad = d.ad_sets[0].ads[0]
    assert ad.ad_title == "للمزيد"
    assert ad.ad_body == "كم لديك من المال اليوم"
    assert ad.ad_link_description == "★★★★"


def test_an_ad_text_without_a_body_is_named(enrich, monkeypatch, tmp_path):
    """Пустое тело доедет до Ads Manager молча, если о нём не сказать."""
    monkeypatch.setattr(
        "src.web.ad_copy_gsheet.lookup_ad_copy_by_title",
        lambda name: {"headline": "H", "body": "", "description": ""},
    )
    d = enrich(_draft(ad_sets=[AdSetDraft("Set", ads=[AdDraft(copy_name="Нет такого")])]))
    assert any("no body" in w for w in d.warnings), d.warnings


# ── что осталось от книги в билдере ───────────────────────────────────────

def test_the_builder_no_longer_reads_creative_lists_from_the_workbook():
    """`/api/creatives` обходит папку; экселевские списки не потреблял никто."""
    from src.web import app as web

    source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    block = source[source.index("def _load_ref_options"): source.index("def _aggregate_key_to_query")]
    assert 'ref.get("image_files")' not in block
    assert 'ref.get("video_files")' not in block
    assert hasattr(web, "_load_ref_options")


def test_the_language_default_comes_from_the_rule_not_from_the_template():
    """Два списка стран разошлись бы молча: на экране одно, в Ads Manager другое."""
    from src.web import app as web

    assert web._geo_language("Saudi Arabia") == "Arabic"
    assert web._geo_language("Costa Rica") == "Spanish"
    assert web._geo_language("Germany") == ""


def test_the_builder_form_carries_a_language_field():
    page = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert "_language" in page, "поля языка в форме нет"
    assert "geo_language" in page, "умолчание не берётся у правила"
    assert "cb-language-empty" in page, "пустой язык ничем не помечен"
    assert "cbLanguageChanged" in page, "подсветка не обновляется после снятия языка"


def test_the_form_field_reaches_the_slot():
    """Поле, которое не доезжает до черновика, — это контрол-обманка."""
    source = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert '"language": language' in source, "язык не кладётся в слот"
    build = (ROOT / "src" / "fb_ads_rpa" / "build_draft.py").read_text(encoding="utf-8")
    assert 'first.get("language")' in build, "черновик не читает язык из слота"


# ── два строителя одной карточки ──────────────────────────────────────────

def test_both_slot_builders_produce_the_same_fields():
    """Карточку кампании строят два места, и они обязаны совпадать.

    Одно — шаблон на сервере, для карточек из рекомендаций. Второе — функция
    `buildBlankSlotHTML` в JavaScript, для «+ Add Campaign».

    9 сентября поле языка добавили только в первое. Выглядело добавленным: на
    странице девятнадцать полей. А человек в тот момент собирал кампанию
    вручную — рекомендаций за выбранный день не было ни у кого, — и на его
    половине поля не оказалось. «Я сделал рифреш, а языка всё ещё нет».

    Поэтому сверяются **множества имён полей**, а не наличие одного поля:
    следующее разойдётся так же тихо.
    """
    page = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")

    slot = page[page.index('<div class="cb-slot" data-idx="{{ ns.idx }}">'):]
    slot = slot[: slot.index("cb-slot-grid")]
    from_template = set(re.findall(r'name="camp_\{\{ ns\.idx \}\}_([a-z_]+)"', slot))

    js = page[page.index("function buildBlankSlotHTML"):]
    js = js[: js.index("cb-slot-grid")]
    from_script = set(re.findall(r"name=\"camp_' \+ idx \+ '_([a-z_]+)\"", js))
    from_script |= set(re.findall(r"mkSel\(idx, '([a-z_]+)'", js))

    assert from_template, "не нашёл полей в шаблонной карточке — проверка сломана"
    assert from_script, "не нашёл полей в скриптовой карточке — проверка сломана"
    assert from_template == from_script, (
        "строители карточки разошлись: только в шаблоне "
        f"{sorted(from_template - from_script)}, только в скрипте "
        f"{sorted(from_script - from_template)}"
    )


def test_the_manual_card_gets_the_country_default_for_the_language():
    """Иначе «+ Add Campaign» даёт пустой язык там, где правило его знает."""
    page = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    assert 'data-language="{{ c.country | geo_language }}"' in page, (
        "форма не несёт умолчание языка — скрипту неоткуда его взять"
    )
    assert "form.dataset.language" in page, "скрипт не читает умолчание"
