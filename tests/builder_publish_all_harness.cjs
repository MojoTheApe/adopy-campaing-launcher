// «Publish all»: что не поедет и как собирается payload без пропущенных.
//
// Здесь живут две вещи, ошибка в которых не видна на экране и стоит
// дорого: отбор кампаний и пересборка номеров полей. Ошибиться — значит
// поднять черновик, который никто не проверял, или задержать собранный.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

function cut(from, to) {
  const s = html.indexOf(from);
  assert.ok(s > 0, 'не найдено: ' + from);
  const e = html.indexOf(to, s);
  assert.ok(e > s, 'не найден конец: ' + to);
  return html.slice(s, e);
}

const code = cut('window.CB_LINK_NOT_BLOCKING =', 'window.cbPublishAll = function');

const sandbox = { window: {}, FormData, RegExp, String, parseInt, Object };
sandbox.window.window = sandbox.window;
vm.runInNewContext(code, sandbox);
const { cbBlockingErrors, cbSkipByName, cbFormDataSkipping } = sandbox;

// ── что держит публикацию, а что нет ──────────────────────────────────
assert.equal(cbBlockingErrors({}).length, 0);
assert.equal(cbBlockingErrors({ errors: [] }).length, 0);
assert.equal(cbBlockingErrors({ errors: ['Daily budget missing'] }).length, 1);

// Пустая ссылка — единственная ошибка, которую владелец просил не считать
// блокирующей: её добавляют и после сборки черновика.
assert.equal(cbBlockingErrors({ errors: ['Link empty in Upload FB Accounts sheet'] }).length, 0);
assert.deepEqual(cbBlockingErrors({ errors: ['Link empty in Upload FB Accounts sheet', 'No ads in campaign'] }),
  ['No ads in campaign'], 'пустая ссылка перестала быть исключением или съела соседнюю ошибку');

// ── поддельная форма ──────────────────────────────────────────────────
function form(names, count) {
  const fields = new Map();
  names.forEach((n, i) => fields.set('camp_' + i + '_name', n));
  return {
    querySelector(sel) {
      if (sel === '[name="camp_count"]') return { value: String(count === undefined ? names.length : count) };
      const m = /^\[name="(camp_\d+_name)"\]$/.exec(sel);
      if (m) return fields.has(m[1]) ? { value: fields.get(m[1]) } : null;
      return null;
    },
  };
}

// ── карточка ищет свою строку плана по имени, а не по номеру ──────────
// Сервер группирует кампании по имени: две карточки с одинаковым именем
// становятся одной кампанией. Отбор по номеру при первом же совпадении
// пропустил бы не ту.
let v = cbSkipByName(form(['Alpha', 'Beta', 'Alpha']), [
  { 'Campaign Name': 'Alpha', errors: [] },
  { 'Campaign Name': 'Beta', errors: ['Daily budget missing'] },
]);
assert.deepEqual(Object.keys(v.skip), ['1'], 'пропущена не та карточка');
assert.equal(v.broken, 1);
assert.equal(v.unchecked, 0);

// Обе одноимённые получают один вердикт — иначе половина уехала бы, а
// половина осталась, хотя кампания на сервере одна.
v = cbSkipByName(form(['Alpha', 'Beta', 'Alpha']), [
  { 'Campaign Name': 'Alpha', errors: ['No ads in campaign'] },
  { 'Campaign Name': 'Beta', errors: [] },
]);
assert.deepEqual(Object.keys(v.skip), ['0', '2']);
assert.equal(v.broken, 2);

// ── непроверенное не едет ─────────────────────────────────────────────
// Имени в плане нет — карточку никто не проверял. Молча поднять такой
// черновик хуже, чем не поднять.
v = cbSkipByName(form(['Alpha', 'Ghost']), [{ 'Campaign Name': 'Alpha', errors: [] }]);
assert.deepEqual(Object.keys(v.skip), ['1']);
assert.equal(v.unchecked, 1, 'непроверенная карточка уехала бы');
assert.equal(v.broken, 0, 'непроверенная посчитана сломанной — это разные вещи');

// Пустой план: не едет ничего.
v = cbSkipByName(form(['Alpha', 'Beta']), []);
assert.equal(Object.keys(v.skip).length, 2);
assert.equal(v.unchecked, 2);

// Только пустая ссылка — кампания едет.
v = cbSkipByName(form(['Alpha']), [{ 'Campaign Name': 'Alpha', errors: ['Link empty in Upload FB Accounts sheet'] }]);
assert.deepEqual(Object.keys(v.skip), [], 'пустая ссылка задержала черновик');

// ── payload без пропущенных: номера сдвигаются ────────────────────────
// Поля называются по номеру, и сервер читает их подряд от нуля. Дыра в
// середине значит, что он прочитает меньше, чем отправлено.
function realForm(entries, count) {
  const fd = new FormData();
  entries.forEach(([k, v]) => fd.append(k, v));
  return {
    _fd: fd,
    querySelector: (sel) => (sel === '[name="camp_count"]' ? { value: String(count) } : null),
  };
}

const F = realForm([
  ['country', 'SA'], ['account_label', 'DQ438'], ['tracking_link', 'http://x'],
  ['camp_0_asset', 'A'], ['camp_0_ad_0_copy', 'a0'], ['camp_0_ad_count', '1'],
  ['camp_1_asset', 'B'], ['camp_1_ad_0_copy', 'b0'], ['camp_1_ad_1_copy', 'b1'], ['camp_1_ad_count', '2'],
  ['camp_2_asset', 'C'], ['camp_2_ad_0_copy', 'c0'], ['camp_2_ad_count', '1'],
  ['camp_count', '3'],
], 3);

// FormData настоящая: подменяем конструктор так, чтобы `new FormData(form)`
// отдавал заготовленные поля — ровно то, что делает браузер.
const RealFormData = FormData;
sandbox.FormData = function (f) { return f && f._fd ? f._fd : new RealFormData(); };

let out = cbFormDataSkipping(F, { 1: true });
assert.equal(out.kept, 2);
assert.equal(out.body.get('camp_count'), '2');
const keys = [...out.body.keys()].filter((k) => /^camp_\d+_asset$/.test(k));
assert.deepEqual(keys, ['camp_0_asset', 'camp_1_asset'], 'в номерах осталась дыра');
assert.equal(out.body.get('camp_0_asset'), 'A');
assert.equal(out.body.get('camp_1_asset'), 'C', 'после сдвига поехала не та кампания');
// Поля объявлений переезжают вместе со своей кампанией.
assert.equal(out.body.get('camp_1_ad_0_copy'), 'c0');
assert.equal(out.body.get('camp_1_ad_count'), '1');
assert.equal(out.body.get('camp_0_ad_0_copy'), 'a0');
// Поля, не принадлежащие кампании, доезжают как были.
assert.equal(out.body.get('country'), 'SA');
assert.equal(out.body.get('account_label'), 'DQ438');
assert.equal(out.body.get('tracking_link'), 'http://x');
// Выброшенная кампания не оставила следов.
assert.ok(![...out.body.values()].includes('B'), 'пропущенная кампания всё равно уехала');
assert.ok(![...out.body.values()].includes('b1'), 'объявление пропущенной кампании уехало');

// Ничего не пропущено — payload совпадает по составу.
out = cbFormDataSkipping(realForm([
  ['camp_0_asset', 'A'], ['camp_1_asset', 'B'], ['camp_count', '2'],
], 2), {});
assert.equal(out.kept, 2);
assert.equal(out.body.get('camp_1_asset'), 'B');

// Пропущено всё — сборщик честно говорит «ноль», а не отправляет пустое.
out = cbFormDataSkipping(realForm([['camp_0_asset', 'A'], ['camp_count', '1']], 1), { 0: true });
assert.equal(out.kept, 0);
assert.equal(out.body.get('camp_count'), '0');

console.log('ok');

// ── очередь: все аккаунты уходят разом (acl-1210) ─────────────────────
// До 16.09 страница отправляла следующий аккаунт только после конца
// предыдущего — потому что сервер пускал по одному (`browser_operation`,
// решение 2026-07-31). Сервер теперь держит замок по профилю: разные
// профили идут разом, один и тот же — по очереди, и «queued» на карточке
// говорит правду сам. Страница шлёт все работы сразу после их превью и ждёт
// последнюю в любом порядке. Первый живой «Publish all» на параллельном
// сервере дал одно задание — второй замок был здесь.
const queueCode = cut('window.cbPublishAll = function', '// ── Диагностика превью');
// ACL-1313: занятая очередь теперь объясняет себя — берём настоящую проверку.
const busyCode = cut('function cbQueueBusy() {', '\nwindow.cbPublish = function');

function runQueue(accounts, opts = {}) {
  const events = [];
  const pending = [];
  const forms = accounts.map((label) => ({
    _label: label,
    querySelectorAll: () => [{}],                        // одна карточка
    querySelector: (sel) => (sel === '[name="account_label"]' ? { value: label } : null),
  }));
  const sandbox = {
    window: {},
    FormData, RegExp, String, parseInt, Object, JSON, Promise,
    aclEscHtml: (v) => String(v == null ? '' : v),
    document: { querySelectorAll: () => forms, querySelector: () => null },
    _safeConfirm: (title, body, onYes) => { events.push('asked'); if (opts.confirm !== false) onYes(); },
    cbPreview: (form, done) => {
      events.push('preview:' + form._label);
      if (opts.preview) return opts.preview(form, done);
      done(opts.plan ? opts.plan(form._label) : [{ 'Campaign Name': form._label, errors: [] }], null);
    },
    cbSkipByName: opts.skip || (() => ({ skip: {}, unchecked: 0, broken: 0 })),
    cbFormDataSkipping: opts.pack || (() => ({ body: new FormData(), kept: 1 })),
    cbPollJob: (jobId, panel, btn, done) => { events.push('poll'); pending.push(done); },
    fetch: (url) => {
      events.push('POST');
      return Promise.resolve({ json: () => Promise.resolve({ job_id: 'j' + pending.length }) });
    },
    alert: (m) => events.push('alert'),
  };
  sandbox.window.window = sandbox.window;
  sandbox.window.cbPreview = sandbox.cbPreview;
  sandbox.window.alert = sandbox.alert;
  vm.runInNewContext(
    'window.cbSkipByName = cbSkipByName; window.cbFormDataSkipping = cbFormDataSkipping;\n' + busyCode + '\n' + queueCode,
    sandbox);
  sandbox.window.cbPublishAll(() => {});
  return { events, pending, sandbox };
}

// Запрос уходит через микрозадачи — без этого проверка смотрит на «ещё
// ничего не случилось» и радуется.
const flush = () => new Promise((r) => setImmediate(r));
const posts = (q) => q.events.filter((e) => e === 'POST').length;

(async () => {
  // Спрашивают один раз на всю пачку, а не на каждый аккаунт.
  let q = runQueue(['A', 'B', 'C']);
  assert.equal(q.events.filter((e) => e === 'asked').length, 1, 'спрашивают на каждый аккаунт');

  await flush();
  assert.equal(posts(q), 3, 'аккаунты снова ждут друг друга — сервер сам ставит в очередь только один и тот же профиль');
  assert.equal(q.pending.length, 3, 'не за каждой работой следят');
  assert.deepEqual(q.events.filter((e) => e.startsWith('preview')), ['preview:A', 'preview:B', 'preview:C'],
    'превью пропущено или перепутан порядок');
  assert.ok(q.sandbox.window.CB_QUEUE.active, 'очередь закрылась, не дождавшись работ');

  // Закончились в любом порядке — очередь закрывается с последней.
  q.pending[2]({ results: [{ publishable: true }] });
  q.pending[0]({ results: [{ publishable: true }] });
  await flush();
  assert.ok(q.sandbox.window.CB_QUEUE.active, 'очередь закрылась, пока одна работа ещё шла');
  q.pending[1]({ results: [{ publishable: true }] });
  await flush();
  assert.ok(!q.sandbox.window.CB_QUEUE.active, 'очередь не закрылась после последней работы');
  assert.equal(posts(q), 3, 'после последнего аккаунта запущено что-то ещё');
  assert.equal(q.sandbox.window.CB_QUEUE.progress.report.length, 3);

  // A lost job stays uncertain; other submitted jobs still own the batch.
  q = runQueue(['A', 'B']);
  await flush();
  q.pending[0]({ status: 'unknown' });
  await flush();
  assert.ok(q.sandbox.window.CB_QUEUE.active);
  assert.ok(q.sandbox.window.CB_QUEUE.uncertain, 'потерянная работа не помечена как неизвестный исход');

  q.pending[1]({ status: 'cancelled' });
  assert.ok(!q.sandbox.window.CB_QUEUE.active);
  assert.ok(q.sandbox.window.CB_QUEUE.uncertain, 'a later cancellation erased an unknown outcome');
  q.sandbox.window.cbPublishAll(() => {});
  assert.equal(posts(q), 2, 'unknown outcome allowed duplicate work');

  q = runQueue(['A', 'B', 'C']);
  await flush();
  q.pending[0]({ status: 'cancelled' });
  assert.ok(q.sandbox.window.CB_QUEUE.active, 'cancelling A released running B and C');
  const alertsBefore = q.events.filter((e) => e === 'alert').length;
  q.sandbox.window.cbPublishAll(() => {});
  assert.equal(posts(q), 3, 'a duplicate batch was submitted');
  // ACL-1313: занятая вкладка говорит, почему кнопка не сработала, а не молчит.
  assert.equal(q.events.filter((e) => e === 'alert').length, alertsBefore + 1,
    'a busy tab ignored Publish all silently');
  q.pending[1]({ results: [{ publishable: true }] });
  assert.ok(q.sandbox.window.CB_QUEUE.active);
  q.pending[2]({ results: [{ publishable: true }] });
  assert.ok(!q.sandbox.window.CB_QUEUE.active);
  q.sandbox.window.cbPublishAll(() => {});
  await flush();
  const currentBatch = q.sandbox.window.CB_QUEUE;
  const progress = currentBatch.progress;
  q.pending[0]({ status: 'unknown' }); // stale callback from the previous batch
  assert.strictEqual(currentBatch.progress, progress);
  assert.ok(currentBatch.active && !currentBatch.uncertain);

  let delayedPreview;
  q = runQueue(['A', 'B', 'C'], { preview: (form, done) => {
    if (form._label === 'B') delayedPreview = done;
    else done([{ 'Campaign Name': form._label, errors: [] }], null);
  }});
  await flush();
  q.pending[0]({ status: 'cancelled' });
  delayedPreview([{ 'Campaign Name': 'B', errors: [] }], null);
  await flush();
  assert.equal(posts(q), 1, 'a late preview submitted another job after cancellation');
  assert.ok(!q.sandbox.window.CB_QUEUE.active);
  assert.equal(q.sandbox.window.CB_QUEUE.progress.report.length, 3);

  // Отказ на вопросе — ни одной работы.
  q = runQueue(['A'], { confirm: false });
  await flush();
  assert.equal(posts(q), 0, 'запустили, несмотря на отказ');

  // Аккаунт, у которого все кампании сломаны, не отправляется вовсе — но и
  // очередь на себе не останавливает.
  q = runQueue(['A', 'B'], { skip: () => ({ skip: { 0: true }, unchecked: 0, broken: 1 }),
                             pack: () => ({ body: new FormData(), kept: 0 }) });
  await flush();
  assert.equal(posts(q), 0, 'отправлена кампания, у которой нет ни одной годной');
  assert.deepEqual(q.events.filter((e) => e.startsWith('preview')), ['preview:A', 'preview:B'],
    'очередь встала на сломанном аккаунте');

  console.log('ok (queue)');
})();
