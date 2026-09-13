"""Билдер принадлежит аккаунту, а не рекомендации.

9 сентября выбрали 8-е число — и билдер по Саудовской Аравии исчез целиком.
Вместе с ним исчез подключённый и готовый `KLD_BFL_X84`, который в ту же
секунду был виден строкой выше, в карте аккаунтов, со спендом и статусом.

Причина: строка билдера рендерилась, только если у аккаунта нашлись открытые
рекомендации, а те считаются **внутри выбранного периода**. За один день их не
было ни у кого — и рабочий инструмент пропал по причине, к нему не
относящейся.

Собрать кампанию можно для аккаунта, а не для отрезка дней. Период отвечает на
вопрос «как шли дела», билдер — на «что запускаем».

Проверяется отрисованная страница, а не шаблон: условие было в Jinja, и по
исходнику видно только то, что оно есть, а не что оно делает.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _render(recs_by_account, *, fallback=False):
    """Страница одной страны с заданными рекомендациями по аккаунтам.

    Контекст берётся у соседнего теста, а не собирается заново: всякая
    рукописная копия рано или поздно падает на ключе, добавленном последним,
    вместо того, что проверяется.
    """
    from test_web_drill import _MOCK_REPORT, _render_html

    country = {**_MOCK_REPORT["countries"][0],
               "country": "Saudi Arabia",
               "open_recs": recs_by_account,
               "close_recs": {}}
    return _render_html({**_MOCK_REPORT, "countries": [country],
                         "is_fallback_mode": fallback})


def _builder_rows(html: str) -> list[str]:
    return re.findall(r'class="cb-acc-row" data-acc-label="([^"]*)"', html)


def test_an_account_without_recommendations_still_has_a_builder():
    """Тот самый случай: аккаунт подключён, рекомендаций за период нет."""
    html = _render({"KLD_BFL_X84": []})
    assert _builder_rows(html) == ["KLD_BFL_X84"], "билдер пропал вместе с рекомендациями"


def test_the_empty_builder_says_what_to_do_next():
    """Пустая форма без объяснения читается как «здесь ничего нет».

    Прежде эту роль играла строка «нет рекомендаций за выбранный период». Она
    исчезла вместе с автозаполнением, а объяснять пустоту по-прежнему надо —
    и теперь объяснение не зависит от того, нашла ли книга рекомендации.
    """
    html = _render({"KLD_BFL_X84": []})
    assert "Add Campaign" in html
    assert "No campaigns here yet" in html


def test_recommendations_no_longer_fill_the_campaign_cards():
    """Кампания заводится пустой — руками.

    Раньше на каждую открытую рекомендацию рисовалась готовая карточка.
    Заполнялось это плохо: таргетинг и креатив угадывались по тексту строки и
    чаще не угадывались, карточка приезжала с красными полями и значком
    «review before publish» — то есть всё равно требовала ручной работы,
    только теперь ещё и с чужими значениями, которые надо было сначала
    заметить. Решение владельца 13 сентября: убрать.

    Считаются карточки в отрисованном **теле формы**, а не по всей странице:
    строка `class="cb-slot"` живёт ещё и в javascript, который их строит, и
    проверка «есть ли карточка» по всему html проходит всегда — чем прежний
    сторож и занимался после удаления.
    """
    rec = {"strategy_type": "scale", "offer": "Aramco Form", "targeting": "FX", "creatives": []}
    html = _render({"KLD_BFL_X84": [rec]})
    assert _builder_rows(html) == ["KLD_BFL_X84"], "билдер аккаунта исчез"
    slots = html[html.index('<div class="cb-slots">'):]
    slots = slots[: slots.index("</div>")]
    assert 'class="cb-slot"' not in slots, "рекомендация снова построила карточку"
    assert 'data-asset="Aramco Form"' not in html
    assert 'data-asset=""' in html


def test_several_accounts_each_get_their_own_builder():
    html = _render({"KLD_BFL_X84": [], "FacebookElf191 SA": []})
    assert sorted(_builder_rows(html)) == ["FacebookElf191 SA", "KLD_BFL_X84"]


def test_a_country_with_no_accounts_points_at_the_account_map():
    """Прежний текст винил GSheet и звал нажать Refresh — искать шли не там."""
    html = _render({})
    assert "No accounts connected" in html
    assert "Account statuses were not loaded from GSheet" not in html


def test_the_builder_is_not_gated_on_the_fallback_mode_either():
    """Запасной режим был единственной дверью для аккаунта без рекомендаций."""
    text = TPL.read_text(encoding="utf-8")
    block = text[text.index('{% for acc_label in c.open_recs.keys()'):]
    block = block[: block.index("{% endfor %}")]
    assert "is_fallback_mode" not in block, (
        "судьба строки билдера снова зависит от режима, а не от аккаунта"
    )
