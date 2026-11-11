// ACL-2188/2533: выбор фоновой картинки (window.formCoverPicker), прогнанный по-настоящему на мини-DOM.
// Код берётся живьём из шаблона страницы. Проверяется то, что нельзя вычитать глазами: одно чтение
// на нажатие, строки только текстом, Remove пустит обе половины пары. ACL-2533: «Upload» - предпросмотр
// только из локального файла (blob:), на Страницу файл уходит по отдельной кнопке, одним запросом.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const a = html.indexOf('window.formCoverPicker = function');
const b = html.indexOf('function coverContext', a);
assert(a > 0 && b > a, 'formCoverPicker не найден');
const code = html.slice(a, b);

const tags = [];
const all_els = [];
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.listeners = {}; this.style = {}; this.attrs = {};
    this._text = ''; this._value = ''; this.disabled = false; this.className = ''; this.hidden = false;
    this.files = []; this.type = ''; this.src = '';
    all_els.push(this);
    tags.push(tag);
  }
  // A real file input forgets its files when its value is set to ''.
  get value() { return this._value; }
  set value(v) { this._value = v; if (this.type === 'file' && v === '') this.files = []; }
  set innerHTML(v) { throw new Error('innerHTML must not be used for picker rows'); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this._text = String(v); this.children = []; }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; if (k === 'src') this.src = ''; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  fire(t) { (this.listeners[t] || []).forEach(fn => fn({target: this})); }
  all() { return [this, ...this.children.flatMap(c => c.all())]; }
}
const fetches = [];
let reply = null;
const revoked = [];
const context = {
  document: {createElement: tag => new El(tag)},
  URL: {createObjectURL: f => 'blob:local/' + f.name, revokeObjectURL: u => revoked.push(u)},
  FormData: class { constructor() { this.f = {}; } append(k, v) { this.f[k] = v; } },
  fetch: async (url, opts) => { fetches.push({url, f: opts.body.f}); return {json: async () => reply}; },
};
context.window = context;
vm.createContext(context);
vm.runInContext(code, context);

const flush = () => new Promise(r => setImmediate(r));
(async () => {
  let ctx = {account: '', pageId: ''};
  const box = new El('div');
  const picker = context.window.formCoverPicker(box, () => ctx);
  const find = text => box.all().find(e => e.tag === 'button' && e.textContent === text);
  const lines = () => box.all().map(e => e.textContent);
  const msgText = () => box.all().filter(e => e.attrs.role === 'status')[0].textContent;
  const errBox = () => box.all().find(e => e.attrs.role === 'alert');
  const errText = () => errBox().textContent;
  const fileState = () => box.all().find(e => e.attrs['aria-live'] === 'polite').textContent;
  const stateText = () => box.all().filter(e => e.tag === 'div' && /^Photo \d/.test(e._text))[0];

  // Тексты из спецификации.
  const all = box.textContent;
  for (const t of ['Background image (optional)',
    'Upload a picture from this computer (JPEG or PNG, up to 8 MB) or choose one already on this Page. It is shown on the intro screen.',
    'Landscape pictures around 1.91:1 (for example 1200×628) usually fit best.', 'Photo ID']) {
    assert(all.includes(t), 'нет текста: ' + t);
  }

  // Нет Страницы: чтения нет.
  find('Choose from Page').fire('click'); await flush();
  assert.equal(msgText(), 'Refresh Facebook Pages before choosing an image.');
  assert.equal(fetches.length, 0, 'чтение ушло без Страницы');

  // Одно чтение на нажатие, строки текстом.
  ctx = {account: 'Acc', pageId: '111'};
  reply = {ok: true, state: 'ok', truncated: true, photos: [
    {id: '12', kind: 'form_cover', form_name: '<b>Evil</b> form', width: 800, height: 800, created_time: ''},
    {id: '11', kind: 'page_photo', form_name: '', width: 1200, height: 628, created_time: '2026-10-20T10:00:00+0000'}]};
  find('Choose from Page').fire('click'); await flush();
  assert.equal(fetches.length, 1);
  assert.equal(fetches[0].url, '/api/page-photos');
  assert.deepEqual(fetches[0].f, {account_label: 'Acc', page_id: '111'});
  const rowsText = lines();
  assert(rowsText.includes('Cover of form "<b>Evil</b> form" — 12'), 'строка формы-обложки: ' + rowsText.join('|'));
  assert(rowsText.includes('Page photo, 1200×628, 2026-10-20 — 11'));
  assert.equal(msgText(), 'Showing the most recent ones. Paste a Photo ID for an older picture.');
  // Единственный <img> - предпросмотр; он пуст, пока нет локального файла (адрес Meta не грузится никогда).
  assert(all_els.filter(e => e.tag === 'img').every(e => e.src === ''), 'в <img> лежит адрес до выбора файла');

  // Выбор строки.
  find('Cover of form "<b>Evil</b> form" — 12').fire('click');
  assert.deepEqual(picker.get(), {cover_photo_id: '12', cover_page_id: '111', cover_sha256: ''});
  assert.equal(stateText().textContent, 'Photo 12, Page 111');

  // Remove пустит обе половины.
  find('Remove').fire('click');
  assert.deepEqual(picker.get(), {cover_photo_id: '', cover_page_id: '', cover_sha256: ''});

  // Вставленный Photo ID берёт Страницу из контекста; очистка поля снимает пару.
  const input = box.all().find(e => e.tag === 'input' && e.type === 'text');
  input.value = ' 999 '; input.fire('input');
  assert.deepEqual(picker.get(), {cover_photo_id: '999', cover_page_id: '111', cover_sha256: ''});
  input.value = ''; input.fire('input');
  assert.deepEqual(picker.get(), {cover_photo_id: '', cover_page_id: '', cover_sha256: ''});

  // set() кладёт сохранённую пару (и sha256 загруженного файла).
  picker.set('5', '6');
  assert.deepEqual(picker.get(), {cover_photo_id: '5', cover_page_id: '6', cover_sha256: ''});
  picker.set('5', '6', 'b'.repeat(64));
  assert.deepEqual(picker.get(), {cover_photo_id: '5', cover_page_id: '6', cover_sha256: 'b'.repeat(64)});

  // ACL-2533: Upload. Чужой тип / слишком большой файл - текст, без предпросмотра и без запроса.
  const fileInput = box.all().find(e => e.tag === 'input' && e.type === 'file');
  const img = box.all().find(e => e.tag === 'img');
  assert.equal(fileInput.attrs.accept, 'image/jpeg,image/png');
  const nFetch = fetches.length;
  fileInput.files = [{name: 'a.gif', type: 'image/gif', size: 100}]; fileInput.fire('change');
  assert.equal(msgText(), 'The background image must be a JPEG or PNG file.');
  assert.equal(img.hidden, true); assert.equal(img.src, '');
  fileInput.files = [{name: 'big.jpg', type: 'image/jpeg', size: 8 * 1024 * 1024 + 1}]; fileInput.fire('change');
  assert.equal(msgText(), 'The background image is larger than 8 MB.');
  assert.equal(find('Upload to Page').disabled, true);
  // Хороший файл: предпросмотр из blob:, запроса ещё нет.
  const jpg = {name: 'cover.jpg', type: 'image/jpeg', size: 5000};
  fileInput.files = [jpg]; fileInput.fire('change');
  assert.equal(img.src, 'blob:local/cover.jpg'); assert.equal(img.hidden, false);
  assert.equal(img.style.display, 'block'); assert.equal(fileState(), 'Chosen (not uploaded yet)');
  assert.equal(picker.pending(), true);
  assert.equal(find('Upload to Page').disabled, false);
  assert.equal(fetches.length, nFetch, 'файл ушёл без нажатия Upload to Page');
  // Нет Страницы - отказ без запроса.
  ctx = {account: 'Acc', pageId: '', formName: 'My form'};
  find('Upload to Page').fire('click'); await flush();
  assert.equal(errText(), 'Refresh pages and choose a Page before uploading the picture.');
  assert.equal(errBox().hidden, false, 'the missing Page must be a visible error');
  assert.equal(fetches.length, nFetch);
  // Отказ Meta: имя формы и текст Meta; пара не меняется.
  ctx = {account: 'Acc', pageId: '111', formName: 'My form'};
  picker.set('5', '6');
  fileInput.files = [jpg]; fileInput.fire('change');
  reply = {ok: false, state: 'refused', message: 'Meta refused the background image: Invalid image'};
  find('Upload to Page').fire('click'); await flush();
  assert.equal(fetches.length, nFetch + 1);
  assert.equal(fetches[nFetch].url, '/api/form-photo-upload');
  assert.deepEqual(Object.keys(fetches[nFetch].f).sort(), ['account_label', 'file', 'page_id']);
  assert.equal(fetches[nFetch].f.file, jpg); assert.equal(fetches[nFetch].f.page_id, '111');
  assert.equal(msgText(), 'Form "My form": Meta refused the background image: Invalid image');
  assert.equal(fileState(), 'Upload failed: Meta refused the background image: Invalid image');
  assert.equal(errText(), 'Form "My form": Meta refused the background image: Invalid image');
  assert.equal(picker.pending(), true, 'a failed upload leaves the file pending');
  assert.deepEqual(picker.get(), {cover_photo_id: '5', cover_page_id: '6', cover_sha256: ''});
  // Успех: id, Страница и sha256 в паре; предпросмотр остаётся локальным.
  const sha = 'a'.repeat(64);
  reply = {ok: true, state: 'uploaded', photo_id: '777', page_id: '111', sha256: sha, width: 1024, height: 1536};
  find('Upload to Page').fire('click'); await flush();
  assert.deepEqual(picker.get(), {cover_photo_id: '777', cover_page_id: '111', cover_sha256: sha});
  assert.equal(stateText().textContent, 'Photo 777, Page 111, uploaded from this computer');
  assert(img.src.startsWith('blob:'), 'предпросмотр не из локального файла');
  assert.equal(fileState(), 'Uploaded to Page 111'); assert.equal(errBox().hidden, true);
  assert.equal(picker.pending(), false);
  // Вставленный вручную id или выбор со Страницы снимает sha256 (картинка уже не «наш файл»).
  input.value = '888'; input.fire('input');
  assert.deepEqual(picker.get(), {cover_photo_id: '888', cover_page_id: '111', cover_sha256: ''});
  // Remove убирает и предпросмотр, и пару.
  find('Remove').fire('click');
  assert.deepEqual(picker.get(), {cover_photo_id: '', cover_page_id: '', cover_sha256: ''});
  assert.equal(img.hidden, true); assert.equal(img.src, '');
  assert.equal(img.style.display, 'none', 'an inline display would show a broken image without a file');
  assert.equal(fileInput.value, '', 'the file input must not keep the name of a dropped file');
  assert(revoked.length >= 1, 'blob: адрес не освобождён');

  for (const f of fetches) assert(['/api/page-photos', '/api/form-photo-upload'].includes(f.url), 'лишний адрес: ' + f.url);

  // Пусто / отказ: понятный текст.
  reply = {ok: false, state: 'unreadable', photos: [], message: 'No pictures could be read on this Page.'};
  find('Choose from Page').fire('click'); await flush();
  assert.equal(msgText(), 'No pictures could be read on this Page. Paste a Photo ID, or upload a picture once in a form in Ads Manager.');
  reply = {ok: false, state: 'failed', photos: [], message: 'Profile is busy'};
  find('Choose from Page').fire('click'); await flush();
  assert.equal(msgText(), 'Profile is busy');
  assert.equal(fetches.length, 5);

  // ACL-2533 round 3: a chosen file is never dropped by a save; the save uploads it first or stops.
  const box2 = new El('div');
  let ctx2 = {account: '', pageId: ''};
  const p2 = context.window.formCoverPicker(box2, () => ctx2);
  const file2 = box2.all().find(e => e.tag === 'input' && e.type === 'file');
  const alert2 = () => box2.all().find(e => e.attrs.role === 'alert');
  const state2 = () => box2.all().find(e => e.attrs['aria-live'] === 'polite').textContent;
  // Page first: the file input waits for an account and a Page, with a hint.
  assert.equal(file2.disabled, true, 'the file input must wait for a Page');
  assert(box2.all().some(e => e._text === 'Choose an account and a Page first (Refresh pages).' && !e.hidden));
  ctx2 = {account: 'Acc', pageId: '111'}; p2.gate();
  assert.equal(file2.disabled, false);
  assert(box2.all().every(e => e._text !== 'Choose an account and a Page first (Refresh pages).' || e.hidden));
  const jpg2 = {name: 'cover.jpg', type: 'image/jpeg', size: 5000};
  file2.files = [jpg2]; file2.fire('change');
  assert.equal(p2.pending(), true);
  // The Page goes away (Account changed): a save cannot go on - a visible error with the fix, nothing sent.
  ctx2 = {account: 'Acc', pageId: ''};
  const n2 = fetches.length;
  let r2 = await p2.ensureUploaded();
  assert.equal(r2.ok, false); assert.equal(fetches.length, n2);
  assert.equal(r2.message, 'Refresh pages and choose a Page before uploading the picture.');
  assert.equal(alert2().hidden, false); assert.equal(state2(), 'Chosen (not uploaded yet)');
  assert.equal(file2.files[0], jpg2, 'the chosen file must survive a missing Page');
  assert.equal(p2.pending(), true);
  // With the Page back: one upload, and the pair is the answer.
  ctx2 = {account: 'Acc', pageId: '111', pageLabel: 'My Page (111)'};
  reply = {ok: true, state: 'uploaded', photo_id: '901', page_id: '111', sha256: 'c'.repeat(64)};
  r2 = await p2.ensureUploaded();
  assert.equal(r2.ok, true); assert.equal(fetches.length, n2 + 1); assert.equal(fetches[n2].url, '/api/form-photo-upload');
  assert.deepEqual(p2.get(), {cover_photo_id: '901', cover_page_id: '111', cover_sha256: 'c'.repeat(64)});
  assert.equal(p2.pending(), false); assert.equal(state2(), 'Uploaded to Page My Page (111)');
  r2 = await p2.ensureUploaded();                       // already on the Page: no second upload
  assert.equal(r2.ok, true); assert.equal(fetches.length, n2 + 1);
  // A failure stops it and leaves the file pending; nothing is saved without the picture.
  file2.files = [{name: 'b.png', type: 'image/png', size: 900}]; file2.fire('change');
  assert.equal(p2.pending(), true);
  reply = {ok: false, state: 'refused', message: 'Meta refused the background image: Invalid image'};
  r2 = await p2.ensureUploaded();
  assert.equal(r2.ok, false); assert.equal(p2.pending(), true);
  assert.equal(state2(), 'Upload failed: Meta refused the background image: Invalid image');
  // Two callers at once share one upload.
  const n3 = fetches.length;
  reply = {ok: true, state: 'uploaded', photo_id: '902', page_id: '111', sha256: 'd'.repeat(64)};
  const both = await Promise.all([p2.ensureUploaded(), p2.ensureUploaded()]);
  assert(both[0].ok && both[1].ok); assert.equal(fetches.length, n3 + 1, 'one file must be sent once');
  // Remove drops the file and its name; a new template has no pending file.
  const find2 = box2.all().find(e => e.tag === 'button' && e.textContent === 'Remove'); find2.fire('click');
  assert.equal(p2.pending(), false); assert.equal(file2.value, '');
  process.stdout.write('ok');
})().catch(e => { console.error(e.stack || e); process.exit(1); });
