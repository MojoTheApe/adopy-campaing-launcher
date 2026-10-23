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
// ACL-1313: занятая очередь объясняет себя — берём настоящую проверку.
// ACL-1355: держат только потерянный статус и тот же аккаунт.
// ACL-2118: тело окна подтверждения собирают чистые помощники.
const confirmCode = cut('/* ACL-2118 begin */', '/* ACL-2118 end */');
const busyCode = cut('function cbQueueUncertain() {', '\nwindow.cbPublish = function');

// `country` — необязательна: один аккаунт бывает привязан к двум странам,
// и тогда у каждой своя форма (ревью PR #363).
function fakeForm(label, country) {
  return {
    _label: country ? label + '@' + country : label,
    querySelectorAll: () => [{ querySelector: () => null }],                        // одна карточка
    querySelector: (sel) => (sel === '[name="account_label"]' ? { value: label }
      : sel === '[name="country"]' && country ? { value: country } : null),
  };
}

function runQueue(accounts, opts = {}) {
  const events = [];
  const said = [];
  const pending = [];
  const keys = [];
  const forms = accounts.map((a) => (Array.isArray(a) ? fakeForm(a[0], a[1]) : fakeForm(a)));
  const sandbox = {
    window: {},
    FormData, RegExp, String, parseInt, Object, JSON, Promise,
    aclEscHtml: (v) => String(v == null ? '' : v),
    document: { querySelectorAll: () => forms, querySelector: () => null,
                getElementById: (id) => (id === 'cb-pf-anyway' && opts.box ? opts.box : null) },
    _safeConfirm: (title, body, onYes) => { events.push('asked'); if (opts.defer) { opts.defer.push(onYes); return; } if (opts.confirm !== false) onYes(); },
    cbPreview: (form, done) => {
      events.push('preview:' + form._label);
      if (opts.preview) return opts.preview(form, done);
      done(opts.plan ? opts.plan(form._label) : [{ 'Campaign Name': form._label, errors: [] }], null);
    },
    cbSkipByName: opts.skip || (() => ({ skip: {}, unchecked: 0, broken: 0 })),
    cbFormDataSkipping: opts.pack || (() => ({ body: new FormData(), kept: 1 })),
    cbPollJob: (jobId, panel, btn, done) => { events.push('poll'); pending.push(done); },
    fetch: (url, init) => {
      events.push('POST');
      keys.push(init && init.body && init.body.get ? init.body.get('request_id') : null);
      // ACL-1399: `fail` — сколько первых отправок сервер не отвечает.
      if (opts.fail === true || (typeof opts.fail === 'number' && keys.length <= opts.fail)) {
        return Promise.reject(new TypeError('Failed to fetch'));
      }
      return Promise.resolve({ json: () => Promise.resolve({ job_id: 'j' + pending.length }) });
    },
    alert: (m) => { events.push('alert'); said.push(String(m)); },
    // ACL-141: отказы — окна страницы (заголовок + пояснение) и строка у кнопки.
    aclAlert: (m, o) => { events.push('alert'); said.push(((o && o.title) || '') + ' ' + m); return Promise.resolve(); },
    aclInlineError: (anchor, m) => { if (m) { events.push('alert'); said.push(String(m)); } },
  };
  sandbox.window.window = sandbox.window;
  sandbox.window.cbPreview = sandbox.cbPreview;
  sandbox.window.alert = sandbox.alert;
  vm.runInNewContext(
    'window.cbSkipByName = cbSkipByName; window.cbFormDataSkipping = cbFormDataSkipping;\n' + confirmCode + '\n' + busyCode + '\n' + queueCode,
    sandbox);
  // ACL-1604: проверка аккаунтов живёт на window; без неё подтверждение открывается сразу.
  if (opts.preflight) sandbox.window.cbPreflightFetch = opts.preflight;
  // ACL-1819: ворота pre-flight audit — заглушка; сам гейт проверяют preflight_gate_harness.cjs.
  const gated = [];
  sandbox.window.cbPreflightGate = (items, go) => { gated.push(items); if (!opts.gateRefuses) go(); };
  sandbox.window.cbPreflightDecision = (queued, skipped, anyway) => { events.push('decision'); (opts.decisions || []).push({ queued, skipped, anyway }); };
  sandbox.window.cbPublishAll(() => {});
  return { events, said, pending, sandbox, forms, keys, gated };
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

  // ACL-1355: Stop у A — только задание A; B и C идут дальше. «Publish all»
  // поверх идущей сборки докладывает в неё A, а идущие B и C второй раз не шлёт.
  q = runQueue(['A', 'B', 'C']);
  await flush();
  q.pending[0]({ status: 'cancelled' });
  assert.ok(q.sandbox.window.CB_QUEUE.active, 'cancelling A released running B and C');
  assert.equal(q.sandbox.window.CB_QUEUE.progress.report[0].why, 'Stopped by user');
  const runBefore = q.sandbox.window.CB_QUEUE;
  const previewsBefore = q.events.filter((e) => e.startsWith('preview')).length;
  q.sandbox.window.cbPublishAll(() => {});
  await flush();
  assert.strictEqual(q.sandbox.window.CB_QUEUE, runBefore, 'a second run replaced the active one');
  assert.equal(posts(q), 4, 'the stopped account was not queued again, or a running one was duplicated');
  assert.deepEqual(q.events.filter((e) => e.startsWith('preview')).slice(previewsBefore), ['preview:A'],
    'a running account went through preview again');
  assert.equal(q.said.length, 0, 'joining the run was refused with an alert');
  assert.equal(q.sandbox.window.CB_QUEUE.progress.of, 4);
  q.pending[1]({ results: [{ publishable: true }] });
  q.pending[2]({ results: [{ publishable: true }] });
  assert.ok(q.sandbox.window.CB_QUEUE.active, 'the run closed while the re-queued A was still going');
  q.pending[3]({ results: [{ publishable: true }] });
  assert.ok(!q.sandbox.window.CB_QUEUE.active);
  assert.equal(q.sandbox.window.CB_QUEUE.progress.report.length, 4);
  q.sandbox.window.cbPublishAll(() => {});
  await flush();
  const currentBatch = q.sandbox.window.CB_QUEUE;
  const progress = currentBatch.progress;
  q.pending[0]({ status: 'unknown' }); // stale callback from the previous batch
  assert.strictEqual(currentBatch.progress, progress);
  assert.ok(currentBatch.active && !currentBatch.uncertain);

  // Все аккаунты уже собираются: вопроса нет, работы нет, сказано почему.
  q = runQueue(['A', 'B']);
  await flush();
  const asked = q.events.filter((e) => e === 'asked').length;
  q.sandbox.window.cbPublishAll(() => {});
  await flush();
  assert.equal(posts(q), 2, 'a duplicate job was submitted');
  assert.equal(q.events.filter((e) => e === 'asked').length, asked, 'asked to publish accounts that are all running');
  assert.equal(q.said.length, 1, 'a busy tab ignored Publish all silently');
  assert.ok(/A, B are already being built/.test(q.said[0]), q.said[0]);

  // Аккаунт, пришедший в идущую сборку, — строкой в ней же; закрывается
  // сборка с последним из всех.
  q = runQueue(['A']);
  await flush();
  q.forms.push(fakeForm('B'));
  q.sandbox.window.cbPublishAll(() => {});
  await flush();
  assert.equal(posts(q), 2);
  assert.deepEqual(q.events.filter((e) => e.startsWith('preview')), ['preview:A', 'preview:B']);
  q.pending[0]({ results: [{ publishable: true }] });
  assert.ok(q.sandbox.window.CB_QUEUE.active, 'the run closed while B was still going');
  q.pending[1]({ results: [{ publishable: true }] });
  assert.ok(!q.sandbox.window.CB_QUEUE.active);
  assert.deepEqual(q.sandbox.window.CB_QUEUE.progress.report.map((r) => r.account), ['A', 'B']);

  // Stop у A не отменяет B, чьё превью ещё в полёте.
  let delayedPreview;
  q = runQueue(['A', 'B', 'C'], { preview: (form, done) => {
    if (form._label === 'B') delayedPreview = done;
    else done([{ 'Campaign Name': form._label, errors: [] }], null);
  }});
  await flush();
  q.pending[0]({ status: 'cancelled' });
  delayedPreview([{ 'Campaign Name': 'B', errors: [] }], null);
  await flush();
  assert.equal(posts(q), 3, 'stopping A cancelled the accounts after it');

  // Потерянный статус — другое дело: превью в полёте после него не шлёт.
  q = runQueue(['A', 'B', 'C'], { preview: (form, done) => {
    if (form._label === 'B') delayedPreview = done;
    else done([{ 'Campaign Name': form._label, errors: [] }], null);
  }});
  await flush();
  q.pending[0]({ status: 'unknown' });
  delayedPreview([{ 'Campaign Name': 'B', errors: [] }], null);
  await flush();
  assert.equal(posts(q), 1, 'a late preview submitted another job after the status was lost');
  assert.ok(!q.sandbox.window.CB_QUEUE.active);
  assert.ok(q.sandbox.window.CB_QUEUE.uncertain);
  assert.equal(q.sandbox.window.CB_QUEUE.progress.report.length, 3);

  // Ревью PR #363: один аккаунт в двух странах — две формы, два задания.
  // По одному имени аккаунта вторая страна молча не собиралась.
  q = runQueue([['UL869', 'Chile'], ['UL869', 'Peru']]);
  await flush();
  assert.equal(posts(q), 2, 'the second country of the same account was dropped');
  assert.equal(q.said.length, 0, q.said.join('; '));
  assert.deepEqual(q.sandbox.window.CB_QUEUE.progress.live.map((x) => x.account),
    ['UL869 (Chile)', 'UL869 (Peru)']);
  // Идут обе — повтор ничего не шлёт и называет обе страны.
  q.sandbox.window.cbPublishAll(() => {});
  await flush();
  assert.equal(posts(q), 2);
  assert.ok(/UL869 \(Chile\), UL869 \(Peru\) are already being built/.test(q.said[0]), q.said[0]);
  // Кончилась одна страна — докладывается она, другая не дублируется.
  q.pending[1]({ results: [{ publishable: true }] });
  q.sandbox.window.cbPublishAll(() => {});
  await flush();
  assert.equal(posts(q), 3);
  assert.equal(q.events.filter((e) => e.startsWith('preview')).pop(), 'preview:UL869@Peru');

  // Сама сборка тоже не берёт идущий аккаунт вторым, даже если вызвать её
  // мимо кнопок (Publish selected и другие вызывающие): отказ в `skipped`.
  q = runQueue(['A']);
  await flush();
  const again = q.sandbox._runPublishQueue([q.forms[0]], () => {});
  await flush();
  assert.deepEqual(again.added, [], 'the run accepted an account that is already building');
  assert.deepEqual(again.skipped, ['A']);
  assert.equal(posts(q), 1, 'a duplicate job was submitted');

  // ACL-1399 (было — ревью PR #363: «исход неизвестен, F5»): сервер не
  // ответил на отправку. 30.09 так пропал Publish владельца — «Failed to
  // fetch», и вкладка встала. Теперь аккаунт ждёт повтора: он занят (второго
  // задания мимо повтора нет), повтор идёт с тем же ключом отправки — сервер
  // по нему вернёт уже созданное задание, если первая попытка до него дошла.
  q = runQueue(['A', 'B'], { fail: 2 });
  await flush();
  assert.equal(posts(q), 2);
  assert.ok(!q.sandbox.window.CB_QUEUE.uncertain, 'a busy server froze the whole tab again');
  assert.ok(q.sandbox.window.CB_QUEUE.active, 'the run closed with accounts still unsent');
  assert.deepEqual(q.sandbox.window.CB_QUEUE.progress.live.map((x) => x.stage), ['busy', 'busy']);
  assert.equal(q.sandbox.window.CB_QUEUE.progress.report.length, 0, 'an unsent account was reported as failed');
  assert.ok(q.keys[0] && q.keys[1] && q.keys[0] !== q.keys[1], 'each account needs its own submission key');
  // Publish all поверх — ничего не шлёт: оба аккаунта ждут своего повтора.
  q.sandbox.window.cbPublishAll(() => {});
  await flush();
  assert.equal(posts(q), 2, 'a second job was sent past the pending retry');
  assert.ok(/A, B are already being built/.test(q.said.pop()));
  // Retry у A — тот же ключ; сервер ответил, за заданием следят.
  assert.equal(q.sandbox.window.CB_QUEUE.retry('|A'), true);
  await flush();
  assert.equal(q.gated.length, 1, 'the retry skipped the pre-flight gate');
  assert.equal(posts(q), 3);
  assert.equal(q.keys[2], q.keys[0], 'retry used a new key: the server could start a second job');
  assert.equal(q.pending.length, 1, 'the retried job is not being watched');
  assert.equal(q.sandbox.window.CB_QUEUE.retry('|A'), false, 'retry fired twice');
  q.pending[0]({ results: [{ publishable: true }] });
  assert.ok(q.sandbox.window.CB_QUEUE.active, 'the run closed while B still waits for its retry');
  q.sandbox.window.CB_QUEUE.retry('|B');
  await flush();
  assert.equal(q.keys[3], q.keys[1]);
  q.pending[1]({ results: [{ publishable: true }] });
  assert.ok(!q.sandbox.window.CB_QUEUE.active);
  assert.deepEqual(q.sandbox.window.CB_QUEUE.progress.report.map((r) => r.state), ['ok', 'ok']);

  // Ревью PR #408, R9: Dismiss освобождает аккаунт, не дожидаясь повтора, —
  // а следующая его отправка идёт с ТЕМ ЖЕ ключом: если первая всё же дошла,
  // сервер вернёт её задание, второго не будет.
  // ACL-1819: ворота не пустили — повтор ничего не шлёт.
  q = runQueue(['A'], { fail: 1, gateRefuses: true });
  await flush();
  assert.equal(posts(q), 1);
  q.sandbox.window.CB_QUEUE.retry('|A');
  await flush();
  assert.equal(posts(q), 1, 'a refused gate still let the retry post');
  assert.equal(q.gated.length, 1);
  q = runQueue(['A', 'B'], { fail: 2 });
  await flush();
  assert.equal(q.sandbox.window.CB_QUEUE.dismiss('|B'), true);
  assert.equal(q.sandbox.window.CB_QUEUE.dismiss('|B'), false, 'dismissed twice');
  assert.ok(/dismissed/.test(q.sandbox.window.CB_QUEUE.progress.report[0].why));
  assert.ok(q.sandbox.window.CB_QUEUE.active, 'A still waits for its retry');
  q.sandbox.window.cbPublishAll(() => {});          // B свободен и уходит снова; A ждёт повтора
  await flush();
  assert.equal(posts(q), 3, 'the dismissed account could not be published again');
  assert.equal(q.keys[2], q.keys[1], 'a dismissed account came back with a new key: possible double job');

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

  // ACL-1604: проверка аккаунтов до подтверждения.
  const notReady = { accounts: [
    { account: 'A', ready: false, reasons: [{ message: 'no token', hint: 'connect', seen_label: '' }], notes: [] },
    { account: 'B', ready: true, reasons: [], notes: [] }] };
  // Не ответила (сеть, таймаут, не тот ответ — на странице это null): старое
  // подтверждение и все аккаунты уходят, как раньше.
  q = runQueue(['A', 'B'], { preflight: () => Promise.resolve(null) });
  await flush();
  assert.equal(q.events.filter((e) => e === 'asked').length, 1, 'без проверки подтверждение не открылось');
  assert.equal(posts(q), 2, 'без ответа проверки аккаунты не ушли');
  assert.ok(!q.events.includes('decision'), 'решение записано без проверки');

  // Неготовый пропускается, готовый уходит; решение записано.
  let decisions = [];
  q = runQueue(['A', 'B'], { preflight: () => Promise.resolve(notReady), decisions });
  await flush();
  assert.equal(posts(q), 1, 'неготовый аккаунт ушёл без Run anyway');
  assert.deepEqual(q.events.filter((e) => e.startsWith('preview')), ['preview:B']);
  assert.deepEqual(decisions, [{ queued: ['B'], skipped: ['A'], anyway: [] }]);

  // Run anyway: человек ставит галку, и оба идут; решение это записывает.
  decisions = [];
  const yes = [];
  const box = { checked: false, addEventListener: (t, f) => { box.handler = f; } };
  q = runQueue(['A', 'B'], { preflight: () => Promise.resolve(notReady), decisions, box, defer: yes });
  await flush();
  assert.equal(posts(q), 0, 'ушло до подтверждения');
  assert.ok(box.handler, 'галка Run anyway не подключена');
  box.checked = true; box.handler();
  yes[0]();
  await flush();
  assert.equal(posts(q), 2, 'Run anyway не отправил неготовый аккаунт');
  assert.deepEqual(decisions, [{ queued: ['A', 'B'], skipped: [], anyway: ['A'] }]);

  console.log('ok (queue)');
})();
