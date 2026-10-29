// ACL-2314: сигнал о конце Check plan / Publish. Берёт настоящий блок из шаблона
// (между маркерами ACL-2314) и гоняет его в node с поддельными окном и Notification.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const s = html.indexOf('/* ACL-2314 begin */'), e = html.indexOf('/* ACL-2314 end */');
assert.ok(s > 0 && e > s, 'нет блока ACL-2314');
const code = html.slice(s, e);

function world(opts) {
  opts = opts || {};
  const w = { asked: 0, shown: [], store: {}, docListeners: {}, winListeners: {}, beeps: 0, focused: 0 };
  const mk = (a) => ({ a, getAttribute: (k) => a[k], setAttribute: (k, v) => { a[k] = v; }, removeAttribute: (k) => { delete a[k]; } });
  // В шаблоне две ссылки: Chrome берёт SVG, поэтому подменять надо обе.
  const iconAttrs = { href: '/favicon.ico?v=acm', sizes: '32x32' };
  const svgAttrs = { href: '/static/logo.svg', type: 'image/svg+xml' };
  const iconLinks = [mk(iconAttrs), mk(svgAttrs)];
  const clickHandlers = [];
  const doc = {
    visibilityState: opts.hidden ? 'hidden' : 'visible', title: 'ACL Dashboard',
    hasFocus: () => !opts.hidden,
    querySelectorAll: (q) => (/rel~="icon"/.test(q) ? iconLinks : []),
    addEventListener: (n, f) => { if (n === 'click') clickHandlers.push(f); else w.docListeners[n] = f; },
  };
  function N(title, o) { this.title = title; this.o = o; this.closed = false; w.shown.push(this); }
  N.prototype.close = function () { this.closed = true; };
  if (opts.perm !== 'none') {
    N.permission = opts.perm || 'default';
    N.requestPermission = () => { w.asked += 1; return Promise.resolve(opts.answer || 'granted'); };
  }
  const win = {
    focus: () => { w.focused += 1; },
    addEventListener: (n, f) => { w.winListeners[n] = f; },
    AudioContext: function () {
      w.beeps += 1;
      return { createOscillator: () => ({ connect() {}, frequency: {}, start() {}, stop() {} }),
        createGain: () => ({ connect() {}, gain: {} }), destination: {}, currentTime: 0 };
    },
  };
  if (opts.badStorage) Object.defineProperty(win, 'localStorage', { get() { throw new Error('blocked'); } });
  else if (opts.noStorage) win.localStorage = null;
  else win.localStorage = { getItem: (k) => (k in w.store ? w.store[k] : null), setItem: (k, v) => { w.store[k] = v; } };
  win.window = win;
  const sandbox = { window: win, document: doc, Promise, JSON, Math, parseInt, encodeURIComponent, console };
  if (opts.perm !== 'none') sandbox.Notification = N;
  vm.runInNewContext(code, sandbox);
  w.api = win.aclDoneNotify; w.doc = doc; w.icon = iconAttrs; w.svg = svgAttrs; w.click = clickHandlers[0];
  return w;
}
const tick = () => new Promise((r) => setTimeout(r, 0));
const btn = (kind, extra) => ({ target: { closest: () => Object.assign({ getAttribute: () => kind }, extra || {}) }, stopPropagation() {} });

(async () => {
  // 1. На загрузке разрешение не спрашивается; по умолчанию всё выключено, звук тоже.
  let w = world({ hidden: true });
  assert.equal(w.asked, 0, 'разрешение спрошено на загрузке');
  assert.deepEqual(w.api.prefs(), { on: false, sound: false });

  // 2. Видимая вкладка в фокусе: ни уведомления, ни значка, ни звука.
  w = world({ hidden: false, perm: 'granted' });
  w.store.acl_done_notify = JSON.stringify({ on: true, sound: true });
  assert.equal(w.api.finished('check', 2, 0), false);
  assert.equal(w.shown.length, 0, 'уведомление при видимой вкладке');
  assert.equal(w.doc.title, 'ACL Dashboard');
  assert.equal(w.beeps, 0);

  // 3. Спрятана + разрешено + включено: уведомление и значок; звук выключен по умолчанию.
  w = world({ hidden: true, perm: 'granted' });
  w.store.acl_done_notify = JSON.stringify({ on: true });
  let clicked = 0;
  assert.equal(w.api.finished('check', 3, 1, () => { clicked += 1; }), true);
  assert.equal(w.shown.length, 1);
  assert.equal(w.shown[0].title, 'Check finished: 3 ready, 1 need attention');
  assert.equal(w.beeps, 0, 'звук играл без включения');
  assert.ok(w.doc.title.startsWith('⚠ Needs attention'), w.doc.title);
  assert.ok(w.icon.href.startsWith('data:image/svg+xml'), 'значок не подменён');
  assert.ok(w.svg.href.startsWith('data:image/svg+xml'), 'SVG-ссылка (её берёт Chrome) не подменена');
  // Клик по уведомлению: фокус вкладки, показ результата, значок снят.
  w.shown[0].onclick();
  assert.equal(w.focused, 1); assert.equal(clicked, 1); assert.ok(w.shown[0].closed);
  assert.equal(w.doc.title, 'ACL Dashboard'); assert.equal(w.icon.href, '/favicon.ico?v=acm');
  assert.equal(w.svg.href, '/static/logo.svg'); assert.equal(w.svg.type, 'image/svg+xml'); assert.equal(w.icon.type, undefined);

  // 4. Значок ставится и без разрешения и снимается, когда вкладку снова видно.
  w = world({ hidden: true });
  w.api.finished('publish', 2, 0, null, 2);
  assert.ok(w.doc.title.startsWith('✓ Ready to publish'), w.doc.title);
  w.doc.title = 'Renamed by page';   // страница сменила заголовок сама
  w.doc.title = '✓ Ready to publish — Renamed by page';
  assert.equal(w.shown.length, 0, 'уведомление без разрешения/включения');
  w.doc.visibilityState = 'visible'; w.doc.hasFocus = () => true;
  w.docListeners.visibilitychange();
  assert.equal(w.doc.title, 'Renamed by page', 'заголовок восстановлен по текущему, а не по сохранённому');
  assert.equal(w.icon.href, '/favicon.ico?v=acm'); assert.equal(w.svg.href, '/static/logo.svg');

  // 5. Звук играет только если включён.
  w = world({ hidden: true, perm: 'granted' });
  w.store.acl_done_notify = JSON.stringify({ on: true, sound: true });
  w.api.finished('publish', 1, 0, null, 1);
  assert.equal(w.beeps, 1);

  // 6. Текст без идентификаторов: только счётчики.
  const t = w.api.text('publish', 4, 2, 6);
  assert.equal(t, 'Publish finished: 4 of 6 campaigns published');
  assert.equal(w.api.text('check', 3, 0), 'Check finished: 3 accounts ready');
  assert.ok(!/\d{6,}|act_|token|http/i.test(t), t);
  w.api.finished('publish', 'act_123456789', 'EAAB-token');   // мусор вместо счётчиков -> нули
  assert.equal(w.shown[w.shown.length - 1].title, 'Publish finished: 0 of 0 campaigns published');

  // 7. Кнопка: разрешение спрашивается из клика, не раньше; согласие сохраняется.
  w = world({ hidden: false, perm: 'default', answer: 'granted' });
  assert.equal(w.asked, 0);
  assert.ok(/Notify me when done/.test(w.api.controlHtml()));
  w.click(btn('enable'));
  await tick();
  assert.equal(w.asked, 1);
  assert.equal(JSON.parse(w.store.acl_done_notify).on, true);
  assert.equal(JSON.parse(w.store.acl_done_notify).sound, false);
  w.click(btn('sound', { checked: true }));
  assert.equal(JSON.parse(w.store.acl_done_notify).sound, true);

  // 8. Отказ в разрешении: тихо, только значок.
  w = world({ hidden: true, perm: 'default', answer: 'denied' });
  assert.equal(await w.api.enable(), 'denied');
  assert.equal(w.api.prefs().on, false);
  assert.doesNotThrow(() => w.api.finished('check', 1, 1));
  assert.equal(w.shown.length, 0);
  assert.ok(w.doc.title.startsWith('⚠'));
  w = world({ hidden: true, perm: 'denied' });
  assert.ok(/blocked/.test(w.api.controlHtml()) && /disabled/.test(w.api.controlHtml()));
  assert.equal(await w.api.enable(), 'denied');
  assert.equal(w.asked, 0, 'при отказе повторно не спрашиваем');

  // 9. Нет API Notification: без исключений, значок есть.
  w = world({ hidden: true, perm: 'none' });
  assert.equal(await w.api.enable(), 'unsupported');
  assert.doesNotThrow(() => w.api.finished('publish', 1, 0));
  assert.ok(w.doc.title.startsWith('✓'));
  assert.ok(/not available/.test(w.api.controlHtml()));

  // 10. localStorage недоступен или бросает: не падаем, настройки по умолчанию.
  for (const o of [{ noStorage: true }, { badStorage: true }]) {
    w = world(Object.assign({ hidden: true, perm: 'granted' }, o));
    assert.deepEqual(w.api.prefs(), { on: false, sound: false });
    assert.doesNotThrow(() => w.api.setSound(true));
    assert.doesNotThrow(() => w.api.finished('check', 1, 0));
  }
  // 11. Задание ждёт руки менеджера: один раз на эпизод, только при спрятанной вкладке, клик открывает задание.
  w = world({ hidden: false, perm: 'granted' });
  w.store.acl_done_notify = JSON.stringify({ on: true });
  assert.equal(w.api.waiting('job:1'), false); assert.equal(w.shown.length, 0, 'ожидание при видимой вкладке');
  w = world({ hidden: true, perm: 'granted' });
  w.store.acl_done_notify = JSON.stringify({ on: true });
  let opened = 0;
  assert.equal(w.api.waiting('job:1', () => { opened += 1; }), true);
  assert.equal(w.api.waiting('job:1'), false, 'повтор в том же эпизоде');
  assert.equal(w.shown.length, 1);
  assert.equal(w.shown[0].title, 'A job is waiting for your action');
  assert.ok(!/job:|\d/.test(w.shown[0].title));
  w.shown[0].onclick();
  assert.equal(opened, 1); assert.equal(w.focused, 1);
  w.api.waitingEnded('job:1');
  assert.equal(w.api.waiting('job:1'), true, 'новый эпизод — новый сигнал');
  assert.equal(w.shown.length, 2);
  console.log('ok');
})().catch((err) => { console.error(err); process.exit(1); });
