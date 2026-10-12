// ACL-1826: Publish all -> две формы из трёх остановлены потерянной сессией
// MLX -> кнопка Retry в баннере. Настоящий код страницы (cbPublishAll,
// cbNewRun, showQueue, cbFormKey...) берётся срезами из шаблона; подделаны
// только края: DOM, fetch, превью, опрос задания, подтверждение.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

function slice(startMark, endMark) {
  const s = html.indexOf(startMark);
  assert(s >= 0, 'нет в шаблоне: ' + startMark);
  const e = html.indexOf(endMark, s);
  assert(e > s, 'нет конца для: ' + startMark);
  return html.slice(s, e + endMark.length);
}
const top = (name) => slice('\nfunction ' + name + '(', '\n}\n');
const code = [
  top('cbBusyError'), top('cbPostPublish'), top('cbNewRun'), top('cbPublishRun'),
  top('_runPublishQueue'), top('cbQueueUncertain'), top('cbFormAccount'), top('cbFormKey'),
  top('cbKeyMatches'), top('cbFormTitle'), top('cbAccountBusy'), top('cbSayAlreadyBuilding'),
  top('cbSkipByName'), top('cbFormDataSkipping'),
  'var CB_PUBLISH_TIMEOUT_MS = 45000;',
  slice('\nwindow.cbPublishAll = function', '\n};\n'),
  // showQueue живёт в замыкании ящика — берём его вместе со справочником остановок.
  slice('  var AMLX_STOPS =', '\n'),
  slice('  function showQueue(p) {', '\n  }\n'),
].join('\n');

// ── поддельный край ────────────────────────────────────────────────────
function field(name, value) { return { name, value }; }
function mkForm(country, account) {
  const f = {
    country, account,
    querySelector(sel) {
      const m = /^\[name="([^"]+)"\]$/.exec(sel);
      if (m) {
        const v = { account_label: account, country, camp_count: '1', camp_0_name: 'C-' + account }[m[1]];
        return v === undefined ? null : field(m[1], v);
      }
      return null;                     // .cb-status-panel, .cb-publish-btn — нет
    },
    querySelectorAll(sel) { return sel === '.cb-slot' ? [{ dataset: {} }] : []; },
  };
  return f;
}
const forms = [mkForm('Peru', 'UL1'), mkForm('Peru', 'UL2'), mkForm('Peru', 'UL3')];

const posts = [];            // что реально ушло на /publish-jobs
let lostAccounts = { UL2: 1, UL3: 1 };
const LOST = 'The shared Multilogin session was lost';
const alerts = [];
let btnHandler = null;
const box = {
  hidden: true, _html: '',
  get innerHTML() { return this._html; },
  set innerHTML(v) { this._html = v; btnHandler = null; },
  querySelector(sel) {
    if (sel === '[data-cb-stop-retry]' && this._html.includes('data-cb-stop-retry'))
      return { addEventListener(ev, fn) { if (ev === 'click') btnHandler = fn; } };
    return null;
  },
  querySelectorAll() { return []; },
};
const foot = { querySelector(sel) { return sel === '.cbd-queue' ? box : { disabled: false }; } };

const sandbox = {
  aclEscHtml: (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => '&#' + c.charCodeAt(0) + ';'),
  console, FormData, JSON, Promise, setTimeout, clearTimeout, AbortController, Date, Math, Object, Array, String, Error,
  document: { querySelectorAll: (sel) => (sel === 'form.open-account-form' ? forms : []) },
  alert: (m) => alerts.push(String(m)),
  foot, refreshTicks() {}, paintFoot() {}, cbReviewWait: () => '', cbReviewRelease: () => '',
  selectedForPublish: () => null,
  _safeConfirm: (_t, _b, onConfirm) => onConfirm(),
  cbApiMode: () => false, cbPublishMode: () => '', cbPublishTarget: () => 'x',
  cbRequestId: () => 'req-' + Math.random().toString(36).slice(2),
  cbSentCampaignNames: () => [], cbBlockingErrors: () => [],
  cbPollJob(_id, _panel, _btn, done) { Promise.resolve().then(() => done({ status: 'done', errors: [], results: [{ publishable: true }] })); },
  cbPreview(form, done) {
    setTimeout(() => {
      const snap = new FormData();
      snap.set('account_label', form.account); snap.set('country', form.country);
      snap.set('camp_count', '1'); snap.set('camp_0_name', 'C-' + form.account);
      done([{ 'Campaign Name': 'C-' + form.account }], null, snap);
    }, 0);
  },
  fetch: async (url, init) => {
    assert.equal(url, '/publish-jobs');
    const fd = init.body;
    const acc = fd.get('account_label');
    posts.push({ account: acc, batch: fd.get('batch') });
    const body = lostAccounts[acc]
      ? { error: LOST, code: 'mlx_session_lost' }
      : { job_id: 'job-' + acc + '-' + posts.length };
    return { status: 200, json: async () => body };
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);

const wait = () => new Promise((r) => setTimeout(r, 60));
(async () => {
  const out = {};
  sandbox.window.cbPublishAll(sandbox.showQueue || undefined);
  await wait();
  out.first = posts.splice(0);
  out.banner = /data-cb-stop-retry/.test(box.innerHTML) ? box.innerHTML : '';
  out.handler_present = typeof btnHandler === 'function';
  lostAccounts = {};                    // при повторе сессия снова жива
  if (btnHandler) btnHandler();
  await wait();
  out.retry = posts.splice(0);
  out.alerts = alerts;
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
