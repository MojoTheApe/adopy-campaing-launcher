// То, что собирается, переживает F5 — и не копится вечно.
//
// Собранная наполовину кампания есть работа: потерять её на перезагрузке
// нельзя. Но и хранить всё когда-либо начатое нельзя тоже — через месяц
// список, который должен отвечать «что я собираю», отвечает «что я
// когда-либо начинал».
//
// Код берётся живьём из шаблона: копия расходится с оригиналом молча.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

const start = html.indexOf('window.cbDraft = (function () {');
assert(start > 0, 'window.cbDraft не найден в шаблоне');
const tail = '\n})();';
const end = html.indexOf(tail, start) + tail.length;
const code = html.slice(start, end);
assert(code.includes('acl.builder.v1'), 'срез захватил не то');

const DAY = 24 * 60 * 60 * 1000;

function memoryStore(initial) {
  const data = Object.assign({}, initial);
  return {
    getItem(k) { return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null; },
    setItem(k, v) { data[k] = String(v); },
    data,
  };
}

// Часы в руках: без них срок годности проверить нечем.
function boot(store, clock) {
  const time = { value: clock === undefined ? 1_700_000_000_000 : clock };
  const sandbox = {
    localStorage: store,
    window: {},
    Date: { now() { return time.value; } },
    JSON, Object, Array,
  };
  vm.runInNewContext(code, sandbox);
  return { api: sandbox.window.cbDraft, time, window: sandbox.window };
}

function saved(store) {
  return JSON.parse(store.data['acl.builder.v1']);
}

// ── пусто по умолчанию ────────────────────────────────────────────────
let store = memoryStore();
let s = boot(store);
assert.equal(s.api.count(), 0);
assert.deepEqual(s.api.all(), []);

// ── заведённое переживает перезагрузку ────────────────────────────────
let c = s.api.add({ country: 'sa', countryName: 'Saudi Arabia', account: 'DQ438' });
assert.ok(c && c.id, 'кампания не заведена');
assert.equal(c.name, '', 'кампания заводится пустой');
assert.deepEqual(c.ads, []);
s.api.update(c.id, { name: 'SA · Trading' });

let again = boot(store);                              // как после F5
assert.equal(again.api.count(), 1, 'набранное не пережило перезагрузку');
assert.equal(again.api.get(c.id).name, 'SA · Trading');
assert.equal(again.api.get(c.id).account, 'DQ438');

// ── id не переписывается правкой ──────────────────────────────────────
again.api.update(c.id, { id: 'подменённый', created: 0, name: 'x' });
assert.equal(again.api.get(c.id).name, 'x');
assert.equal(again.api.get(c.id).id, c.id, 'id кампании переписали');

// ── две кампании в одну миллисекунду остаются двумя ───────────────────
s = boot(memoryStore());
const a = s.api.add({ country: 'sa' });
const b = s.api.add({ country: 'sa' });
assert.notEqual(a.id, b.id, 'два черновика получили один id');
assert.equal(s.api.count(), 2);

// ── удаление ──────────────────────────────────────────────────────────
assert.equal(s.api.remove(a.id), true);
assert.equal(s.api.count(), 1);
assert.equal(s.api.remove('нет такого'), false, 'удаление несуществующего сошло за успех');

// ── срок годности ─────────────────────────────────────────────────────
store = memoryStore();
s = boot(store, 1_000 * DAY);
const old = s.api.add({ country: 'sa', countryName: 'Saudi Arabia' });
const kept = s.api.add({ country: 'kw', countryName: 'Kuwait' });

// Через десять дней трогаем вторую — она снова свежая.
s.time.value = 1_010 * DAY;
s.api.update(kept.id, { name: 'ещё собираю' });

// Через пятнадцать дней после начала первая протухла, вторая — нет.
let later = boot(store, 1_015 * DAY);
assert.equal(later.api.count(), 1, 'протухшее не убрано или убрано лишнее');
assert.equal(later.api.get(old.id), null, 'двухнедельный черновик остался');
assert.ok(later.api.get(kept.id), 'тронутый десять дней назад черновик выброшен');
assert.equal(saved(store).campaigns.length, 1, 'уборка не записана');

// Срок именно две недели, а не «когда-нибудь».
assert.equal(later.api.staleMs, 14 * DAY);

// ── мусор в хранилище не роняет страницу ──────────────────────────────
for (const junk of ['не json', '{}', '[]', '{"v":99,"campaigns":[]}', '{"v":1}']) {
  s = boot(memoryStore({ 'acl.builder.v1': junk }));
  assert.equal(s.api.count(), 0, 'на мусоре ' + junk + ' получилось не пусто');
  assert.ok(s.api.add({ country: 'sa' }), 'после мусора ящик не принимает работу');
}

// ── закрытое хранилище: не помним, но работаем ────────────────────────
const locked = {
  getItem() { throw new Error('denied'); },
  setItem() { throw new Error('denied'); },
};
s = boot(locked);
assert.equal(s.api.count(), 0);
assert.ok(s.api.add({ country: 'sa' }), 'без хранилища кампанию не завести');
assert.equal(s.api.count(), 1, 'без хранилища не работает и в памяти');

// ── потолок ───────────────────────────────────────────────────────────
s = boot(memoryStore());
for (let i = 0; i < s.api.limit; i += 1) {
  s.time.value += 1;
  assert.ok(s.api.add({ country: 'sa' }), 'потолок сработал раньше времени на ' + i);
}
assert.equal(s.api.add({ country: 'sa' }), null, 'потолка нет — записи плодятся без предела');

// ── страна → аккаунт → кампании ───────────────────────────────────────
s = boot(memoryStore());
s.api.add({ country: 'sa', countryName: 'Saudi Arabia', account: 'DQ438' });
s.api.add({ country: 'sa', countryName: 'Saudi Arabia', account: 'DQ438' });
s.api.add({ country: 'sa', countryName: 'Saudi Arabia', account: 'UL921' });
s.api.add({ country: 'kw', countryName: 'Kuwait', account: 'KLD_X84' });
const tree = s.api.grouped();
assert.equal(tree.length, 2, 'страны не сгруппированы');
assert.equal(tree[0].countryName, 'Saudi Arabia', 'порядок стран не тот, в каком заводили');
assert.equal(tree[0].accounts.length, 2);
assert.equal(tree[0].accounts[0].account, 'DQ438');
assert.equal(tree[0].accounts[0].campaigns.length, 2);
assert.equal(tree[1].accounts[0].account, 'KLD_X84');
assert.ok(!('_byAccount' in tree[0]), 'во внешний вид протекла внутренняя кухня');

// ── о переменах узнают ────────────────────────────────────────────────
s = boot(memoryStore());
const seen = [];
s.api.onChange(function (list) { seen.push(list.length); });
s.api.onChange(function () { throw new Error('плохой слушатель'); });
const heard = [];
s.api.onChange(function (list) { heard.push(list.length); });
const one = s.api.add({ country: 'sa' });
s.api.update(one.id, { name: 'x' });
s.api.remove(one.id);
assert.deepEqual(seen, [1, 1, 0], 'о переменах сообщают не обо всех');
assert.deepEqual(heard, [1, 1, 0], 'один упавший слушатель заглушил остальных');

// ── счётчик на язычке ходит за содержимым ─────────────────────────────
const counted = [];
s = (function () {
  const store = memoryStore();
  const time = { value: 1_700_000_000_000 };
  const sandbox = {
    localStorage: store,
    window: { cbDrawer: { setCount(n) { counted.push(n); } } },
    Date: { now() { return time.value; } },
    JSON, Object, Array,
  };
  vm.runInNewContext(code, sandbox);
  return { api: sandbox.window.cbDraft };
})();
const two = s.api.add({ country: 'sa' });
s.api.add({ country: 'sa' });
s.api.remove(two.id);
assert.deepEqual(counted, [1, 2, 1], 'язычок не знает, сколько кампаний собирается');

console.log('ok');
