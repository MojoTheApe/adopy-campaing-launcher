"""Личность медиа на объявлении: миниатюра выбранной плитки против миниатюры объявления.

Порт ACL-1284, вариант A (решение владельца 24.09: живой код — правда,
ролики с жёлтым треугольником берутся). Строгий вариант #238, под который
был написан этот файл, читал только строки размещения проверенного блока
Media и отказывал плитке с любым значком, скрытому/занятому/выключенному
блоку и «неизвестному предупреждению». Живой код так не делает:

* миниатюры объявления читаются по всей странице, кроме открытых диалогов;
* плитка с жёлтым треугольником берётся;
* имя файла, если показано, подтверждает файл; не показано — не отказ;
* нечитаемое состояние блока Media — не отказ (наличие медиа проверено
  отдельно), а «Video rejected» — отказ (решение владельца 25.09);
* отказ по самому медиа объявления (чужой файл, обработка) внешний повтор
  не отменяет (порт ACL-1284); прочие отказы медиатеки после выбора —
  отменяет, как в живом коде, — см. последние тесты.

Остаются инварианты владельца: чужой файл никогда не «наш креатив» (по
имени и по миниатюре, в том числе у плитки, выбранной до нас), обработка без
конца — отказ, файл, который Meta не обработала, — отказ.

Мутации ниже (чужая миниатюра, значок, ошибки) — синтетические поверх
записанной разметки, а не записи живых отказов.
"""
from pathlib import Path
import os
import sys

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import page_capture

FIX = Path(__file__).parent / "fixtures" / "meta"
IDENTITY = "813948786_38351125907864671_2841079256059848182_n"
OTHER = "999999999_88888888888888888_7777777777777777777_n"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield browser
        browser.close()


@pytest.fixture
def page(browser, monkeypatch):
    context = browser.new_context()
    page = context.new_page()
    page.route("**/*", lambda route: route.abort())
    monkeypatch.setattr(md, "_sleep_h", lambda *_: None)
    monkeypatch.setattr(md, "_MEDIA_PROCESSING_MAX_S", 0)
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: None)
    yield page
    context.close()


def attached(page, extra=""):
    page.set_content(extra + (FIX / "ad_media_attached.html").read_text())


def verify(page):
    return md._verify_ad_media_is_the_file(page, "1741_1.mp4")


def test_a_thumbnail_inside_an_open_dialog_is_not_the_ads_media(page):
    """Сетка медиатеки поверх объявления — не медиа объявления."""
    page.set_content(f'<div role="dialog"><img src="https://offline.invalid/{IDENTITY}.jpg"'
                     ' width="48" height="48"></div>')
    assert md._ad_media_thumb_ids(page) == []
    page.set_content(f'<nav><img src="https://offline.invalid/{IDENTITY}.jpg" width="48" height="48"></nav>')
    assert md._ad_media_thumb_ids(page) == [IDENTITY], "вариант A: читается вся страница вне диалогов"


def test_the_recorded_attachment_carries_the_picked_thumbnail(page):
    attached(page)
    assert md._ad_media_thumb_ids(page) == [IDENTITY]
    ok, detail = verify(page)
    assert ok is True and detail["why"] == "name_not_shown"


def test_a_wrong_filename_on_the_page_is_never_taken_for_ours(page):
    attached(page, '<nav>wrong.mp4</nav>')
    ok, detail = verify(page)
    assert ok is False and detail["why"] == "other_file_attached"


def test_our_filename_shown_confirms_the_file(page):
    """Вариант A: показанное имя — это личность файла (#238 требовал миниатюру)."""
    page.set_content('<div>Uploaded media</div><div>1741_1.mp4</div><div>Change selections</div>')
    ok, detail = verify(page)
    assert ok is True and detail["state"]["named"] == ["1741_1.mp4"]


@pytest.mark.parametrize("text, why", [("Processing your video", "still_processing"),
                                       ("This media couldn't be processed", "processing_failed"),
                                       ("Video rejected", "media_rejected")])
def test_media_errors_block_the_attachment(page, text, why):
    """Обработка без конца, необработанный и отклонённый файл — отказ.

    «Video rejected» — отказ и в варианте A (решение владельца 25.09): такое
    объявление не запустится.
    """
    attached(page)
    page.locator('[role=list]').evaluate(
        '(el, text) => { const x=document.createElement("div"); x.textContent=text; el.append(x); }', text)
    ok, detail = verify(page)
    assert ok is False and detail["why"] == why


def _reuse(page, monkeypatch, *, fresh=False, wrong=False, warning=False):
    page.unroute("**/*")
    page.route("**/*", lambda route: route.fulfill(body="<html></html>")
               if route.request.is_navigation_request() else route.abort())
    page.goto('https://offline.invalid/ads/edit/test')
    page.set_content('<div role="dialog">' + (FIX / 'library_one_picked.html').read_text() + '</div>')
    # The capture excludes Meta's stylesheets. Keep the footer clickable
    # without changing its attributes, selection or attachment evidence.
    page.add_style_tag(content='[data-surface$="lib:ads-omp-primary-button"] '
                               '{position:fixed;bottom:0;right:0;z-index:10;background:white}')
    tile = md._library_tiles(page, '1741_1.mp4').first
    # First tile is the recorded selection. Mutation models an initially
    # empty grid, and its counter changes in the same event as the mark.
    if fresh:
        page.locator('[data-test-decoration="selected"]').evaluate('(el) => el.removeAttribute("data-test-decoration")')
        page.get_by_text('1 selected', exact=True).evaluate('(el) => el.textContent="0 selected"')
        tile.evaluate('''el => el.addEventListener('click', () => {
          el.parentElement.setAttribute('data-test-decoration','selected');
          const counter = [...document.querySelectorAll('*')].find(n => !n.children.length && n.textContent==='0 selected');
          counter.textContent='1 selected';
        })''')
    if warning:
        tile.evaluate('el => el.insertAdjacentHTML("beforeend", \'<svg width="16" height="16"><path d="M0 0L16 16"/></svg>\')')
    monkeypatch.setattr(md, '_library_search', lambda *args: True)
    monkeypatch.setattr(md, '_verify_ad_media_present', lambda *_: True)
    monkeypatch.setattr(md, '_close_creative_setup_modal', lambda *_: page.set_content(
        (FIX / 'ad_media_attached.html').read_text().replace(IDENTITY, OTHER) if wrong
        else (FIX / 'ad_media_attached.html').read_text()))
    records = []
    monkeypatch.setattr(md._telemetry, 'record', lambda step, ok=None, **kw: records.append((step, ok, kw)))
    shots = []
    # Снимок удачного шага с 25.09 только по FB_RPA_SHOT_ALL=1 (ACL-1313); здесь
    # он — способ прочитать diag сверки миниатюр.
    monkeypatch.setenv('FB_RPA_SHOT_ALL', '1')
    monkeypatch.setattr(page_capture, 'capture', lambda pg, step, diag=None, **kw: shots.append((step, diag)))
    result = md._try_reuse_library_media(page, '1741_1.mp4')
    return result, records, shots


@pytest.mark.parametrize('fresh', [False, True])
def test_the_picked_tile_and_the_ad_show_the_same_thumbnail(page, monkeypatch, fresh):
    """И у щёлкнутой плитки, и у выбранной до нас (порт ACL-1284: у второй
    миниатюру раньше не брали, и сверка писала `unconfirmed`)."""
    result, records, shots = _reuse(page, monkeypatch, fresh=fresh)
    assert result == md.REUSE_ATTACHED
    ready = [diag for step, diag in shots if step == 'ad_media_ready']
    assert ready and ready[-1]['thumb'] == 'match'
    assert ready[-1]['thumbs']['picked'] == [IDENTITY]


@pytest.mark.parametrize('fresh', [False, True])
def test_a_wrong_attachment_remains_dirty(page, monkeypatch, fresh):
    result, records, shots = _reuse(page, monkeypatch, wrong=True, fresh=fresh)
    assert result == md.REUSE_DIRTY
    assert [kw.get('note') for step, ok, kw in records if step == 'ad_creative_reuse' and ok is False] \
        == ['other_file_attached']
    assert md.media_refused_on_ad() == 'other_file_attached'


def test_a_tile_with_the_yellow_triangle_is_attached(page, monkeypatch):
    """Вариант A (решение владельца 24.09): значок на плитке — не отказ.

    Строгий #238 отказывал такой плитке («предупреждение неизвестного
    смысла»), и объявления 2–3 оставались без видео (прогон 199fa985d531).
    """
    result, records, shots = _reuse(page, monkeypatch, warning=True)
    assert result == md.REUSE_ATTACHED


@pytest.mark.parametrize('known', [False, True])
def test_outer_retry_cannot_override_a_refusal_about_the_ads_media(monkeypatch, known):
    """Чужой или битый файл уже на объявлении: «медиа есть» не делает его нашим.

    Порт ACL-1284: живой внешний повтор после такого отказа видел медиа на
    объявлении и объявлял успех. Флаг строгого варианта `reuse_unverified`
    заменён `media_refused_on_ad()` — он ставится только за отказ по самому
    медиа объявления.
    """
    calls = []
    monkeypatch.setattr(md, 'upload_already_failed', lambda *_: False)
    monkeypatch.setattr(md, 'upload_already_succeeded', lambda *_: known)
    monkeypatch.setattr(md, '_media_block_filled_now', lambda *_: True)
    monkeypatch.setattr(md, '_wizard_past_media', lambda *_: True)
    monkeypatch.setattr(md, '_verify_ad_media_present', lambda *_: True)
    monkeypatch.setattr(md, '_close_creative_setup_modal', lambda *_: True)
    monkeypatch.setattr(md, '_remember_upload_failed', lambda *_: None)
    monkeypatch.setattr(md._telemetry, 'record', lambda *a, **kw: None)

    def upload(*a, **kw):
        calls.append(kw)
        md._note_media_refused('other_file_attached')
        return False

    monkeypatch.setattr(md, 'upload_ad_creative', upload)
    assert md.upload_ad_creative_with_retry(object(), 'video', '1741_1.mp4') is False
    assert len(calls) == 1


@pytest.mark.parametrize('known', [False, True])
def test_outer_retry_still_recovers_other_library_refusals(monkeypatch, known):
    """Выбор не снялся, счётчик не тот — внешний повтор по-прежнему достраивает.

    Живая телеметрия 19–25.09: после `library_pick_left_selection` 15
    объявлений получили медиа на повторе. Этот путь порт не закрывает.
    """
    monkeypatch.setattr(md, 'upload_already_failed', lambda *_: False)
    monkeypatch.setattr(md, 'upload_already_succeeded', lambda *_: known)
    monkeypatch.setattr(md, '_media_block_filled_now', lambda *_: True)
    monkeypatch.setattr(md, '_verify_ad_media_present', lambda *_: True)
    monkeypatch.setattr(md, '_close_creative_setup_modal', lambda *_: True)
    monkeypatch.setattr(md, 'remember_upload_succeeded', lambda *_: None)
    monkeypatch.setattr(md._telemetry, 'record', lambda *a, **kw: None)
    monkeypatch.setattr(md, 'upload_ad_creative', lambda *a, **kw: False)
    assert md.upload_ad_creative_with_retry(object(), 'video', '1741_1.mp4') is True
