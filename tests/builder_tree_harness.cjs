// Дерево в ящике: страна → аккаунт → кампания → объявление.
//
// Проверяется тем же способом, что и остальной код страницы: настоящий
// блок вырезается из шаблона и исполняется. Разметка тут собирается
// строками, поэтому её и проверяем строками — а обработчик клика зовём
// настоящий, подсунув ему событие.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

const start = html.indexOf('window.cbTree = (function () {');
assert(start > 0, 'window.cbTree не найден в шаблоне');
const tail = '\n})();';
const end = html.indexOf(tail, start) + tail.length;
const code = html.slice(start, end);
assert(code.includes('cbd-country'), 'срез захватил не то');

// ── поддельная страница ───────────────────────────────────────────────
// Ровно столько, сколько трогает дерево, и ни строчкой больше: стоит
// стубу начать додумывать, он начинает врать вместо страницы.

function panel(country, accounts, active) {
  return {
    dataset: { country },
    classList: { contains: (c) => c === 'active' && !!active },
    querySelectorAll: () => accounts.map((label) => ({ dataset: { accLabel: label } })),
  };
}

function makePage(opts) {
  opts = opts || {};
  const panels = opts.panels || [];
  const names = opts.names || {};
  const store = opts.store;

  const nodes = {};
  ['cbd-body', 'cbd-foot', 'cbd-panel'].forEach((id) => {
    nodes[id] = {
      id,
      innerHTML: '',
      hidden: true,
      handlers: {},
      addEventListener(name, fn) { (this.handlers[name] = this.handlers[name] || []).push(fn); },
      // Разметка строкой не разбирается: тесты смотрят на неё как на строку,
      // а по элементам ходят через подставленное событие.
      querySelector: () => null,
    };
  });

  const listened = [];
  const document = {
    getElementById: (id) => nodes[id] || null,
    querySelectorAll(sel) {
      if (sel === '.view-panel[data-country]') return panels;
      return [];
    },
    querySelector(sel) {
      const m = /^\.view-panel\.active\[data-country\]$/.test(sel);
      if (m) return panels.find((p) => p.classList.contains('active')) || null;
      const c = sel.match(/^\.sidebar-country-btn\[data-country="(.+)"\]$/);
      if (c) return names[c[1]] ? { textContent: names[c[1]] } : null;
      return null;
    },
    addEventListener(name) { listened.push(name); },
  };

  const alerts = [], confirms = [];
  const sandbox = {
    document,
    localStorage: store || {
      getItem: () => null,
      setItem() {},
    },
    aclEscHtml(v) {
      return String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    },
    window: {
      cbDraft: opts.draft,
      alert(m) { alerts.push(m); },
      confirm(m) { confirms.push(m); return opts.confirm !== false; },
    },
    Object, Array, JSON,
  };
  sandbox.window.window = sandbox.window;
  vm.runInNewContext(code, sandbox);
  return { api: sandbox.window.cbTree, nodes, alerts, confirms, listened, sandbox };
}

// Поддельное хранилище: то же лицо, что у настоящего cbDraft.
function fakeDraft(campaigns) {
  const list = campaigns.slice();
  const api = {
    limit: 200,
    all: () => list,
    count: () => list.length,
    get: (id) => list.find((c) => c.id === id) || null,
    add(where) {
      if (list.length >= api.limit) return null;
      const c = { id: 'id' + (list.length + 1), name: '', ads: [], ...where };
      list.push(c);
      return c;
    },
    update(id, patch) {
      const c = api.get(id);
      if (c) Object.assign(c, patch);
      return c;
    },
    remove(id) {
      const i = list.findIndex((c) => c.id === id);
      if (i === -1) return false;
      list.splice(i, 1);
      return true;
    },
    grouped() {
      const out = [], byC = {};
      list.forEach((c) => {
        if (!byC[c.country]) {
          byC[c.country] = { country: c.country, countryName: c.countryName, accounts: [], _a: {} };
          out.push(byC[c.country]);
        }
        const g = byC[c.country];
        if (!g._a[c.account]) { g._a[c.account] = { account: c.account, campaigns: [] }; g.accounts.push(g._a[c.account]); }
        g._a[c.account].campaigns.push(c);
      });
      out.forEach((g) => delete g._a);
      return out;
    },
    onChange(fn) { api._changed = fn; },
  };
  return api;
}

// ── готовность: зелёного до проверки не бывает ────────────────────────
let page = makePage({ draft: fakeDraft([]) });
const r = page.api.readiness;
assert.equal(r({ name: '', ads: [] }).state, 'bad', 'безымянная кампания сошла за готовую');
assert.equal(r({ name: 'X', ads: [] }).state, 'bad', 'кампания без объявлений сошла за готовую');
assert.equal(r({ name: 'X', ads: [{}] }).state, 'wait', 'проверок ещё нет, а состояние уже не серое');
// Ни одно состояние не опознаётся одним цветом: у каждого свой значок.
const signs = [r({ name: '', ads: [] }), r({ name: 'X', ads: [] }), r({ name: 'X', ads: [{}] })]
  .map((x) => x.sign);
assert.ok(signs.every((s) => s && s.length), 'у состояния нет значка — остался один цвет');
assert.ok(r({ name: '', ads: [] }).why, 'состояние без объяснения — загадка, а не индикатор');
// Зелёного здесь не выдают вовсе: обещать нечего, пока никто не проверял.
assert.ok(![r({ name: '', ads: [] }), r({ name: 'X', ads: [] }), r({ name: 'X', ads: [{}] })]
  .some((x) => x.state === 'ok'), 'выдан зелёный без единой проверки');

// ── страны и аккаунты берутся со страницы ─────────────────────────────
page = makePage({
  draft: fakeDraft([]),
  panels: [
    panel('saudi-arabia', ['DQ438', 'KLD_X84']),
    panel('saudi-arabia', ['DQ438']),          // та же страна второй панелью
    panel('kuwait', ['KW_W72']),
    panel('peru', []),                          // без аккаунтов — не предлагается
  ],
  names: { 'saudi-arabia': 'Saudi Arabia', kuwait: 'Kuwait' },
});
let list = page.api.places();
assert.deepEqual(list.map((p) => p.country), ['saudi-arabia', 'kuwait'], 'страна без аккаунтов попала в выбор');
assert.deepEqual(list[0].accounts, ['DQ438', 'KLD_X84'], 'аккаунт задвоился между панелями');
assert.equal(list[0].name, 'Saudi Arabia', 'имя страны не взято с боковой панели');

// ── пусто ─────────────────────────────────────────────────────────────
page = makePage({ draft: fakeDraft([]), panels: [panel('kuwait', ['KW_W72'])], names: { kuwait: 'Kuwait' } });
assert.ok(/Nothing is being assembled yet/.test(page.nodes['cbd-body'].innerHTML));
assert.ok(/cbd-pick-country/.test(page.nodes['cbd-foot'].innerHTML), 'завести первую кампанию нечем');

// ── дерево рисуется подписями ─────────────────────────────────────────
const draft = fakeDraft([
  { id: 'a', country: 'sa', countryName: 'Saudi Arabia', account: 'DQ438', name: '', ads: [] },
  { id: 'b', country: 'sa', countryName: 'Saudi Arabia', account: 'DQ438', name: 'SA · Trading', ads: [{}, {}] },
  { id: 'c', country: 'kw', countryName: 'Kuwait', account: 'KW_W72', name: 'KW · Amazon', ads: [{}] },
]);
page = makePage({ draft, panels: [panel('kw', ['KW_W72'])], names: { kw: 'Kuwait' } });
let body = page.nodes['cbd-body'].innerHTML;
assert.equal((body.match(/cbd-country/g) || []).length, 2, 'стран не две');
assert.equal((body.match(/class="cbd-account"/g) || []).length, 2);
assert.equal((body.match(/class="cbd-camp"/g) || []).length, 3);
assert.ok(body.indexOf('Saudi Arabia') < body.indexOf('DQ438'), 'аккаунт выше своей страны');
assert.ok(/1 ad<\/span>/.test(body) && /2 ads<\/span>/.test(body), 'число объявлений не единственное/множественное');
assert.ok(/Untitled campaign/.test(body), 'безымянная кампания названа пустотой');
// Отступов на уровень нет: иерархию несут подписи, а не лесенка.
assert.ok(!/margin-left|padding-left:\s*\d/.test(body), 'в дереве появилась лесенка');

// ── имя кампании экранируется ─────────────────────────────────────────
page = makePage({
  draft: fakeDraft([{ id: 'x', country: 'sa', countryName: 'S<b>A', account: 'A"C', name: '<img src=x onerror=1>', ads: [] }]),
  panels: [],
});
body = page.nodes['cbd-body'].innerHTML;
assert.ok(!/<img/.test(body), 'имя кампании попало в разметку как разметка');
assert.ok(!/<b>/.test(body), 'имя страны попало в разметку как разметка');

// ── клики идут через настоящий обработчик ─────────────────────────────
function clickOn(page, act, attrs, campId) {
  const hit = { dataset: Object.assign({ act }, attrs || {}), closest: (sel) => (sel === '.cbd-camp' ? (campId ? { dataset: { camp: campId } } : null) : hit) };
  const event = { target: { closest: (sel) => (sel === '[data-act]' ? hit : null) } };
  page.nodes['cbd-panel'].handlers.click.forEach((fn) => fn(event));
}

// раскрывается одна за раз
const two = fakeDraft([
  { id: 'a', country: 'sa', countryName: 'SA', account: 'A', name: 'one', ads: [{}] },
  { id: 'b', country: 'sa', countryName: 'SA', account: 'A', name: 'two', ads: [{}] },
]);
page = makePage({ draft: two, panels: [] });
clickOn(page, 'toggle', {}, 'a');
body = page.nodes['cbd-body'].innerHTML;
assert.ok(/data-camp="a" data-open="1"/.test(body), 'кампания не раскрылась');
clickOn(page, 'toggle', {}, 'b');
body = page.nodes['cbd-body'].innerHTML;
assert.equal((body.match(/data-open="1"/g) || []).length, 1, 'раскрыты две сразу — в узкой панели это каша');
assert.ok(/data-camp="b" data-open="1"/.test(body));
clickOn(page, 'toggle', {}, 'b');
assert.ok(!/data-open="1"/.test(page.nodes['cbd-body'].innerHTML), 'повторный клик не свернул');

// объявление добавляется в свою кампанию
clickOn(page, 'toggle', {}, 'a');
clickOn(page, 'add-ad', {}, 'a');
assert.equal(two.get('a').ads.length, 2, 'объявление не добавилось');
assert.equal(two.get('b').ads.length, 1, 'объявление уехало в чужую кампанию');

// и убирается: добавить, не имея как убрать, — ловушка
two.update('a', { ads: [{ name: 'первое' }, { name: 'второе' }, { name: 'третье' }] });
clickOn(page, 'drop-ad', { ad: '1' }, 'a');
assert.deepEqual(two.get('a').ads.map((x) => x.name), ['первое', 'третье'], 'убрали не то объявление');

// удаление спрашивает
page = makePage({ draft: two, panels: [], confirm: false });
clickOn(page, 'drop', {}, 'a');
assert.equal(two.count(), 2, 'кампанию удалили, несмотря на отказ');
assert.ok(/one/.test(page.confirms[0]), 'в вопросе не названо, что именно удаляют');
page = makePage({ draft: two, panels: [], confirm: true });
clickOn(page, 'drop', {}, 'a');
assert.equal(two.count(), 1, 'кампания не удалилась');

// кампания заводится под своим аккаунтом и сразу раскрыта
const empty = fakeDraft([]);
page = makePage({ draft: empty, panels: [panel('kw', ['KW_W72'])], names: { kw: 'Kuwait' } });
clickOn(page, 'add-here', { country: 'kw', countryName: 'Kuwait', account: 'KW_W72' });
assert.equal(empty.count(), 1);
assert.equal(empty.all()[0].account, 'KW_W72');
assert.ok(/data-open="1"/.test(page.nodes['cbd-body'].innerHTML), 'заведённую кампанию не раскрыли');

// потолок виден как потолок, а не как переставшая работать кнопка
const full = fakeDraft([]);
for (let i = 0; i < full.limit; i += 1) full.add({ country: 'kw', countryName: 'Kuwait', account: 'A' });
page = makePage({ draft: full, panels: [] });
clickOn(page, 'add-here', { country: 'kw', countryName: 'Kuwait', account: 'A' });
assert.equal(page.alerts.length, 1, 'о потолке не сказали');
assert.ok(/200/.test(page.alerts[0]), 'в сообщении не названо, сколько именно');

console.log('ok');
