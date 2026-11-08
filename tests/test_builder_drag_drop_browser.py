"""Builder: a drag always copies - an ad to another campaign, a campaign to another account (ACL-2397).

Копия без удаления оригинала (с Ctrl/Alt и без), Undo убирает ровно копию, дроп на своё
место - пусто, замок только на получателе, меню «Copy to…», ручка 32x32. Настоящий ящик,
офлайн-Chromium.
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


def _drag(p, src, dst, ctrl=False, alt=False):
    keys = (["Control"] if ctrl else []) + (["Alt"] if alt else [])
    for k in keys:
        p.keyboard.down(k)
    try:
        src.drag_to(dst)
    finally:
        for k in keys:
            p.keyboard.up(k)


def _open_campaign(p, n):
    p.locator(".cbd-camp-row").nth(n).click()
    p.locator(".cbd-seg [data-level='ad']").click()


def _toast(p):
    return p.locator(".cbm-toast")


def _snapshot(p):
    """Состояние доски: FormData каждой формы (отсортированно), число карточек и строк дерева."""
    return p.evaluate(
        """() => ({
          forms: Array.from(document.querySelectorAll('form.open-account-form')).map(f =>
            Array.from(new FormData(f).entries()).map(([k, v]) => k + '=' + String(v && v.name !== undefined ? v.name : v)).sort()),
          slots: document.querySelectorAll('.cb-slot').length,
          rows: document.querySelectorAll('.cb-ad-row').length,
          tree: document.querySelectorAll('.cbd-camp').length })"""
    )


def _first_grip(p):
    return p.locator("#cbd-editor .cb-ad-row").first.locator(".cbm-grip")


def test_drag_ad_to_another_campaign_leaves_the_original_and_adds_one_copy(page):
    _seed(page, count=2, ads=2)
    _open_campaign(page, 0)
    _drag(page, _first_grip(page), page.locator(".cbd-camp-head").nth(1))
    assert _files(page, 0) == [["offline-0.png", "offline-1.png"], ["offline-10.png", "offline-11.png", "offline-0.png"]]
    assert "Copied 1 ad to" in _toast(page).inner_text()
    assert "Ad set 1" in _toast(page).inner_text()
    assert _names_ok(page)


@pytest.mark.parametrize("mods", [{}, {"ctrl": True}, {"alt": True}])
def test_a_drag_copies_with_or_without_modifiers(page, mods):
    _seed(page, count=2, ads=1)
    _open_campaign(page, 0)
    _drag(page, _first_grip(page), page.locator(".cbd-camp-head").nth(1), **mods)
    assert _files(page, 0) == [["offline-0.png"], ["offline-10.png", "offline-0.png"]]
    assert "Copied 1 ad to" in _toast(page).inner_text()
    assert "Moved" not in _toast(page).inner_text()


def test_dragover_always_shows_copy_here(page):
    _seed(page, count=2, ads=1)
    _open_campaign(page, 0)
    chip = page.evaluate(
        """() => {
          const grip = document.querySelector('#cbd-editor .cb-ad-row .cbm-grip');
          const dt = new DataTransfer();
          grip.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
          const head = document.querySelectorAll('.cbd-camp-head')[1];
          head.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, ctrlKey: true, dataTransfer: dt }));
          const out = [head.getAttribute('data-cbm-chip'), dt.effectAllowed, dt.dropEffect];
          grip.dispatchEvent(new DragEvent('dragend', { bubbles: true, dataTransfer: dt }));
          return out;
        }"""
    )
    assert chip[0] == "Copy here"


def test_copy_equals_original_field_by_field(page):
    _seed(page, count=2, ads=1)
    res = page.evaluate(
        """async () => {
          const slots = document.querySelectorAll('.cb-slot');
          const row = slots[0].querySelector('.cb-ad-row');
          const norm = (r) => Array.from(r.querySelectorAll('input,select,textarea'))
            .filter(i => i.name && !/_page_id$|_form_id$/.test(i.name))
            .map(i => i.name.replace(/^camp_\\d+_ad_\\d+_/, '') + '=' + ((i.type === 'checkbox' || i.type === 'radio') ? (i.checked ? i.value : '') : i.value)).sort();
          const before = norm(row);
          await cbMove.run({ kind: 'ad', row, destSlot: slots[1], k: 0 });
          const copy = slots[1].querySelectorAll('.cb-ad-row')[1];
          return [JSON.stringify(before) === JSON.stringify(norm(copy)), JSON.stringify(before) === JSON.stringify(norm(row))];
        }"""
    )
    assert res == [True, True]


def test_undo_removes_only_the_copy_and_the_board_is_identical(page):
    _seed(page, count=2, ads=1)
    _open_campaign(page, 0)
    before = _snapshot(page)
    _drag(page, _first_grip(page), page.locator(".cbd-camp-head").nth(1))
    assert _snapshot(page) != before
    _toast(page).locator(".cbm-undo").click()
    assert _snapshot(page) == before
    assert _names_ok(page)


def test_undo_of_a_campaign_copy_repaints_the_tree(page):
    _seed(page, account=0, count=1)
    before = _snapshot(page)
    page.evaluate(
        """async () => { const f = document.querySelectorAll('form.open-account-form');
          await cbMove.run({ kind: 'camp', slot: f[0].querySelector('.cb-slot'), destForm: f[1] }); }"""
    )
    assert _snapshot(page)["slots"] == 2
    _toast(page).locator(".cbm-undo").click()
    assert _snapshot(page) == before


def test_stacked_drags_toast_undo_removes_only_its_own_copy(page):
    _seed(page, count=2, ads=1)
    _open_campaign(page, 0)
    _drag(page, _first_grip(page), page.locator(".cbd-camp-head").nth(1))
    page.evaluate("window.__undo1 = document.querySelector('.cbm-undo')")
    _drag(page, _first_grip(page), page.locator(".cbd-camp-head").nth(1))
    assert _files(page, 0)[1] == ["offline-10.png", "offline-0.png", "offline-0.png"]
    page.evaluate("window.__undo1.click()")           # кнопка первого тоста
    assert _files(page, 0)[1] == ["offline-10.png", "offline-0.png"]
    assert page.evaluate("cbMove.undoDepth()") == 1


def test_cross_account_campaign_copy_takes_destination_identity_and_leaves_the_original(page):
    _seed(page, account=0, count=2)
    page.evaluate(
        """() => { const f = document.querySelectorAll('form.open-account-form')[0];
          f.querySelector('.cb-slot input[name$="_page_id"]').value = '123';
          f.querySelector('.cb-slot input[name$="_ad_0_form_id"]').value = 'FORM1'; }"""
    )
    result = page.evaluate(
        """async () => {
          const forms = document.querySelectorAll('form.open-account-form');
          const slot = forms[0].querySelector('.cb-slot');
          await cbMove.run({ kind: 'camp', slot, destForm: forms[1] });
          const copy = forms[1].querySelector('.cb-slot');
          return { src: forms[0].querySelectorAll('.cb-slot').length, dest: forms[1].querySelectorAll('.cb-slot').length,
                   page: copy.querySelector('input[name$="_page_id"]').value,
                   formId: copy.querySelector('input[name$="_ad_0_form_id"]').value,
                   origPage: slot.querySelector('input[name$="_page_id"]').value,
                   origFormId: slot.querySelector('input[name$="_ad_0_form_id"]').value };
        }"""
    )
    assert result["src"] == 2 and result["dest"] == 1
    assert result["page"] == "" and result["formId"] == "", "Страница и форма чужого аккаунта не сбросились"
    assert result["origPage"] == "123" and result["origFormId"] == "FORM1", "оригинал изменился"
    assert "Budget unchanged" in _toast(page).inner_text()
    assert _names_ok(page)


def test_copy_campaign_into_other_account_and_undo(page):
    _seed(page, account=0, count=1)
    n = page.evaluate(
        """async () => { const f = document.querySelectorAll('form.open-account-form');
          await cbMove.run({ kind: 'camp', slot: f[0].querySelector('.cb-slot'), destForm: f[1] });
          return [f[0].querySelectorAll('.cb-slot').length, f[1].querySelectorAll('.cb-slot').length]; }"""
    )
    assert n == [1, 1]
    assert "Copied campaign" in _toast(page).inner_text()
    _toast(page).locator(".cbm-undo").click()
    assert page.evaluate("document.querySelectorAll('form.open-account-form')[1].querySelectorAll('.cb-slot').length") == 0


def test_name_clash_keeps_both_warns_and_writes_no_copy_suffix(page):
    _seed(page, account=0, count=1)
    page.evaluate("""() => { const f = document.querySelectorAll('form.open-account-form');
      cbAddSlot(f[1]); const s = f[1].querySelector('.cb-slot');
      s.querySelector('[name$="_asset"]').value = 'Offer 0'; }""")
    page.evaluate("""() => { document.querySelectorAll('.cb-slot').forEach(s => {
      let h = s.querySelector('.cb-name-hidden'); if (h) h.value = 'SAME_NAME';
      const d = s.querySelector('[data-name-display]'); if (d) d.textContent = 'SAME_NAME'; }); }""")
    page.evaluate("""async () => { const f = document.querySelectorAll('form.open-account-form');
      await cbMove.run({ kind: 'camp', slot: f[0].querySelector('.cb-slot'), destForm: f[1] }); }""")
    text = _toast(page).inner_text()
    assert "already has a campaign named" in text and "Publish all will combine" in text
    assert page.evaluate("document.querySelectorAll('form.open-account-form')[1].querySelectorAll('.cb-slot').length") == 2
    names = page.evaluate("""() => Array.from(document.querySelectorAll('.cb-slot')).map(s =>
      (s.querySelector('.cb-name-hidden') || {}).value + '|' + Array.from(s.querySelectorAll('[name$="name_override"]')).map(i => i.value).join(','))""")
    assert not any(" copy" in n.lower() for n in names), names


def test_source_of_a_building_campaign_can_be_dragged_but_destination_lock_holds(page):
    _seed(page, account=0, count=2)
    _open_campaign(page, 0)
    # Источник собирается: копия ничего не вынимает, ручка не заперта.
    page.evaluate("window.cbBuild.dot = (slot) => (slot === document.querySelectorAll('.cb-slot')[0] ? ({ state: 'build-run' }) : null)")
    _drag(page, _first_grip(page), page.locator(".cbd-camp-head").nth(1))
    assert _files(page, 0) == [["offline-0.png"], ["offline-10.png", "offline-0.png"]], "источник под сборкой не копируется"
    # Получатель собирается: отказ.
    page.evaluate("window.cbBuild.dot = (slot) => ({ state: 'build-run' })")
    _drag(page, _first_grip(page), page.locator(".cbd-camp-head").nth(1))
    assert _files(page, 0) == [["offline-0.png"], ["offline-10.png", "offline-0.png"]]
    # Отказ виден уже на dragover (drop не наступает), поэтому тоста нет.
    # Аккаунт-получатель занят: копия отклоняется на проверке.
    page.evaluate("window.cbBuild.dot = () => null; window.CB_QUEUE = { busy: (k) => k.endsWith('|Other') };")
    why = page.evaluate(
        """() => { const f = Array.from(document.querySelectorAll('form.open-account-form'));
          const by = (n) => f.find(x => x.querySelector('[name="account_label"]').value === n);
          return cbMove.check({ kind: 'camp', slot: by('Demo').querySelector('.cb-slot'), destForm: by('Other') }); }"""
    )
    assert "is being built" in why and "Other" in why


def test_drop_rechecks_the_destination_lock_when_the_job_starts_mid_drag(page):
    _seed(page, account=0, count=2)
    _open_campaign(page, 0)
    res = page.evaluate(
        """async () => {
          const slots = document.querySelectorAll('.cb-slot');
          const row = slots[0].querySelector('.cb-ad-row');
          window.cbBuild.dot = () => ({ state: 'build-attn' });   // задание стартовало после dragstart
          const ok = await cbMove.run({ kind: 'ad', row, destSlot: slots[1], k: 0 });
          return [ok, slots[0].querySelectorAll('.cb-ad-row').length, slots[1].querySelectorAll('.cb-ad-row').length];
        }"""
    )
    assert res == [False, 1, 1]


def test_drop_on_the_own_place_is_a_no_op_but_the_menu_may_copy_in_place(page):
    _seed(page, account=0, count=1, ads=1)
    _open_campaign(page, 0)
    snap = _snapshot(page)
    res = page.evaluate(
        """async () => {
          const slot = document.querySelector('.cb-slot'), row = slot.querySelector('.cb-ad-row');
          const form = slot.closest('form');
          const a = await cbMove.run({ kind: 'ad', row, destSlot: slot, k: 0 });
          const c = await cbMove.run({ kind: 'camp', slot, destForm: form });
          return [a, c, cbMove.check({ kind: 'ad', row, destSlot: slot, k: 0 }), cbMove.check({ kind: 'camp', slot, destForm: form })];
        }"""
    )
    assert res == [False, False, "noop", "noop"]
    assert _snapshot(page) == snap
    inplace = page.evaluate(
        """async () => {
          const slot = document.querySelector('.cb-slot'), row = slot.querySelector('.cb-ad-row');
          const ok = await cbMove.run({ kind: 'ad', menu: true, row, destSlot: slot, k: 0 });
          return [ok, slot.querySelectorAll('.cb-ad-row').length];
        }"""
    )
    assert inplace == [True, 2]


def test_copy_to_menu_works_from_the_keyboard_and_reaches_an_account_without_drafts(page):
    _seed(page, account=0, count=1)
    btn = page.locator(".cbd-camp-head .cbm-moveto").first
    assert btn.inner_text() == "Copy to…"
    btn.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("cbMove.menuOpen()")
    assert page.locator(".cbm-seg").count() == 0, "переключатель Move|Copy должен исчезнуть"
    page.keyboard.type("Other")
    page.keyboard.press("Enter")
    assert page.evaluate("document.querySelectorAll('form.open-account-form')[1].querySelectorAll('.cb-slot').length") == 1
    assert page.evaluate("document.querySelectorAll('form.open-account-form')[0].querySelectorAll('.cb-slot').length") == 1
    assert "Copied campaign" in _toast(page).inner_text()
    assert not page.evaluate("cbMove.menuOpen()")
    assert _names_ok(page)


def test_copy_to_menu_copies_an_ad_to_a_chosen_ad_set_and_esc_returns_focus(page):
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
    assert _files(page, 0) == [["offline-0.png"], ["offline-10.png", "offline-0.png"]]


def test_cap_of_100_ads_refuses_the_copy(page):
    _seed(page, account=0, count=2)
    refused = page.evaluate(
        """async () => {
          const slots = document.querySelectorAll('.cb-slot');
          while (slots[1].querySelectorAll('.cb-ad-row').length < 100) cbAddAd(slots[1].querySelector('.cb-add-btn-sm'));
          const row = slots[0].querySelector('.cb-ad-row');
          const ok = await cbMove.run({ kind: 'ad', row, destSlot: slots[1], k: 0 });
          return [ok, slots[0].querySelectorAll('.cb-ad-row').length, slots[1].querySelectorAll('.cb-ad-row').length];
        }"""
    )
    assert refused == [False, 1, 100]
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


def test_copied_campaign_survives_a_keep_round_trip(page):
    """Ящик сохраняет черновик по изменению DOM: копия доходит до localStorage."""
    _seed(page, account=0, count=1)
    page.evaluate(
        """async () => { const f = document.querySelectorAll('form.open-account-form');
          await cbMove.run({ kind: 'camp', slot: f[0].querySelector('.cb-slot'), destForm: f[1] }); }"""
    )
    dest = page.evaluate(
        """() => { const f = Array.from(document.querySelectorAll('form.open-account-form'))
            .find(x => x.querySelector('[name="account_label"]').value === 'Other'); const fd = new FormData(f);
          return [fd.get('account_label'), fd.get('camp_0_ad_0_creative_file')]; }"""
    )
    assert dest == ["Other", "offline-0.png"]
    page.wait_for_timeout(3500)
    stored = page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('acl.builder.v2')).map(k => localStorage.getItem(k)).join('|')")
    assert "offline-0.png" in stored, "скопированная кампания не попала в черновик"


def _two_accounts_with_form(page):
    _seed(page, account=0, count=1, ads=1)
    page.evaluate(
        """() => { const f = document.querySelectorAll('form.open-account-form')[0];
          f.querySelector('.cb-slot input[name$="_page_id"]').value = '123';
          f.querySelector('.cb-slot input[name$="_ad_0_form_id"]').value = 'FID'; }"""
    )
    _seed(page, account=1, count=1, ads=1)


def _form_ids(page):
    return page.evaluate(
        """() => Object.fromEntries(Array.from(document.querySelectorAll('form.open-account-form')).map(f =>
            [f.querySelector('[name="account_label"]').value,
             Array.from(f.querySelectorAll('.cb-ad-row input[name$="_form_id"], .cb-ad-row input[data-renumber-name$="_form_id"]')).map(i => i.value)]))"""
    )


def test_copying_an_ad_to_another_account_clears_the_copys_instant_form_only(page):
    _two_accounts_with_form(page)
    page.evaluate(
        """async () => { const f = Array.from(document.querySelectorAll('form.open-account-form'));
          const byLabel = l => f.find(x => x.querySelector('[name="account_label"]').value === l);
          await cbMove.run({ kind: 'ad', row: byLabel('Demo').querySelector('.cb-ad-row'),
                             destSlot: byLabel('Other').querySelector('.cb-slot'), k: 0 }); }"""
    )
    ids = _form_ids(page)
    assert ids["Demo"] == ["FID"], "оригинал потерял Instant Form"
    assert len(ids["Other"]) == 2 and all(v == "" for v in ids["Other"]), ids
    assert _names_ok(page)
    _toast(page).locator(".cbm-undo").click()
    assert len(_form_ids(page)["Other"]) == 1 and _form_ids(page)["Demo"] == ["FID"]


def test_the_source_card_is_untouched_by_a_drag(page):
    _seed(page, account=0, count=2, ads=1)
    _open_campaign(page, 0)
    grab = """() => document.querySelectorAll('.cb-slot')[0].querySelector('.cb-ad-row').outerHTML
      .replace(/camp_\\d+_ad_\\d+_/g, 'X_')"""
    before = page.evaluate(grab)
    _drag(page, _first_grip(page), page.locator(".cbd-camp-head").nth(1))
    assert page.evaluate(grab) == before


@pytest.mark.parametrize("width", [1280, 800])
def test_handles_are_at_least_32px_and_hittable(page, width):
    _seed(page, account=0, count=2)
    _open_campaign(page, 0)
    page.set_viewport_size({"width": width, "height": 900})
    res = page.evaluate(
        """() => {
          const out = {};
          for (const [name, sel] of [['ad', '#cbd-editor .cb-ad-row .cbm-grip'], ['camp', '#cbd-body .cbd-camp-head .cbm-grip']]) {
            const g = document.querySelector(sel);
            const r = g.getBoundingClientRect();
            const hits = [[r.left + 2, r.top + 2], [r.right - 2, r.top + 2], [r.left + 2, r.bottom - 2], [r.right - 2, r.bottom - 2]]
              .map(([x, y]) => { const e = document.elementFromPoint(x, y); return !!e && (e === g || g.contains(e)); });
            out[name] = { w: r.width, h: r.height, shown: g.offsetParent !== null, vis: getComputedStyle(g).visibility, hits };
          }
          return out;
        }"""
    )
    for name in ("ad", "camp"):
        g = res[name]
        assert g["w"] >= 32 and g["h"] >= 32, (name, g)
        assert g["shown"] and g["vis"] == "visible", (name, g)
        assert all(g["hits"]), (name, g)


def test_handle_hover_state_and_grab_cursor(page):
    _seed(page, account=0, count=2)
    _open_campaign(page, 0)
    grip = _first_grip(page)
    css = "g => { const c = getComputedStyle(g); return [c.backgroundColor, c.color, c.cursor, g.title]; }"
    before = grip.evaluate(css)
    grip.hover()
    after = grip.evaluate(css)
    assert before[2] == "grab" and after[2] == "grab"
    assert before[0] != after[0] and before[1] != after[1]
    assert after[3] == "Drag to copy"


def test_a_click_on_the_handle_opens_the_copy_menu_without_a_drag(page):
    _seed(page, account=0, count=2)
    _open_campaign(page, 0)
    _first_grip(page).click()
    assert page.evaluate("cbMove.menuOpen()")
    assert not page.evaluate("cbMove.dragging()")


def test_static_strings_exist():
    html = REPORT.read_text(encoding="utf-8")
    for text in ("Drag to copy", "Copy here", "Copy to" + chr(0x2026), "Copy this ad to another campaign",
                 "Copy this campaign to another account", "Already here " + chr(0x2014) + " use Duplicate to copy in place.",
                 "Can" + chr(0x2019) + "t undo " + chr(0x2013) + " the draft changed.", "Budget unchanged",
                 "Publish all will combine them into one campaign.", "A campaign can hold up to 100 ads.", "Drop here"):
        assert text in html, text
    for gone in ("Left a blank ad", "Move here", "Move to" + chr(0x2026), "Drag to move", "Moved 1 ad", "Moved campaign"):
        assert gone not in html, gone


# ── ACL-2441: дроп на карточку кампании и фильтр аккаунтов по стране ──────────

def _three_accounts(browser):
    """Demo и Other в одной стране, Third - в другой; у Other уже есть кампании."""
    ctx = browser.new_context()
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    html = _render({"Demo": [], "Other": [], "Third": []})

    def route(r):
        if r.request.resource_type == "document":
            r.fulfill(content_type="text/html", body=html)
            return
        r.fulfill(json={"files": OFFLINE_FILES, "texts": [], "rows": [], "profiles": [], "waiting": False})

    p.route("**/*", route)
    p.goto("http://acl.test/")
    p.evaluate("cbDrawer.open()")
    return ctx, p, errors


def _set_country(p, label, country):
    p.evaluate(
        """([label, country]) => {
          const f = Array.from(document.querySelectorAll('form.open-account-form'))
            .find(x => x.querySelector('[name="account_label"]').value === label);
          f.querySelector('[name="country"]').value = country;
        }""",
        [label, country],
    )


def _count(p, label):
    return p.evaluate(
        """(label) => Array.from(document.querySelectorAll('form.open-account-form'))
          .find(x => x.querySelector('[name="account_label"]').value === label).querySelectorAll('.cb-slot').length""",
        label,
    )


def test_drop_on_a_campaign_card_of_another_account_adds_one_more_campaign(page):
    _seed(page, account=0, count=1)
    _seed(page, account=1, count=2)
    before = _count(page, "Other")
    grip = page.locator(".cbd-acc").first.locator(".cbd-camp-head .cbm-grip").first
    target = page.locator(".cbd-acc").nth(1).locator(".cbd-camp-row").first
    grip.drag_to(target)
    assert _count(page, "Other") == before + 1, "копия не добавилась как ещё одна кампания"
    assert _count(page, "Demo") == 1, "оригинал тронут"
    assert "Copied campaign" in _toast(page).inner_text()
    assert _names_ok(page)


def test_drag_over_a_campaign_card_highlights_the_account_block(page):
    _seed(page, account=0, count=1)
    _seed(page, account=1, count=2)
    page.evaluate(
        """() => { const g = document.querySelector('.cbd-acc .cbm-grip');
          g.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: new DataTransfer() })); }"""
    )
    page.wait_for_function("document.body.classList.contains('cbm-dragging')")
    page.evaluate(
        """() => { const row = document.querySelectorAll('.cbd-acc')[1].querySelector('.cbd-camp-row');
          row.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer() })); }"""
    )
    assert page.evaluate("document.querySelectorAll('.cbd-acc')[1].classList.contains('cbm-over')")
    page.evaluate("document.dispatchEvent(new DragEvent('dragend', { bubbles: true }))")


def test_drop_on_a_campaign_card_of_the_own_account_is_a_no_op(page):
    _seed(page, account=0, count=2)
    snap = _snapshot(page)
    grip = page.locator(".cbd-camp-head .cbm-grip").first
    grip.drag_to(page.locator(".cbd-camp-row").nth(1))
    after = _snapshot(page)
    # Имя кампании билдер пересобирает сам при любой перерисовке - сравниваем число карточек и строк.
    assert {k: after[k] for k in ("slots", "rows", "tree")} == {k: snap[k] for k in ("slots", "rows", "tree")}
    assert not _toast(page).count() or not _toast(page).is_visible()
    assert _count(page, "Demo") == 2


def test_ghosts_and_menu_list_only_accounts_of_the_campaigns_country(browser):
    ctx, p, errors = _three_accounts(browser)
    try:
        _set_country(p, "Third", "Peru")
        _seed(p, account=0, count=1)
        p.evaluate(
            """() => { const g = document.querySelector('.cbd-acc .cbm-grip');
              g.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: new DataTransfer() })); }"""
        )
        p.wait_for_function("document.body.classList.contains('cbm-dragging')")
        ghosts = p.locator(".cbm-ghost").all_inner_texts()
        assert len(ghosts) == 1 and ghosts[0].startswith("Other"), ghosts
        p.evaluate("document.dispatchEvent(new DragEvent('dragend', { bubbles: true }))")
        p.locator(".cbd-camp-head .cbm-moveto").first.dispatch_event("click")
        labels = p.locator(".cbm-menu .cbm-item").all_inner_texts()
        joined = " ".join(labels)
        assert "Other" in joined and "Third" not in joined and "Demo" not in joined, labels
    finally:
        assert not errors, errors
        ctx.close()


def test_a_country_without_other_accounts_shows_a_note_instead_of_an_empty_list(browser):
    ctx, p, errors = _three_accounts(browser)
    try:
        _set_country(p, "Other", "Peru")
        _set_country(p, "Third", "Chile")
        _seed(p, account=0, count=1)
        p.evaluate(
            """() => { const g = document.querySelector('.cbd-acc .cbm-grip');
              g.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: new DataTransfer() })); }"""
        )
        p.wait_for_function("document.body.classList.contains('cbm-dragging')")
        assert p.locator(".cbm-ghost:not(.cbm-ghost-none)").count() == 0
        note = p.locator(".cbm-ghosts .cbm-ghost-none").inner_text()
        assert note.startswith("No other account for ")
        p.evaluate("document.dispatchEvent(new DragEvent('dragend', { bubbles: true }))")
        p.locator(".cbd-camp-head .cbm-moveto").first.dispatch_event("click")
        assert p.locator(".cbm-menu .cbm-item").count() == 0
        assert "No other account for" in p.locator(".cbm-menu").inner_text()
    finally:
        assert not errors, errors
        ctx.close()
