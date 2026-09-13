// Дерево в ящике — отражение страницы, а не собственный список.
//
// Кампания, заведённая кнопкой «+ Add Campaign» на странице страны, и
// кампания, заведённая в ящике, — одна и та же карточка в одной и той же
// форме. Пока ящик вёл свою бухгалтерию, половина работы была ему не видна.
//
// Раскрытая кампания получает НАСТОЯЩУЮ форму аккаунта, перенесённую в
// ящик целиком. Целиком — потому что браузер собирает данные формы только
// из того, что лежит внутри её тега: карточка, вынутая наружу, ушла бы в
// публикацию без единого своего поля, молча.
//
// Код берётся живьём из шаблона. DOM здесь поддельный, но настоящий по
// поведению: узлы, селекторы, перенос между родителями — ровно то, на что
// дерево опирается.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

const start = html.indexOf('window.cbTree = (function () {');
assert(start > 0, 'window.cbTree не найден в шаблоне');
const tail = '\n})();';
const code = html.slice(start, html.indexOf(tail, start) + tail.length);
assert(code.includes('cbd-country') && code.includes('realTree'), 'срез захватил не то');

// ── поддельный DOM ────────────────────────────────────────────────────
// Селекторы поддерживаются ровно те, что дерево использует; встретив
// незнакомый, стуб ругается, а не возвращает молча null.

const SEL = /^([a-z]*)((?:\.[-\w]+)*)(?:\[([-\w]+)(?:="([^"]*)")?\])?$/;

function parseSel(sel) {
  const m = SEL.exec(sel.trim());
  assert.ok(m, 'стуб не знает такого селектора: ' + sel);
  return {
    tag: m[1] || '',
    classes: (m[2] || '').split('.').filter(Boolean),
    attr: m[3] || '',
    value: m[4],
  };
}

function el(tag, opts = {}) {
  const node = {
    tag,
    dataset: opts.dataset || {},
    hidden: false,
    value: opts.value !== undefined ? opts.value : '',
    textContent: opts.textContent || '',
    children: [],
    parentNode: null,
    _classes: new Set(opts.classes || []),
    classList: {
      add: (c) => node._classes.add(c),
      remove: (c) => node._classes.delete(c),
      contains: (c) => node._classes.has(c),
      toggle: (c, on) => (on === undefined ? (node._classes.has(c) ? node._classes.delete(c) : node._classes.add(c))
                                           : (on ? node._classes.add(c) : node._classes.delete(c))),
    },
    matches(sel) {
      const s = parseSel(sel);
      if (s.tag && s.tag !== node.tag) return false;
      if (!s.classes.every((c) => node._classes.has(c))) return false;
      if (s.attr) {
        const key = s.attr.replace(/^data-/, '').replace(/-(\w)/g, (_, c) => c.toUpperCase());
        const has = s.attr.startsWith('data-') ? node.dataset[key] !== undefined : node[s.attr] !== undefined;
        if (!has) return false;
        if (s.value !== undefined) {
          const got = s.attr.startsWith('data-') ? node.dataset[key] : node[s.attr];
          if (String(got) !== s.value) return false;
        }
      }
      return true;
    },
    querySelectorAll(sel) {
      const out = [];
      (function walk(n) {
        n.children.forEach((c) => { if (c.matches(sel)) out.push(c); walk(c); });
      })(node);
      return out;
    },
    querySelector(sel) { return node.querySelectorAll(sel)[0] || null; },
    appendChild(child) {
      if (child.parentNode) child.parentNode.removeChild(child);
      child.parentNode = node;
      node.children.push(child);
      return child;
    },
    removeChild(child) {
      node.children = node.children.filter((c) => c !== child);
      child.parentNode = null;
      return child;
    },
    insertBefore(child, ref) {
      if (child.parentNode) child.parentNode.removeChild(child);
      const i = node.children.indexOf(ref);
      node.children.splice(i === -1 ? node.children.length : i, 0, child);
      child.parentNode = node;
      return child;
    },
    remove() { if (node.parentNode) node.parentNode.removeChild(node); },
    addEventListener(name, fn) { (node._on = node._on || {})[name] = (node._on[name] || []).concat(fn); },
    fire(name, e) { ((node._on || {})[name] || []).forEach((fn) => fn(e)); },
    closest(sel) {
      let n = node;
      while (n) { if (n.matches && n.matches(sel)) return n; n = n.parentNode; }
      return null;
    },
  };
  // `className = '...'` — обычный способ задать класс на созданном узле;
  // без него classList остаётся пустым, и узел не находится ни одним
  // селектором.
  Object.defineProperty(node, 'className', {
    get: () => [...node._classes].join(' '),
    set: (v) => { node._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  (opts.children || []).forEach((c) => node.appendChild(c));
  return node;
}

// Узел, куда дерево пишет разметку строкой. Разбирать её незачем — карточки
// адресуются по id, а он в строке есть.
function sink(id) {
  const node = el('div', { classes: [id] });
  node.id = id;
  node._cards = new Map();
  Object.defineProperty(node, 'innerHTML', {
    get() { return node._html || ''; },
    set(v) {
      node._html = v;
      node.children.slice().forEach((c) => node.removeChild(c));
      node._cards = new Map();
      for (const m of v.matchAll(/data-camp="([^"]+)"/g)) {
        node._cards.set(m[1], el('div', { classes: ['cbd-camp'], dataset: { camp: m[1] } }));
      }
      // Выбор внизу — два настоящих узла: второй заполняется отдельно, и
      // без него проверка «какие аккаунты предлагаются» ничего не видит.
      node._picks = /cbd-pick-country/.test(v)
        ? { '.cbd-pick-country': el('select', { classes: ['cbd-pick-country'] }),
            '.cbd-pick-account': el('select', { classes: ['cbd-pick-account'] }) }
        : null;
      if (node._picks) {
        const opt = /<option value="([^"]*)"(?: selected)?>/.exec(v);
        node._picks['.cbd-pick-country'].value = opt ? opt[1] : '';
      }
    },
  });
  const rawQS = node.querySelector;
  node.querySelector = (sel) => {
    const m = /^\.cbd-camp\[data-camp="([^"]+)"\]$/.exec(sel);
    if (m) return node._cards.get(m[1]) || null;
    if (sel === '.cbd-pick-country' || sel === '.cbd-pick-account') return node._picks?.[sel] || null;
    return rawQS(sel);
  };
  return node;
}

function slot(name, ads) {
  const kids = [
    el('input', { classes: ['cb-name-hidden'], value: name }),
    el('span', { dataset: { nameDisplay: '' }, textContent: name }),
  ];
  for (let i = 0; i < ads; i += 1) kids.push(el('div', { classes: ['cb-ad-row'] }));
  return el('div', { classes: ['cb-slot'], children: kids });
}

function page(spec) {
  // spec: [{country, name, accounts: [{label, slots: [[name, ads], ...]}]}]
  const panels = [], sideBtns = [], forms = [];
  spec.forEach((c, ci) => {
    const rows = c.accounts.map((a) => {
      const form = el('form', {
        classes: ['open-account-form'],
        children: a.slots.map((s) => slot(s[0], s[1])),
      });
      forms.push(form);
      return el('div', { classes: ['cb-acc-row'], dataset: { accLabel: a.label }, children: [form] });
    });
    const panel = el('div', {
      classes: ['view-panel'].concat(ci === 0 ? ['active'] : []),
      dataset: { country: c.country },
      children: rows,
    });
    panels.push(panel);
    sideBtns.push(el('button', { classes: ['sidebar-country-btn'], dataset: { country: c.country }, textContent: c.name }));
  });
  return { panels, sideBtns, forms };
}

function boot(spec, opts = {}) {
  const p = page(spec);
  const bodyNode = sink('cbd-body');
  const footNode = sink('cbd-foot');
  const panelNode = el('div', { classes: ['cbd-panel'] });
  const root = el('div', { children: p.panels.concat(p.sideBtns) });
  const ids = { 'cbd-body': bodyNode, 'cbd-foot': footNode, 'cbd-panel': panelNode };

  const timers = [];
  const counts = [];
  const confirms = [];
  const document = {
    getElementById: (id) => ids[id] || null,
    querySelectorAll: (sel) => root.querySelectorAll(sel),
    querySelector: (sel) => root.querySelector(sel),
    addEventListener: (name, fn) => ((document._on = document._on || {})[name] = (document._on[name] || []).concat(fn)),
    fire: (name, e) => ((document._on || {})[name] || []).forEach((fn) => fn(e)),
    createElement: (tag) => el(tag),
  };

  const sandbox = {
    document,
    localStorage: { getItem: () => opts.open || null, setItem() {} },
    aclEscHtml: (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout() {},
    MutationObserver: function (fn) { this.observe = () => { sandbox._observed = (sandbox._observed || 0) + 1; }; this._fn = fn; sandbox._mo = this; },
    window: {
      innerHeight: 900,
      addEventListener() {},
      cbDrawer: { setCount: (n) => counts.push(n) },
      confirm: (m) => { confirms.push(m); return opts.confirm !== false; },
      aclFindAccountRow: (country, label) => {
        const panel = p.panels.find((x) => x.dataset.country === country);
        return panel ? panel.querySelector('.cb-acc-row[data-acc-label="' + label + '"]') : null;
      },
      cbAddSlot: (form) => form.appendChild(slot('', 1)),
      cbRemoveSlot: (node) => node.closest('.cb-slot').remove(),
    },
    Array, Object, String, JSON, Math, clearTimeout() {},
  };
  sandbox.window.window = sandbox.window;
  vm.runInNewContext(code, sandbox);
  return { api: sandbox.window.cbTree, body: bodyNode, foot: footNode, panel: panelNode,
           p, counts, confirms, timers, sandbox, document };
}

const SPEC = [
  { country: 'sa', name: 'Saudi Arabia', accounts: [
      { label: 'DQ438', slots: [['SA_one', 2], ['', 1]] },
      { label: 'KLD_X84', slots: [] },
  ] },
  { country: 'kw', name: 'Kuwait', accounts: [
      { label: 'KW_W72', slots: [['KW_one', 1]] },
  ] },
];

// ── дерево показывает то, что есть на странице ────────────────────────
let t = boot(SPEC);
let body = t.body.innerHTML;
assert.ok(/Saudi Arabia/.test(body) && /Kuwait/.test(body), 'страны не показаны');
assert.equal((body.match(/class="cbd-camp"/g) || []).length, 3, 'карточек не три');
assert.ok(/SA_one/.test(body) && /KW_one/.test(body), 'имена кампаний не со страницы');
assert.ok(/Untitled campaign/.test(body), 'безымянная кампания названа пустотой');
assert.ok(/2 ads/.test(body) && /1 ad</.test(body), 'объявления считаются не по странице');

// Аккаунт, в котором ничего не собирается, в дереве не нужен: их два
// десятка, и стена пустых строк прячет две кампании, ради которых открыли.
assert.ok(!/KLD_X84/.test(body), 'пустой аккаунт занял место в дереве');
// Но завести в нём кампанию можно — через выбор внизу, где перечислены все.
const sa = t.api.places().find((p) => p.country === 'sa');
assert.deepEqual(sa.accounts, ['DQ438', 'KLD_X84'], 'пустой аккаунт нельзя выбрать внизу');
assert.equal(sa.name, 'Saudi Arabia', 'имя страны не взято с боковой панели');

// ── счётчик на язычке считает настоящие карточки ──────────────────────
assert.equal(t.counts[t.counts.length - 1], 3);

// ── ничего не собирается ──────────────────────────────────────────────
let empty = boot([{ country: 'sa', name: 'Saudi Arabia', accounts: [{ label: 'DQ438', slots: [] }] }]);
assert.ok(/Nothing is being assembled yet/.test(empty.body.innerHTML));
assert.ok(/cbd-pick-country/.test(empty.foot.innerHTML), 'начать первую кампанию нечем');

// ── заведённое на странице видно в ящике без единого клика в ящике ────
t = boot(SPEC);
const form = t.p.forms[0];
t.sandbox.window.cbAddSlot(form);          // как «+ Add Campaign» на странице
assert.ok(t.sandbox._mo, 'за формами никто не следит');
t.sandbox._mo._fn();                        // наблюдатель сообщает об изменении
t.timers.forEach((fn) => fn());             // перерисовка отложена — выполняем
assert.equal((t.body.innerHTML.match(/class="cbd-camp"/g) || []).length, 4,
  'кампания, заведённая на странице, в ящике не появилась');

// ── раскрытая кампания получает настоящую форму ───────────────────────
t = boot(SPEC);
let id = /data-camp="([^"]+)"/.exec(t.body.innerHTML)[1];
t.api.open(id);
const card = t.body.querySelector('.cbd-camp[data-camp="' + id + '"]');
const host = card.children[0];
assert.ok(host, 'в раскрытой карточке нет места под форму');
assert.equal(host.querySelector('form.open-account-form'), t.p.forms[0], 'в ящик приехала не та форма');
assert.equal(t.p.forms[0].parentNode, host, 'форма осталась на странице');

// на её месте — заметка, куда она делась
const row = t.p.panels[0].querySelector('.cb-acc-row[data-acc-label="DQ438"]');
assert.ok(row.querySelector('.cb-parked-note'), 'на странице не сказано, куда делась форма');

// показана одна кампания, остальные скрыты — но не выброшены: они
// по-прежнему кампании этого аккаунта и по-прежнему уйдут в публикацию
const slots = t.p.forms[0].querySelectorAll('.cb-slot');
assert.equal(slots.length, 2, 'скрытая кампания потерялась');
assert.equal(slots.filter((s) => !s.hidden).length, 1, 'показана не одна кампания');

// ── закрыли — форма вернулась на место ────────────────────────────────
t.api.close();
assert.equal(t.p.forms[0].parentNode, row, 'форма не вернулась в свой блок');
assert.equal(row.querySelector('.cb-parked-note'), null, 'заметка осталась после возврата');
assert.equal(t.p.forms[0].querySelectorAll('.cb-slot').filter((s) => s.hidden).length, 0,
  'кампании остались скрытыми на странице');

// ── раскрыта одна за раз ──────────────────────────────────────────────
t = boot(SPEC);
const ids = [...t.body.innerHTML.matchAll(/data-camp="([^"]+)"/g)].map((m) => m[1]);
t.api.open(ids[0]);
t.api.open(ids[2]);                        // кампания другого аккаунта
assert.equal(t.p.forms[0].parentNode, t.p.panels[0].querySelector('.cb-acc-row[data-acc-label="DQ438"]'),
  'первая форма не вернулась, когда раскрыли вторую');
assert.equal(t.p.panels[0].querySelectorAll('.cb-parked-note').length
  + t.p.panels[1].querySelectorAll('.cb-parked-note').length, 1, 'заметок больше одной');

// ── удаление спрашивает и называет ────────────────────────────────────
t = boot(SPEC, { confirm: false });
t.panel.fire('click', { target: { closest: (s) => (s === '[data-act]' ? { dataset: { act: 'drop' }, closest: () => ({ dataset: { camp: ids[0] } }) } : null) } });
assert.ok(/SA_one/.test(t.confirms[0] || ''), 'в вопросе не названо, что удаляют');
assert.equal(t.p.forms[0].querySelectorAll('.cb-slot').length, 2, 'удалили, несмотря на отказ');

console.log('ok');
