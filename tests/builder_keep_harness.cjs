// Набранное переживает перезагрузку — и не копится вечно.
//
// Форму рисует сервер, и рисует пустой: карточек кампаний в разметке
// больше нет. Значит, после F5 в билдере чисто, а собранная наполовину
// кампания есть работа: имя, бюджет, таргетинг, выбранные креативы.
//
// Код берётся живьём из шаблона, DOM поддельный, часы в руках — иначе
// срок годности проверить нечем.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const { el } = require('./fake_dom.cjs');

const html = fs.readFileSync(process.argv[2], 'utf8');
const start = html.indexOf('window.cbKeep = (function () {');
assert(start > 0, 'window.cbKeep не найден в шаблоне');
const code = html.slice(start, html.indexOf('\n})();', start) + 6);
assert(code.includes('acl.builder.v2'), 'срез захватил не то');

const DAY = 24 * 60 * 60 * 1000;

// ── поддельная страница: одна страна, один аккаунт, одна форма ─────────
function input(name, value, opts = {}) {
  const node = el('input', { classes: opts.classes || [], value });
  node.name = name;
  node.type = opts.type || 'text';
  node.checked = !!opts.checked;
  return node;
}

function selWrap(name, hiddenValue, shownValue) {
  return el('div', {
    classes: ['cb-sel-wrap'],
    children: [
      el('input', { classes: ['cb-sel-text'], value: shownValue }),
      input(name, hiddenValue),
    ],
  });
}

function slot(idx, values = {}) {
  const kids = [
    selWrap('camp_' + idx + '_targeting', values.targeting || '', values.targetingShown || ''),
    input('camp_' + idx + '_age', values.age || ''),
    input('camp_' + idx + '_budget', values.budget || ''),
    input('camp_' + idx + '_asset', values.asset || ''),
    input('camp_' + idx + '_name', values.name || '', { classes: ['cb-name-hidden'] }),
    // без `name`: своё имя живёт только в черновике (ACL-1769)
    el('input', { classes: ['cb-name-override'], value: values.override || '' }),
    input('camp_' + idx + '_ad_0_creative_type', 'Image', { type: 'radio', checked: true }),
    input('camp_' + idx + '_ad_0_creative_type', 'Video', { type: 'radio' }),
    input('camp_' + idx + '_ad_0_copy', values.copy || ''),
    input('camp_' + idx + '_ad_count', String(values.ads || 1)),
    el('div', { classes: ['cb-targeting-hint'], textContent: values.hint || '' }),
    el('button', { classes: ['cb-add-btn-sm'] }),
  ];
  for (let i = 0; i < (values.ads || 1); i += 1) kids.push(el('div', { classes: ['cb-ad-row'] }));
  return el('div', { classes: ['cb-slot'], dataset: { idx: String(idx) }, children: kids });
}

function boot(opts = {}) {
  const form = el('form', { classes: ['open-account-form'], children: (opts.slots || []).map((v, i) => slot(i, v)) });
  const row = el('div', { classes: ['cb-acc-row'], dataset: { accLabel: 'DQ438' }, children: [form] });
  const panel = el('div', { classes: ['view-panel'], dataset: { country: 'saudi-arabia' }, children: [row] });
  const root = el('div', { children: [panel] });

  const time = { value: opts.now === undefined ? 1_700_000_000_000 : opts.now };
  const listeners = {};
  const document = {
    readyState: 'complete',
    querySelectorAll: (sel) => root.querySelectorAll(sel),
    querySelector: (sel) => root.querySelector(sel),
    addEventListener: (n, fn) => ((listeners[n] = listeners[n] || []).push(fn)),
    fire: (n, e) => (listeners[n] || []).forEach((fn) => fn(e)),
  };

  const timers = [];
  const sandbox = {
    document,
    localStorage: opts.store || memoryStore(),
    Date: { now: () => time.value },
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout() {},
    window: {
      addEventListener: (n, fn) => ((listeners[n] = listeners[n] || []).push(fn)),
      cbAddSlot: (f) => {
        const n = f.querySelectorAll('.cb-slot').length;
        return f.appendChild(slot(n));
      },
      cbAddAd: (btn) => {
        const s = btn.closest('.cb-slot');
        s.appendChild(el('div', { classes: ['cb-ad-row'] }));
      },
      cbUpdateName: (s) => { s._named = (s._named || 0) + 1; },
    },
    Array, Object, JSON, String, parseInt,
  };
  sandbox.window.window = sandbox.window;
  vm.runInNewContext(code, sandbox);
  return { api: sandbox.window.cbKeep, form, row, panel, document, timers, time, sandbox };
}

function memoryStore(initial) {
  const data = Object.assign({}, initial);
  return {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    data,
  };
}

function saved(store) { return JSON.parse(store.data['acl.builder.v2']); }

// ── пусто ─────────────────────────────────────────────────────────────
let store = memoryStore();
let t = boot({ store, slots: [] });
t.api.save();
assert.deepEqual(saved(store).forms, {}, 'пустой аккаунт занял место в хранилище');

// ── что запоминается ──────────────────────────────────────────────────
store = memoryStore();
t = boot({ store, slots: [
  { asset: 'Alpha', age: '41', budget: '88', name: 'SA_Alp', targeting: 'Day trading, Swing', targetingShown: 'Trading', hint: 'Day trading, Swing', ads: 2 },
  { asset: 'Beta', age: '35', name: 'SA_Bet' },
] });
t.api.save();
let entry = saved(store).forms['saudi-arabia|DQ438'];
assert.ok(entry, 'ключ собран не из страны и аккаунта');
assert.equal(entry.campaigns.length, 2);
assert.equal(entry.campaigns[0].fields.asset, 'Alpha');
assert.equal(entry.campaigns[0].fields.age, '41');
assert.equal(entry.campaigns[0].fields.name, 'SA_Alp');
assert.equal(entry.campaigns[0].ads, 2, 'число объявлений не запомнено');

// Видимый текст поиска хранится отдельно от выбранного значения: в
// скрытом поле список интересов, в видимом — код, набранный человеком.
// Без него после перезагрузки поле выглядит пустым при заполненном
// значении.
assert.equal(entry.campaigns[0].fields.targeting, 'Day trading, Swing');
assert.equal(entry.campaigns[0].shown.targeting, 'Trading', 'видимый текст поиска потерян');
assert.equal(entry.campaigns[0].hint, 'Day trading, Swing');

// Радио запоминается выбранное, а не оба.
assert.equal(entry.campaigns[0].fields.ad_0_creative_type, 'Image');

// ── и раскладывается обратно ──────────────────────────────────────────
// Сервер отдал форму пустой — ровно так и бывает после F5.
// Раскладывает сам при загрузке — звать никого не надо.
let back = boot({ store, slots: [] });
let slots = back.form.querySelectorAll('.cb-slot');
assert.equal(slots.length, 2, 'при загрузке ничего не разложилось');
// Повторный вызов ничего не задваивает.
assert.equal(back.api.restore(), 0, 'повторное восстановление задвоило карточки');
slots = back.form.querySelectorAll('.cb-slot');
assert.equal(slots.length, 2);
assert.equal(slots.length, 2);
assert.equal(slots[0].querySelector('[name="camp_0_asset"]').value, 'Alpha');
assert.equal(slots[0].querySelector('[name="camp_0_age"]').value, '41');
assert.equal(slots[0].querySelector('[name="camp_0_budget"]').value, '88');
assert.equal(slots[1].querySelector('[name="camp_1_asset"]').value, 'Beta');
assert.equal(slots[0].querySelector('.cb-sel-text').value, 'Trading', 'видимый текст не вернулся');
assert.equal(slots[0].querySelector('.cb-targeting-hint').textContent, 'Day trading, Swing');
assert.equal(slots[0].querySelectorAll('.cb-ad-row').length, 2, 'объявления не восстановлены');
assert.ok(slots[0]._named, 'имя кампании не пересобрано после восстановления');

// ── восстановление не задваивает ──────────────────────────────────────
// Если карточки почему-то уже есть, раскладываются только недостающие.
let half = boot({ store, slots: [{ asset: 'уже тут', name: 'X' }] });
half.api.restore();
assert.equal(half.form.querySelectorAll('.cb-slot').length, 2, 'карточки задвоились');

// ── срок годности ─────────────────────────────────────────────────────
store = memoryStore();
t = boot({ store, now: 1000 * DAY, slots: [{ asset: 'старое', name: 'Old' }] });
t.api.save();
let later = boot({ store, now: 1015 * DAY, slots: [] });
assert.equal(later.api.restore(), 0, 'двухнедельный черновик вернулся');
assert.equal(later.api.staleMs, 14 * DAY);

let soon = boot({ store: memoryStore(), now: 1000 * DAY, slots: [{ asset: 'свежее', name: 'New' }] });
soon.api.save();
// (тот же ключ, десять дней спустя — ещё живо)

// ── мусор и закрытое хранилище не роняют страницу ─────────────────────
for (const junk of ['не json', '{}', '[]', '{"v":99,"forms":{}}', '{"v":2}']) {
  const s = boot({ store: memoryStore({ 'acl.builder.v2': junk }), slots: [] });
  assert.equal(s.api.restore(), 0, 'на мусоре ' + junk + ' что-то восстановилось');
}
const locked = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
const l = boot({ store: locked, slots: [{ asset: 'A', name: 'A' }] });
assert.equal(l.api.restore(), 0);
l.api.save();                      // не должно бросить

// ── ввод откладывает запись, а не пишет на каждую букву ───────────────
store = memoryStore();
t = boot({ store, slots: [{ asset: 'A', name: 'A' }] });
const before = t.timers.length;
t.document.fire('input', { target: { closest: (sel) => (sel === 'form.open-account-form' ? t.form : null) } });
assert.equal(t.timers.length, before + 1, 'ввод не поставил запись в очередь');
t.document.fire('input', { target: { closest: () => null } });
assert.equal(t.timers.length, before + 1, 'ввод вне формы поставил запись');

// ── ACL-1769: своё имя кампании — ещё одно необязательное поле ─────────
// Форма хранения (v2) не менялась: `name_override` лежит среди `fields`
// рядом с остальными. Значит, черновик, записанный до двухколоночного
// билдера, раскладывается как раньше, а новый — со своим именем.
store = memoryStore();
t = boot({ store, slots: [{ asset: 'Alpha', name: 'SA_Alp', override: 'My own name' }] });
t.api.save();
entry = saved(store).forms['saudi-arabia|DQ438'];
assert.equal(entry.campaigns[0].fields.name_override, 'My own name', 'своё имя не запомнено');
back = boot({ store, slots: [] });
assert.equal(back.form.querySelectorAll('.cb-slot')[0].querySelector('.cb-name-override').value, 'My own name', 'своё имя не вернулось');

// Черновик, записанный раньше: поля name_override в нём нет вовсе.
const oldDraft = { v: 2, forms: { 'saudi-arabia|DQ438': { touched: 1_700_000_000_000, tracking: '', campaigns: [
  { fields: { asset: 'Old', age: '35', budget: '40', name: 'SA_Old_name', ad_0_creative_type: 'Image' }, shown: {}, hint: '', ads: 1 },
] } } };
store = memoryStore({ 'acl.builder.v2': JSON.stringify(oldDraft) });
back = boot({ store, slots: [] });
const restored = back.form.querySelectorAll('.cb-slot');
assert.equal(restored.length, 1, 'старый черновик не поднялся');
assert.equal(restored[0].querySelector('[name="camp_0_asset"]').value, 'Old');
assert.equal(restored[0].querySelector('[name="camp_0_budget"]').value, '40');
assert.equal(restored[0].querySelector('.cb-name-override').value, '', 'у старого черновика появилось чужое имя');
assert.ok(restored[0]._named, 'имя старого черновика не пересобрано');

console.log('ok');
