# -*- coding: utf-8 -*-
"""Шаг Media мастера креатива: дойти до него или назвать, что мешает (ACL-1348).

Живые прогоны 25.09 (CL_ML Video Day 956d0d184138 и SA Investing
08ca37a0b941, второе объявление) и 28.09 у байера (DAN-LT25, аккаунт 5844A,
две кампании MX_AMZ, релиз 26.09): второе объявление осталось без ролика,
Sentry PYTHON-14 — `ad2_creative_upload: silent`. Байер видел, что по шагу
**Media** мастера нельзя кликнуть, и загрузка не начиналась.

Что установлено по снимкам 25.09. На скриншотах отказа мастер стоит ПОСЛЕ
Media («Creative setup ✓ · Media ✓ · ◐ Text» у 956d, «Media ✓ · ◐ Creative
setup · Crop …» у 08ca), но это состояние ПОСЛЕ работы робота, а не то, в
котором мастер открылся. В 956d робот дошёл до Media и выбрал плитку
(`library_pick_selected`, 06:52:44), а на Text мастер перевёл его же Next —
это случай ACL-1313 (мастер на Text = медиа выбрано), он уже закрыт. У 08ca
строки медиатеки нет: до выбора робот не дошёл, и что показывал шаг Media
(кнопки Upload не нашлось?), снимок не говорит. Что мешало у DAN-LT25 —
неизвестно: причина не записывалась (`silent`).

Поэтому проверяется не одна догадка, а поведение в каждом положении мастера:
вернуться к Media, если он позади и доступен; не жать Next, если он позади и
недоступен; назвать причину отказа; снять мастер отдельным блоком.

Разметка: строка шагов из снимка 956d0d/04 — фикстура
tests/fixtures/ads_manager/creative_wizard_steps_2026_09_25.html (role=list >
listitem > role=button, `aria-current="step"`); карточка объявления под
мастером — ad_creative_media_step_2026_09_25.html. Строки шагов с запертым,
перекрытым или глухим к клику Media — синтетические: таких состояний в
снимках нет.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ads_manager" / "ad_creative_media_step_2026_09_25.html"
ZWSP = "\u200b"


def _ad_page() -> str:
    html = FIXTURE.read_text(encoding="utf-8")
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    return f'<div id="ad-page" style="padding:20px">{html}</div>'


# Строка шагов по скриншотам 25.09: иконка, подпись, точка-разделитель.
# `step_attrs` — атрибуты шага Media (роль, aria-disabled), `label` — его
# подпись (с невидимым пробелом или без), `clicks` — открывает ли клик Media.
_WIZARD = """
<div id="wizard" role="dialog" style="position:fixed; top:20px; left:120px; width:1200px;
     height:800px; background:#fff; z-index:10">
  <div id="steps" style="display:flex; gap:18px; height:28px; align-items:center">
    STEPS
    <div role="button" aria-label="Close" style="margin-left:auto">×</div>
  </div>
  <div id="body" style="padding:10px">BODY</div>
  <div style="position:absolute; bottom:10px; right:10px">
    <div role="button" id="cancel" style="display:inline-block; padding:6px">Cancel</div>
    <div role="button" id="next" style="display:inline-block; padding:6px">Next</div>
  </div>
</div>
<script>
  window.nextClicks = 0; window.mediaClicks = 0;
  const bodies = {
    'Creative setup': '<div><span>Creative setup</span></div><div><span>Review and manage the information we found in your ad sources.</span></div>',
    'Media': '<div><span>Media</span></div><div role="button" id="upload" style="display:inline-block">+ Upload</div>',
    'Text': '<div><span>Text</span></div><div><span>Add multiple text options and we\\'ll show the one we predict will perform best when your ad is delivered.</span></div>',
    'Crop': '<div><span>Crop</span></div>',
    'Creative generation': '<div><span>Creative generation</span></div>',
    'Enhancements': '<div><span>Enhancements</span></div>',
    'Translation': '<div><span>Translation</span></div>',
  };
  const order = ORDER;
  let cur = START;
  const show = (name) => { cur = name; document.getElementById('body').innerHTML = bodies[name]; };
  show(cur);
  document.getElementById('next').addEventListener('click', () => {
    window.nextClicks++;
    const i = order.indexOf(cur);
    if (i + 1 < order.length) show(order[i + 1]);
  });
  const media = document.getElementById('step-media');
  media.addEventListener('click', () => {
    window.mediaClicks++;
    if (MEDIA_OPENS) show('Media');
  });
</script>
"""


def _step(name: str) -> str:
    return (f'<div class="step" style="display:flex; gap:4px"><i>✓</i><span>{name}</span>'
            f'<span aria-hidden="true">·</span></div>')


def _wizard(order: list[str], start: str, *, media_attrs: str = "", label: str = "Media",
            media_opens: bool = True) -> str:
    steps = []
    for name in order:
        if name == "Media":
            steps.append(f'<div id="step-media" {media_attrs} style="display:flex; gap:4px; cursor:pointer">'
                         f'<i>✓</i><span>{label}</span><span aria-hidden="true">·</span></div>')
        else:
            steps.append(_step(name))
    return (_WIZARD.replace("STEPS", "".join(steps))
            .replace("ORDER", json.dumps(order))
            .replace("START", json.dumps(start))
            .replace("MEDIA_OPENS", "true" if media_opens else "false"))


ORDER_956D = ["Creative setup", "Media", "Text", "Creative generation", "Enhancements", "Translation"]
ORDER_08CA = ["Media", "Creative setup", "Crop", "Text", "Enhancements", "Translation"]


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser, monkeypatch, tmp_path):
    from src.fb_ads_rpa import page_capture

    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    # Снимки страниц — во временную папку, не в output/ чекаута.
    monkeypatch.setattr(page_capture, "_root", lambda: tmp_path / "rpa_pages")
    page_capture._taken.clear()
    # Снимок мастера — один на объявление; флаг сбрасывает обёртка, а здесь
    # `upload_ad_creative` зовётся напрямую.
    md._upload_why_tls.wizard_captured = False
    rt.set_current_run("run-1348", "CAMP")
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    p = ctx.new_page()
    p.route("**/*", lambda route: route.abort())
    try:
        yield p
    finally:
        ctx.close()


def _rows(tmp_path, step="ad_creative_upload"):
    f = tmp_path / "telemetry.jsonl"
    if not f.exists():
        return []
    return [r for r in (json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip())
            if r["step"] == step]


@pytest.fixture
def wizard_opens(page, monkeypatch):
    """«Set up creative» → мастер в заданном состоянии; медиатека — заглушка."""
    state = {"html": "", "reuse": []}

    def open_wizard(p, trigger, item, **kw):
        p.evaluate("(h) => { const d = document.createElement('div'); d.innerHTML = h;"
                   " document.body.appendChild(d);"
                   " d.querySelectorAll('script').forEach(s => { const n = document.createElement('script');"
                   " n.textContent = s.textContent; s.replaceWith(n); }); }", state["html"])
        return True, []

    def reuse(p, path):
        # До медиатеки дошли только если на экране кнопка Upload шага Media.
        state["reuse"].append(p.locator("#upload").count())
        return True

    monkeypatch.setattr(md, "_open_creative_wizard", open_wizard)
    monkeypatch.setattr(md, "_scroll_until_mounted", lambda *a, **k: None)
    monkeypatch.setattr(md, "reuse_before_upload", reuse)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: True)
    page.set_content(_ad_page())
    return state


# ── дойти до Media ────────────────────────────────────────────────────────

@pytest.mark.parametrize("attrs,label", [
    ("", "Media"),                          # подпись без роли
    ('role="button" tabindex="0"', "Media" + ZWSP),  # роль есть, но в подписи \u200b
], ids=["no_role", "zwsp_label"])
def test_the_wizard_opened_past_media_goes_back_to_media(page, wizard_opens, tmp_path, attrs, label):
    """956d0d, 25.09: мастер открыт на Text с галочкой у Media. Робот обязан
    вернуться к Media кликом по шагу, а не жать Next (оно ведёт дальше)."""
    wizard_opens["html"] = _wizard(ORDER_956D, "Text", media_attrs=attrs, label=label)
    assert md.upload_ad_creative(page, "Video", "C:/media/2012.mp4", reuse_only=True) is True
    assert wizard_opens["reuse"] == [1], "до медиатеки на шаге Media не дошли"
    assert page.evaluate("window.mediaClicks") >= 1
    assert page.evaluate("window.nextClicks") == 0, "Next уводит от Media — жать его здесь нельзя"


def test_media_after_the_current_step_is_still_reached_with_next(page, wizard_opens):
    """Обычный свежий мастер: Creative setup → Next → Media. Не сломать."""
    wizard_opens["html"] = _wizard(ORDER_956D, "Creative setup", media_attrs='aria-disabled="true"',
                                   media_opens=False)
    assert md.upload_ad_creative(page, "Video", "C:/media/2012.mp4", reuse_only=True) is True
    assert wizard_opens["reuse"] == [1]
    assert page.evaluate("window.nextClicks") == 1


# ── настоящая строка шагов (956d0d, 25.09, 06:52:44) ─────────────────────

STEPS_FIXTURE = ROOT / "tests" / "fixtures" / "ads_manager" / "creative_wizard_steps_2026_09_25.html"

# Без CSS Meta список шагов лёг бы столбцом; на экране он — ряд (скриншоты 25.09).
_ROW_CSS = "<style>[role=list]{display:flex; gap:18px} [role=listitem]{display:flex; gap:4px}</style>"


def _real_wizard() -> str:
    html = re.sub(r"<!--.*?-->", "", STEPS_FIXTURE.read_text(encoding="utf-8"), flags=re.S)
    return _ROW_CSS + html


def test_the_real_step_row_is_read(page):
    """Разметка из снимка: role=list > listitem > role=button, aria-current="step"."""
    from src.fb_ads_rpa import page_capture

    page.set_content(_ad_page() + _real_wizard())
    step = md._wizard_media_step(page)
    assert step["found"] is True and step["role"] == "button"
    assert step["current"] == "Media"
    assert step["steps"] == ["Creative setup", "Media", "Text", "Creative generation",
                             "Enhancements", "Translation"]
    assert step["disabled"] is False and step["covered"] is False
    assert md._media_step_behind(step) is False
    role = page.evaluate("(() => { const el = " + page_capture.CREATIVE_WIZARD_BLOCK_JS
                         + "; return el && el.getAttribute('role'); })()")
    assert role == "dialog"


def test_the_real_row_after_next_goes_back_to_media(page, wizard_opens):
    """Тот же ряд, но текущий шаг — Text (так мастер стоял на скриншоте отказа
    956d0d после собственного Next робота): вернуться кликом по Media."""
    wizard_opens["html"] = _real_wizard() + """
<div role="button" id="next">Next</div>
<script>
  window.nextClicks = 0; window.mediaClicks = 0;
  const btn = (name) => [...document.querySelectorAll('[role=listitem] [role=button]')]
    .find(b => b.textContent.trim() === name);
  const media = btn('Media'), text = btn('Text');
  const head = document.querySelector('[role=heading][aria-level="3"]');
  media.removeAttribute('aria-current'); media.setAttribute('tabindex', '-1');
  text.setAttribute('aria-current', 'step'); head.textContent = 'Text';
  document.getElementById('next').addEventListener('click', () => window.nextClicks++);
  media.addEventListener('click', () => {
    window.mediaClicks++;
    text.removeAttribute('aria-current'); media.setAttribute('aria-current', 'step');
    head.textContent = 'Media';
    if (!document.getElementById('upload')) {
      const u = document.createElement('div');
      u.id = 'upload'; u.setAttribute('role', 'button'); u.textContent = '+ Upload';
      head.parentElement.appendChild(u);
    }
  });
</script>"""
    assert md.upload_ad_creative(page, "Video", "C:/media/2012.mp4", reuse_only=True) is True
    assert wizard_opens["reuse"] == [1]
    assert page.evaluate("window.mediaClicks") == 1
    assert page.evaluate("window.nextClicks") == 0


# ── не дошли — назвать причину ───────────────────────────────────────────

def test_a_locked_media_step_behind_is_named_and_next_is_not_pressed(page, wizard_opens, tmp_path):
    """Media позади текущего шага и aria-disabled: Next туда не приведёт,
    а отказ должен сказать, что именно заперто, а не `silent`."""
    wizard_opens["html"] = _wizard(ORDER_956D, "Text", media_attrs='role="button" aria-disabled="true"',
                                   media_opens=False)
    assert md.upload_ad_creative(page, "Video", "C:/media/2012.mp4", reuse_only=True) is False
    assert page.evaluate("window.nextClicks") == 0
    row = _rows(tmp_path)[-1]
    assert row["ok"] is False and row["note"] == "media_step_locked", row
    step = row["diag"]["media_step"]
    assert step["current"] == "Text" and step["disabled"] is True
    assert step["steps"][:3] == ["Creative setup", "Media", "Text"]
    assert md.last_upload_why() == "media_step_locked"
    # Мастер снят отдельным блоком, пока он открыт: в странице целиком его
    # съедает обрезка на 4 МБ (все снимки 25.09).
    folder = Path(row["diag"]["page"])
    ctx = json.loads((folder / "context.json").read_text(encoding="utf-8"))
    assert ctx["step"] == "ad_creative_media_step"
    assert ctx["blocks"]["creative_wizard"]["found"] is True
    block = (folder / "block_creative_wizard.html").read_text(encoding="utf-8")
    assert 'id="step-media"' in block and "Creative setup" in block
    assert "Please specify the media" not in block, "в блок попала страница объявления"


def test_a_media_step_that_ignores_clicks_is_unreachable(page, wizard_opens, tmp_path):
    """08ca37, 25.09: «Media ✓ · ◐ Creative setup». Клик по Media ничего не
    меняет — два клика и честный отказ, без шести Next вперёд."""
    wizard_opens["html"] = _wizard(ORDER_08CA, "Creative setup", media_opens=False)
    assert md.upload_ad_creative(page, "Image", "C:/media/pic.jpg", reuse_only=True) is False
    assert page.evaluate("window.nextClicks") == 0
    assert page.evaluate("window.mediaClicks") == 2
    row = _rows(tmp_path)[-1]
    assert row["note"] == "media_step_unreachable", row
    assert row["diag"]["media_step"]["current"] == "Creative setup"
    assert md.last_upload_why() == "media_step_unreachable"


def test_a_covered_media_step_is_named(page, wizard_opens, tmp_path):
    """Слой поверх строки шагов: клик уйдёт в него — так и пишем."""
    wizard_opens["html"] = _wizard(ORDER_956D, "Text") + (
        '<div id="veil" style="position:fixed; top:0; left:0; width:1500px; height:50px; z-index:20"></div>'
        "<script>window.veilClicks = 0; document.getElementById('veil')"
        ".addEventListener('click', () => window.veilClicks++);</script>")
    assert md.upload_ad_creative(page, "Video", "C:/media/2012.mp4", reuse_only=True) is False
    assert page.evaluate("window.mediaClicks") == 0
    # Клик по перекрытому пункту ушёл бы в слой — мало ли что под ним живёт.
    assert page.evaluate("window.veilClicks") == 0, "кликали по перекрытому шагу"
    assert page.evaluate("window.nextClicks") == 0
    assert _rows(tmp_path)[-1]["note"] == "media_step_covered"


def test_the_wizard_is_captured_once_per_ad(page, wizard_opens, tmp_path, monkeypatch):
    """Снимков за прогон — MAX_PER_RUN; повторные попытки одного объявления не
    должны съедать место снимкам следующих отказов (ревью PR #352)."""
    from src.fb_ads_rpa import page_capture

    taken: list[str] = []
    monkeypatch.setattr(page_capture, "capture", lambda p, **kw: taken.append(kw["step"]) or "")
    wizard_opens["html"] = _wizard(ORDER_956D, "Text", media_attrs='role="button" aria-disabled="true"',
                                   media_opens=False)
    for _ in range(2):
        page.set_content(_ad_page())
        assert md.upload_ad_creative(page, "Video", "C:/media/2012.mp4", reuse_only=True) is False
    assert taken == ["ad_creative_media_step"]


# ── память прогона: навигация — не приговор файлу ─────────────────────────

FILE = r"C:\creatives\1_2333_eslat_post.mp4"  # linux-ok: a Windows path as opaque data, never touched on disk


@pytest.fixture
def memo(monkeypatch):
    md._failed_uploads.clear()
    md._uploaded_ok.clear()
    rt.set_current_run("run-memo", "CAMP")
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "_close_creative_setup_modal", lambda p: True)
    monkeypatch.setattr(md, "_verify_ad_media_present", lambda p: False)
    monkeypatch.setattr(md, "_upload_stalled", lambda p: False)
    monkeypatch.setattr(md, "_media_block_filled_now", lambda p: False)
    monkeypatch.setattr(md, "_wizard_past_media", lambda p: False)
    monkeypatch.setattr(md, "_creative_wizard_open", lambda p: False)
    said: list[dict] = []
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok, **kw: said.append({"step": step, "ok": ok, **kw}))
    yield said
    md._failed_uploads.clear()
    md._uploaded_ok.clear()


def test_a_navigation_failure_does_not_condemn_the_file(monkeypatch, memo):
    """DAN-LT25, 28.09: объявление 1 не дошло до Media, файл даже не
    предлагали Meta — а объявление 2 получило `same_file_already_failed_this_run`
    и не попробовало ничего. Файл тут ни при чём: следующему объявлению —
    полная попытка."""
    tries: list[dict] = []

    def upload(*a, **k):
        tries.append(k)
        md._set_upload_why("media_step_locked")
        return False

    monkeypatch.setattr(md, "upload_ad_creative", upload)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert md.upload_already_failed(FILE) is False
    assert md.last_upload_why() == "media_step_locked"

    tries.clear()
    md.upload_ad_creative_with_retry(object(), "video", FILE)
    assert tries and not any(t.get("reuse_only") for t in tries), (
        "объявление 2 обязано получить настоящую попытку"
    )


def test_a_file_that_was_offered_and_failed_is_still_remembered(monkeypatch, memo):
    """Прежнее правило для файла, который Meta действительно не взяла."""
    def upload(*a, **k):
        md._note_file_offered()
        md._set_upload_why("file_not_landed")
        return False

    monkeypatch.setattr(md, "upload_ad_creative", upload)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert md.upload_already_failed(FILE) is True
    assert md.last_upload_why() == "file_not_landed"


def test_an_earlier_ads_offered_file_does_not_condemn_the_next_ads_file(monkeypatch, memo):
    """Объявление 1 подало свой файл и не встало; объявление 2 (другой файл)
    упало на мастере. «Файл подавали» — про объявление 1, не про 2."""
    other = r"C:\creatives\2_other.mp4"  # linux-ok: a Windows path as opaque data, never touched on disk

    def upload(page, ctype, path, **k):
        if path == FILE:
            md._note_file_offered()
            md._set_upload_why("file_not_landed")
        else:
            md._set_upload_why("media_step_unreachable")
        return False

    monkeypatch.setattr(md, "upload_ad_creative", upload)
    md.upload_ad_creative_with_retry(object(), "video", FILE)
    assert md.upload_already_failed(FILE) is True
    md.upload_ad_creative_with_retry(object(), "video", other)
    assert md.upload_already_failed(other) is False


def test_the_reason_is_the_last_attempts(monkeypatch, memo):
    """Первая попытка упала на мастере, вторая — где-то без своей метки:
    причина первой не должна выдаваться за причину второй."""
    calls = {"n": 0}

    def upload(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            md._set_upload_why("media_step_unreachable")
        return False

    monkeypatch.setattr(md, "upload_ad_creative", upload)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert calls["n"] == 2
    assert md.last_upload_why() == "upload_failed_unnamed"


def test_the_known_file_refusal_says_what_blocked_the_library(monkeypatch, memo):
    """Файл уже поднят; заход за копией не дошёл до Media. В итоге шага —
    блокирующая причина, в строке телеметрии — прежняя метка и `blocked_by`."""
    md.remember_upload_succeeded(FILE)

    def upload(*a, **k):
        md._set_upload_why("media_step_unreachable")
        return False

    monkeypatch.setattr(md, "upload_ad_creative", upload)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    row = memo[-1]
    assert row["note"] == "same_file_already_uploaded_this_run"
    assert row["diag"]["blocked_by"] == "media_step_unreachable"
    assert md.last_upload_why() == "media_step_unreachable"


def test_an_exception_inside_the_upload_is_named(monkeypatch, memo):
    def upload(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(md, "upload_ad_creative", upload)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert md.last_upload_why() == "upload_exception"
    assert any(r["step"] == "ad_creative_upload" and r.get("note") == "upload_exception" for r in memo)


# ── причина доезжает до строки шага объявления (Sentry) ───────────────────

@pytest.fixture
def flow(monkeypatch, tmp_path):
    """Тот же подставной поток, что в test_additional_ad_without_creative_has_no_text.py."""
    from src.fb_ads_rpa import browser as br
    from src.fb_ads_rpa import page_capture, screenshots

    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    pg = MagicMock()
    loc = MagicMock()
    loc.first = loc
    loc.bounding_box.return_value = None
    loc.input_value.return_value = "Example"
    loc.evaluate.return_value = None
    for name in ("get_by_role", "get_by_text", "get_by_placeholder", "locator"):
        getattr(pg, name).return_value = loc
        getattr(loc, name).return_value = loc
    pg.evaluate.return_value = None
    pg.url = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit"

    monkeypatch.setattr(br, "ensure_editor_window", lambda p, section: True)
    monkeypatch.setattr(md, "_clear_blocking_overlays", MagicMock(return_value=[]))
    for name in ("_recover_from_browser_error", "_dismiss_floating_banners", "_sleep_h",
                 "wait_editor_saved", "_ensure_campaign_budget", "scroll_to_section",
                 "click_conversion_location_dropdown", "pick_website_conversion_option",
                 "is_website_conversion_selected", "select_conversion_event_lead",
                 "fill_audience_age", "fill_placements"):
        monkeypatch.setattr(md, name, MagicMock(return_value=True))
    monkeypatch.setattr(md, "_roots", lambda p: [p])
    monkeypatch.setattr(md, "_first_visible", lambda *a, **kw: None)
    monkeypatch.setattr(rt, "window_state", lambda p: {})
    monkeypatch.setattr(rt, "page_context", lambda p: {})
    monkeypatch.setattr(screenshots, "capture", lambda *a, **kw: None)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **kw: None)

    whys = {"a.mp4": "wizard_not_opened", "b.mp4": "media_step_locked"}

    def upload(p, ctype, path, **kw):
        md._set_upload_why(whys[path])
        return False

    monkeypatch.setattr(md, "upload_ad_creative_with_retry", upload)
    monkeypatch.setattr(md, "write_ad_copy", lambda *a, **k: True)
    monkeypatch.setattr(md, "create_additional_ad", lambda p, name: True)
    monkeypatch.setattr(md, "_open_tree_ad", lambda p, name: False)
    monkeypatch.setattr(md, "_tree_shell_ids", lambda p: frozenset())
    monkeypatch.setattr(md, "set_ad_destination_website", lambda p, url: True)
    monkeypatch.setattr(md, "verify_and_fix_ad_destinations", lambda p, names, url: [])
    monkeypatch.setattr(md, "recover_stuck_page", lambda *a, **k: False)
    monkeypatch.setattr(md, "_loading_visible", lambda p: False)

    soft: list[str] = []
    md.run_recorded_campaign_flow(
        pg, campaign_name="Example", budget="10", adset_name="Set",
        creative_path="a.mp4", ad_title="t1", ad_body="b1",
        destination_url="https://example.com",
        additional_ads=[{"ad_name": "Ad 2", "creative_type": "Video", "creative_path": "b.mp4",
                         "ad_title": "t2", "ad_body": "b2"}],
        soft_fails_out=soft,
    )
    return soft


def test_each_ads_upload_failure_carries_its_own_reason(flow, tmp_path):
    """PYTHON-14 `ad2_creative_upload: silent`: причина лежала в строке
    `ad_creative_upload`, которую Sentry для `ad2_…` не читает; а отказ
    объявления 1 получал причину объявления 2 (та же строка, записанная
    позже). Теперь у каждой строки шага объявления — своя причина."""
    # ACL-1637: `wizard_not_opened` теперь называется в самой строке объявления 1.
    assert any(r.startswith("ad_creative_upload") for r in flow) and "ad2_creative_upload" in flow
    ad1 = [r for r in _rows(tmp_path, "ad_creative_upload") if r["ok"] is False]
    ad2 = [r for r in _rows(tmp_path, "ad2_creative_upload") if r["ok"] is False]
    assert ad1 and ad1[-1]["note"] == "wizard_not_opened"
    assert ad2 and ad2[-1]["note"] == "media_step_locked"

    from src import acl_sentry
    assert acl_sentry.failure_reason("ad2_creative_upload", ad2[-1]["note"]) == "media-step-locked"


def test_a_step_row_without_a_reason_borrows_the_helpers(tmp_path, monkeypatch):
    """Запасной путь `_RELATED`: строка шага без причины (старые версии)
    берёт её у помощника; своя причина строки шага — важнее."""
    from src.fb_ads_rpa import sentry_signal

    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    rt.set_current_run("run-related", "CAMP")
    rt.record("ad_creative_upload", False, note="same_file_already_failed_this_run")
    rt.record("ad2_creative_upload", False)
    seen = sentry_signal._seen("ad2_creative_upload", "run-related")
    assert seen["why"] == "same_file_already_failed_this_run"
    assert seen["diag"]["reason_from_step"] == "ad_creative_upload"

    rt.record("ad3_creative_upload", False, note="media_step_locked")
    assert sentry_signal._seen("ad3_creative_upload", "run-related")["why"] == "media_step_locked"


def test_the_triage_map_knows_these_kinds():
    """Разбор Sentry относит эти виды к ACL-1348, а чужие не трогает."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import sentry_triage as st

    rows = st.load_map()
    for step, reason in (("ad_copy", "no-creative"), ("ad_creative_upload", "silent"),
                         ("ad_creative_upload", "media-step-locked"),
                         ("ad_creative_upload", "media-step-unreachable"),
                         ("ad_creative_upload", "same-file-already-failed-this-run"),
                         ("ad_creative_upload", "upload-button-missing")):
        assert (st.known_task(step, reason, rows) or {}).get("task") == "ACL-1348", (step, reason)
    assert st.known_task("ad_copy", "copy-fields-not-revealed", rows) is None
    # ACL-1559: выбор медиа шага Media так и не догрузился.
    got = st.known_task("ad_creative_upload", "media-picker-loading", rows)
    assert (got or {}).get("task") == "ACL-1559", got
    from src import acl_sentry
    assert acl_sentry.failure_reason("ad2_creative_upload", "media_picker_loading") == "media-picker-loading"


# ── ACL-1559: Media уже текущий шаг, выбор медиа ещё грузится ─────────────
#
# Живьём 04.10 (Facebook_1443, 2f140fa687fa): мастер открылся сразу на Media,
# а выбор медиа под ним показывал только «Loading...». Робот дважды кликнул
# по текущему шагу Media, четырежды пробовал запертый Next и через ~20 с
# сдался с общим `upload_button_missing`. Теперь: не кликать, не жать Next,
# читать выбор медиа (только чтение) до ~45 с, потом — прежний путь.
# Настоящая разметка — tests/test_bench_wizard_media_picker_1004.py; здесь —
# синтетическая строка шагов из начала файла.

_SKELETON = ("<div><span>Media</span></div><div id='picker'>"
             + "<div role='progressbar' aria-busy='true' aria-valuetext='Loading...'></div>" * 3
             + "</div>")


def _loading_media(order: list[str], loads_after_ms: int | None) -> str:
    """Мастер на Media с заглушками выбора медиа; через `loads_after_ms` —
    обычное тело Media с Upload (None — не догружается никогда)."""
    then = ("" if loads_after_ms is None
            else f"setTimeout(() => show('Media'), {loads_after_ms});")
    return _wizard(order, "Media") + (
        "<script>document.getElementById('body').innerHTML = " + json.dumps(_SKELETON) + ";"
        + then + "</script>")


@pytest.fixture
def quick_picker(monkeypatch):
    monkeypatch.setattr(md, "MEDIA_PICKER_LOAD_S", 1.5)
    monkeypatch.setattr(md, "_MEDIA_PICKER_POLL_S", 0.05)


def test_a_current_media_step_that_loads_is_waited_for_not_clicked(page, wizard_opens, tmp_path,
                                                                   quick_picker, monkeypatch):
    monkeypatch.setattr(md, "MEDIA_PICKER_LOAD_S", 10.0)
    wizard_opens["html"] = _loading_media(ORDER_08CA, 500)
    assert md.upload_ad_creative(page, "Video", "C:/media/2012.mp4", reuse_only=True) is True
    assert wizard_opens["reuse"] == [1], "до медиатеки не дошли"
    assert page.evaluate("window.mediaClicks") == 0, "кликнули по текущему шагу Media"
    assert page.evaluate("window.nextClicks") == 0, "жали Next на шаге Media"
    row = [r for r in _rows(tmp_path) if r.get("note") == "media_picker:ready"]
    assert row and row[-1]["diag"]["skeletons_at_start"] == 3, _rows(tmp_path)
    assert row[-1]["diag"]["waited_s"] >= 0.4


def test_a_current_media_step_that_never_loads_is_named(page, wizard_opens, tmp_path, quick_picker):
    wizard_opens["html"] = _loading_media(ORDER_08CA, None)
    assert md.upload_ad_creative(page, "Video", "C:/media/2012.mp4") is False
    assert wizard_opens["reuse"] == []
    assert page.evaluate("window.mediaClicks") == 0
    assert page.evaluate("window.nextClicks") == 0
    row = _rows(tmp_path)[-1]
    assert row["ok"] is False and row["note"] == "media_picker_loading", row
    assert row["diag"]["skeletons"] == 3 and row["diag"]["waited_s"] >= 1.4
    assert row["diag"]["picker"]["upload"] is False
    assert row["diag"]["media_step"]["current"] == "Media"
    assert md.last_upload_why() == "media_picker_loading"


@pytest.mark.parametrize("start", ["Creative setup", "Text"])
def test_media_is_still_clicked_when_another_step_is_current(page, wizard_opens, start):
    """Регрессия ACL-1348: текущий шаг — Creative setup или Text, Media кликабелен —
    робот по-прежнему кликает Media (и не ждёт выбор медиа впустую)."""
    wizard_opens["html"] = _wizard(ORDER_956D, start)
    assert md.upload_ad_creative(page, "Video", "C:/media/2012.mp4", reuse_only=True) is True
    assert wizard_opens["reuse"] == [1]
    assert page.evaluate("window.mediaClicks") == 1
    assert page.evaluate("window.nextClicks") == 0


def test_a_picker_that_never_loaded_does_not_condemn_the_file(monkeypatch, memo):
    """`media_picker_loading` — про мастер, не про файл: файл Meta не подавали,
    следующему объявлению — полная попытка; одна свежая переоткрывка остаётся."""
    tries: list[dict] = []

    def upload(*a, **k):
        tries.append(k)
        md._set_upload_why("media_picker_loading")
        return False

    monkeypatch.setattr(md, "upload_ad_creative", upload)
    assert md.upload_ad_creative_with_retry(object(), "video", FILE) is False
    assert len(tries) == 2, "одна свежая переоткрывка мастера"
    assert md.upload_already_failed(FILE) is False
    assert md.last_upload_why() == "media_picker_loading"
    assert "media_picker_loading" in md._NAVIGATION_WHYS


def test_media_picker_loading_reads_as_a_blocking_named_problem():
    from src.fb_ads_rpa import failure_policy as fp
    from src.fb_ads_rpa import simple_create as sc

    text = sc.creative_why_text("media_picker_loading")
    assert text and "loading" in text.lower()
    md._set_upload_why("media_picker_loading")
    try:
        fail = md.creative_soft_fail("ad2", "ad 2")
    finally:
        md._set_upload_why(None)
    assert fail == "ad2_creative_upload:media_picker_loading - ad 2: " + text
    assert fp.severity(fail) == fp.BLOCKING
    prob = fp.problem(fail)
    assert prob["ad_index"] == 2
    assert "picker" in prob["title"].lower() or "loading" in prob["title"].lower(), prob
    assert "loading" in fp.reason(fail).lower()
    json.dumps(fp.problems([fail]))
