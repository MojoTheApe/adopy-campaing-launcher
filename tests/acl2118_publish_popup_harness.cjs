// ACL-2118: окно подтверждения «Publish all» — группировка замечаний и сборка тела.
// Берёт настоящие помощники из шаблона (между маркерами ACL-2118) и гоняет их в node.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const s = html.indexOf('/* ACL-2118 begin */'), e = html.indexOf('/* ACL-2118 end */');
assert.ok(s > 0 && e > s, 'нет блока ACL-2118');
const sandbox = { aclEscHtml: (v) => String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;') };
vm.runInNewContext(html.slice(s, e) + '\nthis.api = { cbNoticeLevel, cbGroupNotices, cbNoticesHtml, cbPublishConfirmParts };', sandbox);
const { cbNoticeLevel, cbGroupNotices, cbNoticesHtml, cbPublishConfirmParts } = sandbox.api;

// ── серьёзность ─────────────────────────────────────────────────────────
assert.equal(cbNoticeLevel('primary_len', 'warning'), 'info', 'длинный текст — серая справка');
assert.equal(cbNoticeLevel('headline_len', 'warning'), 'info');
assert.equal(cbNoticeLevel('link_tracking', 'warning'), 'warn', 'ссылка без меток — настоящее предупреждение');
assert.equal(cbNoticeLevel('budget_set', 'error'), 'block', 'ошибка блокирует');

// ── повторы склеиваются в одну строку со счётчиком ──────────────────────
const items = [];
for (let i = 0; i < 8; i++) {
  items.push({ level: 'info', rule: 'primary_len', label: 'Text visible in full (up to 125)',
    hint: "after 125 characters the text is folded behind 'See more'", campaign: 'Camp ' + i });
}
items.push({ level: 'warn', rule: 'link_tracking', label: 'Tracking parameters in the link', hint: 'no ?key=value', campaign: 'Camp 1' });
items.push({ level: 'warn', rule: 'link_tracking', label: 'Tracking parameters in the link', hint: 'no ?key=value', campaign: 'Camp 1' });
items.push({ level: 'block', rule: 'budget_set', label: 'Daily budget', hint: 'enter a budget', campaign: 'Camp 2' });
const groups = cbGroupNotices(items);
assert.equal(groups.length, 3, 'одинаковые замечания не склеились');
assert.deepEqual(groups.map((g) => g.level), ['block', 'warn', 'info'], 'порядок: красные, жёлтые, серые');
assert.equal(groups[2].count, 8);
assert.equal(groups[2].campaigns.length, 8);
assert.equal(groups[1].count, 2);
assert.equal(groups[1].campaigns.length, 1, 'одна кампания дважды не считается двумя');

const nh = cbNoticesHtml(groups);
assert.equal((nh.match(/<details/g) || []).length, 3, 'по одной раскрываемой строке на группу');
assert.ok(nh.includes('8 campaigns'), 'счётчик кампаний не показан');
assert.ok(nh.includes('pc-info') && nh.includes('pc-warn') && nh.includes('pc-block'));
assert.ok(!/<details[^>]* open/.test(nh), 'строки должны быть свёрнуты');
assert.equal(cbGroupNotices([]).length, 0);
assert.equal(cbGroupNotices(null).length, 0);

// Чужое содержимое экранируется.
const evil = cbNoticesHtml(cbGroupNotices([{ level: 'warn', rule: 'x', label: '<img onerror=1>', campaign: '<b>c</b>' }]));
assert.ok(!evil.includes('<img') && !evil.includes('<b>c'), 'метка не экранирована');

// ── сборка окна ─────────────────────────────────────────────────────────
function model(accountsCount, perAccount, extra) {
  const accounts = [];
  for (let a = 0; a < accountsCount; a++) {
    accounts.push({ name: 'Acc' + a, camps: Array.from({ length: perAccount }, (_, i) => 'C' + a + '_' + i) });
  }
  return Object.assign({ accounts, targetText: 'campaign PAUSED, ad sets and ads active', modeHtml: '<b>PAUSED</b>' }, extra || {});
}
let p = cbPublishConfirmParts(model(4, 2));
assert.ok(/Publish <strong>8<\/strong> campaigns on <strong>4<\/strong> accounts — campaign PAUSED, ad sets and ads active/.test(p.lead), p.lead);
assert.equal((p.body.match(/<details class="pc-acc/g) || []).length, 4, '8 кампаний (> 6) — аккаунты свёрнуты');

p = cbPublishConfirmParts(model(2, 3));
assert.equal((p.body.match(/<details class="pc-acc/g) || []).length, 0, '6 кампаний не сворачиваются');
assert.ok(p.body.includes('C0_0</span><span class="pc-sep">, </span><span class="pc-chip">C0_1'), 'названия идут чипами через запятую');

p = cbPublishConfirmParts(model(1, 1));
assert.ok(p.lead.includes('<strong>1</strong> campaign on <strong>1</strong> account'), 'единственное число');

p = cbPublishConfirmParts(model(1, 1, { skipped: ['Busy'], noticesHtml: '<div id="n">x</div>' }));
assert.ok(p.body.includes('Already being built, skipped: Busy'));
assert.ok(p.body.includes('id="n"'), 'замечания не попали в правую колонку');
assert.ok(p.body.includes('Do not lock the screen'), 'подсказка про блокировку экрана пропала');
assert.ok(p.body.includes('class="pc-i"'), 'подсказка ⓘ пропала');
console.log('ok');
