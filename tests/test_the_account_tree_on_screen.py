"""То, что Read принёс, видно на экране.

Чтение научилось приносить ad set'ы и объявления с их id, положением тумблера
и доставкой (ACL-1124) — и всё это приходило и пропадало: рисовать было нечем.
Владелец нажал Read дважды и справедливо сказал, что ничего не изменилось.

Проверяется настоящий код из шаблона: функции вырезаются из `report.html` и
выполняются в браузере. Переписать их в тест значило бы проверять копию,
которая разойдётся с оригиналом в первый же день.

Тумблер здесь **не переключает**: это показ. Переключение — вторая половина
`acl-113`, и до неё состояние нарисовано словом, а не контролом, похожим на
переключатель: такой контрол обещает то, чего не делает.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates"
REPORT = TPL / "report.html"


def _script() -> str:
    """Обе функции показа — из шаблона, а не по памяти."""
    src = REPORT.read_text(encoding="utf-8")
    start = src.index("function amapTreeLine(o, adCount) {")
    tail = "  amapTurnOffDisarm();\n});"
    end = src.index(tail, start) + len(tail)
    block = src[start:end]
    for needed in ("amapPaintTree", "amapEnsureCaret", "amapTreeLine", "amapCoverage",
                   "amapTurnOffAsk", "amapTurnOffDo"):
        assert needed in block, f"срез не захватил {needed}"
    return block


def _one(head: str) -> str:
    src = REPORT.read_text(encoding="utf-8")
    start = src.index(head)
    return src[start: src.index("\nfunction ", start + 10)]


def _money() -> str:
    """`amapMoney` определён выше по файлу и нужен оговорке — берём и его."""
    return _one("function amapMoney(spend, currency) {")


def _period() -> str:
    """`amapPeriod` — источник периода для выключения. Тоже выше по файлу."""
    return _one("function amapPeriod() {")


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=__import__("os").environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


COLUMNS = 11  # столько их в таблице аккаунтов страны


@pytest.fixture
def page(browser):
    p = browser.new_page()
    p.set_content(
        "<table><tbody>"
        '<tr id="acc"><td><span class="amap-caret-off">·</span>KLD_BFL_X84</td>'
        # Аккаунт выключение берёт отсюда — из строки, под которой стоит дерево.
        '<td class="amap-live" data-account="KLD_BFL_X84">USD 1.00</td>'
        + ("<td>x</td>" * (COLUMNS - 2)) + "</tr>"
        '<tr id="after"><td>next account</td></tr>'
        "</tbody></table>"
    )
    p.add_script_tag(content=_money() + "\n" + _period() + "\n" + _script())
    p.evaluate("() => { window._amapPick = {from: '2026-09-01', to: '2026-09-12', span: ''}; }")
    yield p
    p.close()


def _paint(page, objects, *, error="", finished="2026-09-11 23:07:51",
           spend=None, currency="USD"):
    page.evaluate(
        "(s) => amapPaintTree(document.getElementById('acc'), s)",
        {"objects": objects, "objects_error": error, "finished_at": finished,
         "spend": spend, "currency": currency},
    )


def _note(page):
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('.amap-tree-note')).map(n => n.textContent)"
    )


ADSET = {"level": "campaign", "id": "120250178859040703", "name": "SA_ARM_iOS_IMG_Reading_30_6.9",
         "switch": "on", "switch_label": "On/off", "delivery": "Active",
         "spend": "$412.02", "results": "18 Leads (Form)",
         "cost_per_result": "$22.89 Per Lead (Form)"}
AD = {"level": "ad", "id": "120250178850950703", "name": "1962_12- Officialy",
      "switch": "on", "switch_label": "On/off", "delivery": "Active",
      "spend": "$120.40", "results": "5 Leads (Form)", "cost_per_result": ""}


def _open(page):
    """Раскрыть дерево так же, как это делает человек, — стрелкой."""
    page.evaluate("() => document.querySelector('#acc .amap-caret').click()")


# ── что появляется ────────────────────────────────────────────────────────

def test_the_tree_starts_collapsed(page):
    """Раскрытым по умолчанию оно давало полтора экрана строк под каждым
    читанным аккаунтом, и карта переставала читаться."""
    _paint(page, [ADSET, AD])
    assert page.evaluate("() => document.querySelector('.amap-tree').hidden") is True


def test_the_arrow_opens_and_closes_it(page):
    _paint(page, [ADSET, AD])
    _open(page)
    assert page.evaluate("() => document.querySelector('.amap-tree').hidden") is False
    assert page.evaluate("() => document.getElementById('acc').classList.contains('amap-open')") is True
    _open(page)
    assert page.evaluate("() => document.querySelector('.amap-tree').hidden") is True


def test_an_account_without_workbook_rows_gets_an_arrow(page):
    """У такого аккаунта шаблон рисует «·»: раскрывать было нечего. Теперь есть."""
    _paint(page, [ADSET])
    assert page.evaluate("() => !!document.querySelector('#acc .amap-caret')")
    assert page.evaluate("() => !document.querySelector('#acc .amap-caret-off')")


def test_reading_again_keeps_it_open(page):
    """Перечитал с открытым деревом — оно и осталось открытым, с новыми числами."""
    _paint(page, [ADSET])
    _open(page)
    _paint(page, [dict(ADSET, spend="$500.00")])
    assert page.evaluate("() => document.querySelector('.amap-tree').hidden") is False
    assert page.evaluate("() => document.querySelector('.amap-tree-num.spend').textContent") == "$500.00"


def test_the_tree_lands_under_its_own_account(page):
    _paint(page, [ADSET, AD])
    got = page.evaluate("""() => {
      const tr = document.getElementById('acc').nextElementSibling;
      return {cls: tr.className, span: tr.firstElementChild.colSpan,
              lines: tr.querySelectorAll('.amap-tree-line').length};
    }""")
    assert got["cls"] == "amap-tree"
    assert got["span"] == COLUMNS, "блок не на всю ширину — таблица разъедется"
    assert got["lines"] == 2


def test_each_line_says_what_it_is(page):
    _paint(page, [ADSET, AD])
    got = page.evaluate("""() => Array.from(document.querySelectorAll('.amap-tree-line')).map(l => ({
      kind: l.querySelector('.amap-tree-kind').textContent,
      sw: l.querySelector('.amap-tree-sw').textContent,
      name: l.querySelector('.amap-tree-name').textContent,
      title: l.querySelector('.amap-tree-name').title,
      ad: l.classList.contains('ad'),
    }))""")
    assert [g["kind"] for g in got] == ["campaign", "ad"]
    assert got[0]["name"] == "SA_ARM_iOS_IMG_Reading_30_6.9"
    assert "120250178859040703" in got[0]["title"], "id негде посмотреть"
    assert got[1]["ad"] is True, "объявление не отступлено под своим уровнем"


def test_the_numbers_say_why_to_touch_it(page):
    """Имя и состояние говорят, ЧТО это. Пять одинаково включённых кампаний
    без чисел выглядят одинаково, а остановить надо тот, что тратит без лидов.
    """
    _paint(page, [ADSET])
    got = page.evaluate("""() => {
      const l = document.querySelector('.amap-tree-line');
      const n = l.querySelectorAll('.amap-tree-num');
      return {spend: n[0].textContent, results: n[1].textContent, cpr: n[1].title};
    }""")
    assert got["spend"] == "$412.02"
    assert got["results"] == "18 Leads (Form)"
    assert got["cpr"] == "$22.89 Per Lead (Form)", "цена результата негде посмотреть"


def test_a_row_without_numbers_stays_blank(page):
    """У черновика спенда ещё не бывает. «$0» — утверждение, которого нет."""
    _paint(page, [dict(ADSET, spend="", results="", cost_per_result="", delivery="In draft")])
    got = page.evaluate(
        "() => Array.from(document.querySelectorAll('.amap-tree-num')).map(n => n.textContent)"
    )
    assert got == ["", ""]


def test_the_header_counts_both_levels(page):
    _paint(page, [ADSET, ADSET, AD])
    head = page.evaluate("() => document.querySelector('.amap-tree-head').textContent")
    assert "2 campaigns" in head and "1 ads" in head
    assert "23:07:51" in head, "не сказано, когда читали — состояние живого аккаунта стареет"


# ── два факта, а не один ──────────────────────────────────────────────────

def test_switched_on_but_not_delivering_shows_both(page):
    """Тот самый случай с живого аккаунта: тумблер `on`, доставка `Campaign off`.

    Включено, а не крутится, потому что выключена кампания сверху. Сведи это в
    один значок — и человек увидит работающим то, что не тратит ничего.
    """
    _paint(page, [dict(ADSET, delivery="Campaign off", name="New Traffic Ad Set")])
    got = page.evaluate("""() => {
      const l = document.querySelector('.amap-tree-line');
      const d = l.querySelector('.amap-tree-del');
      return {sw: l.querySelector('.amap-tree-sw').textContent,
              swCls: l.querySelector('.amap-tree-sw').className,
              del: d.textContent, stopped: d.classList.contains('stopped')};
    }""")
    assert got["sw"] == "On" and "on" in got["swCls"]
    assert got["del"] == "Campaign off"
    assert got["stopped"] is True, "«не доставляется» ничем не отмечено"


def test_an_off_row_reads_off(page):
    _paint(page, [dict(AD, switch="off", delivery="Off")])
    sw = page.evaluate("() => document.querySelector('.amap-tree-sw').textContent")
    assert sw == "Off"


def test_no_switch_is_not_off(page):
    """«Тумблера не видно» и «выключено» — разные ответы.

    Спутать их значит показать остановленным то, что тратит деньги.
    """
    _paint(page, [dict(ADSET, switch="unknown", switch_label="")])
    got = page.evaluate("""() => {
      const s = document.querySelector('.amap-tree-sw');
      return {text: s.textContent, cls: s.className, title: s.title};
    }""")
    assert got["text"] == "—"
    assert "off" not in got["cls"].split(), "нарисовано как выключенное"
    assert "not the same as switched off" in got["title"]


def test_delivery_words_come_from_meta(page):
    """«In review» не сплющивается в булев и не переписывается нами."""
    _paint(page, [dict(ADSET, delivery="In review")])
    got = page.evaluate("""() => {
      const d = document.querySelector('.amap-tree-del');
      return {text: d.textContent, stopped: d.classList.contains('stopped')};
    }""")
    assert got["text"] == "In review"
    assert got["stopped"] is True


# ── повторное чтение ──────────────────────────────────────────────────────

def test_a_second_read_replaces_the_first(page):
    """Устаревшее состояние живого аккаунта — худшее, что можно оставить."""
    _paint(page, [ADSET])
    _paint(page, [dict(ADSET, switch="off", delivery="Off")])
    got = page.evaluate("""() => ({
      blocks: document.querySelectorAll('.amap-tree').length,
      lines: document.querySelectorAll('.amap-tree-line').length,
      sw: document.querySelector('.amap-tree-sw').textContent,
    })""")
    assert got["blocks"] == 1, "старый блок остался рядом с новым"
    assert got["lines"] == 1
    assert got["sw"] == "Off"


def test_the_next_account_row_is_not_disturbed(page):
    _paint(page, [ADSET, AD])
    assert page.evaluate(
        "() => document.getElementById('after').previousElementSibling.className"
    ) == "amap-tree"


# ── когда показывать нечего ───────────────────────────────────────────────

def test_nothing_read_draws_nothing(page):
    """Пустой блок «в аккаунте ничего» — утверждение, которого мы не делали."""
    _paint(page, [])
    assert page.evaluate("() => document.querySelectorAll('.amap-tree').length") == 0


def test_a_failure_is_said_out_loud(page):
    _paint(page, [], error="на уровне ad страница не сказала, что это за объекты")
    got = page.evaluate("""() => {
      const e = document.querySelector('.amap-tree-err');
      return e ? e.textContent : null;
    }""")
    assert got and "не сказала" in got


def test_a_failure_next_to_rows_is_shown_too(page):
    """Один уровень прочитан, другой нет — молчать об этом нельзя."""
    _paint(page, [ADSET], error="ad: таблица не отрисовалась")
    got = page.evaluate("""() => ({
      lines: document.querySelectorAll('.amap-tree-line').length,
      err: !!document.querySelector('.amap-tree-err'),
    })""")
    assert got["lines"] == 1 and got["err"] is True


# ── красные линии ─────────────────────────────────────────────────────────

def test_the_markup_lives_in_one_place():
    """Вторая копия в шаблоне разошлась бы с этой, и заметил бы это тот, кто
    поверил не той. Поэтому дерево рисует только JS, а после перезагрузки
    восстанавливается из сохранённого чтения."""
    for f in TPL.glob("_account_map*.html"):
        body = f.read_text(encoding="utf-8")
        assert "amap-tree" not in body, f"{f.name} несёт свою копию дерева"
    assert "amap-tree" in REPORT.read_text(encoding="utf-8")


def test_a_reload_brings_the_tree_back_without_touching_the_account():
    """Иначе ради уже известного ответа пришлось бы снова читать живой аккаунт."""
    src = REPORT.read_text(encoding="utf-8")
    start = src.index("function amapResumeReadings()")
    block = src[start: src.index("\nif (document.readyState", start)]
    assert 'data-read-state="ok"' in block, "прочитанные раньше аккаунты не восстанавливаются"
    assert "amapPaintTree" in block
    assert "/account-spend/read" not in block, "восстановление запускает новое чтение аккаунта"


def test_nothing_here_can_turn_anything_on():
    """Включение отсюда невозможно, и это не недоделка.

    Выключение денег не тратит, включение тратит. Обратный ход — руками в Ads
    Manager, где человек видит последствия.
    """
    block = _script()
    assert "/account-spend/turn-off" in block, "выключать нечем"
    for forbidden in ("turn-on", "turn_on", "/account-spend/read"):
        assert forbidden not in block, f"в показе есть {forbidden}"
    assert block.count("fetch(") == 1, "второй запрос наружу — какой?"


def test_only_a_switched_on_row_is_a_button():
    """Нажимаемое «Off» обещало бы включение, которого нет."""
    block = _script()
    assert "o.switch === 'on'" in block
    assert "document.createElement(live ? 'button' : 'span')" in block


def test_the_listeners_are_the_arrows_and_the_off_button():
    """Сторож на сторожа: обработчик тут ровно один, и он про стрелку.

    Проверка выше разрешает `addEventListener` — значит кто-то может повесить
    рядом второй, уже переключающий. Пусть это будет видно.
    """
    block = _script()
    assert block.count("caret.addEventListener('click'") == 2, "обработчики стрелок не два"
    assert block.count("document.addEventListener('click'") == 1, (
        "второй общий обработчик нажатий — что он делает?"
    )
    assert block.count("addEventListener") == 3, "появился четвёртый обработчик"


# ── покрывают ли строки спенд аккаунта ────────────────────────────────────
#
# На живом аккаунте 12.09 пять кампаний сложились в итог копейка в копейку
# ($2 117.46), а шесть объявлений дали $661.57 из тех же денег. Прокрутка
# новых строк не добавила — это весь список, который отдаёт страница.
#
# Молчать об этом нельзя: человек решает по этим строкам, что остановить, а
# «шесть объявлений» выглядят как весь аккаунт.

LIVE_SETS = [
    dict(ADSET, name="a", spend="$678.45"), dict(ADSET, name="b", spend="$676.99"),
    dict(ADSET, name="c", spend="$685.79"), dict(ADSET, name="d", spend="$34.26"),
    dict(ADSET, name="e", spend="$41.97"),
]
LIVE_ADS = [dict(AD, name="ad%d" % i, spend=v) for i, v in enumerate(
    ["$422.22", "$149.00", "$18.17", "$9.35", "$34.40", "$28.43"])]


def test_the_gap_is_said_out_loud(page):
    _paint(page, LIVE_SETS + LIVE_ADS, spend=2117.46)
    notes = _note(page)
    assert len(notes) == 1, f"оговорка не одна: {notes}"
    assert "6 ads here add up to USD 661.57" in notes[0]
    assert "USD 2,117.46" in notes[0], "не с чем сравнить"


def test_the_level_that_adds_up_is_not_doubted(page):
    """Общая фраза про «строки» заставила бы усомниться и в тех, что сходятся."""
    _paint(page, LIVE_SETS + LIVE_ADS, spend=2117.46)
    assert "campaigns" not in _note(page)[0]


def test_rows_that_cover_the_total_say_nothing(page):
    _paint(page, LIVE_SETS, spend=2117.46)
    assert _note(page) == []


def test_rounding_does_not_trigger_it(page):
    """Расхождение в копейки бывает от округления самой Meta. Жаловаться на
    него значит приучить не читать оговорку."""
    _paint(page, LIVE_SETS, spend=2117.50)
    assert _note(page) == []


def test_unparseable_money_stays_silent(page):
    """Неверная оговорка хуже отсутствующей."""
    _paint(page, [dict(ADSET, spend="about a lot")], spend=2117.46)
    assert _note(page) == []


def test_no_account_total_means_no_claim(page):
    _paint(page, LIVE_ADS, spend=None)
    assert _note(page) == []


def test_drafts_without_spend_are_not_a_shortfall(page):
    """У черновика спенда ещё нет — это не пробел в покрытии."""
    _paint(page, [dict(ADSET, spend="$100.00"), dict(ADSET, spend="", delivery="In draft")],
           spend=100.00)
    assert _note(page) == []


def test_the_note_explains_itself(page):
    _paint(page, LIVE_SETS + LIVE_ADS, spend=2117.46)
    title = page.evaluate("() => document.querySelector('.amap-tree-note').title")
    assert "Deleted items" in title and "view switcher" in title


# ── объявления под своим кампанией ────────────────────────────────────────
#
# Пока связи не было, объявления шли общим списком следом за кампаниими, и
# понять, чьё какое, было нельзя. Чтение теперь спрашивает их по одному ad
# set'у за раз (ACL-1124), и `parent_id` известен по построению.

SET_A = dict(ADSET, id="11111111", name="set A", spend="$700.00")
SET_B = dict(ADSET, id="22222222", name="set B", spend="$300.00")


def _ad(name, parent, spend="$10.00"):
    return dict(AD, id="ad-" + name, name=name, parent_id=parent, spend=spend)


def _names(page):
    return page.evaluate("""() => Array.from(document.querySelectorAll('.amap-tree-line'))
        .map(l => l.querySelector('.amap-tree-name').textContent)""")


def test_ads_stand_under_their_own_campaign(page):
    _paint(page, [SET_A, SET_B,
                  _ad("a1", "11111111"), _ad("b1", "22222222"), _ad("a2", "11111111")])
    assert _names(page) == ["set A", "a1", "a2", "set B", "b1"]


def test_a_campaign_without_ads_is_still_shown(page):
    """Пустой ad set — это ответ: он есть и в нём ничего не крутится."""
    _paint(page, [SET_A, SET_B, _ad("b1", "22222222")])
    assert _names(page) == ["set A", "set B", "b1"]


def test_an_ad_is_never_guessed_into_a_neighbour(page):
    """Приписать к последнему попавшемуся — худшее, что можно сделать: по этим
    строкам решают, что останавливать."""
    _paint(page, [SET_A, _ad("lost", "99999999")])
    names = _names(page)
    assert names == ["set A", "lost"]
    heads = page.evaluate(
        "() => Array.from(document.querySelectorAll('.amap-tree-head')).map(h => h.textContent)"
    )
    assert any("whose campaign is not in this answer" in h for h in heads), heads


def test_an_ad_without_a_parent_is_an_orphan_too(page):
    _paint(page, [SET_A, dict(AD, id="x", name="nobody", parent_id="")])
    heads = page.evaluate(
        "() => Array.from(document.querySelectorAll('.amap-tree-head')).map(h => h.textContent)"
    )
    assert any("1 ads whose campaign" in h for h in heads), heads


# ── ad set тоже раскрывается ──────────────────────────────────────────────
#
# У DQ438 десять кампаний и 99 объявлений: раскрытые разом, они уезжали на
# километр и прятали всё, что ниже по карте.

def test_ads_start_hidden_under_their_campaign(page):
    _paint(page, [SET_A, _ad("a1", "11111111"), _ad("a2", "11111111")])
    _open(page)  # дерево свёрнуто само по себе — считаем видимое внутри раскрытого
    got = page.evaluate("""() => ({
      kids: document.querySelectorAll('.amap-tree-kids').length,
      hidden: document.querySelector('.amap-tree-kids').hidden,
      visible: Array.from(document.querySelectorAll('.amap-tree-line'))
        .filter(l => l.offsetParent !== null).length,
    })""")
    assert got["kids"] == 1 and got["hidden"] is True
    assert got["visible"] == 1, "объявления видны, хотя ad set свёрнут"


def test_the_campaign_arrow_opens_its_ads(page):
    _paint(page, [SET_A, _ad("a1", "11111111"), _ad("a2", "11111111")])
    _open(page)  # само дерево
    page.evaluate("() => document.querySelector('.amap-tree-caret').click()")
    assert page.evaluate("() => document.querySelector('.amap-tree-kids').hidden") is False
    page.evaluate("() => document.querySelector('.amap-tree-caret').click()")
    assert page.evaluate("() => document.querySelector('.amap-tree-kids').hidden") is True


def test_a_collapsed_campaign_still_says_how_many(page):
    """Иначе свёрнутая строка молчит о том, есть ли под ней что-нибудь."""
    _paint(page, [SET_A, _ad("a1", "11111111"), _ad("a2", "11111111")])
    kind = page.evaluate("() => document.querySelector('.amap-tree-kind').textContent")
    assert kind == "campaign · 2"


def test_a_campaign_without_ads_has_no_arrow(page):
    """Раскрывать нечего, а стрелка обещала бы, что есть."""
    _paint(page, [SET_A])
    got = page.evaluate("""() => ({
      carets: document.querySelectorAll('.amap-tree-caret').length,
      gaps: document.querySelectorAll('.amap-tree-caret-off').length,
    })""")
    assert got["carets"] == 0
    assert got["gaps"] == 1, "без заглушки строки разъедутся"


def test_an_opened_campaign_stays_open_after_a_re_read(page):
    """Иначе после каждого Read пришлось бы открывать заново."""
    _paint(page, [SET_A, _ad("a1", "11111111")])
    page.evaluate("() => document.querySelector('.amap-tree-caret').click()")
    _paint(page, [SET_A, _ad("a1", "11111111", spend="$99.00")])
    assert page.evaluate("() => document.querySelector('.amap-tree-kids').hidden") is False
    assert page.evaluate("() => document.querySelector('.amap-tree-caret').classList.contains('open')")


def test_each_campaign_opens_on_its_own(page):
    _paint(page, [SET_A, SET_B, _ad("a1", "11111111"), _ad("b1", "22222222")])
    page.evaluate("() => document.querySelectorAll('.amap-tree-caret')[1].click()")
    got = page.evaluate(
        "() => Array.from(document.querySelectorAll('.amap-tree-kids')).map(k => k.hidden)"
    )
    assert got == [True, False]


# ══ выключение ════════════════════════════════════════════════════════════
#
# Первое, чем дешборд меняет живой аккаунт, а не читает его. Отсюда два шага
# на нажатие и дословный ответ: «нажали, но тумблер не стал выключенным» — это
# не успех, и подкрашивать его успехом нельзя.

def _armed(page, index=0):
    page.evaluate(
        "(i) => document.querySelectorAll('.amap-tree-sw[data-object-id]')[i].click()", index
    )


def _stub_server(page, answer):
    """Сервер-заглушка: настоящий ходит в живой аккаунт."""
    page.evaluate(
        """(ans) => {
             window.__sent = [];
             window.fetch = async (url, opt) => {
               window.__sent.push({url: url, body: String(opt && opt.body || '')});
               return {json: async () => ans};
             };
           }""",
        answer,
    )


def _sw(page, index=0):
    return page.evaluate(
        """(i) => {
             const e = document.querySelectorAll('.amap-tree-sw')[i];
             return {tag: e.tagName, text: e.textContent, cls: e.className, title: e.title};
           }""",
        index,
    )


def test_only_a_switched_on_row_offers_the_button(page):
    _paint(page, [ADSET, dict(ADSET, id="2", switch="off", delivery="Off"),
                  dict(ADSET, id="3", switch="unknown")])
    got = page.evaluate(
        "() => Array.from(document.querySelectorAll('.amap-tree-sw')).map(e => e.tagName)"
    )
    assert got == ["BUTTON", "SPAN", "SPAN"], "нажимаемым стало то, что включить нельзя"


def test_the_first_click_only_asks(page):
    _paint(page, [ADSET])
    _stub_server(page, {"ok": True, "result": {"state": "off"}})
    _armed(page)
    assert _sw(page)["text"] == "Turn off?"
    assert page.evaluate("() => window.__sent.length") == 0, "выключили с одного промаха"


def test_the_second_click_turns_it_off(page):
    _paint(page, [ADSET])
    _stub_server(page, {"ok": True, "result": {"state": "off", "how": "подтверждено перезагрузкой"}})
    _armed(page); _armed(page)
    page.wait_for_function("() => document.querySelector('.amap-tree-sw').tagName === 'SPAN'")
    got = _sw(page)
    assert got["text"] == "Off" and got["tag"] == "SPAN", "осталось нажимаемым, хотя включить нельзя"
    assert "Ads Manager" in got["title"], "не сказано, где включать обратно"


def test_what_is_sent_is_what_was_shown(page):
    _paint(page, [dict(ADSET, id="120250178859040703", name="SA_ARM")])
    _stub_server(page, {"ok": True, "result": {"state": "off"}})
    _armed(page); _armed(page)
    page.wait_for_function("() => window.__sent.length === 1")
    sent = page.evaluate("() => window.__sent[0]")
    assert sent["url"] == "/account-spend/turn-off"
    assert "object_id=120250178859040703" in sent["body"]
    assert "level=campaign" in sent["body"]
    assert "name=SA_ARM" in sent["body"], "имя не послано — сверить на странице будет нечем"


def test_an_ad_carries_its_campaign(page):
    """Список объявлений сужается по кампании: без неё до строки не добраться."""
    _paint(page, [SET_A, _ad("a1", "11111111")])
    _stub_server(page, {"ok": True, "result": {"state": "off"}})
    _armed(page, 1); _armed(page, 1)
    page.wait_for_function("() => window.__sent.length === 1")
    assert "parent_id=11111111" in page.evaluate("() => window.__sent[0].body")


def test_a_refusal_is_not_dressed_up_as_success(page):
    _paint(page, [ADSET])
    _stub_server(page, {"ok": True, "result": {
        "state": "failed", "error": "после перезагрузки тумблер снова включён — Meta не приняла"}})
    _armed(page); _armed(page)
    page.wait_for_function("() => document.querySelector('.amap-tree-sw').classList.contains('failed')")
    got = _sw(page)
    assert got["text"] == "Check", "unconfirmed action must not claim the old on state"
    assert "Meta не приняла" in got["title"], "причина не видна"


def test_already_off_is_said_plainly(page):
    _paint(page, [ADSET])
    _stub_server(page, {"ok": True, "result": {"state": "already_off", "how": "ничего не нажимали"}})
    _armed(page); _armed(page)
    page.wait_for_function("() => document.querySelector('.amap-tree-sw').tagName === 'SPAN'")
    assert "already off" in _sw(page)["title"]


def test_clicking_elsewhere_takes_the_question_back(page):
    _paint(page, [ADSET])
    _stub_server(page, {"ok": True, "result": {"state": "off"}})
    _armed(page)
    page.evaluate("() => document.body.click()")
    assert _sw(page)["text"] == "On"
    assert page.evaluate("() => window.__sent.length") == 0


def test_a_row_from_an_older_reading_is_not_a_button(page):
    """Сохранённые чтения переживают смену формы ответа.

    После перехода с ad set'ов на кампании (12.09) в хранилище остались
    деревья с уровнем `adset`. Выключение такую строку не примет, и кнопка на
    ней обещала бы работу, которой не будет.
    """
    _paint(page, [dict(ADSET, level="adset")])
    assert _sw(page)["tag"] == "SPAN"


def test_an_ad_without_its_campaign_is_not_a_button(page):
    """Список объявлений сужается по кампании: без неё до строки не добраться."""
    _paint(page, [dict(AD, parent_id="")])
    assert _sw(page)["tag"] == "SPAN"


def test_a_proper_campaign_row_still_is(page):
    _paint(page, [ADSET])
    assert _sw(page)["tag"] == "BUTTON"


# ── стрелка раскрывает только дерево ──────────────────────────────────────

def test_the_account_arrow_no_longer_pulls_the_workbook_drill():
    """Одна стрелка открывала два списка: книжный первым и длинный, живой под
    ним. Две таблицы с похожими словами одна над другой читаются как одна
    поломанная (решение владельца 12.09).
    """
    import re

    # Комментарии Jinja вырезаются: объяснение убранной конструкции — не её
    # возвращение. Сторож, читающий файл целиком, ловит собственный текст;
    # за эту сессию так вышло уже трижды.
    body = re.sub(r"\{#.*?#\}", "", (TPL / "_account_map_country.html").read_text(encoding="utf-8"),
                  flags=re.S)
    assert "section=account" not in body, "книжная детализация снова висит на стрелке"
    assert 'class="amap-caret"' not in body, "стрелку рисует JS, и только когда есть что раскрыть"
    assert 'class="amap-caret-off"' in body, "без заглушки строки разъедутся"


def test_the_arrow_appears_only_when_there_is_a_tree(page):
    """До чтения раскрывать нечего, и обещать обратное незачем."""
    assert page.evaluate("() => !!document.querySelector('#acc .amap-caret')") is False
    _paint(page, [ADSET])
    assert page.evaluate("() => !!document.querySelector('#acc .amap-caret')") is True


def test_turning_off_never_runs_in_the_web_loop():
    """Playwright здесь синхронный, и вызов его из работающего цикла asyncio
    кончается `RuntimeError: Cannot enter into task …` — выключение падает, ни
    разу не тронув страницу. Так и вышло на первом живом нажатии 12.09.

    Чтение по той же причине живёт в своём потоке (`start_read`).
    """
    src = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    start = src.index("async def account_spend_turn_off(")
    body = src[start: src.index("\n@app.", start)]
    assert "run_in_threadpool" in body, "выключение снова вызывается в цикле веб-сервера"
    assert "await run_in_threadpool(\n            account_spend.turn_off," in body


# ══ след выключения ═══════════════════════════════════════════════════════
#
# Первое живое нажатие 12.09 вернуло отказ, и прочитать его причину было
# негде: результат уходил в ответ HTTP и исчезал. Действие, меняющее чужой
# аккаунт, обязано оставлять след — «что-то изменилось вчера» не ответ.

def test_every_turn_off_leaves_a_record(monkeypatch, tmp_path):
    from src.analysis_agent import account_spend as sp

    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "readings.json")
    seen = []
    monkeypatch.setattr(sp, "_turn_off", lambda *a, **k: sp.TurnOffResult(
        account="acc", level="campaign", object_id="1", state="failed",
        error="на экране другой период — ничего не трогаем"))
    monkeypatch.setattr(sp, "_record_turn_off", lambda out: seen.append(out))
    sp.turn_off("acc", "campaign", "1")
    assert len(seen) == 1, "у действия нет следа"


def test_the_record_survives_a_refusal(monkeypatch, tmp_path):
    """Отказ нужнее удачи: именно его причину приходится искать."""
    from src.analysis_agent import account_spend as sp

    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "readings.json")
    sp._record_turn_off(sp.TurnOffResult(
        account="KLD_BFL_X84", level="ad", object_id="777", name="ad one",
        state="failed", error="под этим id на странице «чужое имя»"))
    rows = sp.turn_off_history()
    assert len(rows) == 1
    assert rows[0]["state"] == "failed"
    assert "чужое имя" in rows[0]["error"], "причина не записана"


def test_the_record_never_breaks_the_answer(monkeypatch, tmp_path):
    """Потерять журнал плохо, потерять ответ — хуже."""
    from src.analysis_agent import account_spend as sp

    monkeypatch.setattr(sp, "_turn_off_log", lambda: tmp_path / "no" / "such" / "dir" / "x.jsonl")
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    sp._record_turn_off(sp.TurnOffResult(account="a", level="ad", object_id="1"))


def test_the_newest_record_comes_first(monkeypatch, tmp_path):
    from src.analysis_agent import account_spend as sp

    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "readings.json")
    for i in range(3):
        sp._record_turn_off(sp.TurnOffResult(account="a", level="ad", object_id=str(i)))
    assert [r["object_id"] for r in sp.turn_off_history()] == ["2", "1", "0"]


# Persistent confirmation is exercised by test_turn_off_safety_browser.py.


# ══ дерево не остаётся старше действия ════════════════════════════════════
#
# 12.09 чтение легло в 15:52:14, выключение случилось в 15:52:38. После
# перезагрузки страницы объявление снова показывалось включённым — да ещё с
# кнопкой на том, что уже выключено.

def _store(monkeypatch, tmp_path, objects):
    from src.analysis_agent import account_spend as sp
    import json

    path = tmp_path / "readings.json"
    monkeypatch.setattr(sp, "_store_path", lambda: path)
    path.write_text(json.dumps({
        "acc|2026-09-08|2026-09-11": {"account": "acc", "date_from": "2026-09-08",
                                      "date_to": "2026-09-11", "objects": objects},
        "acc|2026-09-11|2026-09-11": {"account": "acc", "date_from": "2026-09-11",
                                      "date_to": "2026-09-11", "objects": objects},
        "other|2026-09-11|2026-09-11": {"account": "other", "objects": objects},
    }), encoding="utf-8")
    return sp, path


def test_a_confirmed_turn_off_updates_the_saved_tree(monkeypatch, tmp_path):
    sp, path = _store(monkeypatch, tmp_path, [
        {"id": "777", "level": "ad", "switch": "on", "delivery": "Active"},
        {"id": "888", "level": "ad", "switch": "on", "delivery": "Active"},
    ])
    touched = sp._mark_object_off("acc", "777", "Processing")
    assert touched == 2, "поправлено не во всех чтениях аккаунта"
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("acc|2026-09-08|2026-09-11", "acc|2026-09-11|2026-09-11"):
        rows = {o["id"]: o for o in data[key]["objects"]}
        assert rows["777"]["switch"] == "off"
        assert rows["777"]["delivery"] == "Processing", "доставка не та, что прочитали"
        assert rows["888"]["switch"] == "on", "тронули соседа"


def test_another_account_is_left_alone(monkeypatch, tmp_path):
    sp, path = _store(monkeypatch, tmp_path, [
        {"id": "777", "level": "ad", "switch": "on", "delivery": "Active"}])
    sp._mark_object_off("acc", "777", "Off")
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["other|2026-09-11|2026-09-11"]["objects"][0]["switch"] == "on"


def test_an_unread_delivery_is_not_invented(monkeypatch, tmp_path):
    """Не прочитали — пусть остаётся прежняя, чем ложная."""
    sp, path = _store(monkeypatch, tmp_path, [
        {"id": "777", "level": "ad", "switch": "on", "delivery": "Active"}])
    sp._mark_object_off("acc", "777", "")
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    row = data["acc|2026-09-11|2026-09-11"]["objects"][0]
    assert row["switch"] == "off" and row["delivery"] == "Active"


def test_only_a_confirmed_outcome_touches_the_tree():
    """Отказ ничего не подтвердил, и дерево трогать не за что."""
    src = (ROOT / "src" / "analysis_agent" / "account_spend.py").read_text(encoding="utf-8")
    start = src.index("def turn_off(account: str")
    body = src[start: src.index("def _turn_off(account: str", start)]
    assert 'if out.state in ("off", "already_off"):' in body
    assert "_mark_object_off" in body
