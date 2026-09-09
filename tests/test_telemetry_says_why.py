"""Телеметрия обязана говорить не только «что», но и «почему».

Прогон 9 сентября дал шесть отказов подряд — `placements`, `ad_copy`,
`ad_destination` дважды, — и по записям нельзя было сказать про них ничего:
у всех `diag` пуст. Сорок три места в потоке умеют сообщить об отказе, и **ни
одно** не передавало причину.

Хуже другое: шаг детального таргетинга целиком стоял под `if targeting_label:`.
Пустой список интересов не оставлял ни строки — шага в потоке просто не
существовало. На экране это выглядело как «интересы не проставились», а
отличить «нечего было вводить» от «не нашёл поле» было нечем: оба исхода
выглядели одинаково, то есть никак.

Это первый этап проекта Navigator и условие для всего остального: агент,
слушающий такой поток, узнает ровно столько же, сколько человек. Но пункт
полезен и сам по себе — причина отказа нужна тому, кто сидит перед экраном.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402


class _Page:
    """Страница, отвечающая ровно то, что ей велели."""

    def __init__(self, url="", title="", dialogs=0, banner=None, raises=()):
        self.url = url
        self._title = title
        self._dialogs = dialogs
        self._banner = banner
        self._raises = raises

    def title(self):
        if "title" in self._raises:
            raise RuntimeError("нет")
        return self._title

    def get_by_role(self, role):
        if role in self._raises:
            raise RuntimeError("нет")
        page = self

        class _Loc:
            def count(self):
                return page._dialogs

            @property
            def first(self):
                return self

            def is_visible(self, timeout=0):
                return page._banner is not None

            def inner_text(self, timeout=0):
                return page._banner or ""

        return _Loc()


# ── третий исход: пропущено ───────────────────────────────────────────────

def test_a_skipped_step_is_neither_success_nor_failure():
    """`ok=None` не попадает в долю неудач: пропуск — не провал.

    Но и не удача: раньше пустые интересы засчитывались успехом молча.
    """
    assert rt.normalise_step("skip_targeting") == ("targeting", None)


def test_success_and_failure_still_mean_what_they_meant():
    assert rt.normalise_step("after_languages") == ("languages", True)
    assert rt.normalise_step("fail_placements") == ("placements", False)


# ── контекст отказа ───────────────────────────────────────────────────────

def test_the_failure_carries_what_was_on_the_page():
    ctx = rt.page_context(_Page(url="https://adsmanager.facebook.com/x", title="Ads Manager", dialogs=1))
    assert ctx["url"].endswith("/x")
    assert ctx["title"] == "Ads Manager"
    assert ctx["dialogs"] == 1


def test_the_banner_is_captured_because_it_is_what_hides_the_button():
    """Верхний баннер Meta сдвигает и закрывает контролы.

    24 июля из-за него прогон не проходил дальше списка кампаний, и понять это
    удалось только по скриншоту.
    """
    ctx = rt.page_context(_Page(banner="We're evolving how we measure results"))
    assert "evolving" in ctx["banner"]


def test_the_account_number_never_reaches_the_context():
    """Контекст уезжает в общую таблицу, которую читает вся компания."""
    ctx = rt.page_context(_Page(url="https://adsmanager.facebook.com/manage/campaigns?act=1234567890123456"))
    assert "1234567890123456" not in ctx["url"]
    assert "act=<hidden>" in ctx["url"]


def test_a_profile_id_never_reaches_the_context():
    ctx = rt.page_context(_Page(title="550e8400-e29b-41d4-a716-446655440000"))
    assert "550e8400" not in ctx["title"]


def test_a_long_number_anywhere_is_hidden():
    """Номер кабинета встречается не только в `act=`."""
    assert "9876543210123" not in rt.scrub("campaign 9876543210123 selected")


def test_a_page_that_answers_nothing_still_yields_what_it_can():
    """Строка лога не должна стоить кампании — то же правило, что у `record`.

    Страница на упавшем шаге бывает в любом состоянии. Что удалось спросить —
    записываем, на чём споткнулись — молча пропускаем, но не теряем остальное.
    """
    ctx = rt.page_context(_Page(url="https://x.example/page", raises=("title", "dialog", "banner")))
    assert ctx == {"url": "https://x.example/page"}, ctx


def test_a_page_that_is_gone_entirely_still_returns_a_dict():
    class _Dead:
        @property
        def url(self):
            raise RuntimeError("target closed")

        def title(self):
            raise RuntimeError("target closed")

        def get_by_role(self, role):
            raise RuntimeError("target closed")

    assert rt.page_context(_Dead()) == {}


# ── где это включено ──────────────────────────────────────────────────────

def test_the_context_is_attached_at_the_single_choke_point():
    """Сорок три места сообщают об отказе, и ни одно не передавало причину.

    Собирать её в каждом значило бы завести сорок три места, которые разойдутся.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("def _shot(label: str"):]
    block = block[: block.index("_shot_clock[\"t\"] = now")]
    assert "_telemetry.page_context(page)" in block, "контекст не собирается в точке записи"
    assert "ok is False" in block, "контекст собирается не только у отказов"


def test_an_explicit_diag_is_not_overwritten_by_the_collected_one():
    """Где причина известна точно, она важнее общего снимка обстановки."""
    merged = rt.merge_diag({"url": "u", "why": "общее"}, {"why": "точное"})
    assert merged["why"] == "точное"


def test_a_step_that_explains_itself_still_keeps_the_page_address():
    """Иначе за собственную причину шаг платит адресом страницы.

    По адресу и заголовку отказ сопоставляют со снимком разметки. Прежнее
    условие `if diag is None` их отбрасывало ровно у тех шагов, которые
    рассказали о себе больше всех.
    """
    merged = rt.merge_diag({"url": "u", "title": "t"}, {"why": "видно 3 категории из 5"})
    assert merged == {"url": "u", "title": "t", "why": "видно 3 категории из 5"}


def test_nothing_to_say_stays_nothing():
    """Пустой словарь в записи читался бы как «причину искали и не нашли»."""
    assert rt.merge_diag(None, None) is None
    assert rt.merge_diag({}, {}) is None


def test_the_merge_is_the_one_the_step_recorder_uses():
    """Сторож: правило проверяется здесь только если код зовёт именно его."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("def _shot(label: str"):]
    block = block[: block.index("_shot_clock[\"t\"] = now")]
    assert "merge_diag" in block, "запись отказа соединяет причины сама по себе"


def test_an_empty_targeting_now_says_so():
    """Тот самый молчащий шаг: пустой список интересов не писал ни строки."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("if targeting_label:"):]
    block = block[: block.index("# Placements")]
    assert 'skip_targeting' in block, "пропуск таргетинга снова молчит"
    assert "else:" in block, "у ветки «интересов нет» нет своей записи"


# ── номер прогона ─────────────────────────────────────────────────────────

def test_a_direct_writer_inherits_the_current_run(tmp_path):
    """Сорок три места пишут об отказах, и не все знают номер прогона."""
    rt.set_current_run("runA", "SA_camp")
    try:
        p = tmp_path / "t.jsonl"
        rt.record("create_additional_ad", False, path=p)
        row = json.loads(p.read_text(encoding="utf-8").strip())
        assert row["run_id"] == "runA" and row["campaign"] == "SA_camp"
    finally:
        rt._current["run_id"] = ""
        rt._current["campaign"] = ""


def test_a_writer_that_knows_better_is_not_overruled(tmp_path):
    rt.set_current_run("runA", "SA_camp")
    try:
        p = tmp_path / "t.jsonl"
        rt.record("s", True, run_id="runB", campaign="other", path=p)
        row = json.loads(p.read_text(encoding="utf-8").strip())
        assert row["run_id"] == "runB" and row["campaign"] == "other"
    finally:
        rt._current["run_id"] = ""
        rt._current["campaign"] = ""


def test_the_flow_names_the_run_once():
    """Сторож: без этого вызова наследовать нечего."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    assert "_telemetry.set_current_run(run_id" in source


# ── попытка — это не отказ ────────────────────────────────────────────────

def test_a_retry_is_not_recorded_as_a_failed_step():
    """Шаг, который не падал ни разу, имел сто процентов отказов.

    Первый клик через CDP регулярно не передаёт фокус, текст не ложится, и
    вторая попытка кладёт его. Каждая первая попытка писалась отдельной
    строкой `ok=False` — 9 сентября таких три за прогон при трёх успешных
    `ad_copy`. По такой статистике чинят то, что работает, и не замечают
    того, что сломано.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("def _dump_stage"):]
    block = block[: block.index("def _locate")]
    assert "_telemetry.record(" not in block, "попытка снова пишется отдельной записью"
    assert 'report["attempts"]' in block, "попытка никуда не записывается — хуже прежнего"


def test_the_attempts_reach_the_failure_record():
    """Убрать шум — не значит потерять подробности.

    Если шаг всё-таки упал, стадии всех попыток должны лежать рядом с
    отказом: без них останется голое «ad_copy ✗», с которого всё начиналось.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    assert '_shot("fail_ad_copy", copy_report)' in source
    assert '_shot(f"fail_{tag}_copy", copy_report)' in source
