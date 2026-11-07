// Country Workbench (ACL-1799): чистая часть страницы — плитки KPI, назначение «Use», подбор файла.
//
// Код берётся живьём из шаблона между CW-PURE-BEGIN и CW-PURE-END: копия рядом
// разошлась бы молча. DOM здесь не нужен — это решения «что показать» и «куда
// положить», а запись в поля билдера проверяет браузерный тест.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const a = html.indexOf('/* CW-PURE-BEGIN */'), b = html.indexOf('/* CW-PURE-END */');
assert(a > 0 && b > a, 'чистая часть Workbench не найдена в шаблоне');
const code = html.slice(a, b) +
  '\nreturn {kpiTile, volumeTile, refsHtml, destinationPath, planUse, useField, useTitle, pickCreativeFile, valueText, metricCells};';
const L = vm.runInNewContext('(function(){' + code + '})()', {});

const cmp = (over) => Object.assign({
  status: 'ok',
  thresholds: { min_leads: 30, min_ftds: 3 },
  you: { spend: 600, leads: 60, ftds: 4, buyers: 1 },
  others: { spend: 600, leads: 80, ftds: 6, buyers: 2 },
  unattributed: { spend: 0, leads: 0, ftds: 0 },
  metrics: {
    cpl: { you: 10, you_state: 'ok', others: 7.5, others_state: 'ok', delta_pct: 33.3, verdict: 'worse' },
    cpa: { you: 150, you_state: 'ok', others: 100, others_state: 'ok', delta_pct: 50, verdict: 'worse' },
    cr: { you: 6.67, you_state: 'ok', others: 7.5, others_state: 'ok', delta_pct: -11.1, verdict: 'worse' },
  },
}, over || {});

// ── плитки ───────────────────────────────────────────────────────────
let t = L.kpiTile('cpa', cmp());
assert(t.includes('$150') && t.includes('$100') && t.includes('+50.0% vs others') && t.includes('cw-bad'), t);
t = L.kpiTile('cr', cmp());
assert(t.includes('6.7%') && t.includes('7.5%') && t.includes('-11.1% vs others'), t);

// мало данных: значение видно, но не сравнивается и не красится
const thin = cmp({ metrics: { cpl: { you: 5, you_state: 'insufficient', others: 7.5, others_state: 'ok', delta_pct: null, verdict: null } } });
t = L.kpiTile('cpl', thin);
assert(t.includes('insufficient (&lt;30 leads)') && t.includes('Not compared') && !t.includes('cw-bad') && !t.includes('cw-good'), t);

// спенд есть, FTD нет: «— · 0 FTDs», не ноль
const noFtd = cmp({ you: { spend: 80, leads: 10, ftds: 0, buyers: 1 },
  metrics: { cpa: { you: null, you_state: 'unavailable', others: 100, others_state: 'ok', delta_pct: null, verdict: null } } });
t = L.kpiTile('cpa', noFtd);
assert(t.includes('— · 0 FTDs'), t);

// нет пиров / нет байера — явные подписи
assert(L.kpiTile('cpa', cmp({ status: 'no_peers' })).includes('No comparable peer data'));
assert(L.kpiTile('cpa', cmp({ status: 'no_buyer' })).includes('Pick a buyer'));
assert(L.volumeTile(cmp({ unattributed: { spend: 999 } })).includes('$999 spend has no CM'));

// ── цель / порог / пиры — три разных поля ─────────────────────────────
const ref = { benchmark: { max_cpl: null, max_cpa: 700, min_cr: 1, source: 'country / global default' },
  peer: { cpl: 7.5, cpa: 100, cr: 7.5, buyers: 2 } };
let r = L.refsHtml({ ftd: 10, cpa: 500, cr: null }, ref);
assert(r.includes('data-ref="goal"') && r.includes('CPA ≤ $500'), r);
assert(r.includes('data-ref="benchmark"') && r.includes('CPA ≤ $700'), r);
assert(r.includes('data-ref="peer"') && r.includes('CPA $100'), r);
// цели нет — «не настроена», а не зелёная отметка
r = L.refsHtml(null, { benchmark: { max_cpl: null, max_cpa: null, min_cr: null, source: 'x' }, peer: { buyers: 0 } });
assert((r.match(/Target not configured/g) || []).length === 2 && r.includes('No comparable peer data'), r);

// ── «Use»: куда ───────────────────────────────────────────────────────
const active = { country: 'colombia', countryName: 'Colombia', account: 'A1', name: 'Camp 1', asset: 'Alpha', adIndex: 1 };
let p = L.planUse('target', { country: 'colombia', countryName: 'Colombia', asset: 'Alpha' }, null);
assert.equal(p.ok, false); assert.equal(p.reason, 'no_target');
assert(/never added to the last campaign/.test(p.message), 'нет выбора -> просьба выбрать, а не «последняя кампания»');
p = L.planUse('target', { country: 'colombia', countryName: 'Colombia', asset: 'Alpha' }, active);
assert.equal(p.ok, true); assert.equal(p.mismatch, '');
assert.equal(p.destination, 'Colombia / A1 / Camp 1 / Ad set 1');
assert.equal(L.planUse('creative', { country: 'colombia' }, active).destination, 'Colombia / A1 / Camp 1 / Ad set 1 / Ad 2');
// номера набора и объявления — настоящие, а с отмеченным полем путь доходит до него
const marked = Object.assign({}, active, { setIndex: 1, adIndex: 2, field: 'vertical' });
assert.equal(L.destinationPath(marked, 'target'), 'Colombia / A1 / Camp 1 / Ad set 2');
assert.equal(L.destinationPath(marked, 'creative'), 'Colombia / A1 / Camp 1 / Ad set 2 / Ad 3 / Stories / Reels file');
assert.equal(L.destinationPath(marked, 'builder'), 'Colombia / A1 / Camp 1 / Ad set 2 / Ad 3 / Stories / Reels file');
assert.equal(L.destinationPath(Object.assign({}, marked, { field: '' }), 'builder'), 'Colombia / A1 / Camp 1 / Ad set 2 / Ad 3');
assert.equal(L.destinationPath(Object.assign({}, marked, { field: 'targeting' }), 'builder'), 'Colombia / A1 / Camp 1 / Ad set 2 / Targeting');

// ── поле-цель: что куда пишется ────────────────────────────────────────
const withText = { pack: '1121', version: '2', text: 'T1' }, noText = { pack: '1121', version: '2', text: '' };
const at = (field) => Object.assign({}, active, { field });
assert.deepEqual(L.useField('creative', withText, at('')), { field: '', ok: true, reason: '' }, 'не отмечено -> файл + Primary text 1');
assert.equal(L.useField('creative', withText, at('file')).field, 'file');
assert.equal(L.useField('creative', withText, at('vertical')).field, 'vertical');
assert.equal(L.useField('creative', noText, at('vertical')).ok, true, 'файл без текста — не помеха');
assert.equal(L.useField('creative', withText, at('text2')).field, 'text2');
let u = L.useField('creative', noText, at('text2'));
assert(!u.ok && /no ad text/.test(u.reason), 'text2 без текста версии — нельзя');
assert.equal(L.useField('creative', withText, at('targeting')).field, '', 'Targeting при креативе -> умолчание');
assert.equal(L.useField('target', { target: 'FX' }, at('text3')).field, 'targeting', 'таргет всегда в Targeting набора');
assert.equal(L.planUse('creative', { ev: noText }, at('text4')).reason, 'field');
assert.equal(L.planUse('creative', { ev: withText }, at('text4')).field, 'text4');
assert(/Stories \/ Reels file/.test(L.useTitle('creative', withText, Object.assign({}, active, { field: 'vertical', adIndex: 1 }))));
assert(/Ad set 2 · Targeting/.test(L.useTitle('target', {}, marked)));
p = L.planUse('landing', { country: 'peru', countryName: 'Peru', asset: 'Beta' }, active);
assert(p.ok && p.mismatch.includes('Peru') && p.mismatch.includes('Colombia') && p.mismatch.includes('Beta'), p.mismatch);
assert.equal(L.planUse('nonsense', {}, active).ok, false);

// ── подбор файла: пакет и версия, только если однозначно ────────────────
const files = ['1_es_1121_post.mp4', '2_es_1121_post.mp4', '1_es_2046_post.mp4', 'folder/11210_x.mp4'];
assert.equal(L.pickCreativeFile(files, '1121', '2'), 1, 'версия 2 пакета 1121');
assert.equal(L.pickCreativeFile(files, '1121', '1'), 0);
assert.equal(L.pickCreativeFile(files, '1121', ''), -1, 'без версии два файла -> не угадываем');
assert.equal(L.pickCreativeFile(files, '1121', '3'), -1);
assert.equal(L.pickCreativeFile(files, '2046', ''), 2);

console.log('ok');
