// ACL-1807: cbMetaCaps (допустимые значения из реестра Meta) на живом коде шаблона.
//   node meta_caps_harness.cjs <template> <caps.json|-> <cases.json>
//   -> {имя: результат}; случай: {fn: 'violations'|'options'|'lines'|'destinationAllowed'|'preflight', args: [...]}
// 'preflight' гонит cbPreflight.lines и отдаёт только подмешанные строки meta_rules.
const fs = require('fs'), vm = require('vm'), assert = require('assert'), path = require('path');
const html = fs.readFileSync(process.argv[2], 'utf8').replace(/\r\n/g, '\n');
function slice(startMark) {
  const start = html.indexOf(startMark);
  assert(start > 0, startMark + ' не найден в шаблоне');
  const tail = '\n})();';
  return html.slice(start, html.indexOf(tail, start) + tail.length);
}
const caps = process.argv[3] === '-' ? {} : JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const rules = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'src', 'fb_api', 'preflight_rules.json'), 'utf8'));
const sandbox = { window: { CB_META_CAPS: caps, CB_PREFLIGHT_RULES: rules }, Object, Array, JSON, RegExp, Number, String, parseInt, isFinite };
vm.createContext(sandbox);
vm.runInContext(slice('window.cbPreflight = (function () {'), sandbox);
vm.runInContext(slice('window.cbMetaCaps = (function () {'), sandbox);
const M = sandbox.window.cbMetaCaps, P = sandbox.window.cbPreflight;
const cases = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const out = {};
Object.keys(cases).forEach((k) => {
  const c = cases[k];
  if (c.fn === 'preflight') {
    const ls = P.lines(c.args[0], c.args[1] || {});
    out[k] = { metaLines: ls.filter((x) => x.rule === 'meta_rules'), blocks: P.blocks(ls) };
  } else {
    out[k] = M[c.fn].apply(null, c.args);
  }
});
process.stdout.write(JSON.stringify(out));
