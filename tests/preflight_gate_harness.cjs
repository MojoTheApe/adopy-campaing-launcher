// Ворота Publish pre-flight audit (ACL-1819) на живом коде шаблона.
//
// Берутся настоящие cbPreflight (правила) и блок «серверные проверки» (cbPreflightRun,
// cbPreflightGate, предупреждения); дерево, сеть и кнопки — заглушки. Проверяется:
//  - клиентская ошибка блокирует Publish сразу и без единого запроса;
//  - при API серверные вердикты обновляются перед отправкой, и ошибка из них блокирует;
//  - свежие вердикты не запрашиваются заново; правка кампании делает их устаревшими;
//  - предупреждения не блокируют и попадают в окно подтверждения;
//  - при RPA запросов нет вовсе, ворота смотрят только клиентские ошибки;
//  - второе нажатие во время проверки её не дублирует.
const fs = require('fs'), vm = require('vm'), assert = require('assert'), path = require('path');
const html = fs.readFileSync(process.argv[2], 'utf8');
const tail = '\n})();';
function slice(from, to) {
  const i = html.indexOf(from);
  assert(i > 0, 'не найдено: ' + from);
  const j = html.indexOf(to, i);
  assert(j > i, 'не найден конец: ' + to);
  return html.slice(i, j);
}
const pstart = html.indexOf('window.cbPreflight = (function () {');
const preflight = html.slice(pstart, html.indexOf(tail, pstart) + tail.length);
const block = slice('/* ── ACL-1819: серверные проверки pre-flight audit', '/* ── ACL-1761: «Check plan (no writes)»');
const rules = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'src', 'fb_api', 'preflight_rules.json'), 'utf8'));

function world(backend, fetchReply) {
  const log = { fetched: [], opened: [], proceeded: 0, alerts: [] };
  const w = { CB_PREFLIGHT_RULES: rules, ACL_FB_BACKEND: backend, alert: (m) => log.alerts.push(m) };
  // ACL-141: страница говорит своим окном — та же запись.
  w.aclAlert = (m) => { log.alerts.push(m); return Promise.resolve(); };
  const sandbox = {
    window: w, Object, Array, JSON, RegExp, Number, String, parseInt, isFinite, Promise, Date,
    aclEscHtml: (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    cbApiMode: () => backend === 'api',
    cbPlanFormData: (form, only) => ({ only: only ? only.length : 0 }),
    fetch: async (url, init) => {
      log.fetched.push(url);
      await new Promise((r) => setTimeout(r, 5));
      return { json: async () => (typeof fetchReply === 'function' ? fetchReply() : fetchReply) };
    },
    document: { querySelector: () => ({ disabled: false, title: '' }) },
    setTimeout,
    aclAlert: (m) => w.aclAlert(m),
  };
  w.window = w;
  vm.createContext(sandbox);
  vm.runInContext(preflight, sandbox);
  w.cbPfRev = (slot) => slot._rev;
  // Дерево: строки карточки считает настоящий cbPreflight из того, что лежит на форме.
  w.cbTree = {
    refreshEditor() {}, refreshAudit() {},
    open: (id) => log.opened.push(id),
    auditLines(slot) {
      const a = slot._form._pfAudit, name = slot._name, hit = a && a.byName[name];
      const opts = { backend, server: null, stale: false };
      if (backend === 'api' && hit) {
        opts.server = hit.audit || []; opts.planError = hit.error || ''; opts.accountAudit = hit.accountAudit || null;
        opts.stale = hit.rev !== slot._rev;
      }
      return w.cbPreflight.lines(slot._input, opts);
    },
  };
  vm.runInContext(block, sandbox);
  return { w, log, sandbox };
}

function okInput() {
  return { destination: 'website', objective: 'leads', link: 'https://track.example.com/c?s=1', country: 'Mexico',
    budget: '20', age_min: 35, age_max: 35,
    ads: [{ name: 'a - t0', file: 'a.png', form_id: '', body: 'Body', headline: 'Head', cta: 'LEARN_MORE' }] };
}

function slotOf(form, name, input, id) {
  return {
    _form: form, _name: name, _input: input, _rev: 'r1', dataset: { cbdId: id, idx: '0' },
    closest: () => form,
    querySelector: (sel) => (sel === '.cb-name-hidden' ? { value: name } : null),
  };
}

function itemsFor(slots, form) { return [{ form: form, slots: slots }]; }

(async () => {
  // 1. Клиентская ошибка: не пускает, запросов нет, нужная кампания открыта.
  {
    const { w, log } = world('api', { results: [] });
    const form = {}, bad = okInput(); bad.link = '';
    const s = slotOf(form, 'C1', bad, 'cbs1');
    w.cbPreflightGate(itemsFor([s], form), () => { log.proceeded += 1; });
    await new Promise((r) => setTimeout(r, 30));
    assert.equal(log.proceeded, 0, 'клиентская ошибка пропустила Publish');
    assert.deepEqual(log.fetched, [], 'проверка кабинета пошла при ошибке, которую видно и так');
    assert.deepEqual(log.opened, ['cbs1']);
  }
  // 2. RPA: чистая кампания идёт, запросов нет; ошибка блокирует, запросов нет.
  {
    const { w, log } = world('rpa', {});
    const form = {};
    w.cbPreflightGate(itemsFor([slotOf(form, 'C1', okInput(), 'cbs1')], form), () => { log.proceeded += 1; });
    assert.equal(log.proceeded, 1, 'RPA: чистая кампания не пошла');
    const bad = okInput(); bad.ads[0].file = '';
    w.cbPreflightGate(itemsFor([slotOf(form, 'C2', bad, 'cbs2')], form), () => { log.proceeded += 1; });
    assert.equal(log.proceeded, 1, 'RPA: клиентская ошибка пропустила Publish');
    await new Promise((r) => setTimeout(r, 30));
    assert.deepEqual(log.fetched, [], 'RPA: ушёл запрос аккаунта');
  }
  // 3. API: устаревшее — проверка перед Publish; серверная ошибка блокирует.
  {
    const reply = { results: [{ campaign_name: 'C1', error: null, audit: [
      { rule: 'page_ok', status: 'error', detail: 'The page from the account map cannot advertise from this ad account.', ads: [] }] }] };
    const { w, log } = world('api', reply);
    const form = {};
    const s = slotOf(form, 'C1', okInput(), 'cbs1');
    w.cbPreflightGate(itemsFor([s], form), () => { log.proceeded += 1; });
    await new Promise((r) => setTimeout(r, 60));
    assert.deepEqual(log.fetched, ['/publish-plan'], 'перед Publish не обновились серверные проверки');
    assert.equal(log.proceeded, 0, 'серверная ошибка пропустила Publish');
    assert.deepEqual(log.opened, ['cbs1']);
    assert.equal(w.cbTree.auditLines(s).filter((x) => x.rule === 'page_ok')[0].status, 'error');
  }
  // 4. API: серверно чисто — идёт; свежее не запрашивается; правка делает устаревшим; предупреждения — в окно.
  {
    const reply = { results: [{ campaign_name: 'C1', error: null, audit: [
      { rule: 'media_vertical', status: 'warning', detail: 'Ad 1', ads: [1] },
      { rule: 'page_ok', status: 'ok', detail: '', ads: [] }] }] };
    const { w, log } = world('api', reply);
    const form = {}, inp = okInput(); inp.ads[0].headline = 'h'.repeat(50);
    const s = slotOf(form, 'C1', inp, 'cbs1');
    w.cbPreflightGate(itemsFor([s], form), () => { log.proceeded += 1; });
    await new Promise((r) => setTimeout(r, 60));
    assert.equal(log.proceeded, 1, 'только предупреждения: Publish не пошёл');
    const warn = w.cbPreflightWarnHtml();
    assert.ok(/Pre-flight warnings/.test(warn) && /Headline up to 40/.test(warn) && /Vertical file/.test(warn), warn);
    assert.equal(w.cbPreflightWarnHtml(), '', 'предупреждения читаются один раз');
    w.cbPreflightGate(itemsFor([s], form), () => { log.proceeded += 1; });
    await new Promise((r) => setTimeout(r, 30));
    assert.equal(log.fetched.length, 1, 'свежие вердикты запрошены заново');
    assert.equal(log.proceeded, 2);
    s._rev = 'r2';                                   // правка кампании
    w.cbPreflightGate(itemsFor([s], form), () => { log.proceeded += 1; });
    await new Promise((r) => setTimeout(r, 60));
    assert.equal(log.fetched.length, 2, 'после правки проверка не обновилась');
  }
  // 5. Второе нажатие во время проверки её не дублирует.
  {
    const { w, log } = world('api', { results: [{ campaign_name: 'C1', error: null, audit: [] }] });
    const form = {}, s = slotOf(form, 'C1', okInput(), 'cbs1');
    w.cbPreflightGate(itemsFor([s], form), () => { log.proceeded += 1; });
    w.cbPreflightGate(itemsFor([s], form), () => { log.proceeded += 1; });
    await new Promise((r) => setTimeout(r, 80));
    assert.equal(log.fetched.length, 1, 'проверка продублирована');
    assert.equal(log.proceeded, 1, 'Publish ушёл дважды');
  }
  // 6. Сервер недоступен: кампания не проверена — ошибка плана блокирует, а не пропускает.
  {
    const { w, log } = world('api', { error: 'The server did not answer' });
    const form = {}, s = slotOf(form, 'C1', okInput(), 'cbs1');
    w.cbPreflightGate(itemsFor([s], form), () => { log.proceeded += 1; });
    await new Promise((r) => setTimeout(r, 60));
    assert.equal(log.proceeded, 0, 'не проверенная кампания ушла в Publish');
  }
  // 7. Одна из двух кампаний с ошибкой — не пускает обе (Publish all).
  {
    const { w, log } = world('rpa', {});
    const form = {}, bad = okInput(); bad.country = '';
    w.cbPreflightGate(itemsFor([slotOf(form, 'A', okInput(), 'a'), slotOf(form, 'B', bad, 'b')], form),
      () => { log.proceeded += 1; });
    assert.equal(log.proceeded, 0);
    assert.deepEqual(log.opened, ['b']);
  }
  // 8. Ворота закрыты наглухо: нет дерева, нет таблицы правил или аудит падает — Publish не идёт.
  {
    const MSG = 'Pre-flight audit unavailable — reload the page';
    for (const backend of ['api', 'rpa']) {
      let x = world(backend, {});
      const form = {}, s = slotOf(form, 'C1', okInput(), 'cbs1');
      delete x.w.cbTree;
      x.w.cbPreflightGate(itemsFor([s], form), () => { x.log.proceeded += 1; });
      assert.equal(x.log.proceeded, 0, backend + ': без дерева Publish прошёл');
      assert.deepEqual(x.log.alerts, [MSG]);
      assert.equal(x.w.CB_PF_GATE.blocked, true);
      assert.equal(x.w.CB_PF_GATE.reason, MSG);

      x = world(backend, {});
      x.w.cbPreflight = undefined;
      x.w.cbPreflightGate(itemsFor([slotOf(form, 'C1', okInput(), 'cbs1')], form), () => { x.log.proceeded += 1; });
      assert.equal(x.log.proceeded, 0, backend + ': без правил Publish прошёл');
      assert.deepEqual(x.log.alerts, [MSG]);

      x = world(backend, {});
      x.w.CB_PREFLIGHT_RULES = { rules: [], consts: {} };
      x.w.cbPreflightGate(itemsFor([slotOf(form, 'C1', okInput(), 'cbs1')], form), () => { x.log.proceeded += 1; });
      assert.equal(x.log.proceeded, 0, backend + ': с пустой таблицей Publish прошёл');
      assert.deepEqual(x.log.alerts, [MSG]);

      x = world(backend, {});
      x.w.cbTree.auditLines = () => { throw new Error('boom'); };
      x.w.cbPreflightGate(itemsFor([slotOf(form, 'C1', okInput(), 'cbs1')], form), () => { x.log.proceeded += 1; });
      assert.equal(x.log.proceeded, 0, backend + ': при сбое аудита Publish прошёл');
      assert.deepEqual(x.log.alerts, [MSG]);
    }
  }
  process.stdout.write('ok');
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
