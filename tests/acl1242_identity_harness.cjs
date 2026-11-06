// ACL-1242: прогон src/web/static/identity.js на заглушке документа (без браузера).
// argv: путь к скрипту, сценарий JSON:
//   {identity, signIn, names: [...], other, reject, shown, click: [{id|label, input}]}
const fs = require('fs');
const [, , scriptPath, scenarioJson] = process.argv;
const sc = JSON.parse(scenarioJson);
const calls = [];
const byId = {};
function el(tag) {
  const e = { tag, children: [], style: {}, attrs: {}, textContent: '', disabled: false, onclick: null, oninput: null,
    hidden: false, value: '', title: '', className: '', id: '',
    setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    insertBefore(c) { this.children.unshift(c); return c; },
    remove() { this.removed = true; } };
  let html = '';
  Object.defineProperty(e, 'innerHTML', { get() { return html; }, set(v) { html = v; if (v === '') e.children = []; } });
  Object.defineProperty(e, 'firstChild', { get() { return e.children[0]; } });
  return e;
}
function walk(node, fn) { fn(node); (node.children || []).forEach(c => { if (!c.removed) walk(c, fn); }); }
function find(pred) { let out = null; walk(body, n => { if (!out && pred(n)) out = n; }); return out; }
const body = el('body');
const chip = el('span'); chip.id = 'identity-chip'; chip.hidden = true;
global.window = global;
global.sessionStorage = { getItem: () => (sc.shown ? '1' : null), setItem() {} };
global.__aclIdentityManual = true;
global.document = { body, createElement: el,
  getElementById: id => (id === 'identity-chip' ? chip : find(n => n.id === id)) };
global.fetch = (url, opts) => {
  calls.push(((opts && opts.method) || 'GET') + ' ' + url + (opts && opts.body && opts.body !== '{}' ? ' ' + opts.body : ''));
  let payload;
  if (url === '/api/identity') payload = sc.identity;
  else if (url === '/api/identity/sign-in') { if (sc.reject) return Promise.reject(new Error('down')); payload = sc.signIn; }
  else if (url === '/api/identity/name') payload = (sc.names || []).shift();
  else payload = sc.other || { ok: true };
  return Promise.resolve({ json: () => Promise.resolve(payload) });
};
eval(fs.readFileSync(scriptPath, 'utf8'));
const wait = () => new Promise(r => setTimeout(r, 10));
function box() { return find(n => n.id === 'identity-box'); }
function text() { const out = []; const b = box(); if (b) walk(b, n => { if (n.textContent) out.push(n.textContent); }); return out.join(' | '); }
function buttons() { const out = []; const b = box(); if (b) walk(b, n => { if (n.tag === 'button') out.push(n.textContent); }); return out; }
(async () => {
  await window.aclIdentity.init();
  const steps = [];
  const snap = () => steps.push({ text: text(), buttons: buttons(), chip: chip.textContent, chipTitle: chip.title });
  snap();
  for (const act of sc.click || []) {
    if (act.input !== undefined) find(n => n.id === 'identity-name').value = act.input;
    const target = act.id ? find(n => n.id === act.id) : find(n => n.tag === 'button' && n.textContent === act.label && box());
    target.onclick();
    await wait(); snap();
  }
  console.log(JSON.stringify({ calls, steps, overlay: !!box() }));
})();
