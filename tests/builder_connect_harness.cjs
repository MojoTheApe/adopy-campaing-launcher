// Подключение аккаунта: одна механика на два экрана.
//
// Подключиться можно со вкладки Accounts и из карты аккаунтов. Спрашивают
// они одно — какой профиль открыт, — и выводят остальное сами: имя из
// заголовка профиля, кабинет из его истории браузера. Формы подключения в
// этом файле уже разъезжались, две сводили в одну отдельной задачей;
// поэтому вывод и запрос живут в одном месте, а экраны отличаются только
// разметкой.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

const start = html.indexOf('window.aclConnect = (function () {');
assert(start > 0, 'window.aclConnect не найден в шаблоне');
const code = html.slice(start, html.indexOf('\n})();', start) + 6);
assert(code.includes('/account-map-patch'), 'срез захватил не то');

function boot(opts = {}) {
  const sent = [];
  const sandbox = {
    window: {},
    Date, Object, Array, String, JSON, Promise,
    aclEscHtml: (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    fetch: (url, init) => {
      sent.push({ url, body: init && init.body ? JSON.parse(init.body) : null });
      if (opts.fail) return Promise.reject(new Error('сеть отвалилась'));
      if (String(url).indexOf('accounts-overview') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve(opts.overview || { ok: true, open_profiles: [], accounts: [] }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
    },
  };
  sandbox.window.window = sandbox.window;
  vm.runInNewContext(code, sandbox);
  return { api: sandbox.window.aclConnect, sent };
}

(async () => {
  // ── имя выводится, только когда совпадение одно ──────────────────────
  // Хвост «F191» носят и «FacebookElf191 SA», и «KLD_BFL_F191» — это разные
  // аккаунты, и угадывание привязало бы кампанию к чужому браузеру.
  let t = boot();
  // Ключи приходят с сервера в том же виде, что считает формула: буква и
  // цифры, заглавными.
  t.api.remember(
    [{ profile_id: 'p1', key: 'X84', name: 'KLD_BFL_X84', ad_account_id: 'act_1' },
     { profile_id: 'p2', key: 'F191', name: 'KLD_BFL_F191', ad_account_id: '' }],
    ['KLD_BFL_X84', 'FacebookElf191 SA', 'KLD_BFL_F191']);

  assert.equal(t.api.nameFor('p1'), 'KLD_BFL_X84', 'однозначное совпадение не выведено');
  // Два РАЗНЫХ аккаунта с одним хвостом — спор, и угадывать его нельзя:
  // кампания уехала бы в чужой браузер.
  assert.equal(t.api.nameFor('p2'), '', 'при двух кандидатах имя всё-таки угадали');
  assert.equal(t.api.nameFor('нет такого'), '');
  assert.equal(t.api.adFor('p1'), 'act_1');
  assert.equal(t.api.adFor('p2'), '', 'пустой кабинет выдан за найденный');

  // Один аккаунт, названный дважды — полным именем и коротким, — это всё
  // ещё один аккаунт, а не спор.
  t = boot();
  t.api.remember([{ profile_id: 'p', key: 'W27', ad_account_id: 'act_2' }],
                 ['FacebookMKTOBFLW27', 'FacebookMKTOBFLW27 SV Ait']);
  assert.equal(t.api.nameFor('p'), 'FacebookMKTOBFLW27 SV Ait', 'две записи одного аккаунта сочтены спорными');

  // ── ключ считается той же формулой, что на сервере ───────────────────
  // Своя, похожая, дала бы другие совпадения, и вывод имени в карте
  // разошёлся бы со вкладкой — ровно то, ради чего модуль и заведён.
  t = boot();
  assert.equal(t.api.key('KLD_BFL_W27'), 'W27');
  assert.equal(t.api.key('FacebookMKTOBFLW27 SV Ait'), 'W27', 'хвост берётся не из первого слова');
  assert.equal(t.api.key('FacebookElf191 SA'), 'F191');
  assert.equal(t.api.key('нет хвоста'), '');
  assert.equal(t.api.key(''), '');

  // ── у заглушки три состояния, и «жду» среди них ──────────────────────
  // Пустой список, пока он едет, читается как сломанный: он приходит из
  // Google Sheets и это секунды.
  t = boot();
  assert.ok(/reading open profiles/.test(t.api.options('', 'loading')), 'нет состояния «читаю»');
  assert.ok(/no profile is open/.test(t.api.options('')), 'пустой список ничего не объясняет');
  t.api.remember([{ profile_id: 'p1', name: 'KLD_BFL_X84' }], []);
  const opts = t.api.options('p1');
  assert.ok(/pick an open profile/.test(opts));
  assert.ok(/selected/.test(opts), 'выбранное не отмечено');
  assert.ok(/KLD_BFL_X84/.test(opts));

  // Профиль, занятый другим аккаунтом, назван занятым: подключить его
  // можно, но знать об этом надо заранее.
  t = boot();
  t.api.remember([{ profile_id: 'p1', name: 'X84', taken_by: 'FacebookDQ438' }], []);
  assert.ok(/taken by: FacebookDQ438/.test(t.api.options('')), 'занятость профиля скрыта');

  // ── подключение ──────────────────────────────────────────────────────
  // Строка без кабинета запишется, но в карте не появится: карта пропускает
  // записи без `ad_account_id`. Молча исчезнувший аккаунт хуже отказа.
  t = boot();
  t.api.remember([{ profile_id: 'p1', key: 'X84', ad_account_id: 'act_1', folder_id: 'f1' }], ['KLD_BFL_X84']);

  let r = await t.api.connect({ profile_id: 'p1', account_name: '', ad_account_id: 'act_1' });
  assert.equal(r.ok, false, 'подключили без имени');
  assert.ok(/manually/.test(r.error), 'не предложен ручной путь');

  r = await t.api.connect({ profile_id: 'p1', account_name: 'KLD_BFL_X84', ad_account_id: '  ' });
  assert.equal(r.ok, false, 'подключили без кабинета');
  assert.equal(t.sent.length, 0, 'отказ всё-таки ушёл на сервер');

  r = await t.api.connect({ profile_id: 'p1', account_name: ' KLD_BFL_X84 ', ad_account_id: ' act_1 ', country: 'chile' });
  assert.equal(r.ok, true);
  const req = t.sent[t.sent.length - 1];
  assert.ok(/account-map-patch/.test(req.url), 'запрос ушёл не туда');
  assert.deepEqual(req.body, {
    account_name: 'KLD_BFL_X84', ad_account_id: 'act_1',
    profile_id: 'p1', folder_id: 'f1', country: 'chile',
  }, 'состав запроса не тот');

  // Папка профиля берётся сама — последнее поле, которое человек копировал
  // руками.
  t = boot();
  t.api.remember([{ profile_id: 'p1', ad_account_id: 'act_1' }], []);
  await t.api.connect({ profile_id: 'p1', account_name: 'A', ad_account_id: 'act_1' });
  assert.equal('folder_id' in t.sent[0].body, false, 'выдумана папка, которой у профиля нет');

  // Сеть отвалилась — отказ, а не тишина.
  t = boot({ fail: true });
  r = await t.api.connect({ account_name: 'A', ad_account_id: 'act_1' });
  assert.equal(r.ok, false);
  assert.ok(String(r.error).length, 'отказ без объяснения');

  // ── список спрашивается один раз ─────────────────────────────────────
  t = boot({ overview: { ok: true, open_profiles: [{ profile_id: 'p1' }], accounts: [], suggestions: [] } });
  await t.api.load(false);
  await t.api.load(false);
  assert.equal(t.sent.filter((x) => /accounts-overview/.test(x.url)).length, 1,
    'список тянется на каждый взгляд — блоков подключения на странице под тридцать');
  await t.api.load(true);
  assert.equal(t.sent.filter((x) => /accounts-overview/.test(x.url)).length, 2, 'Reload ничего не перечитал');

  console.log('ok');
})();
