// Instant Forms в билдере (ACL-1775): выбор места конверсии и формы Страницы.
//
// Код берётся живьём из шаблона (cbLayout, склейка с DOM, план): копия рядом
// расходится с оригиналом молча. DOM здесь поддельный и узкий - ровно то, что
// трогают проверяемые функции. Правила Meta (чья форма, какой статус годен)
// проверяет сервер; тут - что показать и что уходит.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

function slice(from, to) {
  const i = html.indexOf(from);
  assert(i > 0, 'не найдено: ' + from);
  const j = html.indexOf(to, i);
  assert(j > i, 'не найден конец: ' + to);
  return html.slice(i, j);
}

const layoutCode = slice('window.cbLayout = (function () {', '\n})();') + '\n})();';
const glueCode = slice('window.cbForms = window.cbForms || {};', '// ── Ad row HTML builder');
const planCode = slice('function cbPlanMoney(s) {', '/* Рамка плана');
const escCode = slice('function aclEscHtml(value) {', '\n    }\n') + '\n    }\n';

const sandbox = { console, Event: function Event(type, init) { this.type = type; this.bubbles = !!(init && init.bubbles); } };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(escCode, sandbox);
vm.runInContext(layoutCode, sandbox);
vm.runInContext(glueCode, sandbox);
vm.runInContext(planCode, sandbox);
const L = sandbox.window.cbLayout;
const W = sandbox.window;

// ── чистые функции раскладки ───────────────────────────────────────────
assert.equal(L.destination('instant_form'), 'instant_form');
assert.equal(L.destination('website'), 'website');
assert.equal(L.destination('whatever'), 'website', 'неизвестное назначение не должно стать формой');
assert.equal(L.destination(undefined), 'website');
assert.deepEqual(Object.keys(L.DESTINATIONS), ['website', 'instant_form']);
assert.equal(L.FUTURE.forms, undefined, 'формы больше не заглушка');

const forms = [
  { id: '7000001', name: 'Lead form A', status: 'ACTIVE' },
  { id: '7000002', name: 'Old form', status: 'ARCHIVED' },
  { id: '7000004', name: 'Draft form', status: 'DRAFT' },
  { id: '7000005', name: 'Gone', status: 'deleted' },
  { id: '7000003', name: 'Lead form B', status: 'ACTIVE' },
  { id: '7000006', name: 'No status' },
  { id: '7000007', name: 'Pending', status: 'PENDING' },
];
let opts = L.formOptions(forms, '');
assert.deepEqual(opts.map((o) => o.id), ['7000001', '7000003'], 'годна только ACTIVE: архивные, черновики, без статуса и незнакомые не показываются');
assert.equal(opts[0].label, 'Lead form A · 7000001');
opts = L.formOptions(forms, '7000003');
assert.equal(opts.length, 2, 'выбранная форма из списка не дублируется');
opts = L.formOptions(forms, '7000002');
assert.equal(opts.length, 3, 'сохранённая архивная форма остаётся видимой пометкой');
assert.equal(opts[2].id, '7000002');
assert.ok(opts[2].missing && /not in the loaded list/.test(opts[2].label));
assert.deepEqual(L.formOptions(undefined, '').length, 0);
assert.equal(L.formOptions(null, '555')[0].id, '555', 'без загруженного списка сохранённый выбор не теряется');

assert.equal(L.formsStatus(null), 'Press "Refresh forms" to read the forms of the Page');
assert.equal(L.formsStatus({ ok: true, forms: [{}] }), '1 form of the Page');
assert.equal(L.formsStatus({ ok: true, forms: [{}, {}] }), '2 forms of the Page');
assert.equal(L.formsStatus({ ok: false, message: "This Page's forms can't be read with the current session" }),
  "This Page's forms can't be read with the current session");
assert.equal(L.formsStatus({ ok: true, forms: [], message: 'This Page has no Instant Forms yet. Create one in Meta, then refresh the forms' }),
  'This Page has no Instant Forms yet. Create one in Meta, then refresh the forms');

// ── сообщения сервера ложатся на выбор формы ───────────────────────────
const sels = (m) => L.routes(m).map((r) => r.sel);
['Ad 1: no Instant Form selected (pick one of the Page\'s forms)',
 'Ad 2: the Instant Form 7000009 is not a form of this Page; pick one of the Page\'s forms (Refresh forms).',
 "This Page's forms can't be read with the current session.",
 'This Page has not accepted the Lead Ads terms. Accept them in Meta (the robot never accepts terms), then refresh the forms.'
].forEach((m) => assert.ok(sels(m).indexOf('.cb-form-select') !== -1, 'не легло на форму: ' + m));
assert.deepEqual(sels('Ad 1: Primary text 2 is not supported by the API backend yet'), ['.cb-text2-search'],
  'ошибка второго текста стала ошибкой формы');

// ── поддельный DOM ─────────────────────────────────────────────────────
function el(props) {
  return Object.assign({ hidden: false, disabled: false, value: '', title: '', textContent: '', innerHTML: '',
    dataset: {}, events: [], dispatchEvent(ev) { this.events.push(ev.type + (ev.bubbles ? ':bubbles' : '')); } }, props || {});
}
function build(opts) {
  const o = Object.assign({ account: 'Acc', dest: 'website', formIds: ['', ''], source: '' }, opts || {});
  const form = { querySelector: (s) => (s === '[name="account_label"]' ? { value: o.account } : null) };
  const optInstant = el({});
  const select = el({ value: o.dest, querySelector: (s) => (s === 'option[value="instant_form"]' ? optInstant : null) });
  const note = el({ hidden: true });
  const rows = o.formIds.map((id) => {
    const hid = el({ value: id, disabled: true });
    const only = [el({ hidden: true }), el({ hidden: true })];
    const existing = [el({ hidden: true }), el({ hidden: true })];
    const srcSel = el({ value: o.source, querySelector: () => null });
    const picker = el({ dataset: {} });
    const fnote = el({});
    const row = el({
      querySelector: (s) => (s === '.cb-form-select' ? picker : s === 'input[name$="_form_id"]' ? hid
        : s === '[data-form-note]' ? fnote : s === '.cb-form-source' ? srcSel : null),
      querySelectorAll: (s) => (s === '.cb-form-only' ? only : s === '.cb-form-existing' ? existing : []),
      closest: (s) => (s === 'form' ? form : null),
    });
    row._ = { hid, only, existing, srcSel, picker, fnote };
    return row;
  });
  const slot = el({
    querySelector: (s) => (s === '.cb-destination' ? select : s === '[data-forms-api-only]' ? note : null),
    querySelectorAll: (s) => (s === '.cb-ad-row' ? rows : []),
    closest: (s) => (s === 'form' ? form : null),
  });
  return { slot, select, optInstant, note, rows, form };
}
function setBackend(b) { W.ACL_FB_BACKEND = b; W.cbForms = {}; }

// Website + API: форма скрыта, её поле не уходит, выбор рабочий.
setBackend('api');
let t = build({ dest: 'website', formIds: ['7000001', ''] });
W.cbSyncDestination(t.slot);
assert.equal(t.select.disabled, false);
assert.equal(t.optInstant.disabled, false);
assert.equal(t.note.hidden, true);
t.rows.forEach((r) => {
  assert.ok(r._.only.every((x) => x.hidden === true), 'выбор формы виден при Website');
  assert.equal(r._.hid.disabled, true, 'значение формы уйдёт на сервер при Website');
});
assert.equal(t.rows[0]._.hid.value, '7000001', 'сохранённое значение пропало');

// Instant Form + API: выбор формы виден у каждого объявления, поле уходит.
t.select.value = 'instant_form';
W.cbDestinationChanged({ closest: () => t.slot });
t.rows.forEach((r) => {
  assert.ok(r._.only.every((x) => x.hidden === false), 'выбор формы не показан');
  assert.equal(r._.hid.disabled, false, 'поле формы не уходит при Instant Form');
});
assert.equal(t.rows[0]._.hid.value, '7000001');

// Назад на Website: поле снова не уходит, значение живёт (dormant).
t.select.value = 'website';
W.cbSyncDestination(t.slot);
assert.equal(t.rows[0]._.hid.disabled, true);
assert.equal(t.rows[0]._.hid.value, '7000001', 'значение формы потеряно при смене назначения');

// RPA + Website: выключено с причиной, Instant Form не выбрать, подпись видна.
setBackend('rpa');
t = build({ dest: 'website' });
W.cbSyncDestination(t.slot);
assert.equal(t.select.disabled, true);
assert.ok(/API backend only/.test(t.select.title));
assert.equal(t.optInstant.disabled, true);
assert.equal(t.note.hidden, false);
t.rows.forEach((r) => assert.equal(r._.hid.disabled, true));

// RPA + уже выбранный Instant Form: не гасим молча - выбор рабочий, сервер откажет вслух.
t = build({ dest: 'instant_form', formIds: ['7000001', '7000003'] });
W.cbSyncDestination(t.slot);
assert.equal(t.select.disabled, false, 'непустой выбор пропал бы молча');
assert.equal(t.optInstant.disabled, true);
assert.equal(t.rows[0]._.hid.disabled, false);
assert.equal(t.note.hidden, false);

// ACL-2189: в режиме «Use existing form» выбор формы и «Refresh forms» видны,
// в режиме «Create from template» скрыты (раньше было наоборот).
setBackend('api');
t = build({ dest: 'instant_form', source: 'existing', formIds: ['7000001'] });
W.cbSyncDestination(t.slot);
assert.ok(t.rows[0]._.existing.every((x) => x.hidden === false), 'Use existing form: выбор формы и Refresh forms должны быть видны');
t.rows[0]._.srcSel.value = 'template';
W.cbSyncDestination(t.slot);
assert.ok(t.rows[0]._.existing.every((x) => x.hidden === true), 'Create from template: выбор формы и Refresh forms должны быть скрыты');
t.rows[0]._.srcSel.value = 'existing';
W.cbSyncDestination(t.slot);
assert.ok(t.rows[0]._.existing.every((x) => x.hidden === false), 'возврат на Use existing form не показал выбор формы');

// Повторная синхронизация ничего не пишет (форму слушает наблюдатель дерева).
setBackend('api');
t = build({ dest: 'instant_form', formIds: ['7000001'] });
W.cbForms['Acc|'] = { ok: true, forms: forms, message: '' };
W.cbSyncDestination(t.slot);
const firstSig = t.rows[0]._.picker.dataset.sig;
let writes = 0;
let inner = t.rows[0]._.picker.innerHTML;
Object.defineProperty(t.rows[0]._.picker, 'innerHTML', { get() { return inner; }, set(v) { writes += 1; inner = v; } });
W.cbSyncDestination(t.slot);
W.cbSyncDestination(t.slot);
assert.equal(writes, 0, 'повторная синхронизация переписала список');
assert.equal(t.rows[0]._.picker.dataset.sig, firstSig);

// Список: пригодные формы, выбранная выбрана, подпись про число форм.
t = build({ dest: 'instant_form', formIds: ['7000003'] });
W.cbForms['Acc|'] = { ok: true, forms: forms, message: '' };
W.cbSyncDestination(t.slot);
const picker = t.rows[0]._.picker;
const ids = (picker.innerHTML.match(/value="([^"]*)"/g) || []).map((x) => x.slice(7, -1));
assert.deepEqual(ids, ['', '7000001', '7000003'], 'в списке не те формы: ' + picker.innerHTML);
assert.equal(picker.value, '7000003');
assert.equal(t.rows[0]._.fnote.textContent, '7 forms of the Page', 'подпись считает то, что пришло от сервера');

// Выбор формы пишет в скрытое поле и сообщает наблюдателю (input, bubbles).
const row0 = t.rows[0];
const pick = el({ value: '7000001', closest: (s) => (s === '.cb-ad-row' ? row0 : null) });
row0.closest = (s) => (s === '.cb-slot' ? t.slot : s === 'form' ? t.form : null);
W.cbFormChosen(pick);
assert.equal(row0._.hid.value, '7000001');
assert.deepEqual(row0._.hid.events, ['input:bubbles']);

// «Refresh forms»: ровно одно обращение на нажатие и только на /api/page-forms.
const fetched = [];
sandbox.FormData = function FormData() { this.d = {}; this.append = (k, v) => { this.d[k] = v; }; };
sandbox.document = { querySelectorAll: (s) => (s === '.cb-slot' ? [t.slot] : []) };
sandbox.fetch = (url, init) => {
  fetched.push({ url, account: init.body.d.account_label, method: init.method, page: init.body.d.page_id });
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, state: 'ok', forms: [forms[0]], message: '' }) });
};
W.cbResolvedPage = () => '111';   // ACL-1880: формы читаются только для разрешённой Страницы
const btn = el({ closest: (s) => (s === '.cb-ad-row' ? row0 : null) });
W.cbRefreshForms(btn);
assert.equal(btn.disabled, true, 'кнопку можно нажать второй раз, пока идёт чтение');
W.cbRefreshForms(btn);
assert.equal(fetched.length, 1, 'второе нажатие во время чтения дало второй вызов');
setTimeout(() => {
  assert.deepEqual(fetched, [{ url: '/api/page-forms', account: 'Acc', method: 'POST', page: '111' }]);
  assert.equal(btn.disabled, false);
  assert.equal(W.cbForms['Acc|111'].forms.length, 1);
  assert.equal(row0._.fnote.textContent, '1 form of the Page');
  // Сбой сети - подпись, а не немой отказ.
  sandbox.fetch = () => Promise.reject(new Error('down'));
  W.cbRefreshForms(btn);
  setTimeout(() => {
    assert.ok(/did not answer/.test(row0._.fnote.textContent), row0._.fnote.textContent);
    assert.equal(btn.disabled, false);

    // ── Check plan: имя и id формы вместо ссылки ──────────────────────────
    const plan = (ads, destination) => ({ account: 'Acc', results: [{ campaign_name: 'C', warnings: [], plan: { summary: {
      campaign: 'C', status: 'PAUSED', objective: 'OUTCOME_LEADS', daily_budget: 20, currency: 'USD', page_id: '111', pixel_id: '',
      existing: false, ad_sets: [{ name: 'C', countries: ['MX'], language: '', age: '25-55', interests: [], placements: 'manual',
        destination: destination, ads: ads }] } } }] });
    const formAd = { name: 'a1', format: 'image', file: 'a.png', headline: 'H', primary_text: 'T', description: '', form_id: '7000001', form_name: 'Lead <form> A', cta: 'LEARN_MORE' };
    const webAd = { name: 'a2', format: 'image', file: 'b.png', headline: 'H', primary_text: 'T', description: '', link: 'https://x.invalid/l', cta: 'LEARN_MORE' };
    const fh = W.cbPlanHtml(plan([formAd], 'Instant Form'));
    assert.ok(fh.includes('Instant Form Lead &lt;form&gt; A (7000001)'), 'имя и id формы не показаны (или не экранированы)');
    assert.ok(!fh.includes('link '), 'у Instant Form показана ссылка сайта');
    assert.ok(!fh.includes('pixel'), 'у Instant Form показан пиксель');
    assert.ok(fh.includes('· Instant Form</div>'), 'набор не назван Instant Form');
    const wh = W.cbPlanHtml(plan([webAd], undefined));
    assert.ok(wh.includes('link https://x.invalid/l') && wh.includes('pixel —'), 'сайтовый план изменился');
    assert.ok(!wh.includes('Instant Form'));
    console.log('ok');
  }, 20);
}, 20);
