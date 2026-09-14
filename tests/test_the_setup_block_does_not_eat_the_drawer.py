"""Блок настройки аккаунта свёрнут, а заголовок говорит, чего не хватает.

Раньше он раскрывался ровно у ненастроенного аккаунта — то есть занимал всю
панель именно тогда, когда человеку и так труднее всего. В ящике билдера это
видно особенно: строка трекинг-ссылки, поле `ad_account_id`, поле `profile_id`,
у каждого своя кнопка Save, — а Targeting, бюджет и язык уходили за нижний край.

Настроить аккаунт — дело одного раза. Собирать в этом ящике кампании — дело
каждого дня, и место принадлежит второму.

Свернуть мало. Красный значок без продолжения — тупик: видно, что не работает,
и непонятно, что нажать. Поэтому заголовок называет недостающее словами. На
вкладке Accounts это давно так; здесь было иначе.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _header() -> str:
    """Кусок шаблона от вычисления состояния до конца заголовка."""
    page = TPL.read_text(encoding="utf-8")
    start = page.index("{% set pf_ok =")
    end = page.index("</button>", start)
    return page[start:end]


def test_the_block_is_collapsed_whatever_the_state():
    head = _header()
    opening = re.search(r'<div class="cb-preflight([^"]*)"', head)
    assert opening, "рамка блока изменилась — проверку надо переписать"
    assert "cb-preflight-collapsed" in opening.group(1), "блок снова раскрыт"
    assert "{% if" not in opening.group(1), (
        "свёрнутость снова зависит от состояния — значит у ненастроенного "
        "аккаунта блок опять займёт всю панель"
    )


def test_it_can_still_be_opened_by_hand():
    """Свёрнутое, что не открывается, — это спрятанное."""
    head = _header()
    assert "cb-preflight-toggle" in head
    assert "classList.toggle('cb-preflight-collapsed')" in head


def test_an_unconfigured_account_names_what_is_missing():
    head = _header()
    assert "needs setup" in head, "состояние больше ничего не говорит"
    assert "pf_missing | join(', ')" in head, (
        "заголовок снова не называет недостающее — «needs setup» без "
        "продолжения не подсказывает, что делать"
    )


def test_the_missing_list_covers_the_link_and_the_ids():
    """Не хватать может и ссылки, и идентификаторов — в заголовке должно быть
    и то и другое, иначе человек починит половину и не поймёт, почему всё ещё
    красно."""
    head = _header()
    src = re.search(r"\{% set pf_missing = ([^%]*)%\}", head)
    assert src, "список недостающего больше не собирается"
    body = src.group(1)
    assert "tracking link" in body
    assert "pf.missing_map_fields" in body


def test_a_configured_account_stays_green_and_quiet():
    head = _header()
    assert "configured ✓" in head
    ok_branch = head[head.index("{% if pf_ok %}"): head.index("{% else %}")]
    assert "needs setup" not in ok_branch
    assert "pf_missing" not in ok_branch, "настроенному аккаунту нечего перечислять"


# ── и то же самое на отрисованном шаблоне ─────────────────────────────

def _render(link_status: str, missing: list[str]) -> str:
    """Отрисовать заголовок блока так, как его увидит человек."""
    from jinja2 import Environment

    head = _header() + "</button>"
    env = Environment(autoescape=True)
    return env.from_string(head).render(
        pf={"link_status": link_status, "missing_map_fields": missing}
    )


def test_the_rendered_header_lists_both_ids():
    out = _render("missing", ["ad_account_id", "profile_id"])
    assert "✗ needs setup" in out
    assert "tracking link, ad_account_id, profile_id" in out


def test_the_rendered_header_of_a_ready_account_says_so():
    out = _render("tracking", [])
    assert "configured ✓" in out
    assert "needs setup" not in out


def test_a_pf_without_the_field_does_not_take_the_page_down():
    """`pf` доходит сюда и без `missing_map_fields`.

    Для проверки «настроен ли» это безобидно: у Jinja несуществующий атрибут
    просто ложь. А вот сложение списка с ним роняет отрисовку **всей** страницы
    — и падает не заголовок блока, а дешборд целиком. Поймали это браузерные
    тесты; здесь то же самое стоит дёшево и объясняет, почему в шаблоне `or []`.
    """
    from jinja2 import Environment

    head = _header() + "</button>"
    out = Environment(autoescape=True).from_string(head).render(pf={"link_status": "missing"})
    assert "needs setup" in out
    assert "tracking link" in out


def test_only_the_ids_are_listed_when_the_link_is_fine():
    out = _render("tracking", ["profile_id"])
    assert "tracking link" not in out, "названо недостающим то, что на месте"
    assert "profile_id" in out
