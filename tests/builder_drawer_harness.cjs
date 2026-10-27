// Ящик билдера: открывается, закрывается, помнит — и не закрывается сам.
//
// Прежняя правая панель гасла от клика мимо себя. Из-за этого ею нельзя
// было пользоваться так, как задумано: выделяешь текст в таблице слева —
// панель захлопывается вместе с набранным. Тут проверяется, что новый
// ящик так не умеет.
//
// Код берётся живьём из шаблона, а не переписывается рядом: копия
// расходится с оригиналом молча.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

const start = html.indexOf('window.cbDrawer = (function () {');
assert(start > 0, 'window.cbDrawer не найден в шаблоне');
const tail = '\n})();';
const end = html.indexOf(tail, start) + tail.length;
const code = html.slice(start, end);
assert(code.includes('localStorage') && code.length < 12000, 'срез захватил не то');

// ── поддельная страница ───────────────────────────────────────────────
function makeElement(id) {
  return {
    id,
    attrs: {},
    title: '',
    textContent: '',
    hidden: false,
    inert: false,
    handlers: {},
    style: { props: {}, setProperty(k, v) { this.props[k] = v; }, getPropertyValue(k) { return this.props[k] || ''; } },
    classes: new Set(),
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; },
    addEventListener(name, fn) { (this.handlers[name] = this.handlers[name] || []).push(fn); },
    click() { this.fire('click', {}); },
    fire(name, ev) { (this.handlers[name] || []).forEach(function (fn) { fn(ev || {}); }); },
    classList: {
      toggle(name, on) { on ? this.owner.classes.add(name) : this.owner.classes.delete(name); },
      add(name) { this.owner.classes.add(name); },
      remove(name) { this.owner.classes.delete(name); },
    },
  };
}

function makePage(store, vw) {
  const ids = ['cbd-root', 'cbd-panel', 'cbd-handle', 'cbd-count', 'cbd-sub', 'cbd-close', 'cbd-preset-closed', 'cbd-preset-normal', 'cbd-preset-wide', 'cbd-body', 'cbd-foot'];
  const els = {};
  ids.forEach(function (id) {
    els[id] = makeElement(id);
    els[id].classList.owner = els[id];
  });
  els['cbd-root'].setAttribute('data-open', '0');
  els['cbd-root'].classList.owner = els['cbd-root'];

  const body = makeElement('body');
  body.classList.owner = body;

  const events = [];
  const documentHandlers = [];
  const document = {
    body,
    getElementById(id) { return els[id] || null; },
    querySelector() { return null; },
    addEventListener(name, fn) { documentHandlers.push(name); },
    dispatchEvent(e) { events.push(e); return true; },
    documentElement: { style: { props: {}, setProperty(k, v) { this.props[k] = v; } } },
  };

  const sandbox = {
    document,
    localStorage: store,
    CustomEvent: function (type, init) { this.type = type; this.detail = (init || {}).detail; },
    window: { innerWidth: vw || 1400, innerHeight: 900, addEventListener() {} },
  };
  vm.runInNewContext(code, sandbox);
  return { api: sandbox.window.cbDrawer, els, body, events, documentHandlers, doc: document };
}

function memoryStore(initial) {
  const data = Object.assign({}, initial);
  return {
    getItem(k) { return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null; },
    setItem(k, v) { data[k] = String(v); },
    data,
  };
}

// ── закрытый ящик ─────────────────────────────────────────────────────
let page = makePage(memoryStore());
assert.equal(page.api.isOpen(), false, 'без записи ящик должен быть закрыт');
assert.equal(page.els['cbd-handle'].getAttribute('aria-expanded'), 'false');
assert.equal(page.els['cbd-panel'].inert, true, 'закрытый ящик остаётся в обходе Tab');
assert.equal(page.body.classes.has('cbd-open'), false, 'страница отодвинута зря');

// ── язычок открывает и закрывает ──────────────────────────────────────
page.els['cbd-handle'].click();
assert.equal(page.api.isOpen(), true, 'язычок не открыл ящик');
assert.equal(page.els['cbd-handle'].getAttribute('aria-expanded'), 'true');
assert.equal(page.els['cbd-panel'].inert, false, 'открытый ящик недоступен с клавиатуры');
assert.equal(page.body.classes.has('cbd-open'), true, 'страница не освободила место');
page.els['cbd-handle'].click();
assert.equal(page.api.isOpen(), false, 'язычок не закрыл ящик');

// ── крестик закрывает ─────────────────────────────────────────────────
page.api.open();
page.els['cbd-close'].click();
assert.equal(page.api.isOpen(), false, 'крестик не закрыл ящик');

// ── ничего не слушается на документе ──────────────────────────────────
// То самое, из-за чего панель нельзя было использовать: клик мимо и
// Escape не должны иметь к ящику никакого отношения.
assert.deepEqual(page.documentHandlers, [], 'ящик слушает документ: ' + page.documentHandlers);
assert.ok(!/document\.addEventListener/.test(code), 'в коде ящика появился обработчик на документе');

// ── состояние переживает перезагрузку ─────────────────────────────────
const store = memoryStore();
page = makePage(store);
page.api.open();
assert.equal(store.data['acl.builder.open'], '1', 'открытость не записана');
let again = makePage(store);                      // как после F5
assert.equal(again.api.isOpen(), true, 'ящик забыл, что был открыт');
again.api.close();
assert.equal(store.data['acl.builder.open'], '0');
again = makePage(store);
assert.equal(again.api.isOpen(), false, 'ящик забыл, что был закрыт');

// ── закрытое хранилище не ломает ящик ─────────────────────────────────
// Приватное окно или запрет на данные сайта: не помнит, но работает.
const locked = {
  getItem() { throw new Error('denied'); },
  setItem() { throw new Error('denied'); },
};
page = makePage(locked);
assert.equal(page.api.isOpen(), false);
page.api.open();
assert.equal(page.api.isOpen(), true, 'ящик не открылся без хранилища');

// ── счётчик на язычке ─────────────────────────────────────────────────
page = makePage(memoryStore());
assert.equal(page.els['cbd-count'].hidden, true, 'пустой счётчик показан');
assert.equal(page.els['cbd-sub'].textContent, '');
page.api.setCount(1);
assert.equal(page.els['cbd-count'].hidden, false);
assert.equal(page.els['cbd-count'].textContent, '1');
assert.equal(page.els['cbd-sub'].textContent, '1 campaign', 'единственное число');
page.api.setCount(3);
assert.equal(page.els['cbd-sub'].textContent, '3 campaigns');
page.api.setCount(0);
assert.equal(page.els['cbd-count'].hidden, true, 'счётчик не спрятался на нуле');
assert.equal(page.api.count(), 0);

// ── о смене состояния можно узнать ────────────────────────────────────
// Куски, которые встанут в ящик позже, должны уметь на это подписаться.
page = makePage(memoryStore());
page.api.open();
assert.equal(page.events.length, 1, 'открытие не объявлено');
assert.equal(page.events[0].type, 'acl:builder-drawer');
assert.equal(page.events[0].detail.open, true);
page.api.open();
assert.equal(page.events.length, 1, 'повторное открытие объявлено зря');

// ── ACL-1871: размер с язычка ─────────────────────────────────────────
// Три кнопки (закрыть / обычная / широкая) и перетаскивание подписи.
const cssW = (pg) => pg.doc.documentElement.style.props['--cbd-w'];
const ptr = (id, x) => ({ pointerId: id, clientX: x, button: 0 });
const wide = memoryStore();
page = makePage(wide);
const h = page.els['cbd-handle'];
assert.equal(page.api.isExpanded(), false, 'без записи панель обычная');
assert.equal(cssW(page), '480px');
assert.equal(page.els['cbd-preset-closed'].getAttribute('aria-pressed'), 'true');
page.els['cbd-preset-wide'].click();
assert.equal(page.api.isOpen(), true, 'Wide не открыл ящик');
assert.equal(page.api.isExpanded(), true);
assert.equal(cssW(page), (1400 - 40 - 16) + 'px', 'wide — почти всё окно');
assert.equal(page.els['cbd-root'].getAttribute('data-expanded'), '1');
assert.equal(page.els['cbd-preset-wide'].getAttribute('aria-pressed'), 'true');
assert.equal(page.els['cbd-preset-normal'].getAttribute('aria-pressed'), 'false');
assert.equal(wide.data['acl.builder.width'], 'wide');
page.els['cbd-preset-closed'].click();
assert.equal(page.api.isOpen(), false);
assert.equal(page.els['cbd-preset-closed'].getAttribute('aria-pressed'), 'true');
assert.equal(wide.data['acl.builder.width'], 'wide', 'закрытие сбросило размер');
h.click();                                           // клик по подписи — вернуть как было
assert.equal(page.api.isOpen(), true);
assert.equal(page.api.isExpanded(), true, 'открылся не на прежней ширине');
page.els['cbd-preset-normal'].click();
assert.equal(cssW(page), '480px');
assert.equal(page.api.isExpanded(), false);
assert.equal(page.els['cbd-preset-normal'].getAttribute('aria-pressed'), 'true');
assert.equal(wide.data['acl.builder.width'], 'normal');

// перетаскивание: тянем влево — шире, клик после протяжки не закрывает
h.fire('pointerdown', ptr(1, 900));
h.fire('pointermove', ptr(1, 898));                  // дрожь руки — ещё не протяжка
assert.equal(cssW(page), '480px', 'дрожь изменила ширину');
h.fire('pointermove', ptr(1, 700));
assert.equal(cssW(page), '680px');
assert.equal(page.els['cbd-root'].classes.has('cbd-dragging'), true);
h.fire('pointerup', ptr(1, 700));
h.click();                                           // браузер шлёт click после pointerup
assert.equal(page.api.isOpen(), true, 'протяжка закончилась закрытием');
assert.equal(wide.data['acl.builder.width'], '680');
assert.equal(page.els['cbd-root'].classes.has('cbd-dragging'), false);
h.click();                                           // следующий клик — настоящий
assert.equal(page.api.isOpen(), false);
// пределы: не уже минимума и не шире окна
h.fire('pointerdown', ptr(2, 900));
h.fire('pointermove', ptr(2, 3000));
assert.equal(cssW(page), '320px', 'ниже минимума');
h.fire('pointermove', ptr(2, -9000));
assert.equal(cssW(page), (1400 - 40 - 16) + 'px', 'шире окна');
h.fire('pointerup', ptr(2, -9000));
assert.equal(page.api.isOpen(), true, 'протяжка закрытого ящика не открыла его');
h.click();                                           // этот click гасится
assert.equal(page.api.isOpen(), true);
// клавиатура
page.els['cbd-preset-normal'].click();
h.fire('keydown', { key: 'ArrowLeft', preventDefault() {} });
assert.equal(cssW(page), '520px');
h.fire('keydown', { key: 'ArrowRight', preventDefault() {} });
assert.equal(cssW(page), '480px');

// после F5 размер возвращается; мусор в записи и узкое окно не ломают
again = makePage(memoryStore({ 'acl.builder.width': '700' }));
assert.equal(cssW(again), '700px');
again = makePage(memoryStore({ 'acl.builder.width': '9999999' }));
assert.equal(cssW(again), '480px', 'мусор принят');
again = makePage(memoryStore({ 'acl.builder.width': 'abc' }));
assert.equal(cssW(again), '480px');
again = makePage(memoryStore({ 'acl.builder.width': '1200' }), 700);
assert.equal(cssW(again), (700 - 40 - 16) + 'px', 'сохранённая ширина переросла окно');
again = makePage(memoryStore(), 380);
assert.equal(cssW(again), '340px', 'обычная ширина не влезает в узкое окно');
// закрытое хранилище
page = makePage(locked);
page.api.expand();
assert.equal(page.api.isExpanded(), true, 'без хранилища растянуть нельзя');
assert.deepEqual(page.documentHandlers, [], 'ящик слушает документ');

console.log('ok');
