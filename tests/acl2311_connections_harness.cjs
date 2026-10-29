// ACL-2311: run src/web/static/connections.js against a stub document (no browser).
// argv: script path, scenario JSON {confirm: bool, response: {...}, reject: bool}
const fs = require('fs');
const [, , scriptPath, scenarioJson] = process.argv;
const scenario = JSON.parse(scenarioJson);
const calls = [];
function el(tag) {
  const e = { tag, children: [], style: {}, attrs: {}, textContent: '', disabled: false, onclick: null,
    setAttribute(k, v) { this.attrs[k] = v; }, appendChild(c) { this.children.push(c); return c; },
    remove() {}, querySelector(sel) { return registry[sel.slice(1)] || (registry[sel.slice(1)] = el('x')); } };
  let html = '';
  Object.defineProperty(e, 'innerHTML', { get() { return html; }, set(v) { html = v; if (v === '') e.children = []; } });
  return e;
}
const registry = {};
global.document = { body: el('body'), getElementById() { return null; }, createElement: el };
global.window = global;
global.confirm = () => scenario.confirm;
global.fetch = (url, opts) => {
  calls.push({ url, method: opts && opts.method });
  if (url.indexOf('/check') !== -1) return Promise.resolve({ json: () => Promise.resolve({ ok: true, checks: [] }) });
  if (scenario.reject) return Promise.reject(new Error('down'));
  return Promise.resolve({ json: () => Promise.resolve(scenario.response) });
};
eval(fs.readFileSync(scriptPath, 'utf8'));
window.aclConnections.open();
setTimeout(() => {
  registry['cx-publish'].onclick();
  setTimeout(() => {
    console.log(JSON.stringify({
      calls: calls.map(c => c.method + ' ' + c.url),
      lines: registry['cx-lines'].children.map(c => c.textContent),
      msg: registry['cx-msg'].textContent,
      disabled: registry['cx-publish'].disabled,
    }));
  }, 20);
}, 20);
