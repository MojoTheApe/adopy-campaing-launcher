// Шаблоны форм (ACL-1776): проверка шаблона на странице. Код берётся живьём из
// шаблона (cbLayout); сверка с Python-версией идёт в test_acl1776_create_forms.py.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

const i = html.indexOf('window.cbLayout = (function () {');
assert(i > 0, 'cbLayout не найден');
const j = html.indexOf('\n})();', i);
const code = html.slice(i, j) + '\n})();';
const sandbox = { console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const L = sandbox.window.cbLayout;

const out = {};
Object.keys(cases).forEach((k) => { out[k] = L.validateTemplate(cases[k]); });
assert.equal(L.formSource('template'), 'template');
assert.equal(L.formSource('whatever'), 'existing');
assert.equal(L.formSource(undefined), 'existing');
const opts = L.templateOptions([{ id: 'a', name: 'A' }, { id: 'a', name: 'dup' }], 'zz');
assert.deepEqual(opts.map((o) => o.id), ['a', 'zz']);
assert.ok(opts[1].missing && /not in the library/.test(opts[1].label));
// маршрутизация сообщений сервера на выбор шаблона
const sels = (m) => L.routes(m).map((r) => r.sel).join('|');
['Ad 1: the form template was not found in the Forms library',
 'Ad 1: choose either an existing form or a template, not both',
 'Meta refused to create the form: x'].forEach((m) =>
  assert.ok(sels(m).indexOf('.cb-form-template-select') !== -1, 'не легло на шаблон: ' + m));
process.stdout.write(JSON.stringify(out));
