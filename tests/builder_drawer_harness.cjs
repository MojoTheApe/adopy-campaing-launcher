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
assert(code.includes('localStorage') && code.length < 7000, 'срез захватил не то');

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
    classes: new Set(),
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; },
    addEventListener(name, fn) { (this.handlers[name] = this.handlers[name] || []).push(fn); },
    click() { (this.handlers.click || []).forEach(function (fn) { fn(); }); },
    classList: {
      toggle(name, on) { on ? this.owner.classes.add(name) : this.owner.classes.delete(name); },
    },
  };
}

function makePage(store) {
  const ids = ['cbd-root', 'cbd-panel', 'cbd-handle', 'cbd-count', 'cbd-sub', 'cbd-close', 'cbd-expand', 'cbd-body', 'cbd-foot'];
  const els = {};
  ids.forEach(function (id) {
    els[id] = makeElement(id);
    els[id].classList.owner = els[id];
  });
  els['cbd-root'].setAttribute('data-open', '0');
  els['cbd-expand'].textContent = 'Expand';

  const body = makeElement('body');
  body.classList.owner = body;

  const events = [];
  const documentHandlers = [];
  const document = {
    body,
    getElementById(id) { return els[id] || null; },
    addEventListener(name, fn) { documentHandlers.push(name); },
    dispatchEvent(e) { events.push(e); return true; },
  };

  const sandbox = {
    document,
    localStorage: store,
    CustomEvent: function (type, init) { this.type = type; this.detail = (init || {}).detail; },
    window: {},
  };
  vm.runInNewContext(code, sandbox);
  return { api: sandbox.window.cbDrawer, els, body, events, documentHandlers };
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

// ── ACL-1769: Expand / Dock right ─────────────────────────────────────
// Та же панель, шире. Закрытие панели ширину не сбрасывает, и она
// переживает перезагрузку; хранилище закрыто — работает без памяти.
const wide = memoryStore();
page = makePage(wide);
assert.equal(page.api.isExpanded(), false, 'без записи панель должна быть пристыкована');
assert.equal(page.els['cbd-expand'].textContent, 'Expand');
page.api.open();
page.els['cbd-expand'].click();
assert.equal(page.api.isExpanded(), true, 'Expand не растянул панель');
assert.equal(page.els['cbd-root'].getAttribute('data-expanded'), '1');
assert.equal(page.els['cbd-expand'].textContent, 'Dock right', 'подпись кнопки не сменилась');
assert.equal(page.els['cbd-expand'].getAttribute('aria-pressed'), 'true');
assert.equal(page.body.classes.has('cbd-expanded'), true);
page.els['cbd-close'].click();
assert.equal(page.api.isExpanded(), true, 'закрытие сбросило ширину');
assert.equal(page.api.isOpen(), false);
assert.equal(wide.data['acl.builder.expanded'], '1', 'ширина не записана');
again = makePage(wide);                            // как после F5
assert.equal(again.api.isExpanded(), true, 'после перезагрузки ширина забыта');
again.els['cbd-expand'].click();
assert.equal(again.api.isExpanded(), false, 'Dock right не вернул панель к краю');
assert.equal(again.body.classes.has('cbd-expanded'), false);
assert.equal(wide.data['acl.builder.expanded'], '0');
page = makePage(locked);
page.api.expand();
assert.equal(page.api.isExpanded(), true, 'без хранилища растянуть нельзя');

console.log('ok');
