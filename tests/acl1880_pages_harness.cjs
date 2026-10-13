// ACL-1880: «Automatic» в account_pages.js на живом коде страницы, без браузера.
//   node acl1880_pages_harness.cjs <account_pages.js> <scenarios.json>
// Сценарий: {payload: ответ /api/account-pages, choose: id страницы или ''}.
// Вывод: {имя: {options, note, resolved, audit, needs}}.
const fs = require('fs'), vm = require('vm');
const code = fs.readFileSync(process.argv[2], 'utf8');
const scenarios = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

class Option { constructor(text, value) { this.text = text; this.value = value; } }

async function run(sc) {
  const hidden = { value: '', dispatchEvent() {} };
  const select = { options: [], value: '', disabled: false,
    replaceChildren(...o) { this.options = o; }, add(o) { this.options.push(o); },
    closest() { return slot; } };
  const note = { textContent: '' };
  const account = { value: 'Demo' };
  const form = { querySelector: (s) => (s === '[name="account_label"]' ? account : null) };
  const slot = {
    closest: (s) => (s === 'form' ? form : s === '.cb-slot' ? slot : null),
    querySelector: (s) => ({'input[name$="_page_id"]': hidden, '.cb-page-select': select, '[data-page-note]': note})[s] || null,
    getClientRects: () => [],
  };
  const document = { querySelectorAll: () => [slot], dispatchEvent() { return true; } };
  const sandbox = {
    document, Option, Object, Array, JSON, Promise, Event: class {}, CustomEvent: class {},
    FormData: class { append() {} },
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(sc.payload) }),
  };
  sandbox.window = sandbox;
  vm.runInNewContext(code, sandbox);
  sandbox.cbRefreshPages(slot, true);
  await new Promise((r) => setTimeout(r, 20));
  if (sc.choose) { select.value = sc.choose; sandbox.cbPageChanged(select); }
  return { options: select.options.map((o) => o.text), note: note.textContent,
           resolved: sandbox.cbResolvedPage(slot), audit: sandbox.cbPageAudit(slot),
           needs: sandbox.cbPageNeedsChoice(slot) };
}

(async () => {
  const out = {};
  for (const k of Object.keys(scenarios)) out[k] = await run(scenarios[k]);
  process.stdout.write(JSON.stringify(out));
})();
