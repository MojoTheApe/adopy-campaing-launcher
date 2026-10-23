"""ACL-2118: окно «Publish all → PAUSED campaigns» помещается в экран ноутбука.

Владелец (22.10, 4 аккаунта / 8 кампаний): окно узкое и высокое, сверху большой
жёлтый блок с абзацем на каждую кампанию, нужное — ниже сгиба, кнопка подтверждения
за экраном. Теперь: итоговая строка, аккаунты чипами, повторы одной строкой со
счётчиком, справка в подсказке; заголовок и кнопки стоят, прокручивается середина.

Группировка проверяется в node на настоящем коде шаблона; раскладка — в
офлайн-Chromium на настоящем CSS при 1366×768 и 1920×1080, данные выдуманные.
С переменной ACL2118_SHOTS=<папка> тот же сценарий снимает экраны.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_duplicate_buttons import browser  # noqa: E402,F401
from test_acl1819_preflight_browser import _open  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "src" / "web" / "templates" / "report.html"


def test_notice_grouping_and_popup_body_in_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    r = subprocess.run([node, str(ROOT / "tests" / "acl2118_publish_popup_harness.cjs"), str(TPL)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr + r.stdout


SHOW = """([accounts, perAccount, many]) => {
  const accs = [];
  for (let a = 0; a < accounts; a++) accs.push({ name: 'Demo account ' + (a + 1) + ' (Chile)',
    camps: Array.from({length: perAccount}, (_, i) => 'Leads | Finance | Broker ' + (a + 1) + '.' + (i + 1) + ' | Chile | 2026-10-22') });
  const items = [];
  accs.forEach(a => a.camps.forEach(c => items.push({ level: 'info', rule: 'primary_len',
    label: 'Text visible in full (up to 125)', hint: "after 125 characters the text is folded behind 'See more'", campaign: c })));
  if (many) accs.forEach(a => items.push({ level: 'warn', rule: 'link_tracking', label: 'Tracking parameters in the link',
    hint: 'the link has no ?key=value parameters: clicks may not be attributed', campaign: a.camps[0] }));
  const parts = cbPublishConfirmParts({ verb: 'Publish', targetText: 'campaign PAUSED, ad sets and ads active', accounts: accs,
    modeHtml: '<b>Campaign PAUSED</b> — ad sets and ads are active and start when you turn the campaign on (created through the Graph API)',
    noticesHtml: '<div class="pc-notes"><div class="pc-h">Pre-flight warnings (they do not block)</div>'
      + cbNoticesHtml(cbGroupNotices(items)) + '</div>' });
  window.__confirmed = 0;
  _safeConfirm('Create every campaign as a PAUSED campaign?', parts.body, () => { window.__confirmed++; },
    { confirmLabel: 'Publish all → PAUSED campaigns', wide: true, lead: parts.lead });
}"""

GEOMETRY = """() => {
  const r = (s) => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect();
    return { top: b.top, bottom: b.bottom, left: b.left, right: b.right, w: b.width, h: b.height }; };
  const body = document.querySelector('.sfc-body');
  return { vw: innerWidth, vh: innerHeight, box: r('.sfc-box'), head: r('.sfc-head'), ok: r('#sfc-ok'), cancel: r('#sfc-cancel'),
    left: r('.pc-left'), right: r('.pc-right'),
    scrolls: body.scrollHeight > body.clientHeight + 1,
    pageScrolls: document.documentElement.scrollHeight > innerHeight + 1 };
}"""


@pytest.mark.parametrize("size", [(1366, 768), (1920, 1080)])
@pytest.mark.parametrize("shape", [(4, 2, False), (6, 6, True)])
def test_the_popup_fits_the_screen_with_fixed_header_and_buttons(browser, size, shape):
    p, state = _open(browser, "api")
    try:
        p.set_viewport_size({"width": size[0], "height": size[1]})
        p.evaluate(SHOW, list(shape))
        g = p.evaluate(GEOMETRY)
        assert g["box"]["top"] >= 0 and g["box"]["bottom"] <= g["vh"], g           # окно целиком на экране
        assert g["ok"]["bottom"] <= g["vh"] and g["cancel"]["bottom"] <= g["vh"], g   # кнопки видны без прокрутки
        assert g["head"]["top"] >= 0, g
        assert g["box"]["w"] <= min(900, g["vw"] * 0.9) + 1, g
        assert g["box"]["w"] >= min(800, g["vw"] * 0.85), "окно осталось узким"
        # Две колонки: слева аккаунты, справа замечания — рядом, а не друг под другом.
        assert abs(g["left"]["top"] - g["right"]["top"]) < 2 and g["right"]["left"] > g["left"]["left"] + 100, g
        # Всё раскрыто — окно по-прежнему в экране, прокручивается только середина.
        p.evaluate("document.querySelectorAll('.sfc-box details').forEach(d => d.open = true)")
        g2 = p.evaluate(GEOMETRY)
        assert g2["box"]["bottom"] <= g2["vh"] and g2["ok"]["bottom"] <= g2["vh"] and g2["head"]["top"] >= 0, g2
        if shape[0] * shape[1] > 30:
            assert g2["scrolls"], "длинное содержимое должно прокручиваться внутри окна"
        p.evaluate("document.querySelectorAll('.sfc-box details').forEach(d => d.open = false)")
        # Повторные замечания — одна строка на правило, свёрнутая.
        assert p.locator("details.pc-note").count() == (2 if shape[2] else 1)
        assert p.locator("details.pc-note[open]").count() == 0
        lead = p.locator(".sfc-lead").inner_text()
        assert "campaign PAUSED, ad sets and ads active" in lead, lead   # ACL-2114 wording
        assert "Publish 8 campaigns on 4 accounts" in lead or shape[0] != 4
        # Аккаунты свернуты при > 6 кампаниях; «Publish» остаётся на месте.
        assert p.locator("details.pc-acc").count() == shape[0]
        p.locator("#sfc-ok").click()
        assert p.evaluate("window.__confirmed") == 1
        assert not state["errors"], state["errors"]
        shots = os.environ.get("ACL2118_SHOTS")
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            p.evaluate(SHOW, list(shape))
            p.screenshot(path=str(Path(shots) / f"after-{size[0]}x{size[1]}-{shape[0]}acc-{shape[0]*shape[1]}camp.png"))
    finally:
        state["ctx"].close()


def test_expanding_a_notice_lists_the_campaigns(browser):
    p, state = _open(browser, "api")
    try:
        p.set_viewport_size({"width": 1366, "height": 768})
        p.evaluate(SHOW, [4, 2, False])
        note = p.locator("details.pc-note").first
        assert "8 campaigns" in note.inner_text()
        note.locator("summary").click()
        assert note.locator("li").count() == 8
    finally:
        state["ctx"].close()
