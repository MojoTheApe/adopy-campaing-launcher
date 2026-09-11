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
    start = src.index("function amapTreeLine(o) {")
    tail = "row.parentNode.insertBefore(tr, row.nextSibling);\n}"
    end = src.index(tail, start) + len(tail)
    block = src[start:end]
    assert "amapPaintTree" in block, "срез не захватил вторую функцию"
    return block


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


COLUMNS = 11  # столько их в таблице аккаунтов страны


@pytest.fixture
def page(browser):
    p = browser.new_page()
    p.set_content(
        "<table><tbody>"
        '<tr id="acc">' + ('<td>x</td>' * COLUMNS) + "</tr>"
        '<tr id="after"><td>next account</td></tr>'
        "</tbody></table>"
    )
    p.add_script_tag(content=_script())
    yield p
    p.close()


def _paint(page, objects, *, error="", finished="2026-09-11 23:07:51"):
    page.evaluate(
        "(s) => amapPaintTree(document.getElementById('acc'), s)",
        {"objects": objects, "objects_error": error, "finished_at": finished},
    )


ADSET = {"level": "adset", "id": "120250178859040703", "name": "SA_ARM_iOS_IMG_Reading_30_6.9",
         "switch": "on", "switch_label": "On/off", "delivery": "Active"}
AD = {"level": "ad", "id": "120250178850950703", "name": "1962_12- Officialy",
      "switch": "on", "switch_label": "On/off", "delivery": "Active"}


# ── что появляется ────────────────────────────────────────────────────────

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
    assert [g["kind"] for g in got] == ["ad set", "ad"]
    assert got[0]["name"] == "SA_ARM_iOS_IMG_Reading_30_6.9"
    assert "120250178859040703" in got[0]["title"], "id негде посмотреть"
    assert got[1]["ad"] is True, "объявление не отступлено под своим уровнем"


def test_the_header_counts_both_levels(page):
    _paint(page, [ADSET, ADSET, AD])
    head = page.evaluate("() => document.querySelector('.amap-tree-head').textContent")
    assert "2 ad sets" in head and "1 ads" in head
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


def test_the_display_cannot_switch_anything_yet():
    """Показ — это показ. Переключение придёт второй половиной acl-113, и до
    тех пор контрола, похожего на переключатель, быть не должно."""
    block = _script()
    for forbidden in ("onclick", "addEventListener", "fetch(", "role=\"switch\"", "'switch'"):
        assert forbidden not in block, f"в показе есть {forbidden} — он обещает то, чего не делает"
