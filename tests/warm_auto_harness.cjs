// ACL-2268: автопрогрев билдера. Загрузка страницы и программные события не греют; настоящее
// действие человека греет один раз; отказ гасит строку, кнопок нет.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const code = fs.readFileSync(process.argv[2], 'utf8');

function world(cards, stateReply) {
  const posts = [];
  const listeners = {};
  const line = { style: {}, textContent: '', parentNode: { insertBefore() {} } };
  const field = { value: 'Acc' };
  const form = {
    _cards: cards,
    querySelector(q) {
      if (q === '[name="account_label"]') return field;
      if (q === '.warm-auto-line') return line;
      return null;
    },
    querySelectorAll(q) { return q === '.cb-slot' ? new Array(this._cards).fill({}) : []; },
    appendChild() {},
  };
  const sb = {
    console, Promise, JSON, Object, encodeURIComponent, String,
    setTimeout: (fn) => { Promise.resolve().then(fn); return 1; },
    document: { addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
                createElement: () => ({ style: {} }) },
    fetch: (url, init) => {
      const reply = (status, body) => Promise.resolve({ status, json: () => Promise.resolve(body) });
      if (url === '/warm/ensure') { posts.push(JSON.parse(init.body)); return reply(202, { accounts: { Acc: 'queued' } }); }
      return reply(200, stateReply);
    },
    window: { ACL_FB_BACKEND: 'api' },
  };
  vm.createContext(sb);
  vm.runInContext(code, sb);
  const fire = (type, trusted, selector) => {
    const target = { closest: (q) => (q.includes(selector) ? target : (q === 'form.open-account-form' ? form : null)) };
    (listeners[type] || []).forEach((fn) => fn({ type, isTrusted: trusted, target }));
  };
  return { posts, form, fire, sb, line, listeners };
}
const tick = () => new Promise((r) => setTimeout(r, 5));
const WARM = { accounts: { Acc: { state: 'warm' } } };

(async () => {
  // 16. Страница загружена с восстановленным черновиком: тишина; программные события не считаются.
  let w = world(1, WARM);
  await tick();
  assert.equal(w.posts.length, 0, 'загрузка сама не греет');
  w.fire('input', false, '.cb-slot'); w.fire('click', false, '.cb-add-btn');
  await tick();
  assert.equal(w.posts.length, 0, 'isTrusted=false не греет');

  // Настоящая правка карточки: один запрос с триггером и числом карточек.
  w.fire('input', true, '.cb-slot'); w.fire('change', true, '.cb-slot');
  await tick();
  assert.equal(w.posts.length, 1);
  assert.deepEqual(w.posts[0], { accounts: ['Acc'], trigger: 'edit', campaigns: 1 });

  // 17. Пустой аккаунт: клик «+ Add Campaign», карточки ещё нет - запроса нет; появилась - один запрос.
  w = world(0, WARM);
  w.fire('click', true, '.cb-add-btn');
  await tick();
  assert.equal(w.posts.length, 0, 'без карточки не греем');
  w.form._cards = 1;
  w.fire('click', true, '.cb-add-btn');
  await tick();
  assert.equal(w.posts.length, 1);
  assert.equal(w.posts[0].trigger, 'add');

  // Дубликат карточки.
  w = world(2, WARM);
  w.fire('click', true, '.cb-dup-slot-btn');
  await tick();
  assert.equal(w.posts[0].trigger, 'duplicate');

  // 18. Ящик: aclWarmAutoUser зовёт обработчик только после настоящего клика (isTrusted проверяет report.html).
  w = world(1, WARM);
  w.sb.window.aclWarmAutoUser(w.form, 'add');
  await tick();
  assert.equal(w.posts.length, 1);
  assert.equal(w.posts[0].trigger, 'add');
  assert.equal(w.line.textContent, 'Profile ready');

  // 19. Отказ: строка гаснет, кнопок нет.
  w = world(1, { accounts: { Acc: { state: 'closed', auto: 'refused', auto_code: 'memory' } } });
  w.fire('input', true, '.cb-slot');
  await tick();
  assert.equal(w.line.textContent, '');
  assert.ok(!/Open profile/.test(code), 'никакой кнопки «Open profile»');

  // 20. Статика: без setInterval, закрытий, публикации и чтений.
  const bare = code.replace(/\/\*[\s\S]*?\*\//g, '');
  assert.ok(!/setInterval/.test(bare));
  assert.ok(!/warm\/close/.test(bare));
  assert.ok(!/publish|account-spend|\/read/i.test(bare));
  console.log('ok');
})().catch((e) => { console.error(e); process.exit(1); });
