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
    # ACL-1277 дописывает сюда `amap-stacked`, когда колонке имени тесно.
    assert got["cls"].split()[0] == "amap-tree"
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
      const spend = l.querySelector('.amap-tree-num.spend'), res = l.querySelector('.amap-tree-num.res');
      return {spend: spend.textContent, results: res.lastChild.textContent.trim(),
              src: res.querySelector('.amap-tree-src').textContent, cpr: res.title};
    }""")
    assert got["spend"] == "$412.02"
    assert got["results"] == "18 Leads (Form)"
    assert "$22.89 Per Lead (Form)" in got["cpr"], "цена результата негде посмотреть"
    # ACL-1275: под заголовком Setup у аккаунта стоит готовность, а у строк
    # дерева — результат Meta. Источник подписан, чтобы их не путали.
    assert got["src"] == "Meta" and "Meta's own result" in got["cpr"]


def test_a_row_without_numbers_stays_blank(page):
    """У черновика спенда ещё не бывает. «$0» — утверждение, которого нет."""
    _paint(page, [dict(ADSET, spend="", results="", cost_per_result="", delivery="In draft")])
    got = page.evaluate(
        "() => Array.from(document.querySelectorAll('.amap-tree-num:not(.attr)')).map(n => n.textContent)"
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
    ).split()[0] == "amap-tree"


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

    # Запросов наружу ровно три, и все названы поимённо. Счёт остаётся
    # жёстким: он и нужен затем, чтобы четвёртый нельзя было добавить молча.
    # Третий (ACL-1256) — числа книги по кампаниям и объявлениям, и он тоже
    # только читает: без `method`, то есть GET.
    assert block.count("fetch(") == 3, "четвёртый запрос наружу — какой?"
    attr = block[block.index("fetch('/account-attribution?"):]
    attr = attr[: attr.index(".then(")]
    assert "method" not in attr, "числа книги перестали быть чтением"
    assert "/account-spend/turn-off-history?account=" in block, "журнал не читается"

    # Второй — журнал, и он обязан быть читающим. Без `method` fetch делает
    # GET; появление метода здесь означало бы, что чтение записи научилось
    # что-то менять, а это ровно та дверь, которую закрывает вся проверка.
    hist = block[block.index("fetch('/account-spend/turn-off-history"):]
    hist = hist[: hist.index(".then(")]
    assert "method" not in hist, "чтение журнала перестало быть чтением"


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
    # Четвёртый — заголовок журнала: он только сворачивает и разворачивает
    # свой же блок. Назван здесь поимённо, чтобы пятый снова стал заметен.
    assert block.count("head.addEventListener('click'") == 1, "журнал слушает не одно нажатие"
    # Пятый (ACL-1256) — смена размера окна: пересчитывает ширины колонок
    # дерева и ничего не нажимает.
    assert block.count("window.addEventListener('resize'") == 1, "окно слушается не только на размер"
    # Шестой — раскрытие блока страны (`toggle`): тоже только пересчёт ширин.
    assert block.count("document.addEventListener('toggle'") == 1, "раскрытие слушается не одно"
    # Седьмой (ACL-1277) — открытие и закрытие билдера: он раздвигает страницу
    # своим отступом, а `resize` при этом не приходит, и дерево оставалось с
    # ширинами, снятыми до него. Тоже только пересчёт ширин.
    assert block.count("document.addEventListener('acl:builder-drawer'") == 1, (
        "билдер слушается не один раз"
    )
    # Восьмой (ACL-1504) — Read again у непрочитанного списка кампаний: он
    # только нажимает Read spend той же строки, своего чтения у него нет.
    assert block.count("again.addEventListener('click'") == 1, "Read again слушает не одно нажатие"
    assert block.count("addEventListener") == 8, "появился девятый обработчик"


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


@pytest.mark.parametrize("status,payload", [
    (500, {"ok": True, "entries": []}),
    (200, {"ok": False, "error": "The record could not be read."}),
    (200, {"ok": True}),
    (200, {"ok": True, "entries": {}}),
])
def test_unavailable_history_is_not_rendered_as_empty(page, status, payload):
    from playwright.sync_api import expect

    page.evaluate("""({status, payload}) => {
      window.fetch = async () => new Response(JSON.stringify(payload), {status});
    }""", {"status": status, "payload": payload})
    _paint(page, [ADSET])
    _open(page)
    page.locator('.amap-tree-log-head').click()
    expect(page.locator('.amap-tree-log-body')).to_have_text('The record could not be read.')
    assert page.locator('.amap-tree-log-none').count() == 0


def test_the_open_journal_shows_an_unconfirmed_attempt_after_http_failure(page):
    from playwright.sync_api import expect

    page.add_script_tag(content=_one('function amapIso(d) {'))
    page.evaluate("""() => {
      window.__historyReads = 0;
      window.__actions = 0;
      window.fetch = async (url, options) => {
        if (url.startsWith('/account-spend/turn-off-history?')) {
          window.__historyReads++;
          const entries = window.__actions ? [{
            account: 'KLD_BFL_X84', object_id: '1', level: 'campaign', name: 'Reviewed campaign',
            state: 'unconfirmed', error: 'Timed out', at: '2026-09-14 10:00:00',
            requester: 'Test operator', route: 'rpa'
          }] : [];
          return new Response(JSON.stringify({ok: true, entries}), {status: 200});
        }
        if (url === '/account-spend/turn-off' && options.method === 'POST') {
          window.__actions++;
          return new Response(JSON.stringify({ok: false, error: 'Could not reach the account.'}),
                              {status: 503});
        }
        throw new Error('Unexpected request: ' + url);
      };
    }""")
    _paint(page, [ADSET])
    _open(page)
    page.locator('.amap-tree-log-head').click()
    expect(page.locator('.amap-tree-log-none')).to_have_text(
        'Nothing has been changed in this account from here.')
    button = page.locator('.amap-tree-sw[data-object-id]').first
    button.click()
    button.click()
    expect(page.locator('.amap-tree-log-said')).to_contain_text('result not confirmed')
    expect(page.locator('.amap-tree-log-who')).to_have_text('Test operator · rpa')
    expect(page.locator('.amap-tree-log-what')).to_contain_text('Reviewed campaign')
    assert page.locator('.amap-tree-log-none').count() == 0
    assert page.evaluate('window.__historyReads') == 2
    assert page.evaluate('window.__actions') == 1


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


# ── таблица без id (Doc19, ACL-1255) ──────────────────────────────────────

def test_a_nameless_table_puts_ads_under_their_campaign(page):
    """У кампании нет `id`, есть `data_id` из данных страницы; у объявлений
    `id` пустой. Все они должны встать под свою кампанию, а не «сиротами»."""
    camp = dict(ADSET, id="", data_id="111")
    other = dict(ADSET, id="", data_id="222", name="other camp")
    ads = [dict(AD, id="", parent_id="111", name="ad one"),
           dict(AD, id="", parent_id="111", name="ad two"),
           dict(AD, id="", parent_id="222", name="ad three")]
    _paint(page, [camp, other] + ads)
    got = page.evaluate("""() => ({
      kids: Array.from(document.querySelectorAll('.amap-tree-kids'))
        .map(k => k.querySelectorAll('.amap-tree-line').length),
      heads: Array.from(document.querySelectorAll('.amap-tree-head')).map(h => h.textContent),
      buttons: document.querySelectorAll('button.amap-tree-sw').length,
    })""")
    assert got["kids"] == [2, 1]
    assert not any("whose campaign is not in this answer" in h for h in got["heads"])
    assert got["buttons"] == 0, "без id выключать нечем — кнопки быть не должно"


# ── числа книги под колонками аккаунта (ACL-1256) ─────────────────────────

ATTR = {
    "ok": True, "has_data": True,
    "campaigns": {"SA_ARM_iOS_IMG_Reading_30_6.9": {
        "spend": "$338", "leads": 7, "deposits": 1, "cpl": "$48", "cpa": "$338", "cr": "14.3%"}},
    "ads": {"SA_ARM_iOS_IMG_Reading_30_6.9": {"1962_12- Officialy": {
        "spend": "$120", "leads": 3, "deposits": 0, "cpl": "$40", "cpa": None, "cr": None}}},
}


def _fill(page, objects, data):
    page.evaluate("""([objects, data]) => {
      amapFillAttribution(document.querySelector('.amap-tree-inner'), objects, data);
    }""", [objects, data])


def _attr(page, n=0):
    return page.evaluate("""(n) => {
      const line = document.querySelectorAll('.amap-tree-line')[n];
      const out = {};
      line.querySelectorAll('.amap-tree-num.attr').forEach(c => { out[c.dataset.attr] = c.textContent; });
      return out;
    }""", n)


def test_workbook_numbers_land_in_the_campaign_and_ad_rows(page):
    ad = dict(AD, parent_id=ADSET["id"])
    _paint(page, [ADSET, ad])
    _fill(page, [ADSET, ad], ATTR)
    assert _attr(page, 0) == {"spend": "$338", "leads": "7", "deposits": "1",
                              "cpl": "$48", "cpa": "$338", "cr": "14.3%"}
    assert _attr(page, 1) == {"spend": "$120", "leads": "3", "deposits": "0",
                              "cpl": "$40", "cpa": "—", "cr": "—"}, "нет FTD — прочерк, не ноль"


def test_a_row_the_workbook_does_not_know_gets_dashes(page):
    other = dict(ADSET, id="999", name="MaxWarmup Engagement")
    _paint(page, [other])
    _fill(page, [other], ATTR)
    assert set(_attr(page).values()) == {"—"}
    assert "No rows" in page.evaluate("() => document.querySelector('.amap-tree-num.attr').title")


def _nowb(page):
    return page.evaluate("""() => Array.from(document.querySelectorAll('.amap-tree-line'))
      .map(l => { const t = l.querySelector('.amap-tree-nowb'); return t ? t.textContent.trim() : ''; })""")


def test_a_meta_only_campaign_says_it_is_not_in_the_workbook(page):
    """ACL-1377: прогревочная кампания (MaxWarmup на Doc19) с числами Meta и
    без строк в книге. Прочерки там — не поломка, и это сказано пометкой."""
    warm = dict(ADSET, id="999", name="MaxWarmup Engagement")
    draft = dict(ADSET, id="998", name="Draft", spend="", results="", cost_per_result="")
    zero = dict(ADSET, id="997", name="Zero", spend="$0.00", results="0", cost_per_result="")
    _paint(page, [ADSET, warm, draft, zero])
    _fill(page, [ADSET, warm, draft, zero], ATTR)
    assert _nowb(page) == ["", "· not in workbook", "", ""], (
        "пометка — только у кампании с числами Meta и без строк книги")
    assert set(_attr(page, 1).values()) == {"—"}, "прочерки в колонках книги остаются"
    assert "not an error" in page.evaluate("() => document.querySelector('.amap-tree-nowb').title")


def test_the_not_in_workbook_hint_goes_when_the_workbook_finds_the_campaign(page):
    warm = dict(ADSET, id="999", name="MaxWarmup Engagement")
    _paint(page, [warm])
    _fill(page, [warm], ATTR)
    assert _nowb(page) == ["· not in workbook"]
    found = {"ok": True, "has_data": True, "ads": {},
             "campaigns": {"MaxWarmup Engagement": {"spend": "$5", "leads": 1, "deposits": 0,
                                                    "cpl": "$5", "cpa": None, "cr": None}}}
    _fill(page, [warm], found)
    assert _nowb(page) == [""], "пометка пережила ответ, в котором кампания нашлась"
    _fill(page, [warm], {"ok": False, "error": "busy"})
    assert _nowb(page) == [""], "непрочитанная книга — не «нет в книге»"


SHARED_TIP = ("The workbook cannot tell ads with the same name apart; "
              "these are the totals for all of them")


def _shared(page, n):
    return page.evaluate("""(n) => {
      const line = document.querySelectorAll('.amap-tree-line')[n];
      const mark = line.querySelector('.amap-tree-shared');
      return {mark: mark ? mark.textContent : null, tip: mark ? mark.title : null,
              cellTip: line.querySelector('.amap-tree-num.attr').title};
    }""", n)


def test_two_ads_with_one_name_share_the_workbook_totals(page):
    """Doc19, Facebook_6540: несколько объявлений с одним именем в одной
    кампании. Книга различить их не может, и решение владельца 30.09 —
    показать каждому итог по этому имени один раз, с пометкой, а не прочерк."""
    a1 = dict(AD, parent_id=ADSET["id"])
    a2 = dict(AD, id="120250178850950704", parent_id=ADSET["id"])
    _paint(page, [ADSET, a1, a2])
    _fill(page, [ADSET, a1, a2], ATTR)
    assert _attr(page, 0)["leads"] == "7", "кампания при этом своё число получает"
    want = {"spend": "$120", "leads": "3", "deposits": "0", "cpl": "$40", "cpa": "—", "cr": "—"}
    assert _attr(page, 1) == want and _attr(page, 2) == want, "итог имени — один раз, не умноженный"
    for n in (1, 2):
        got = _shared(page, n)
        assert got["mark"] == "shared by 2 ads"
        assert got["tip"] == SHARED_TIP and got["cellTip"] == SHARED_TIP
    assert _shared(page, 0)["mark"] is None, "у кампании пометки нет"


def test_a_single_ad_gets_its_own_numbers_without_a_mark(page):
    ad = dict(AD, parent_id=ADSET["id"])
    _paint(page, [ADSET, ad])
    _fill(page, [ADSET, ad], ATTR)
    assert _attr(page, 1)["leads"] == "3"
    assert _shared(page, 1)["mark"] is None
    assert _shared(page, 1)["cellTip"] == "From the stats workbook"


def test_the_mark_goes_away_when_the_numbers_do(page):
    """Второй ответ без строк для этого имени: прочерк, и пометка «общие
    числа» над прочерком больше ничего не значит."""
    a1 = dict(AD, parent_id=ADSET["id"])
    a2 = dict(AD, id="120250178850950704", parent_id=ADSET["id"])
    _paint(page, [ADSET, a1, a2])
    _fill(page, [ADSET, a1, a2], ATTR)
    _fill(page, [ADSET, a1, a2], dict(ATTR, ads={}))
    assert set(_attr(page, 1).values()) == {"—"}
    assert _shared(page, 1)["mark"] is None


def test_same_name_ads_in_same_name_campaigns_keep_the_dash(page):
    """Две кампании с одним именем — книга не знает, чьи это объявления."""
    twin = dict(ADSET, id="120250178859040704")
    a1 = dict(AD, parent_id=ADSET["id"])
    a2 = dict(AD, id="120250178850950704", parent_id=twin["id"])
    _paint(page, [ADSET, twin, a1, a2])
    _fill(page, [ADSET, twin, a1, a2], ATTR)
    lines = page.evaluate("() => document.querySelectorAll('.amap-tree-line').length")
    assert lines == 4
    for n in range(lines):
        assert set(_attr(page, n).values()) == {"—"}
        assert _shared(page, n)["mark"] is None


def test_meta_labels_in_a_stored_name_do_not_hide_the_numbers(page):
    """Чтения, сохранённые до ACL-1395, несут имя вместе с надписью Meta.
    Дерево показывает и сопоставляет имя без неё."""
    camp = dict(ADSET, name="SA_ARM_iOS_IMG_Reading_30_6.9 In draft")
    dirty = dict(AD, parent_id=camp["id"],
                 name="1962_12- Officialy Unpublished edits Unpublished edits")
    _paint(page, [camp, dirty])
    _fill(page, [camp, dirty], ATTR)
    assert _attr(page, 0)["leads"] == "7" and _attr(page, 1)["leads"] == "3"
    names = page.evaluate(
        "() => Array.from(document.querySelectorAll('.amap-tree-name')).map(n => n.textContent)")
    assert names == ["SA_ARM_iOS_IMG_Reading_30_6.9", "1962_12- Officialy"]


def test_a_real_name_with_a_label_word_inside_stays_whole(page):
    got = page.evaluate("""() => [amapTreeName('2002 - Draft 5 promo'),
                                  amapTreeName('Draft'),
                                  amapTreeName('ad  one In review Unpublished edits')]""")
    assert got == ["2002 - Draft 5 promo", "Draft", "ad one"]


def test_a_failed_workbook_answer_is_said_not_zeroed(page):
    _paint(page, [ADSET])
    _fill(page, [ADSET], {"ok": False, "error": "Could not read the workbook: busy"})
    assert set(_attr(page).values()) == {"—"}
    assert "busy" in page.evaluate("() => document.querySelector('.amap-tree-num.attr').title")


def test_tree_numbers_stand_under_the_account_columns(page):
    """Владелец: «цифры выровнялись, но не под наименованиями». Каждая колонка
    строки дерева начинается там же, где колонка строки аккаунта."""
    page.set_viewport_size({"width": 1400, "height": 800})
    page.evaluate("""() => {
      const cells = document.getElementById('acc').children;
      const widths = [220, 70, 110, 60, 50, 60, 60, 60, 110, 100, 180];
      document.querySelector('table').style.tableLayout = 'fixed';
      document.querySelector('table').style.width = widths.reduce((a, b) => a + b) + 'px';
      Array.from(cells).forEach((c, i) => { c.style.width = widths[i] + 'px'; });
    }""")
    # Раскладку делают стили дерева — берём их из шаблона, как и скрипт.
    src = REPORT.read_text(encoding="utf-8")
    css = src[src.index("    .amap-tree > td {"): src.index("    .amap-tree-err {")]
    page.add_style_tag(content=css)
    ad = dict(AD, parent_id=ADSET["id"])
    _paint(page, [ADSET, ad])
    _open(page)
    got = page.evaluate("""() => {
      const row = document.getElementById('acc');
      const line = document.querySelector('.amap-tree-line');
      const cells = Array.from(line.children);
      return cells.map((c, i) => [Math.round(c.getBoundingClientRect().left),
                                  Math.round(row.children[i].getBoundingClientRect().left)]);
    }""")
    assert len(got) == COLUMNS, "в строке дерева не столько колонок, сколько у аккаунта"
    for i, (tree_x, acc_x) in enumerate(got):
        assert abs(tree_x - acc_x) <= 1, f"колонка {i}: дерево {tree_x}, аккаунт {acc_x}"



def _tree_css():
    src = REPORT.read_text(encoding="utf-8")
    # ACL-1797: размеры шрифта — токены; без файла токенов вырезка рисуется крупнее.
    tokens = (TPL / "adopy-tokens.css").read_text(encoding="utf-8")
    return tokens + src[src.index("    .amap-tree > td {"): src.index("    .amap-tree-err {")]


def _colgroup_shares():
    """Доли колонок берём из самого шаблона: фикстура молча разъезжалась с ним."""
    import re

    src = (TPL / "_account_map_country.html").read_text(encoding="utf-8")
    shares = [int(x) for x in re.findall(r"width:(\d+)%", src)]
    assert len(shares) == COLUMNS and sum(shares) == 100, shares
    return shares


def _cell_padding():
    """Отступы ячейки — из самой страницы, а не на глаз.

    Ревью 23.09: фикстура ставила ширины прямо на ячейки и оставляла им
    браузерный отступ в 1px. Дерево берёт отступ у соседней ячейки аккаунта
    (`--amap-cell-pad`), поэтому все замеры ширины в этом файле выходили на
    26px на ячейку щедрее правды — и «числа стоят под заголовками» означало
    «стоят, но текста в них помещается два пикселя».
    """
    import re

    src = REPORT.read_text(encoding="utf-8")
    rule = re.search(r"\.amap td \{([^}]*)\}", src).group(1)
    return re.search(r"padding:\s*([^;]+);", rule).group(1).strip()


def _table_heads():
    """Заголовки колонок — из самого шаблона, а не списком в тесте."""
    import re

    src = (TPL / "_account_map_country.html").read_text(encoding="utf-8")
    head = src[src.index("<thead>"): src.index("</thead>")]
    return [re.sub(r"<[^>]+>", "", h).strip()
            for h in re.findall(r"<th[^>]*>(.*?)</th>", head, re.S)]


def _real_widths(page, table_px=982):
    """Доли колонок из шаблона на настоящей ширине таблицы.

    982px — ширина таблицы на ноутбуке 1280px (окно минус боковая колонка и
    поля). Мерить на 1200px было самообманом: такой ширины на экранах, ради
    которых написаны ACL-1275 и ACL-1277, не бывает.

    Колонки задаются `<colgroup>`, как в шаблоне, и ячейки получают отступ
    страницы: иначе таблица под тестом шире настоящей на 26px в каждой
    ячейке, и тест меряет не то, что видит человек.
    """
    page.set_viewport_size({"width": 1400, "height": 800})
    page.add_style_tag(content=_tree_css())
    page.add_style_tag(content="table td { padding: %s; }" % _cell_padding())
    page.evaluate("""([px, shares]) => {
      const t = document.querySelector('table');
      t.style.tableLayout = 'fixed'; t.style.width = px + 'px';
      const old = t.querySelector('colgroup');
      if (old) old.remove();
      const cg = document.createElement('colgroup');
      shares.forEach(w => { const c = document.createElement('col');
                            c.style.width = w + '%'; cg.appendChild(c); });
      t.insertBefore(cg, t.firstChild);
    }""", [table_px, _colgroup_shares()])


def test_names_stay_readable_in_the_narrow_first_column(page):
    """Ревью ACL-1256: имя кампании получало 32px, объявления — 10px. По имени
    решают, что выключать. ACL-1275/1277: имя стоит одной строкой с
    многоточием, а колонка расширена за счёт пустой колонки кнопок."""
    _real_widths(page)
    long_camp = dict(ADSET, name="KW_trl_All_Video_Mix_35_17.09")
    ad = dict(AD, parent_id=ADSET["id"], name="2221 - AR KW Trolley For citizens")
    _paint(page, [long_camp, ad])
    _open(page)
    page.evaluate("() => document.querySelector('.amap-tree-caret').click()")
    got = page.evaluate("""() => Array.from(document.querySelectorAll('.amap-tree-name')).map(n => ({
      w: n.getBoundingClientRect().width,
      lines: Math.round(n.getBoundingClientRect().height / parseFloat(getComputedStyle(n).lineHeight)),
      cellRight: n.closest('.amap-tree-who').getBoundingClientRect().right,
      right: n.getBoundingClientRect().right}))""")
    for g in got:
        assert g["w"] >= 110, f"имени досталось {g['w']:.0f}px"
        assert g["lines"] == 1, "имя перенеслось на вторую строку вместо многоточия"
        assert g["right"] <= g["cellRight"] + 1, "имя залезло в соседнюю колонку"


def test_the_manage_tab_copy_asks_for_its_country_too(page):
    """Ревью ACL-1256: на вкладке страны таблица аккаунтов стоит без блока
    `details.amap-country`, и числа не запрашивались вовсе. Страна теперь
    записана на самой таблице."""
    page.evaluate("""() => {
      document.querySelector('table').dataset.countryName = 'Kuwait';
      window._asked = [];
      window.fetch = (url, opts) => { window._asked.push([url, opts]);
        return Promise.resolve({json: () => Promise.resolve({ok: true, has_data: true,
          campaigns: {'SA_ARM_iOS_IMG_Reading_30_6.9': {spend: '$1', leads: 2, deposits: 0,
            cpl: '$0.50', cpa: null, cr: null}}, ads: {}})}); };
    }""")
    _paint(page, [ADSET])
    page.wait_for_function("() => document.querySelector('.amap-tree-num.attr').textContent !== ''")
    asked = page.evaluate("() => window._asked")
    assert len(asked) == 1
    url, opts = asked[0]
    assert url.startswith("/account-attribution?account=KLD_BFL_X84&country=Kuwait&date_from=2026-09-01&date_to=2026-09-12")
    assert not opts, "чтение чисел ушло не GET-ом"
    assert _attr(page)["leads"] == "2"


def test_an_answer_for_a_replaced_tree_is_dropped(page):
    """Перечитали аккаунт, пока шёл запрос: старый ответ не пишет в новое дерево."""
    page.evaluate("""() => {
      document.querySelector('table').dataset.countryName = 'Kuwait';
      window._release = null;
      window.fetch = () => new Promise(res => { window._release = () => res({json: () =>
        Promise.resolve({ok: true, campaigns: {'SA_ARM_iOS_IMG_Reading_30_6.9': {spend: '$999',
          leads: 99, deposits: 9, cpl: null, cpa: null, cr: null}}, ads: {}})}); });
    }""")
    _paint(page, [ADSET])
    first = page.evaluate("() => window._release")
    page.evaluate("() => { window._first = window._release; }")
    page.evaluate("() => { window.fetch = () => new Promise(() => {}); }")
    _paint(page, [ADSET])
    page.evaluate("() => window._first()")
    page.wait_for_timeout(50)
    assert _attr(page)["leads"] == "", "ответ для снятого дерева попал в новое"


def test_two_campaigns_with_one_name_get_dashes(page):
    twin = dict(ADSET, id="120250178859040704")
    _paint(page, [ADSET, twin])
    _fill(page, [ADSET, twin], ATTR)
    assert set(_attr(page, 0).values()) == {"—"} and set(_attr(page, 1).values()) == {"—"}
    assert "Two campaigns share this name" in page.evaluate(
        "() => document.querySelector('.amap-tree-num.attr').title")


def test_a_campaign_called_like_an_object_method_is_not_found_by_accident(page):
    odd = dict(ADSET, name="constructor")
    _paint(page, [odd])
    _fill(page, [odd], ATTR)
    assert set(_attr(page).values()) == {"—"}
    assert "No rows" in page.evaluate("() => document.querySelector('.amap-tree-num.attr').title")


def test_headings_and_journal_keep_their_own_spacing(page):
    page.add_style_tag(content=_tree_css())
    _paint(page, [ADSET])
    _open(page)
    mb = page.evaluate("() => getComputedStyle(document.querySelector('.amap-tree-head')).marginBottom")
    assert mb not in ("0px", ""), "заголовок дерева прилип к первой строке"



# ── дерево из чтения за другие даты (ACL-1271) ────────────────────────────

def _head(page):
    return page.evaluate("() => document.querySelector('.amap-tree-head').textContent")


def test_a_tree_from_other_dates_says_so(page):
    """Период на экране 2026-09-01…12, а дерево из чтения за 09-01…09-21."""
    page.evaluate("""(o) => amapPaintTree(document.getElementById('acc'),
      {objects: [o], date_from: '2026-09-01', date_to: '2026-09-21', finished_at: '2026-09-22 08:52:04'})""", ADSET)
    assert "Meta numbers for 2026-09-01…2026-09-21, workbook numbers for the dates on screen" in _head(page)


def test_a_tree_for_the_dates_on_screen_says_nothing_extra(page):
    page.evaluate("""(o) => amapPaintTree(document.getElementById('acc'),
      {objects: [o], date_from: '2026-09-01', date_to: '2026-09-12', finished_at: '2026-09-12 23:00:00'})""", ADSET)
    assert "Meta numbers for" not in _head(page)



def test_a_row_without_a_meta_result_has_no_source_label(page):
    """Нет результата — нет и подписи: пустая ячейка не притворяется данными."""
    _paint(page, [dict(ADSET, results="", cost_per_result="")])
    assert page.evaluate("() => document.querySelector('.amap-tree-num.res').textContent") == ""


def test_the_tree_row_has_no_dead_button_column(page):
    """ACL-1277: колонка кнопок у строк дерева всегда пуста и раньше была самой
    широкой (23% против 19% у имени). ACL-1282 свёл Read spend и Countries в
    одну кнопку, и колонка ужалась до 13% — имя получило 28%. Сама пустая
    колонка в строках дерева остаётся: её ширину доберёт ACL-1277."""
    _real_widths(page)
    _paint(page, [ADSET])
    _open(page)
    got = page.evaluate("""() => {
      const cells = Array.from(document.querySelector('.amap-tree-line').children);
      return {name: cells[0].getBoundingClientRect().width,
              tail: cells[cells.length - 1].getBoundingClientRect().width};
    }""")
    assert got["name"] > got["tail"], f"имя {got['name']:.0f}px против пустой {got['tail']:.0f}px"



def test_the_remove_control_keeps_its_action_and_its_name():
    """ACL-1277: «Remove» стал значком «×». Действие и подпись для экранного
    диктора остаются: кнопка убирает аккаунт из страны, а не удаляет его."""
    src = (TPL / "_account_map_country.html").read_text(encoding="utf-8")
    start = src.index('class="amap-x"')
    cell = src[start: src.index("</td>", start)]
    assert ">×</button>" in cell
    assert "amapUnlink(" in cell and "'country'" in cell
    assert "aria-label=" in cell and "out of {{ mc.country }}" in cell
    assert "keeps its Multilogin profile" in cell


def test_with_the_builder_open_the_source_label_yields_to_the_number(page):
    """С открытым билдером таблице остаётся мало места: подпись «Meta» прячется,
    чтобы само число Meta было видно. Источник остаётся в подсказке."""
    page.add_style_tag(content=_tree_css())
    _paint(page, [ADSET])
    page.evaluate("() => document.body.classList.add('cbd-open')")
    assert page.evaluate(
        "() => getComputedStyle(document.querySelector('.amap-tree-src')).display") == "none"
    assert "Meta's own result" in page.evaluate(
        "() => document.querySelector('.amap-tree-num.res').title")


def test_the_meta_label_does_not_glue_itself_to_the_number(page):
    """Без пробела копирование и экранный диктор дают «Meta18 Leads (Form)»."""
    _paint(page, [ADSET])
    assert page.evaluate(
        "() => document.querySelector('.amap-tree-num.res').textContent") == "Meta 18 Leads (Form)"


def test_a_price_without_a_result_still_has_its_tooltip(page):
    _paint(page, [dict(ADSET, results="", cost_per_result="$22.89 Per Lead (Form)")])
    assert page.evaluate(
        "() => document.querySelector('.amap-tree-num.res').title") == "$22.89 Per Lead (Form)"


def test_the_two_buttons_fit_the_narrowest_real_width(page):
    """ACL-1282: кнопок две, и они обязаны помещаться в свою колонку.

    582px — ширина таблицы на ноутбуке 1280px с открытым билдером; уже этого
    таблица на рабочем экране не бывает. Прежняя тройка кнопок в такой
    колонке не помещалась и выталкивала «Remove» за край: он оставался в
    разметке и в тестах, а нажать его было нельзя (acl-357).
    """
    _real_widths(page, table_px=582)
    page.evaluate("""() => {
      const row = document.querySelector('tbody tr') || document.querySelector('tr');
      const cell = row.children[10];
      cell.innerHTML = '<button type="button" class="amap-btn amap-read">Read spend</button>'
                     + '<button type="button" class="amap-x">\u00d7</button>';
    }""")
    got = page.evaluate("""() => {
      const cell = (document.querySelector('tbody tr') || document.querySelector('tr')).children[10];
      const btns = Array.from(cell.querySelectorAll('button'));
      return {right: Math.max(...btns.map(b => b.getBoundingClientRect().right)),
              cellRight: cell.getBoundingClientRect().right,
              widths: btns.map(b => Math.round(b.getBoundingClientRect().width))};
    }""")
    assert got["right"] <= got["cellRight"] + 1, (
        f"кнопки вышли за край колонки на {got['right'] - got['cellRight']:.0f}px"
    )
    assert min(got["widths"]) > 0, "кнопка сжалась в ничто"


# ── Узкий экран: имя своей строкой (ACL-1277) ─────────────────────────────
#
# Замер 23.09, настоящий фрагмент карты: десяти колонкам кроме имени нужно по
# содержимому 660px. С открытым билдером таблица получает 582px на ноутбуке
# 1280 и 668px на 1366 — имени не остаётся ничего, и перераспределением долей
# это не чинится. Владелец выбрал не прятать числа: имя уходит на свою строку.


def test_a_narrow_table_puts_the_name_on_its_own_line(page):
    """Имя получает всю ширину таблицы, а не 91px из колонки."""
    _real_widths(page, table_px=582)
    _paint(page, [ADSET])
    _open(page)
    got = page.evaluate("""() => {
      const line = document.querySelector('.amap-tree-line');
      const who = line.querySelector('.amap-tree-who');
      const name = line.querySelector('.amap-tree-name');
      // Пустые клетки в узкой раскладке скрыты — берём первое настоящее число.
      const first = line.querySelector('.amap-tree-num.spend');
      return {stacked: !!line.closest('.amap-stacked'),
              who: Math.round(who.getBoundingClientRect().width),
              name: Math.round(name.getBoundingClientRect().width),
              line: Math.round(line.getBoundingClientRect().width),
              nameTop: Math.round(name.getBoundingClientRect().top),
              numTop: Math.round(first.getBoundingClientRect().top)};
    }""")
    assert got["stacked"], "узкая таблица осталась в одну строку"
    assert got["who"] > got["line"] * 0.9, (
        f"имя заняло {got['who']}px из {got['line']}px — это не своя строка"
    )
    assert got["name"] >= 400, f"имени досталось {got['name']}px"
    assert got["numTop"] > got["nameTop"], "числа остались на строке имени"


def test_every_number_says_what_it_is_when_the_columns_are_gone(page):
    """Решение владельца 23.09, принятое по честному замеру.

    Сначала числа во второй строке оставили в колонках — и они получили по
    2px текста: «$…», «1..», «6..». Под какими заголовками они при этом
    стояли, значения не имело: на этой ширине сама шапка читается как
    «S… A… L… F…». Поэтому колонок во второй строке нет, а у каждого числа
    своя подпись.
    """
    _real_widths(page, table_px=582)
    _paint(page, [ADSET])
    _open(page)
    page.evaluate("""() => {
      // Книжная атрибуция приходит отдельным запросом — здесь ставим сами.
      const vals = {spend: '$1,044.82', leads: '181', deposits: '12',
                    cpl: '$5.77', cpa: '$87.07', cr: '6.6%'};
      document.querySelectorAll('.amap-tree-line [data-attr]').forEach(c => {
        c.textContent = vals[c.dataset.attr] || '';
      });
    }""")
    got = page.evaluate("""() => {
      const line = document.querySelector('.amap-tree-line');
      return Array.from(line.children).filter(c => !c.classList.contains('amap-tree-who'))
        .map(c => ({label: c.dataset.label || '', text: c.textContent.trim(),
                    shown: c.getBoundingClientRect().width > 0,
                    full: c.scrollWidth <= c.clientWidth + 1}));
    }""")
    filled = [g for g in got if g["text"]]
    assert len(filled) >= 8, f"чисел в строке всего {len(filled)}"
    for g in filled:
        assert g["label"], f"число «{g['text']}» не говорит, что оно такое"
        assert g["shown"] and g["full"], f"«{g['label']} {g['text']}» обрезано"
    # Пустая ячейка исчезает целиком: «CPL» без числа обещает число, которого нет.
    for g in got:
        if not g["text"]:
            assert not g["shown"], f"пустая клетка «{g['label']}» показана"


def test_the_labels_are_the_words_from_the_table_head(page):
    """Подпись называет то же, что заголовок колонки, — иначе одно и то же
    число на широком и узком экране называется по-разному.

    Три подписи нарочно не совпадают с шапкой, и каждая — решение, а не
    оплошность:

    * «Campaign list» не обозначает сумму: ACL-2063 заменяет ею спенд аккаунта.
      Совместимый старый форматтер дерева сохраняет отдельную подпись «Meta spend».
    * «Status» → «Delivery». Status строки аккаунта — предупреждение
      кабинета; здесь стоит доставка Meta.
    * «Note» → «Meta». Note аккаунта — заметка менеджера (ACL-1620; раньше Setup), а в дереве
      эта колонка несёт результат Meta (ACL-1275).

    Остальные читаются из шаблона: переименуют заголовок — тест это заметит.
    """
    heads = _table_heads()
    _real_widths(page, table_px=582)
    _paint(page, [ADSET])
    _open(page)
    labels = page.evaluate("""() => Array.from(
      document.querySelectorAll('.amap-tree-line > [data-label]')).map(c => c.dataset.label)""")
    # ACL-2063: в текущей карте вместо расхода Meta — состояние списка.
    # Старое дерево сохраняет собственную подпись для совместимого ответа.
    assert labels[1] == 'Meta spend' and heads[2] == 'Campaign list', (heads[2], labels[1])
    assert labels[7] == 'Delivery' and heads[8] == 'Status', (heads[8], labels[7])
    assert labels[8] == 'Meta' and heads[9] == 'Note', (heads[9], labels[8])
    # Книжные числа названы ровно так, как их зовёт шапка.
    assert [labels[0]] + labels[2:7] == [heads[1]] + heads[3:8], (labels, heads)


def test_the_labels_do_not_show_up_in_the_wide_layout(page):
    """В широкой раскладке слово стоит в шапке таблицы; второе рядом с
    числом — это шум и лишняя ширина."""
    _real_widths(page, table_px=982)
    _paint(page, [ADSET])
    _open(page)
    shown = page.evaluate("""() => {
      const c = document.querySelector('.amap-tree-line > [data-label]');
      return getComputedStyle(c, '::before').content;
    }""")
    assert shown in ('none', 'normal', '""'), f"подпись видна и в широкой раскладке: {shown}"


def test_a_wide_table_keeps_the_single_line(page):
    """На своём экране строка остаётся одной: лишняя высота даром никому."""
    _real_widths(page, table_px=982)
    _paint(page, [ADSET])
    _open(page)
    got = page.evaluate("""() => {
      const line = document.querySelector('.amap-tree-line');
      const who = line.querySelector('.amap-tree-who');
      const first = line.children[1];
      return {stacked: !!line.closest('.amap-stacked'),
              who: Math.round(who.getBoundingClientRect().width),
              line: Math.round(line.getBoundingClientRect().width),
              lineH: Math.round(line.getBoundingClientRect().height),
              sameRow: first.getBoundingClientRect().top
                       < who.getBoundingClientRect().bottom};
    }""")
    assert not got["stacked"], "широкая таблица зря разложилась на две строки"
    assert got["who"] < got["line"] * 0.4, "имя заняло строку целиком без нужды"
    assert got["sameRow"], "числа уехали под строку имени"
    assert got["lineH"] < 40, f"строка выросла до {got['lineH']}px"


def test_ads_opened_later_get_the_same_layout(page):
    """Строка объявления скрыта, когда раскладку выбирают, и обязана встать
    правильно, когда её показали.

    Дорисовкой это не назвать: `amapPaintTree` строит строки объявлений сразу,
    а стрелка только снимает `hidden` (ревью 23.09). Проверяется именно
    «скрытую строку разложило правильно».
    """
    _real_widths(page, table_px=582)
    ad = dict(AD, parent_id=ADSET["id"])
    _paint(page, [ADSET, ad])
    _open(page)
    page.evaluate("() => document.querySelector('.amap-tree-caret').click()")
    got = page.evaluate("""() => Array.from(document.querySelectorAll('.amap-tree-line.ad')).map(l => {
      const who = l.querySelector('.amap-tree-who');
      return {who: Math.round(who.getBoundingClientRect().width),
              line: Math.round(l.getBoundingClientRect().width)};
    })""")
    assert got, "объявления не раскрылись"
    for g in got:
        assert g["who"] > g["line"] * 0.9, (
            f"строке объявления досталось {g['who']}px из {g['line']}px"
        )


def test_the_workbook_dash_does_not_become_a_label_without_a_number(page):
    """У объявления строк в книге обычно нет вовсе, и узкая раскладка читалась
    бы как «SPEND — LEADS — FTD — CPL — CPA — CR —»: шесть подписей, за
    которыми ничего. В широкой раскладке прочерк остаётся — там он стоит в
    своей колонке и говорит «смотрели, не нашли»."""
    _real_widths(page, table_px=582)
    _paint(page, [ADSET])
    _open(page)
    page.evaluate("""() => {
      document.querySelectorAll('.amap-tree-line [data-attr]').forEach(c => {
        c.textContent = '—'; c.classList.add('nodata');
      });
    }""")
    shown = page.evaluate("""() => Array.from(
      document.querySelectorAll('.amap-tree-line [data-attr]'))
      .filter(c => c.getBoundingClientRect().width > 0).length""")
    assert shown == 0, f"{shown} подписей без числа осталось на экране"


def test_the_numbers_line_keeps_the_tree_indent(page):
    """Уступ — единственное, чем строка чисел показывает, чья она. Без него
    числа объявления начинаются левее имени его же кампании."""
    _real_widths(page, table_px=582)
    ad = dict(AD, parent_id=ADSET["id"])
    _paint(page, [ADSET, ad])
    _open(page)
    page.evaluate("() => document.querySelector('.amap-tree-caret').click()")
    got = page.evaluate("""() => {
      const pick = sel => {
        const line = document.querySelector(sel);
        return {num: line.querySelector('.amap-tree-num.spend').getBoundingClientRect().left,
                line: line.getBoundingClientRect().left,
                who: line.querySelector('.amap-tree-who').getBoundingClientRect().left,
                name: line.querySelector('.amap-tree-name').getBoundingClientRect().left};
      };
      return {camp: pick('.amap-tree-line:not(.ad)'), ad: pick('.amap-tree-line.ad')};
    }""")
    # Имя объявления стоит там же, где начинается его строка чисел: уступ
    # считается один раз, а не дважды (строкой и её же ячейкой имени).
    assert abs(got["ad"]["who"] - got["ad"]["num"]) < 1.5, (
        f"имя объявления на {got['ad']['who'] - got['ad']['num']:.0f}px правее "
        "собственной строки чисел — уступ посчитан дважды"
    )
    step = got["ad"]["num"] - got["camp"]["num"]
    assert step > 8, (
        f"числа объявления сдвинуты относительно кампании на {step:.0f}px — "
        "по строке чисел не видно, чья она"
    )
    # Уступ держит строка целиком, а не одна ячейка имени: иначе числа
    # встали бы у левого края, одинаково у кампании и у объявления.
    assert (got["ad"]["num"] - got["ad"]["line"])         > (got["camp"]["num"] - got["camp"]["line"]) + 8


def test_a_workbook_that_could_not_be_read_says_so(page):
    """«Книга не прочиталась» и «в книге нет строк» — разные вещи.

    В узкой раскладке прочерки спрятаны, и поломка выглядела бы там ровно
    как отсутствие строк, то есть никак: причина жила только в подсказке к
    клетке, которой больше нет.
    """
    _real_widths(page, table_px=582)
    _paint(page, [ADSET])
    _open(page)
    page.evaluate("""() => {
      const box = document.querySelector('.amap-tree-inner');
      amapFillAttribution(box, [], {ok: false, error: 'Workbook numbers could not be loaded'});
    }""")
    got = page.evaluate("""() => {
      const n = document.querySelector('.amap-tree-attr-err');
      return n ? {text: n.textContent, shown: n.getBoundingClientRect().width > 0} : null;
    }""")
    assert got and got["shown"], "поломка книги не сказана словами"
    assert "could not be loaded" in got["text"]


def test_the_note_goes_away_when_the_workbook_answers(page):
    """Иначе одна неудача осталась бы на экране навсегда."""
    _real_widths(page, table_px=582)
    _paint(page, [ADSET])
    _open(page)
    page.evaluate("""() => {
      const box = document.querySelector('.amap-tree-inner');
      amapFillAttribution(box, [], {ok: false, error: 'nope'});
      amapFillAttribution(box, [], {ok: true, campaigns: {}, ads: {}});
    }""")
    assert page.evaluate("() => document.querySelectorAll('.amap-tree-attr-err').length") == 0


def test_a_blank_workbook_value_is_not_a_value(page):
    """Пробел из книги не должен оставлять подпись без числа."""
    _real_widths(page, table_px=582)
    _paint(page, [ADSET])
    _open(page)
    page.evaluate("""(name) => {
      const box = document.querySelector('.amap-tree-inner');
      amapFillAttribution(box, [], {ok: true, ads: {},
        campaigns: {[name]: {spend: ' ', leads: '181'}}});
    }""", ADSET["name"])
    shown = page.evaluate("""() => Array.from(
      document.querySelectorAll('.amap-tree-line [data-attr]'))
      .filter(c => c.getBoundingClientRect().width > 0)
      .map(c => c.dataset.attr + '=' + c.textContent.trim())""")
    assert shown == ['leads=181'], shown


def test_the_workbook_failure_is_said_in_both_layouts(page):
    """Решение осознанное, а не побочное: в широкой раскладке прочерки с
    подсказками остаются, но причина, живущая только в подсказке, — это
    причина, которую никто не читает. Строка стоит в обеих раскладках."""
    _real_widths(page, table_px=982)
    _paint(page, [ADSET])
    _open(page)
    page.evaluate("""() => amapFillAttribution(
      document.querySelector('.amap-tree-inner'), [], {ok: false, error: 'no workbook'})""")
    got = page.evaluate("""() => {
      const n = document.querySelector('.amap-tree-attr-err');
      const dash = document.querySelector('.amap-tree-num.attr');
      return {shown: !!n && n.getBoundingClientRect().width > 0,
              dash: dash.textContent.trim(), tip: dash.title,
              stacked: !!document.querySelector('.amap-stacked')};
    }""")
    assert not got["stacked"], "широкая раскладка неожиданно разложилась"
    assert got["shown"], "в широкой раскладке поломка книги молчит"
    assert got["dash"] == "—" and got["tip"], "прочерк и его подсказка пропали"


def test_the_failure_line_stands_with_the_numbers_not_under_the_log(page):
    """Остальные оговорки дерева стоят до журнала; эта приходит позже, и без
    вставки на место падала под свёрнутый «What was changed here»."""
    _real_widths(page, table_px=582)
    _paint(page, [ADSET])
    _open(page)
    page.evaluate("""() => amapFillAttribution(
      document.querySelector('.amap-tree-inner'), [], {ok: false, error: 'no workbook'})""")
    order = page.evaluate("""() => {
      const box = document.querySelector('.amap-tree-inner');
      const kids = Array.from(box.children);
      const log = box.querySelector('.amap-tree-log');
      return {note: kids.indexOf(box.querySelector('.amap-tree-attr-err')),
              log: log ? kids.indexOf(log) : -1};
    }""")
    assert order["note"] >= 0
    if order["log"] >= 0:
        assert order["note"] < order["log"], "оговорка встала под журналом"
