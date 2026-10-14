// Раскладка билдера (ACL-1769): чистые функции, без DOM.
//
// Код берётся живьём из шаблона: копия, написанная рядом, расходится с
// оригиналом молча. Здесь — что решает «что показать и как назвать»;
// правил Meta и создателя кампаний тут нет, они на сервере.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

const start = html.indexOf('window.cbLayout = (function () {');
assert(start > 0, 'window.cbLayout не найден в шаблоне');
const tail = '\n})();';
const code = html.slice(start, html.indexOf(tail, start) + tail.length);
assert(code.includes('effectiveName') && code.includes('FUTURE'), 'срез захватил не то');

const sandbox = { window: {} };
vm.runInNewContext(code, sandbox);
const L = sandbox.window.cbLayout;

// Режим «вывести результат»: тесты на соответствие серверу берут сообщения
// создателя и спрашивают, на какие поля они ляжут.
if (process.argv[3] === '--routes') {
  const messages = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
  process.stdout.write(JSON.stringify(messages.map((m) => L.routes(m))));
  process.exit(0);
}
if (process.argv[3] === '--future') {
  const out = {};
  Object.keys(L.FUTURE).forEach((k) => {
    out[k] = { button: L.futureHtml(k, 'button', 'x'), select: L.futureHtml(k, 'select', 'x'), tip: L.futureTip(k), task: L.FUTURE[k].task };
  });
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}

// ── уровни ────────────────────────────────────────────────────────────
assert.deepEqual(L.LEVELS, ['campaign', 'adset', 'ad']);
assert.equal(L.level('adset'), 'adset');
assert.equal(L.level('nonsense'), 'campaign', 'неизвестный уровень не сводится к кампании');
assert.equal(L.level(null), 'campaign');
assert.equal(L.LEVEL_LABEL.adset, 'Ad set');

// ── имя кампании: своё — только если заполнено ────────────────────────
assert.equal(L.effectiveName('MX_auto_name', ''), 'MX_auto_name');
assert.equal(L.effectiveName('MX_auto_name', '   '), 'MX_auto_name', 'пробелы приняты за имя');
assert.equal(L.effectiveName('MX_auto_name', '  My name '), 'My name');
assert.equal(L.effectiveName('MX_auto_name', undefined), 'MX_auto_name');
assert.equal(L.effectiveName('', ''), '');

// ── статус аккаунта ───────────────────────────────────────────────────
assert.equal(L.accountState({ link_status: 'tracking', missing_map_fields: [] }, false).state, 'ok');
assert.equal(L.accountState({ link_status: 'tracking', missing_map_fields: [] }, true).state, 'busy', 'идущая сборка важнее настройки');
let st = L.accountState({ link_status: 'jira', missing_map_fields: ['profile_id'] }, false);
assert.equal(st.state, 'warn');
assert.equal(st.why, 'Needs setup: tracking link, profile_id');
assert.equal(L.accountState(undefined, false).state, 'wait', 'нет данных — не «настроен»');

// ── номер объявления не выходит за список ─────────────────────────────
assert.equal(L.clampAd(3, 1), 1);
assert.equal(L.clampAd(3, 9), 2);
assert.equal(L.clampAd(3, -4), 0);
assert.equal(L.clampAd(0, 5), 0);

// ── строка кампании ───────────────────────────────────────────────────
assert.equal(L.campMeta({ budget: '50' }), '$50/day');
assert.equal(L.campMeta({ budget: '' }), '');

// ── список свёрнутых аккаунтов из хранилища ───────────────────────────
assert.deepEqual(L.parseList('["a|b","c|d"]'), ['a|b', 'c|d']);
assert.deepEqual(L.parseList('не json'), []);
assert.deepEqual(L.parseList('{"a":1}'), []);
assert.deepEqual(L.parseList('[1,"x",null]'), ['x']);
assert.deepEqual(L.parseList(null), []);

// ── то, чего ещё нет: выключено, без name, с задачей ──────────────────
const tasks = Object.keys(L.FUTURE).map((k) => L.FUTURE[k].task);
tasks.forEach((tk) => assert.ok(/^ACL-17\d\d$/.test(tk), 'задача выглядит не как задача: ' + tk));
Object.keys(L.FUTURE).forEach((key) => {
  ['button', 'select'].forEach((kind) => {
    const h = L.futureHtml(key, kind, 'Shown');
    assert.ok(/ disabled /.test(h), key + ' ' + kind + ': не выключен');
    assert.ok(/aria-disabled="true"/.test(h), key + ': скринридер не знает, что выключено');
    assert.ok(!/ name=/.test(h), key + ': у выключенного есть name — попадёт в форму');
    assert.ok(h.includes(L.FUTURE[key].task), key + ': задача не названа');
    assert.ok(h.includes('coming in ' + L.FUTURE[key].task), key + ': подсказка не называет задачу');
  });
});
assert.equal(L.futureTip('adsets'), 'Multiple ad sets — coming in ACL-1768');
assert.equal(L.futureTip('forms'), 'Instant Forms — coming in ACL-1775');
assert.equal(L.futureTip('texts'), 'Up to 5 primary texts — coming in ACL-1774');
assert.equal(L.futureTip('placements'), 'Placement editor — coming in ACL-1772');
assert.equal(L.futureTip('copyTo'), 'Copy to accounts — coming in ACL-1773');
assert.equal(L.futureTip('cta'), 'CTA list — coming in ACL-1767');
assert.equal(L.futureHtml('нет-такого', 'button'), '', 'неизвестный ключ нарисовал элемент');
// Показанное в выключенном списке экранируется.
assert.ok(!/<script/.test(L.futureHtml('cta', 'select', '<script>x</script>')));

// ── сообщения сервера → поля ──────────────────────────────────────────
const sels = (m) => L.routes(m).map((r) => r.sel);
assert.deepEqual(sels('Ad 1: Primary text 2 is not supported by the API backend yet'), ['.cb-text2-search'],
  'ошибка второго текста ушла и в Ad Title');
assert.deepEqual(sels('Ad 1: no creative file'), ['.cb-creative-search']);
// Тексты ошибок сервера и робота, где поле раньше называлось «Ad Title» / «ad text»:
// все они ложатся на поле Primary text 1.
["Ad text 'Text9': The ad text “Text9” is missing or its title is ambiguous.",
 "Ad title empty for creative 2403",
 "ad text 'Text9' has no body — check it in Ad titles",
 "Ads 1, 2 would all be named 'x'; give them different creatives or ad texts"].forEach((m) =>
  assert.ok(sels(m).includes('.cb-adtitle-search'), 'не ложится на Primary text 1: ' + m));
assert.deepEqual(sels("Primary text 2 'T' needs an Ad Title with a primary text — ignored"), ['.cb-text2-search'],
  'ошибка второго текста ушла в Primary text 1');
assert.deepEqual(sels('Daily budget missing'), ['[name$="_budget"]']);
assert.ok(sels('Detailed targeting not found by Meta: Foo').includes('.cb-targeting-search'));
assert.ok(sels('Tracking link is empty; the API backend needs it for the ads').includes('input[name="tracking_link"]'));
assert.deepEqual(sels('Objective is fine'), [], 'сообщение без поля подсветило поле');
assert.equal(L.routes('Daily budget missing')[0].level, 'campaign');
assert.equal(L.routes('Ad 1: no creative file')[0].level, 'ad');
assert.equal(L.routes('Detailed targeting not found by Meta: Foo')[0].level, 'adset');

console.log('ok');
