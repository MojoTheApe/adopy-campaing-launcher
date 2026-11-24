// Вкладка Forms (ACL-2054), прогнанная по-настоящему: код страницы исполняется,
// а не вычитывается глазами. Проверка шаблона берётся живьём из cbLayout
// (зеркало form_templates.validate), как в form_templates_harness.cjs.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3] || 'default';

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
if (scenario === 'contacts') {
  Object.assign(VALID, {
    contact_fields: ['email', 'last_name', 'phone', 'first_name'],
    intro_title: 'Existing intro title', questions_first: true,
    follow_up_action_url: 'https://example.com/followup', form_type: 'higher_intent',
    privacy_link_text: 'Existing policy label', thank_you_title: 'Existing thank you title',
    button_text: 'Existing button label',
  });
}
if (scenario === 'legacy-locale') VALID.locale = 'UK_UA';
if (scenario === 'logic') {
  // ACL-2178: Q1 Yes -> question 3 (skips Q2), No -> close; the non-lead page rides with the template.
  Object.assign(VALID, {
    questions_first: true,
    questions: [
      {label: 'Budget', type: 'multiple_choice_logic', options: ['Yes', 'No'], logic: {Yes: 'q3', No: 'close'}},
      {label: 'Name of the company', type: 'short_answer'},
      {label: 'Phone model', type: 'short_answer'}],
    non_lead_end: {title: 'Sorry', text: 'Not for you.', website_url: 'https://example.com/', button_text: 'Visit website'},
  });
}
if (scenario === 'cover' || scenario === 'cover-pending') Object.assign(VALID, {intro: 'Tell us more.', cover_photo_id: '111', cover_page_id: '222'});
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
  getAttribute() { return null; }, scrollIntoView() {}, appendChild() {},
};
let confirmed = true, saveOk = true;
let saveError = 'A template named \'X\' already exists';
let library = {connected: true, ok: true, error: '', synced_at: '2026-10-21 10:05:00 UTC'};
let migration = {available: true, uploadable: 2, skipped: [
  {name: 'Demo <"form">', reason: 'Already in the team library'},
  {name: 'Old local', reason: 'Invalid template: Headline is required'}]};
// ACL-2188: общий выбор картинки живёт в билдере; здесь его заменяет заглушка с тем же get/set.
const coverState = {photo: '', page: '', local: '', sets: 0, pending: false, uploads: 0, result: {ok: true, sha: 'f'.repeat(64)}};
const context = {
  formCoverPicker() {
    return {
      get: () => ({cover_photo_id: coverState.photo, cover_page_id: coverState.page, cover_local_sha256: coverState.local}),
      set: (photo, page) => { coverState.photo = photo || ''; coverState.page = page || ''; coverState.sets++; coverState.pending = false; },
      // ACL-2571: a chosen file that is not yet kept on this computer; the save must keep it first or stop.
      pending: () => coverState.pending,
      ensureStored: async () => {
        if (!coverState.pending) return {ok: true, message: ''};
        coverState.uploads++;
        const r = coverState.result;
        if (!r.ok) return {ok: false, message: r.message};
        coverState.local = r.sha; coverState.pending = false;
        return {ok: true, message: ''};
      },
      gate() {},
    };
  },
  document: {
    createElement: () => ({style: {}, appendChild() {}, textContent: '', value: ''}),
    getElementById: get,
    querySelectorAll() { return Object.values(elements); },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    dispatchEvent(event) { events.push(event); (listeners[event.type] || []).forEach(fn => fn(event)); return true; },
  },
  location: {hash: ''},
  CustomEvent: class { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } },
  confirm() { return confirmed; },
  aclConfirm() { return Promise.resolve(confirmed); },
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
const click = (id, attrs = {}) => fire('click', {id, isConnected: true, getAttribute(k) { return attrs[k] ?? null; }});
const type = (id, value) => { get(id).value = value; fire('input', get(id)); };
const lastPost = () => requests.filter(r => r.body).pop();

(async () => {
  context.document.dispatchEvent(new context.CustomEvent('acl:viewchange', {detail: {view: 'forms'}}));
  await flush();
  const list = () => get('fm-list').innerHTML;
  assert(requests[0].url.includes('force=true'), 'вкладка должна перечитывать библиотеку');


  if (scenario === 'cover-pending') {
    // ACL-2571: a chosen file that is not yet kept on this computer is never dropped by Save.
    const posts = () => requests.filter(r => r.body).length;
    click('', {'data-fm-edit': VALID.id});
    coverState.photo = ''; coverState.page = ''; coverState.pending = true;     // a file is chosen, not uploaded
    get('fm-cover-page').value = '222';
    // 1. Keeping the file fails: the save stops with a visible message and sends nothing.
    coverState.result = {ok: false, message: 'The background image is larger than 8 MB.'};
    let n = posts();
    click('fm-save-btn'); await flush();
    assert.equal(posts(), n, 'saved without the chosen picture');
    assert(get('fm-editor-msg').textContent.includes('The template was not saved.'), get('fm-editor-msg').textContent);
    assert(get('fm-editor-msg').textContent.includes('larger than 8 MB'), 'no reason named');
    assert.equal(get('fm-editor-msg').style.color, 'var(--danger)');
    assert.equal(coverState.pending, true, 'the file must stay chosen after a failure');
    assert.equal(get('fm-save-btn').disabled, false, 'Save must be usable again');
    // 2. It works: one request, then one save that carries the returned hash (and no Page photo pair).
    coverState.result = {ok: true, sha: 'f'.repeat(64)};
    n = posts(); const u = coverState.uploads;
    click('fm-save-btn'); await flush();
    assert.equal(coverState.uploads, u + 1, 'the pending file was not uploaded');
    assert.equal(posts(), n + 1);
    assert.equal(lastPost().body.cover_local_sha256, 'f'.repeat(64));
    assert(!lastPost().body.cover_photo_id && !lastPost().body.cover_page_id);
    // 3. Nothing pending: Save uploads nothing.
    click('', {'data-fm-edit': VALID.id});
    const u2 = coverState.uploads;
    click('fm-save-btn'); await flush();
    assert.equal(coverState.uploads, u2);
    // 4. A pending file and an invalid form: no upload at all.
    click('', {'data-fm-edit': VALID.id});
    coverState.pending = true; type('fm-headline', '');
    const u3 = coverState.uploads; n = posts();
    click('fm-save-btn'); await flush();
    assert.equal(coverState.uploads, u3, 'uploaded a picture for a form that cannot be saved'); assert.equal(posts(), n);
    return;
  }
  if (scenario === 'cover') {
    // ACL-2188: the saved pair reaches the picker, comes back on save, and Remove blanks both keys.
    click('', {'data-fm-edit': VALID.id});
    assert.deepEqual([coverState.photo, coverState.page], ['111', '222'], 'saved cover was not given to the picker');
    click('fm-save-btn'); await flush();
    assert.deepEqual([lastPost().body.cover_photo_id, lastPost().body.cover_page_id], ['111', '222']);
    // ACL-2533: the same Page keeps the picture; another Page clears it (never re-labels it).
    click('', {'data-fm-edit': VALID.id});
    fire('change', {id: 'fm-cover-page', value: '222'});
    assert.deepEqual([coverState.photo, coverState.page], ['111', '222'], 'the same Page must keep the picture');
    fire('change', {id: 'fm-cover-page', value: '333'});
    assert.deepEqual([coverState.photo, coverState.page], ['', ''], 'another Page must clear the picture');
    click('', {'data-fm-edit': VALID.id});
    coverState.photo = ''; coverState.page = '';                       // Remove
    click('fm-save-btn'); await flush();
    assert.strictEqual(lastPost().body.cover_photo_id, '', 'Remove left the old photo id in the request');
    assert.strictEqual(lastPost().body.cover_page_id, '', 'Remove left the old page id in the request');
    // New template: no inherited cover.
    click('fm-new-btn');
    assert.deepEqual([coverState.photo, coverState.page], ['', ''], 'New inherited the previous cover');
    // A cover without a Description is refused on the page, as on the server.
    click('', {'data-fm-edit': VALID.id});
    type('fm-intro', '');
    const n = requests.filter(r => r.body).length;
    click('fm-save-btn'); await flush();
    assert.equal(requests.filter(r => r.body).length, n, 'cover without Description was submitted');
    assert(get('fm-editor-msg').textContent.includes('Background image needs a Description'));
    return;
  }

  if (scenario === 'logic') {
    const posts = () => requests.filter(r => r.body).length;
    click('', {'data-fm-edit': VALID.id});
    assert.equal(get('fm-logic-on').checked, true, 'the checkbox must be on for a template with conditions');
    assert.equal(get('fm-nonlead').hidden, false, 'the non-lead page must show while an answer closes the form');
    assert(get('fm-questions').innerHTML.includes('data-fm-logic="Yes"'), 'no row for the answer Yes');
    // 1. Open and save: conditions, the follow-up flag and the non-lead page come back unchanged.
    click('fm-save-btn'); await flush();
    assert.deepEqual(lastPost().body.questions, VALID.questions, 'Save lost or changed the conditions');
    assert.deepEqual(lastPost().body.non_lead_end, VALID.non_lead_end);
    // 2. A renamed answer is flagged, nothing is sent.
    let n = posts();
    fire('input', {getAttribute: k => ({'data-fm-q': '0', 'data-fm-f': 'options'})[k] ?? null, value: 'Maybe\nNo'});
    click('fm-save-btn'); await flush();
    assert.equal(posts(), n, 'a renamed answer was saved');
    assert(get('fm-editor-msg').textContent.includes('the condition for "Yes" does not match any answer'), get('fm-editor-msg').textContent);
    // 3. Removing question 2 renumbers the jump to question 3 (it becomes question 2).
    click('', {'data-fm-edit': VALID.id});
    click('', {'data-fm-qdel': '1'});
    click('fm-save-btn'); await flush();
    assert.equal(lastPost().body.questions.length, 2);
    assert.equal(lastPost().body.questions[0].logic.Yes, 'q2', 'the jump did not follow its question');
    // 4. Removing the question a jump points at clears the jump: save is blocked until it is chosen again.
    click('', {'data-fm-edit': VALID.id});
    click('', {'data-fm-qdel': '2'});
    n = posts();
    click('fm-save-btn'); await flush();
    assert.equal(posts(), n, 'a jump to a removed question was saved');
    assert(get('fm-editor-msg').textContent.includes('choose what happens after the answer "Yes"'), get('fm-editor-msg').textContent);
    // 5. Turning conditions off (confirmed) sends plain questions and no non-lead page.
    click('', {'data-fm-edit': VALID.id});
    fire('change', {id: 'fm-logic-on', checked: false}); await flush();
    click('fm-save-btn'); await flush();
    assert.equal(lastPost().body.questions[0].type, 'multiple_choice');
    assert(!('logic' in lastPost().body.questions[0]), 'logic stayed after turning conditions off');
    assert(!('non_lead_end' in lastPost().body), 'the non-lead page stayed after turning conditions off');
    return;
  }

  if (scenario === 'contacts') {
    // Both no-op and headline-only saves preserve each supported split-name field
    // and the original effective order, rather than reconstructing checkbox order.
    click('', {'data-fm-edit': VALID.id});
    for (const k of VALID.contact_fields) assert(get('fm-c-' + k).checked, 'Missing selected contact ' + k);
    click('fm-save-btn'); await flush();
    assert.deepEqual(lastPost().body.contact_fields, VALID.contact_fields, 'No-op edit rewrote contacts');
    click('', {'data-fm-edit': VALID.id});
    type('fm-headline', 'Only the headline changed');
    click('fm-save-btn'); await flush();
    assert.deepEqual(lastPost().body.contact_fields, VALID.contact_fields, 'Headline edit rewrote contacts');
    assert.equal(lastPost().body._expected_revision, 'revision-opened-by-anna');
    for (const k of ['intro_title', 'questions_first', 'follow_up_action_url', 'form_type',
                     'privacy_link_text', 'thank_you_title', 'button_text', 'future_field']) {
      assert.deepEqual(lastPost().body[k], VALID[k], 'Lost retained field ' + k);
    }
    // Intentional removal works; retained contacts keep their order.
    click('', {'data-fm-edit': VALID.id});
    get('fm-c-email').checked = false;
    get('fm-c-phone').checked = false;
    click('fm-save-btn'); await flush();
    assert.deepEqual(lastPost().body.contact_fields, ['last_name', 'first_name']);
    click('fm-new-btn');
    // 25.10 (owner): Full name is gone; a new template starts with all four contact fields selected.
    assert(get('fm-c-first_name').checked && get('fm-c-last_name').checked && get('fm-c-email').checked && get('fm-c-phone').checked);
    return;
  }

  if (scenario === 'legacy-name') {
    // ACL-2177: an old template with the removed "Full name" field opens as First name + Last name.
    click('', {'data-fm-edit': VALID.id});
    assert(get('fm-c-first_name').checked && get('fm-c-last_name').checked && get('fm-c-email').checked, 'legacy name was not converted');
    assert(!get('fm-c-phone').checked, 'phone appeared from nowhere');
    click('fm-save-btn'); await flush();
    assert(lastPost(), 'legacy template was blocked: ' + get('fm-editor-msg').textContent);
    assert.deepEqual(lastPost().body.contact_fields, ['email', 'first_name', 'last_name']);
    return;
  }

  if (scenario === 'auto-locale') {
    // ACL-2180: the locale is a dropdown with readable names; a new template defaults to Auto.
    click('fm-new-btn');
    const opts = get('fm-locale').innerHTML;
    assert(opts.includes('Auto (from the ad set language and country)'), 'no Auto option');
    assert(opts.includes('Spanish (Latin America) (ES_LA)') && opts.includes('Portuguese (Brazil) (PT_BR)'), 'no readable names');
    assert(opts.indexOf('value="AUTO"') < opts.indexOf('value="AR_AR"'), 'Auto is not the first option');
    assert(opts.includes('value="AUTO" selected'), 'Auto is not selected by default');
    assert.equal(get('fm-locale').value, 'AUTO');
    for (const [id, value] of Object.entries({
      'fm-name': 'Auto template', 'fm-headline': VALID.headline,
      'fm-privacy': VALID.privacy_url, 'fm-thanks': VALID.thank_you_text,
    })) type(id, value);
    click('fm-save-btn'); await flush();
    assert(lastPost(), 'Auto template was blocked: ' + get('fm-editor-msg').textContent);
    assert.equal(lastPost().body.locale, 'AUTO');
    // An old template keeps its manual locale, selected in the dropdown.
    click('', {'data-fm-edit': VALID.id});
    assert(get('fm-locale').innerHTML.includes('value="EN_US" selected'), 'saved locale not selected');
    assert.equal(get('fm-locale').value, 'EN_US');
    return;
  }

  if (scenario === 'legacy-locale') {
    // Only the originally saved locale is compatible; no broader input waiver.
    click('', {'data-fm-edit': VALID.id});
    type('fm-headline', 'Legacy headline edit');
    click('fm-save-btn'); await flush();
    assert(lastPost(), 'Saved legacy locale was blocked: ' + get('fm-editor-msg').textContent);
    assert.equal(lastPost().body.locale, 'UK_UA');
    assert.equal(lastPost().body._expected_revision, 'revision-opened-by-anna');
    click('', {'data-fm-edit': VALID.id});
    type('fm-locale', 'ZZ_ZZ');
    let n = requests.filter(r => r.body).length;
    click('fm-save-btn'); await flush();
    assert.equal(requests.filter(r => r.body).length, n, 'Changed unsupported locale was submitted');
    assert(get('fm-editor-msg').textContent.includes('Unknown locale: ZZ_ZZ'));
    // The same saved-locale edit also works without a connected team library.
    library = {connected: false, ok: true};
    click('fm-refresh-btn'); await flush();
    click('', {'data-fm-edit': VALID.id});
    click('fm-save-btn'); await flush();
    assert.equal(requests.filter(r => r.body).length, n + 1);
    assert(get('fm-msg').textContent.includes('on this computer only'));
    // New has no inherited legacy context, even after editing a legacy template.
    click('fm-new-btn');
    for (const [id, value] of Object.entries({
      'fm-name': 'New template', 'fm-headline': VALID.headline,
      'fm-privacy': VALID.privacy_url, 'fm-thanks': VALID.thank_you_text, 'fm-locale': 'UK_UA',
    })) type(id, value);
    n = requests.filter(r => r.body).length;
    click('fm-save-btn'); await flush();
    assert.equal(requests.filter(r => r.body).length, n, 'New unsupported locale was submitted');
    assert(get('fm-editor-msg').textContent.includes('Unknown locale: UK_UA'));
    type('fm-locale', 'EN_US');
    click('fm-save-btn'); await flush();
    assert.equal(requests.filter(r => r.body).length, n + 1);
    assert.equal(lastPost().body.locale, 'EN_US');
    assert(!('_expected_revision' in lastPost().body));
    return;
  }

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

  // New: пустая форма, все четыре контактных поля выбраны (без Full name).
  click('fm-new-btn');
  assert.equal(get('fm-editor').hidden, false);
  assert.equal(get('fm-name').value, '');
  assert(get('fm-c-first_name').checked && get('fm-c-last_name').checked && get('fm-c-email').checked && get('fm-c-phone').checked);

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
