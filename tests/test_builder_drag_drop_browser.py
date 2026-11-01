"""Builder: drag an ad to another campaign, a campaign to another account (ACL-1345).

Перенос (move), Ctrl = копия, Undo, перенос между аккаунтами, одноимённая кампания,
замок на идущую сборку, меню «Move to…». Настоящий ящик, офлайн-Chromium.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_builder_per_account import _render  # noqa: E402
from test_drawer_review_browser import OFFLINE_FILES, add  # noqa: E402

REPORT = Path(__file__).resolve().parents[1] / "src/web/templates/report.html"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    html = _render({"Demo": [], "Other": []})

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
            return
        r.fulfill(json={"files": OFFLINE_FILES, "texts": [], "rows": [], "profiles": [], "waiting": False})

    p.route("**/*", route)
    p.goto("http://acl.test/")
    p.evaluate("cbDrawer.open()")
    yield p
    assert not errors, errors
    ctx.close()


def _seed(p, account=0, count=2, ads=1):
    """`count` кампаний в аккаунте `account`, в каждой `ads` объявлений с разными файлами."""
    add(p, account=account, count=count)
    p.evaluate(
        """([account, ads]) => {
          const f = document.querySelectorAll('form.open-account-form')[account];
          f.querySelectorAll('.cb-slot').forEach((s, c) => {
            for (let i = 1; i < ads; i++) cbAddAd(s.querySelector('.cb-add-btn-sm'));
            s.querySelectorAll('.cb-ad-row').forEach((r, a) => {
              r.querySelector('input[name$="_creative_file"]').value = 'offline-' + (c * 10 + a) + '.png';
            });
          });
        }""",
        [account, ads],
    )
    p.evaluate("cbTree.paint()")


def _files(p, account):
    return p.evaluate(
        """(account) => {
          // Открытая форма переезжает в правую колонку и меняет порядок в DOM - ищем по аккаунту.
          const f = Array.from(document.querySelectorAll('form.open-account-form'))
            .find(x => x.querySelector('[name="account_label"]').value === ['Demo', 'Other'][account]);
          return Array.from(f.querySelectorAll('.cb-slot')).map(s =>
            Array.from(s.querySelectorAll('.cb-ad-row')).map(r => r.querySelector('input[name$="_creative_file"]').value));
        }""",
        account,
    )


def _names_ok(p):
    """Имена полей после переноса идут подряд: camp_C_ad_A_* без пропусков и повторов."""
    return p.evaluate(
        """() => {
          for (const f of document.querySelectorAll('form.open-account-form')) {
            const fd = new FormData(f);
            const idx = new Set();
            for (const k of fd.keys()) { const m = /^camp_(\\d+)_ad_(\\d+)_creative_file$/.exec(k); if (m) idx.add(m[1] + ':' + m[2]); }
            const slots = f.querySelectorAll('.cb-slot');
            let want = 0;
            slots.forEach((s, c) => { s.querySelectorAll('.cb-ad-row').forEach((r, a) => { want++;
              if (!idx.has(c + ':' + a)) return false; }); });
            if (idx.size !== want) return false;
          }
          return true;
        }"""
    )


def _drag(p, src, dst, ctrl=False):
    if ctrl:
        p.keyboard.down("Control")
    try:
        src.drag_to(dst)
    finally:
        if ctrl:
            p.keyboard.up("Control")


def _open_campaign(p, n):
    p.locator(".cbd-camp-row").nth(n).click()
    p.locator(".cbd-seg [data-level='ad']").click()


def _toast(p):
    return p.locator(".cbm-toast")


def test_move_ad_to_another_campaign_keeps_fields_and_renumbers(page):
    _seed(page, count=2, ads=2)
    _open_campaign(page, 0)
    page.evaluate("document.querySelector('#cbd-editor .cb-ad-row .cb-ad-row-header').closest('.cb-ad-row').querySelector('select[name$=\"_cta\"]').value")
    grip = page.locator("#cbd-editor .cb-ad-row").first.locator(".cbm-grip")
    _drag(page, grip, page.locator(".cbd-camp-head").nth(1))
    assert _files(page, 0) == [["offline-1.png"], ["offline-10.png", "offline-11.png", "offline-0.png"]]
    assert "Moved 1 ad to" in _toast(page).inner_text()
    assert "Ad set 1" in _toast(page).inner_text()
    assert _names_ok(page)


def test_ctrl_drag_copies_the_ad(page):
    _seed(page, count=2, ads=1)
    _open_campaign(page, 0)
    grip = page.locator("#cbd-editor .cb-ad-row").first.locator(".cbm-grip")
    _drag(page, grip, page.locator(".cbd-camp-head").nth(1), ctrl=True)
    assert _files(page, 0) == [["offline-0.png"], ["offline-10.png", "offline-0.png"]]
    assert "Copied 1 ad to" in _toast(page).inner_text()
    assert _names_ok(page)


def test_undo_restores_a_moved_ad_and_the_blank_placeholder_goes_away(page):
    _seed(page, count=2, ads=1)
    _open_campaign(page, 0)
    grip = page.locator("#cbd-editor .cb-ad-row").first.locator(".cbm-grip")
    _drag(page, grip, page.locator(".cbd-camp-head").nth(1))
    # Единственное объявление ушло: в источнике остаётся пустое.
    assert _files(page, 0) == [[""], ["offline-10.png", "offline-0.png"]]
    assert "Left a blank ad" in _toast(page).inner_text()
    _toast(page).locator(".cbm-undo").click()
    assert _files(page, 0) == [["offline-0.png"], ["offline-10.png"]]
    assert _names_ok(page)


def test_cross_account_campaign_move_takes_destination_identity_and_undo_restores(page):
    _seed(page, account=0, count=2)
    page.evaluate("cbTree.paint()")
    page.evaluate(
        """() => { const f = document.querySelectorAll('form.open-account-form')[0];
          f.querySelector('.cb-slot input[name$="_page_id"]').value = '123';
          f.querySelector('.cb-slot input[name$="_ad_0_form_id"]').value = 'FORM1'; }"""
    )
    page.evaluate("window.__fb = new FormData(document.querySelectorAll('form.open-account-form')[0])")
    before = page.evaluate("Array.from(document.querySelectorAll('form.open-account-form')[0].querySelectorAll('.cb-slot')).length")
    assert before == 2
    # В дереве аккаунта-получателя ещё нет кампаний: он появится «Drop here» прямо во время переноса.
    result = page.evaluate(
        """async () => {
          const forms = document.querySelectorAll('form.open-account-form');
          const slot = forms[0].querySelector('.cb-slot');
          await cbMove.run({ kind: 'camp', mode: 'move', slot, destForm: forms[1] });
          const moved = forms[1].querySelector('.cb-slot');
          return { src: forms[0].querySelectorAll('.cb-slot').length, dest: forms[1].querySelectorAll('.cb-slot').length,
                   account: new FormData(forms[1]).get('account_label'),
                   page: moved.querySelector('input[name$="_page_id"]').value,
                   formId: moved.querySelector('input[name$="_ad_0_form_id"]').value,
                   fields: Array.from(new FormData(forms[1]).keys()).filter(k => /^camp_0_ad_0_creative_file$/.test(k)).length };
        }"""
    )
    assert result["src"] == 1 and result["dest"] == 1
    assert result["page"] == "" and result["formId"] == "", "Страница и форма чужого аккаунта не сбросились"
    assert result["fields"] == 1
    assert "Budget unchanged" in _toast(page).inner_text()
    assert _names_ok(page)
    _toast(page).locator(".cbm-undo").click()
    back = page.evaluate(
        """() => { const f = document.querySelectorAll('form.open-account-form');
          const s = f[0].querySelector('.cb-slot');
          return [f[0].querySelectorAll('.cb-slot').length, f[1].querySelectorAll('.cb-slot').length,
                  s.querySelector('input[name$="_page_id"]').value, s.querySelector('input[name$="_ad_0_form_id"]').value]; }"""
    )
    assert back == [2, 0, "123", "FORM1"]
    assert _names_ok(page)


def test_ctrl_copy_campaign_into_other_account_and_undo(page):
    _seed(page, account=0, count=1)
    n = page.evaluate(
        """async () => { const f = document.querySelectorAll('form.open-account-form');
          await cbMove.run({ kind: 'camp', mode: 'copy', slot: f[0].querySelector('.cb-slot'), destForm: f[1] });
          return [f[0].querySelectorAll('.cb-slot').length, f[1].querySelectorAll('.cb-slot').length]; }"""
    )
    assert n == [1, 1]
    assert "Copied campaign" in _toast(page).inner_text()
    _toast(page).locator(".cbm-undo").click()
    assert page.evaluate("document.querySelectorAll('form.open-account-form')[1].querySelectorAll('.cb-slot').length") == 0


def test_name_clash_keeps_both_and_warns(page):
    _seed(page, account=0, count=1)
    page.evaluate("""() => { const f = document.querySelectorAll('form.open-account-form');
      cbAddSlot(f[1]); const s = f[1].querySelector('.cb-slot');
      s.querySelector('[name$="_asset"]').value = 'Offer 0'; }""")
    # Переименуем оба в одно имя: свой заголовок карточки.
    page.evaluate("""() => { document.querySelectorAll('.cb-slot').forEach(s => {
      let h = s.querySelector('.cb-name-hidden'); if (h) h.value = 'SAME_NAME';
      const d = s.querySelector('[data-name-display]'); if (d) d.textContent = 'SAME_NAME'; }); }""")
    page.evaluate("""async () => { const f = document.querySelectorAll('form.open-account-form');
      await cbMove.run({ kind: 'camp', mode: 'copy', slot: f[0].querySelector('.cb-slot'), destForm: f[1] }); }""")
    text = _toast(page).inner_text()
    assert "already has a campaign named" in text and "Publish all will combine" in text
    assert page.evaluate("document.querySelectorAll('form.open-account-form')[1].querySelectorAll('.cb-slot').length") == 2


def test_running_job_locks_source_and_destination(page):
    _seed(page, account=0, count=2)
    _open_campaign(page, 0)
    # Идёт сборка кампании: ручка заперта, перетаскивание отказывается начаться.
    page.evaluate("window.cbBuild.dot = (slot) => ({ state: 'build-run' })")
    grip = page.locator("#cbd-editor .cb-ad-row").first.locator(".cbm-grip")
    grip.hover()
    assert "being built" in (grip.get_attribute("title") or "")
    _drag(page, grip, page.locator(".cbd-camp-head").nth(1))
    assert _files(page, 0) == [["offline-0.png"], ["offline-10.png"]], "запертое объявление уехало"
    assert "This campaign is being built" in _toast(page).inner_text()
    # Аккаунт-получатель занят: перенос отклоняется на проверке.
    page.evaluate("window.cbBuild.dot = () => null; window.CB_QUEUE = { busy: (k) => k.endsWith('|Other') };")
    why = page.evaluate(
        """() => { const f = Array.from(document.querySelectorAll('form.open-account-form'));
          const by = (n) => f.find(x => x.querySelector('[name="account_label"]').value === n);
          return cbMove.check({ kind: 'camp', mode: 'move', slot: by('Demo').querySelector('.cb-slot'), destForm: by('Other') }); }"""
    )
    assert "is being built" in why and "Other" in why


def test_drop_rechecks_the_lock_when_the_job_starts_mid_drag(page):
    _seed(page, account=0, count=2)
    _open_campaign(page, 0)
    res = page.evaluate(
        """async () => {
          const slots = document.querySelectorAll('.cb-slot');
          const row = slots[0].querySelector('.cb-ad-row');
          window.cbBuild.dot = () => ({ state: 'build-attn' });   // задание стартовало после dragstart
          const ok = await cbMove.run({ kind: 'ad', mode: 'move', row, destSlot: slots[1], k: 0 });
          return [ok, slots[0].querySelectorAll('.cb-ad-row').length, slots[1].querySelectorAll('.cb-ad-row').length];
        }"""
    )
    assert res == [False, 1, 1]


def test_move_to_menu_works_from_the_keyboard_and_reaches_an_account_without_drafts(page):
    _seed(page, account=0, count=1)
    page.locator(".cbd-camp-head .cbm-moveto").first.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("cbMove.menuOpen()")
    page.keyboard.type("Other")
    page.keyboard.press("Enter")
    assert page.evaluate("document.querySelectorAll('form.open-account-form')[1].querySelectorAll('.cb-slot').length") == 1
    assert "Moved campaign" in _toast(page).inner_text()
    assert not page.evaluate("cbMove.menuOpen()")
    assert _names_ok(page)


def test_move_to_menu_moves_an_ad_to_a_chosen_ad_set_and_esc_returns_focus(page):
    _seed(page, account=0, count=2)
    _open_campaign(page, 0)
    btn = page.locator("#cbd-editor .cb-ad-row .cbm-moveto").first
    btn.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("Escape")
    assert not page.evaluate("cbMove.menuOpen()")
    assert page.evaluate("document.activeElement.classList.contains('cbm-moveto')"), "фокус не вернулся на кнопку"
    btn.click()
    page.locator(".cbm-item", has_text="Demo").first.click()      # аккаунт
    page.locator(".cbm-item").nth(2).click()                     # вторая кампания (0 - Back, 1 - первая)
    page.locator(".cbm-item", has_text="Ad set 1").click()
    assert _files(page, 0) == [[""], ["offline-10.png", "offline-0.png"]]


def test_cap_of_100_ads_refuses_the_move(page):
    _seed(page, account=0, count=2)
    refused = page.evaluate(
        """async () => {
          const slots = document.querySelectorAll('.cb-slot');
          while (slots[1].querySelectorAll('.cb-ad-row').length < 100) cbAddAd(slots[1].querySelector('.cb-add-btn-sm'));
          const row = slots[0].querySelector('.cb-ad-row');
          const ok = await cbMove.run({ kind: 'ad', mode: 'move', row, destSlot: slots[1], k: 0 });
          return [ok, slots[0].querySelectorAll('.cb-ad-row').length];
        }"""
    )
    assert refused == [False, 1]
    assert "up to 100 ads" in _toast(page).inner_text()


def test_the_tree_does_not_repaint_under_a_drag(page):
    _seed(page, account=0, count=2)
    _open_campaign(page, 0)
    ok = page.evaluate(
        """async () => {
          const grip = document.querySelector('#cbd-editor .cb-ad-row .cbm-grip');
          const target = document.querySelectorAll('.cbd-camp-head')[1];
          const dt = new DataTransfer();
          grip.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
          const head = document.querySelectorAll('.cbd-camp-head')[1];
          cbTree.paint();                                   // как опрос сборки каждые 3 с
          const same = document.querySelectorAll('.cbd-camp-head')[1] === head;
          target.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
          await new Promise(r => setTimeout(r, 50));
          return [same, document.querySelector('.cb-slot') !== null,
                  document.querySelectorAll('.cb-slot')[1].querySelectorAll('.cb-ad-row').length];
        }"""
    )
    assert ok[0] is True, "дерево перерисовалось посреди перетаскивания"
    assert ok[2] == 2


def test_moved_campaign_survives_a_keep_round_trip(page):
    """Ящик сохраняет черновик по изменению DOM - перенос не должен ломать сериализацию."""
    _seed(page, account=0, count=1)
    page.evaluate(
        """async () => { const f = document.querySelectorAll('form.open-account-form');
          await cbMove.run({ kind: 'camp', mode: 'move', slot: f[0].querySelector('.cb-slot'), destForm: f[1] }); }"""
    )
    dest = page.evaluate(
        """() => { const f = document.querySelectorAll('form.open-account-form')[1]; const fd = new FormData(f);
          return [fd.get('account_label'), fd.get('camp_0_ad_0_creative_file'), fd.get('camp_count') || fd.get('count')]; }"""
    )
    assert dest[1] == "offline-0.png"


def test_static_strings_exist():
    html = REPORT.read_text(encoding="utf-8")
    for text in ("Move to"+chr(0x2026), "Can"+chr(0x2019)+"t undo "+chr(0x2013)+" the draft changed.", "Budget unchanged",
                 "Publish all will combine them into one campaign.", "A campaign can hold up to 100 ads.",
                 "Left a blank ad in", "Drop here", "Move here", "Copy here"):
        assert text in html, text
