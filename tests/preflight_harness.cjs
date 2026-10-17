// Pre-flight audit (ACL-1819): клиентские правила страницы на живом коде шаблона.
//
// Режимы:
//   node preflight_harness.cjs <template> --evaluate <fixtures.json>
//       -> {имя: [вердикты cbPreflight.evaluate]}  (сверяется с evaluate_client на Python)
//   node preflight_harness.cjs <template> --lines <cases.json>
//       -> {имя: {lines, count, blocks}}  (карточка: клиент + серверные вердикты)
//   node preflight_harness.cjs <template> --topics <cases.json>
//       -> {имя: [{id, label, status, rules}]}  (разделы карточки, ACL-1990)
// Таблица правил — тот же JSON, что уходит на страницу (src/fb_api/preflight_rules.json).
const fs = require('fs'), vm = require('vm'), assert = require('assert'), path = require('path');
const html = fs.readFileSync(process.argv[2], 'utf8');
const start = html.indexOf('window.cbPreflight = (function () {');
assert(start > 0, 'window.cbPreflight не найден в шаблоне');
const tail = '\n})();';
const code = html.slice(start, html.indexOf(tail, start) + tail.length);
const rules = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'src', 'fb_api', 'preflight_rules.json'), 'utf8'));
const sandbox = { window: { CB_PREFLIGHT_RULES: rules }, Object, Array, JSON, RegExp, Number, String, parseInt, isFinite };
vm.runInNewContext(code, sandbox);
const P = sandbox.window.cbPreflight;

const mode = process.argv[3];
const data = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const out = {};
if (mode === '--evaluate') {
  Object.keys(data).forEach((k) => { out[k] = P.evaluate(data[k]); });
} else if (mode === '--lines') {
  Object.keys(data).forEach((k) => {
    const c = data[k];
    const lines = P.lines(c.input, c.opts || {});
    out[k] = { lines, count: P.count(lines), blocks: P.blocks(lines) };
  });
} else if (mode === '--topics') {
  // ACL-1990: разделы карточки из тех же строк.
  Object.keys(data).forEach((k) => {
    const c = data[k];
    out[k] = P.topics(P.lines(c.input, c.opts || {})).map((t) => ({ id: t.id, label: t.label, status: t.status,
      rules: t.lines.map((x) => x.rule) }));
  });
} else if (mode === '--names') {
  out.names = data.map((x) => P.adName(x[0], x[1]));
} else {
  throw new Error('режим: --evaluate | --lines | --topics | --names');
}
process.stdout.write(JSON.stringify(out));
