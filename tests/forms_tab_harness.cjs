// Вкладка Forms (ACL-2054), прогнанная по-настоящему: код страницы исполняется,
// а не вычитывается глазами. Проверка шаблона берётся живьём из cbLayout
// (зеркало form_templates.validate), как в form_templates_harness.cjs.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

const MARK = '// ── Forms: общая библиотека шаблонов Instant Form (ACL-2054) ──';
const start = html.indexOf(MARK);
assert(start !== -1, 'кода вкладки Forms на странице нет');
const code = html.slice(start, html.indexOf('</script>', start));
const li = html.indexOf('window.cbLayout = (function () {');
assert(li > 0, 'cbLayout не найден');
const layoutCode = html.slice(li, html.indexOf('\n})();', li)) + '\n})();';

const VALID = {
  id: 'aaaaaaaaaaaa', name: 'Demo <"form">', headline: 'Get more information', intro: '',
  contact_fields: ['name', 'email'],
  questions: [{label: 'Budget', type: 'multiple_choice', options: ['Low', 'High']}],
  privacy_url: 'https://example.com/privacy', thank_you_text: 'Thank you.', website_url: '',
  locale: 'EN_US', future_field: 'keep me',
};
const rows = [
  {id: 'aaaaaaaaaaaa', name: 'Demo <"form">', locale: 'EN_US', summary: 'Name, Email · 1 question',
   author: 'Anna', changed_at: '2026-10-21 10:00:00 UTC', revision: 'revision-opened-by-anna', invalid: false, errors: [], template: VALID},
  {id: 'bbbbbbbbbbbb', name: 'Broken form', locale: 'EN_US', summary: 'Name · no questions',
   author: 'Boris', changed_at: '', revision: 'broken-row-revision', invalid: true, errors: ['Privacy policy URL must start with https://'],
   template: {id: 'bbbbbbbbbbbb', name: 'Broken form', headline: 'H', privacy_url: 'http://x.example.com',
              thank_you_text: 'T', contact_fields: ['name'], questions: [], locale: 'EN_US'}},
];

const elements = {}, listeners = {}, requests = [], events = [];
const get = id => elements[id] ||= {
  id, value: '', style: {}, hidden: false, disabled: false, checked: false, innerHTML: '', textContent: '',
  getAttribute() { return null; }, scrollIntoView() {},
};
let confirmed = true, saveOk = true;
let saveError = 'A template named \'X\' already exists';
let library = {connected: true, ok: true, error: '', synced_at: '2026-10-21 10:05:00 UTC'};
let migration = {available: true, uploadable: 2, skipped: [
  {name: 'Demo <"form">', reason: 'Already in the team library'},
  {name: 'Old local', reason: 'Invalid template: Headline is required'}]};
const context = {
  document: {
    getElementById: get,
    querySelectorAll() { return Object.values(elements); },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    dispatchEvent(event) { events.push(event); (listeners[event.type] || []).forEach(fn => fn(event)); return true; },
  },
  location: {hash: ''},
  CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
  confirm() { return confirmed; },
  fetch: async (url, opts) => {
    if (opts) {
      requests.push({url, body: JSON.parse(opts.body)});
      return saveOk
        ? {ok: true, json: async () => ({ok: true, template: {name: 'Copy'}, uploaded: 2,
                                         skipped: [{name: 'Old local', reason: 'Invalid template'}]})}
        : {ok: false, json: async () => ({ok: false, errors: [saveError]})};
    }
    requests.push({url, body: null});
    return {ok: true, json: async () => ({
      ok: true, rows, templates: [VALID], library, migration,
      deleted: [{id: 'cccccccccccc', name: 'Old <b>', deleted_by: 'Anna', deleted_at: '2026-10-20 09:00:00 UTC',
                 summary: 'Name · no questions'}],
    })};
  },
};
context.window = context;
vm.createContext(context);
vm.runInContext(layoutCode, context);
vm.runInContext(code, context);

const flush = () => new Promise(r => setImmediate(r));
const fire = (type, target) => (listeners[type] || []).forEach(fn => fn({target}));
const click = (id, attrs = {}) => fire('click', {id, getAttribute(k) { return attrs[k] ?? null; }});
const type = (id, value) => { get(id).value = value; fire('input', get(id)); };
const lastPost = () => requests.filter(r => r.body).pop();

(async () => {
  context.document.dispatchEvent(new context.CustomEvent('acl:viewchange', {detail: {view: 'forms'}}));
  await flush();
  const list = () => get('fm-list').innerHTML;
  assert(requests[0].url.includes('force=true'), 'вкладка должна перечитывать библиотеку');

  // Список: имя, автор, время, язык, состав; имя экранировано.
  assert(!list().includes('<"form">'), 'имя попало в разметку сырым: ' + list().slice(0, 200));
  assert(list().includes('Demo &lt;&quot;form&quot;&gt;'));
  assert(list().includes('Name, Email · 1 question') && list().includes('Anna') && list().includes('EN_US'));
  assert.equal(get('fm-count').textContent, '2 templates');
  assert(get('fm-status').textContent.includes('refreshed 2026-10-21 10:05:00 UTC'));

  // Битая запись: на месте, с бейджем и причиной; Duplicate ей не предлагается, Edit и Delete - да.
  const broken = list().slice(list().indexOf('Broken form') - 40);
  assert(broken.includes('invalid') && broken.includes('must start with https://'));
  assert(broken.includes('data-fm-edit="bbbbbbbbbbbb"') && broken.includes('data-fm-del="bbbbbbbbbbbb"'));
  assert(!broken.includes('data-fm-dup="bbbbbbbbbbbb"'), 'битый шаблон нельзя дублировать');
  assert(list().includes('data-fm-dup="aaaaaaaaaaaa"'));

  // Билдер получает список годных шаблонов.
  const ev = events.filter(e => e.type === 'acl:formtemplateschange').pop();
  assert.deepEqual(ev.detail.templates.map(t => t.id), ['aaaaaaaaaaaa']);

  // Перенос: кнопка с числом; в «не загрузятся» нет шума про «Already in».
  assert.equal(get('fm-migrate').hidden, false);
  assert(get('fm-migrate').innerHTML.includes('Upload 2 local templates to the team library'));
  assert(get('fm-migrate').innerHTML.includes('Old local') && !get('fm-migrate').innerHTML.includes('Already in'));

  // Удалённое: автор, экранирование, Restore.
  assert(get('fm-deleted').innerHTML.includes('removed by Anna'));
  assert(!get('fm-deleted').innerHTML.includes('Old <b>'));

  // Поиск.
  type('fm-search', 'broken');
  assert.equal(get('fm-count').textContent, '1 of 2 templates');
  assert(!list().includes('Demo'));
  type('fm-search', '');

  // New: пустая форма с Name и Email по умолчанию.
  click('fm-new-btn');
  assert.equal(get('fm-editor').hidden, false);
  assert.equal(get('fm-name').value, '');
  assert(get('fm-c-name').checked && get('fm-c-email').checked && !get('fm-c-phone').checked);

  // Клиентская проверка (зеркало Python) не пускает на сервер.
  const posts = requests.filter(r => r.body).length;
  click('fm-save-btn'); await flush();
  assert.equal(requests.filter(r => r.body).length, posts, 'пустой шаблон ушёл на сервер');
  assert(get('fm-editor-msg').textContent.includes('Template name is required'), get('fm-editor-msg').textContent);

  // Edit: все поля подставлены, id и чужие поля едут обратно; вопросы правятся.
  click('', {'data-fm-edit': 'aaaaaaaaaaaa'});
  assert.equal(get('fm-name').value, 'Demo <"form">');
  assert(get('fm-questions').innerHTML.includes('Budget') && get('fm-questions').innerHTML.includes('Low\nHigh'));
  assert.equal(get('fm-invalid-note').hidden, true);
  type('fm-headline', 'New headline');
  const q = {getAttribute(k) { return {'data-fm-q': '0', 'data-fm-f': 'label'}[k] ?? null; }, value: 'Budget?'};
  fire('input', q);
  click('fm-add-q-btn');
  assert(get('fm-questions').innerHTML.includes('data-fm-q="1"'));
  click('', {'data-fm-qdel': '1'});
  assert(!get('fm-questions').innerHTML.includes('data-fm-q="1"'));
  click('fm-save-btn'); await flush();
  const saved = lastPost();
  assert.equal(saved.url, '/api/form-templates');
  assert.equal(saved.body.id, 'aaaaaaaaaaaa');
  assert.equal(saved.body._expected_revision, 'revision-opened-by-anna');
  assert.equal(saved.body.headline, 'New headline');
  assert.equal(saved.body.questions[0].label, 'Budget?');
  assert.deepEqual(saved.body.questions[0].options, ['Low', 'High']);
  assert.equal(saved.body.future_field, 'keep me', 'поле, которого нет в редакторе, потерялось');
  assert(get('fm-msg').textContent.includes('Saved') && get('fm-msg').textContent.includes('already on Pages'));
  assert.equal(get('fm-editor').hidden, true);

  // Edit битого: причина сверху, сохранение идёт с его id.
  click('', {'data-fm-edit': 'bbbbbbbbbbbb'});
  assert.equal(get('fm-invalid-note').hidden, false);
  assert(get('fm-invalid-note').textContent.includes('must start with https://'));
  type('fm-privacy', 'https://example.com/privacy');
  click('fm-save-btn'); await flush();
  assert.equal(lastPost().body.id, 'bbbbbbbbbbbb');
  assert.equal(lastPost().body._expected_revision, 'broken-row-revision');
  assert.equal(lastPost().body.privacy_url, 'https://example.com/privacy');

  // Отказ сервера виден на экране редактора.
  click('', {'data-fm-edit': 'aaaaaaaaaaaa'});
  saveOk = false;
  click('fm-save-btn'); await flush();
  assert(get('fm-editor-msg').textContent.includes('already exists'), get('fm-editor-msg').textContent);
  assert.equal(get('fm-editor').hidden, false, 'при отказе редактор должен остаться открытым');
  // A concurrency refusal keeps both the unsaved text and the old precondition.
  saveError = 'This template changed since you opened it. Nothing was saved. Keep your edits, then reopen the template to compare the latest version.';
  type('fm-headline', 'Keep this unsaved headline');
  click('fm-save-btn'); await flush();
  assert(get('fm-editor-msg').textContent.includes('changed since you opened'));
  assert.equal(get('fm-headline').value, 'Keep this unsaved headline');
  assert.equal(lastPost().body._expected_revision, 'revision-opened-by-anna');
  assert.equal(get('fm-editor').hidden, false);
  saveOk = true;
  click('fm-cancel-btn');
  assert.equal(get('fm-editor').hidden, true);

  // Duplicate, Delete (с подтверждением), Restore, перенос.
  click('', {'data-fm-dup': 'aaaaaaaaaaaa'}); await flush();
  assert.equal(lastPost().url, '/api/form-templates/duplicate');
  assert(get('fm-msg').textContent.includes('Duplicated as'));
  let n = requests.filter(r => r.body).length;
  confirmed = false;
  click('', {'data-fm-del': 'aaaaaaaaaaaa'}); await flush();
  assert.equal(requests.filter(r => r.body).length, n, 'удаление ушло на сервер без подтверждения');
  confirmed = true;
  click('', {'data-fm-del': 'aaaaaaaaaaaa'}); await flush();
  assert.equal(lastPost().url, '/api/form-templates/delete');
  assert(get('fm-msg').textContent.includes('Recently deleted'));
  click('', {'data-fm-restore': 'cccccccccccc'}); await flush();
  assert.equal(lastPost().url, '/api/form-templates/restore');
  assert.equal(lastPost().body.id, 'cccccccccccc');
  click('fm-migrate-btn'); await flush();
  assert.equal(lastPost().url, '/api/form-templates/migrate');
  assert(get('fm-msg').textContent.includes('Uploaded 2 local templates') && get('fm-msg').textContent.includes('Old local'),
    get('fm-msg').textContent);

  // Библиотека недоступна: по копии, и это сказано прямо.
  library = {connected: true, ok: false, error: 'GitHub is unreachable', synced_at: '2026-10-21 10:05:00 UTC'};
  click('fm-refresh-btn'); await flush();
  assert(get('fm-status').textContent.includes('unreachable') && get('fm-status').textContent.includes('cannot be saved'));

  // Без библиотеки: файл на машине, и сообщения не врут про «всю команду».
  library = {connected: false, ok: true};
  migration = {available: false, uploadable: 0, skipped: []};
  click('fm-refresh-btn'); await flush();
  assert(get('fm-status').textContent.includes('not connected'));
  assert.equal(get('fm-migrate').hidden, true);
  click('', {'data-fm-edit': 'aaaaaaaaaaaa'});
  click('fm-save-btn'); await flush();
  assert(get('fm-msg').textContent.includes('on this computer only'), get('fm-msg').textContent);
})().catch(e => { console.error(e); process.exitCode = 1; });
