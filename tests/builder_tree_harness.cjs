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

// ACL-1769: раскладка — чистые функции, которыми дерево пользуется.
const lstart = html.indexOf('window.cbLayout = (function () {');
assert(lstart > 0, 'window.cbLayout не найден в шаблоне');
const layoutCode = html.slice(lstart, html.indexOf(tail, lstart) + tail.length);

// ACL-1819: правила pre-flight audit — чистая страница-часть и таблица с сервера.
const pstart = html.indexOf('window.cbPreflight = (function () {');
assert(pstart > 0, 'window.cbPreflight не найден в шаблоне');
const preflightCode = html.slice(pstart, html.indexOf(tail, pstart) + tail.length);
const RULES = JSON.parse(fs.readFileSync(require('path').join(__dirname, '..', 'src', 'fb_api', 'preflight_rules.json'), 'utf8'));

// ── поддельный DOM ────────────────────────────────────────────────────
// Селекторы поддерживаются ровно те, что дерево использует; встретив
// незнакомый, стуб ругается, а не возвращает молча null.

const { el, parseSel } = require('./fake_dom.cjs');

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
      // Место под форму подключения — такой же настоящий узел: дерево
      // кладёт её туда после перерисовки.
      node._connectSlot = /cbd-connect-slot/.test(v)
        ? el('div', { classes: ['cbd-connect-slot'] }) : null;
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
    if (sel === '.cbd-connect-slot') return node._connectSlot || null;
    const hit = node._connectSlot && node._connectSlot.querySelector(sel);
    return hit || rawQS(sel);
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
  // Карта аккаунтов: у каждой страны своя форма подключения. Ящик берёт
  // её оттуда, а не заводит третью копию.
  const maps = spec.map((c) => el('details', {
    classes: ['amap-country'], dataset: {countryName:c.name},
    children: [
      el('span', { classes: ['amap-cell-name'], textContent: c.name }),
      el('div', { classes: ['amap-add'], dataset: { amapCountry: c.name } }),
    ],
  }));
  return { panels, sideBtns, forms, maps };
}

function boot(spec, opts = {}) {
  const p = page(spec);
  const bodyNode = sink('cbd-body');
  const footNode = sink('cbd-foot');
  const panelNode = el('div', { classes: ['cbd-panel'] });
  const editorNode = el('div', { classes: ['cbd-col-edit'] });
  const root = el('div', { children: p.panels.concat(p.sideBtns, p.maps) });
  const ids = { 'cbd-body': bodyNode, 'cbd-foot': footNode, 'cbd-panel': panelNode, 'cbd-editor': editorNode };
  const stored = Object.assign({}, opts.stored || {});
  if (opts.open) stored['acl.builder.open-campaign'] = opts.open;

  const timers = [];
  const counts = [];
  const confirms = [];
  const alerts = [];
  const sent = [];
  const document = {
    body: root,
    getElementById: (id) => ids[id] || null,
    querySelectorAll: (sel) => root.querySelectorAll(sel),
    querySelector: (sel) => root.querySelector(sel),
    addEventListener: (name, fn) => ((document._on = document._on || {})[name] = (document._on[name] || []).concat(fn)),
    fire: (name, e) => ((document._on || {})[name] || []).forEach((fn) => fn(e)),
    createElement: (tag) => el(tag),
    createComment: () => el('#comment'),
  };

  const sandbox = {
    document,
    localStorage: { getItem: (k) => (k in stored ? stored[k] : null), setItem: (k, v) => { stored[k] = String(v); } },
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
      alert: (m) => alerts.push(m),
      amapSend: (body) => sent.push(body),
    },
    Array, Object, String, JSON, Math, clearTimeout() {},
  };
  sandbox.window.window = sandbox.window;
  sandbox.window.CB_PREFLIGHT_RULES = RULES;
  vm.runInNewContext([layoutCode, preflightCode, code].join(';' + String.fromCharCode(10)), sandbox);
  return { api: sandbox.window.cbTree, body: bodyNode, foot: footNode, panel: panelNode, editor: editorNode,
           stored, p, counts, confirms, alerts, sent, timers, sandbox, document };
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
const added = t.sandbox.window.cbAddSlot(form);   // как «+ Add Campaign» на странице
assert.ok(t.sandbox._mo, 'за формами никто не следит');
t.sandbox._mo._fn([{ type: 'childList', target: form, addedNodes: [added], removedNodes: [] }]);
t.timers.forEach((fn) => fn());             // перерисовка отложена — выполняем
assert.equal((t.body.innerHTML.match(/class="cbd-camp"/g) || []).length, 4,
  'кампания, заведённая на странице, в ящике не появилась');

// ── набор текста НЕ перерисовывает дерево ─────────────────────────────
// Перерисовка вынимает форму из документа и возвращает обратно, а это
// снимает фокус: вводишь букву — и тебя выбрасывает из поля.
t = boot(SPEC);
let openId = /data-camp="([^"]+)"/.exec(t.body.innerHTML)[1];
t.api.open(openId);
const hosted = t.p.forms[0];
const nameNode = hosted.querySelector('[data-name-display]');
const timersBefore = t.timers.length;
nameNode.textContent = 'SA_new_name';       // ровно то, что делает cbUpdateName
t.sandbox._mo._fn([{ type: 'characterData', target: nameNode, addedNodes: [], removedNodes: [] }]);
assert.equal(t.timers.length, timersBefore, 'набор текста поставил дерево на перерисовку');
assert.equal(hosted.parentNode.matches('.cbd-host'), true, 'форму вынули из ящика на вводе буквы');
// Но подпись строки всё равно обновилась — на месте, без перерисовки.
assert.equal(t.body.querySelector('.cbd-camp[data-camp="' + openId + '"]')._html, undefined);

// А вот появление карточки внутри ящика дерево перестроить обязано.
const inDrawerSlot = t.sandbox.window.cbAddSlot(hosted);
t.sandbox._mo._fn([{ type: 'childList', target: hosted, addedNodes: [inDrawerSlot], removedNodes: [] }]);
assert.ok(t.timers.length > timersBefore, 'новая карточка в ящике не перестроила дерево');

// ── раскрытая кампания получает настоящую форму ───────────────────────
t = boot(SPEC);
let id = /data-camp="([^"]+)"/.exec(t.body.innerHTML)[1];
t.api.open(id);
const card = t.body.querySelector('.cbd-camp[data-camp="' + id + '"]');
assert.ok(card, 'выбранной карточки нет в дереве');
// ACL-1769: форма живёт в правой колонке, а не в карточке дерева.
const host = t.editor.children.find((c) => c.matches('.cbd-host'));
assert.ok(host, 'в правой колонке нет места под форму');
assert.equal(card.children.length, 0, 'форма осталась в карточке дерева');
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

// ── подключить аккаунт, не уходя из ящика ─────────────────────────────
// Третьей копии формы подключения нет: их было две, они разошлись, и
// сведение в одну было отдельной задачей. Ящик берёт ту самую форму из
// карты аккаунтов и возвращает её на место.
t = boot(SPEC);
const mapBlock = t.p.maps[0].querySelector('.amap-add');
const mapDetails = t.p.maps[0];

function clickAct(page, act, attrs) {
  const hit = { dataset: Object.assign({ act }, attrs || {}),
                closest: (sel) => (sel === '.cbd-camp' ? null : hit) };
  page.panel.fire('click', { target: { closest: (sel) => (sel === '[data-act]' ? hit : null) } });
}

clickAct(t, 'connect', { country: 'sa', countryName: 'Saudi Arabia' });
const connectHost = t.body.querySelector('.cbd-connect-host');
assert.ok(connectHost, 'форме подключения негде встать в ящике');
assert.equal(connectHost.querySelector('.amap-add'), mapBlock, 'в ящик приехала не та форма');
assert.equal(mapBlock.parentNode, connectHost, 'форма осталась в карте');

// Закрыли — вернулась туда, откуда взяли, на своё место.
clickAct(t, 'connect', { country: 'sa', countryName: 'Saudi Arabia' });
assert.equal(mapBlock.parentNode, mapDetails, 'форма не вернулась в карту');
assert.equal(t.body.querySelector('.cbd-connect-host'), null);

// Страна, которой нет в карте, — честный отказ, а не пустая панель.
t = boot(SPEC);
clickAct(t, 'connect', { country: 'sa', countryName: 'Страны-нет' });
assert.ok(/not on the account map/.test(t.alerts[0] || ''), 'о ненайденной стране не сказали');
assert.equal(t.body.querySelector('.cbd-connect-host'), null, 'осталась пустая панель подключения');

// Открыта одна форма подключения за раз.
t = boot(SPEC);
clickAct(t, 'connect', { country: 'sa', countryName: 'Saudi Arabia' });
clickAct(t, 'connect', { country: 'kw', countryName: 'Kuwait' });
assert.equal(t.p.maps[0].querySelector('.amap-add'), mapBlockOf(t, 0), 'первая форма не вернулась');
assert.ok(t.body.querySelector('.cbd-connect-host').querySelector('.amap-add'), 'вторая не приехала');

function mapBlockOf(page, i) { return page.p.maps[i].querySelector('.amap-add'); }

// ── завести страну — тем же отправителем, что и карта ─────────────────
// Свой запрос завёл бы второе место, где решается, что значит «завести
// страну».
t = boot(SPEC);
clickAct(t, 'add-country');
assert.equal(t.sent.length, 0, 'пустое имя ушло на сервер');

console.log('ok (connect)');

// ── ACL-1769: две колонки ─────────────────────────────────────────────
// Слева дерево из имён, справа редактор выбранного с переключателем
// Campaign / Ad set / Ad. Форма одна: редактор лишь показывает её раздел.
t = boot(SPEC);
let h = t.body.innerHTML;
assert.ok(/data-act="names-only"[^>]*aria-pressed="false"/.test(h), 'нет переключателя «Names only»');
assert.equal((h.match(/class="cbd-acc-add"/g) || []).length, 2, '«+» не у каждого аккаунта');
assert.ok(/class="cbd-acc-add" data-act="add-here" data-country="sa" data-account="DQ438"/.test(h), '«+» не знает своего аккаунта');
clickAct(t, 'names-only');
assert.equal(t.stored['acl.builder.names-only'], '1', '«Names only» не запомнен');
assert.equal(t.body.dataset.namesOnly, '1');
assert.equal(t.panel.dataset.namesOnly, '1', '«Names only» не свернул редакторы (атрибут панели)');
assert.ok(/aria-pressed="true"/.test(t.body.innerHTML));
// …и переживает перезагрузку страницы
assert.equal(boot(SPEC, { stored: t.stored }).body.dataset.namesOnly, '1', '«Names only» забыт после F5');

// Аккаунт сворачивается до одной строки, и это помнится.
clickAct(t, 'acc-toggle', { key: 'sa|DQ438' });
assert.deepEqual(JSON.parse(t.stored['acl.builder.folded-accounts']), ['sa|DQ438']);
assert.ok(/data-acc="sa\|DQ438" data-collapsed="1"/.test(t.body.innerHTML), 'аккаунт не свёрнут');
assert.ok(/data-acc="kw\|KW_W72" data-collapsed="0"/.test(t.body.innerHTML), 'свернулся чужой аккаунт');
clickAct(t, 'acc-toggle', { key: 'sa|DQ438' });
assert.deepEqual(JSON.parse(t.stored['acl.builder.folded-accounts']), [], 'аккаунт не раскрылся');

// Статус аккаунта — из того, что страница знает сегодня.
t = boot(SPEC);
t.sandbox.window.ACCOUNT_PREFLIGHT = {
  DQ438: { link_status: 'tracking', missing_map_fields: [] },
  KW_W72: { link_status: 'jira', missing_map_fields: ['profile_id'] },
};
t.api.paint();
h = t.body.innerHTML;
assert.ok(/cbd-ind cbd-acc-state cbd-acc-st-ok" title="Account is configured"/.test(h), 'настроенный аккаунт не отмечен');
assert.ok(/cbd-acc-st-warn" title="Needs setup: tracking link, profile_id"/.test(h), 'чего не хватает аккаунту — не сказано');

// Справа пусто, пока ничего не выбрано.
assert.equal(t.editor.children.filter((c) => c.matches('.cbd-host')).length, 0, 'форма в редакторе без выбора');

// Выбранная кампания: переключатель уровней и один раздел формы.
t = boot(SPEC);
id = /data-camp="([^"]+)"/.exec(t.body.innerHTML)[1];
t.api.open(id);
const hostEl = t.editor.children.find((c) => c.matches('.cbd-host'));
assert.equal(hostEl.dataset.level, 'campaign', 'по умолчанию — раздел кампании');
let head = t.editor.children.find((c) => c.matches('.cbd-ed-head'));
assert.equal(head.hidden, false);
for (const lv of ['campaign', 'adset', 'ad']) assert.ok(head.innerHTML.includes('data-level="' + lv + '"'), 'нет уровня ' + lv);
assert.ok(/data-level="campaign"[^>]*aria-selected="true"/.test(head.innerHTML));

// Ad set (ACL-1768): выбор набора, «+ Ad set», «Copy ad set» - рабочие кнопки, не заглушки.
clickAct(t, 'level', { level: 'adset' });
assert.equal(hostEl.dataset.level, 'adset');
assert.equal(t.stored['acl.builder.level'], 'adset', 'уровень не запомнен');
assert.ok(/data-act="add-adset"/.test(head.innerHTML) && /data-act="copy-adset"/.test(head.innerHTML), 'нет кнопок набора');
assert.ok(!/data-future="adsets"/.test(head.innerHTML), 'набор всё ещё заглушка');
assert.ok(/data-act="remove-adset"[^>]* disabled/.test(head.innerHTML), 'единственный набор нельзя убрать');

// Ad: выбора объявления нет, все карточки набора идут одним списком (владелец, 13.10).
clickAct(t, 'level', { level: 'ad' });
head = t.editor.children.find((c) => c.matches('.cbd-ed-head'));
assert.ok(!head.innerHTML.includes('cbd-ed-ad') && !head.innerHTML.includes('<select'), 'выбор объявления остался');
assert.ok(!head.innerHTML.includes('<option '), 'в шапке остался список объявлений');
const slotNode = t.p.forms[0].querySelectorAll('.cb-slot')[0];
const rows = slotNode.querySelectorAll('.cb-ad-row');
assert.equal(rows.length, 2, 'строка объявления выброшена из формы');
assert.deepEqual(rows.map((r) => r.classList.contains('cbd-ad-off')), [false, false], 'карточка скрыта');
// Добавили объявление — карточка в списке, ни одна не скрыта.
slotNode.appendChild(el('div', { classes: ['cb-ad-row'] }));
t.api.refreshEditor();
assert.deepEqual(slotNode.querySelectorAll('.cb-ad-row').map((r) => r.classList.contains('cbd-ad-off')), [false, false, false], 'после добавления часть карточек скрыта');
// Убрали объявление — остальные на месте и видны.
slotNode.querySelectorAll('.cb-ad-row')[1].remove();
t.api.refreshEditor();
assert.deepEqual(slotNode.querySelectorAll('.cb-ad-row').map((r) => r.classList.contains('cbd-ad-off')), [false, false]);

// Мусорный уровень — кампания; запомненный уровень поднимается при загрузке.
t.api.setLevel('nonsense');
assert.equal(t.api.level(), 'campaign');
assert.equal(boot(SPEC, { stored: { 'acl.builder.level': 'ad' } }).api.level(), 'ad', 'запомненный уровень не поднялся');
assert.equal(boot(SPEC, { stored: { 'acl.builder.level': 'zzz' } }).api.level(), 'campaign');

// Уровень с ошибкой помечен «!» на своей вкладке.
t = boot(SPEC);
id = /data-camp="([^"]+)"/.exec(t.body.innerHTML)[1];
t.api.open(id);
const bad = el('input', { classes: ['cb-field-error'] });
const adSection = el('section', { classes: ['cb-lvl', 'cb-lvl-ad'], children: [bad] });
t.p.forms[0].querySelectorAll('.cb-slot')[0].appendChild(adSection);
t.api.refreshEditor();
head = t.editor.children.find((c) => c.matches('.cbd-ed-head'));
assert.ok(/class="cbd-lv-bad"[^>]*>Ad</.test(head.innerHTML), 'ошибка в разделе Ad не видна на вкладке');
assert.ok(!/class="cbd-lv-bad"[^>]*>Campaign</.test(head.innerHTML), 'ошибка приписана не тому уровню');

// Закрыли выбор — форма вернулась на страницу, справа снова пусто.
t.api.close();
assert.equal(t.editor.children.filter((c) => c.matches('.cbd-host')).length, 0, 'форма осталась в редакторе после закрытия');
console.log('ok (two columns)');

// ── ACL-1769: выбор кампании выводит из «Names only» ───────────────────
t = boot(SPEC);
clickAct(t, 'names-only');
assert.equal(t.panel.dataset.namesOnly, '1');
const pickId = /data-camp="([^"]+)"/.exec(t.body.innerHTML)[1];
const rowHit = { dataset: { act: 'toggle' }, closest: () => ({ dataset: { camp: pickId } }) };
t.panel.fire('click', { target: { closest: (s) => (s === '[data-act]' ? rowHit : null) } });
assert.equal(t.panel.dataset.namesOnly, '0', 'выбор кампании оставил «Names only» включённым — редактору негде показаться');
assert.equal(t.stored['acl.builder.names-only'], '0');
assert.ok(t.editor.children.some((c) => c.matches('.cbd-host')), 'кампания не открылась');
console.log('ok (names only exits on select)');

// ── ACL-1819: карточка pre-flight audit, значок в дереве и Publish ─────────
t = boot(SPEC);
id = /data-camp="([^"]+)"/.exec(t.body.innerHTML)[1];
t.api.open(id);
let pfCard = t.editor.children.find((c) => c.matches('.cbd-pf'));
assert.ok(pfCard && !pfCard.hidden, 'карточка Pre-flight audit не показана у выбранной кампании');
assert.ok(/Pre-flight audit/.test(pfCard.innerHTML) && /errors: \d+/.test(pfCard.innerHTML), 'нет заголовка или значка errors: N');
assert.ok(/Account checks need the API backend/.test(pfCard.innerHTML), 'под RPA нет подписи про серверные проверки');
assert.ok(!/data-act="pf-check"/.test(pfCard.innerHTML), 'кнопка Check под RPA не нужна');
assert.ok(/data-act="pf-focus"[^>]*data-rule="link_https"/.test(pfCard.innerHTML), 'строка не открывает поле по клику');
assert.ok(/cbd-pf-error/.test(pfCard.innerHTML) && /cbd-pf-sign/.test(pfCard.innerHTML), 'нет знака ошибки');
// Фейковая кампания пуста (нет ссылки, файла, текста): Publish all держится выключенным и говорит почему.
const pfGate = t.api.gateState();
assert.equal(pfGate.blocked, true, 'кампания с ошибками не блокирует Publish');
assert.ok(/^Publish is off: /.test(pfGate.reason), pfGate.reason);
assert.equal(t.sandbox.window.CB_PF_GATE.blocked, true);
// Закрыли — карточки нет.
t.api.close();
pfCard = t.editor.children.find((c) => c.matches('.cbd-pf'));
assert.ok(pfCard && pfCard.hidden, 'карточка осталась после закрытия кампании');
console.log('ok (pre-flight audit)');
