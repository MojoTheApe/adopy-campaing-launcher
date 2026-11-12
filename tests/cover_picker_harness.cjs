// ACL-2188/2533: выбор фоновой картинки (window.formCoverPicker), прогнанный по-настоящему на мини-DOM.
// Код берётся живьём из шаблона страницы. Проверяется то, что нельзя вычитать глазами: одно чтение
// на нажатие, строки только текстом, Remove пустит обе половины пары. ACL-2571: файл выбирается без
// аккаунта и Страницы, предпросмотр только из локального файла (blob:), файл остаётся на этом компьютере.
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
  const stateText2 = () => box.all().filter(e => e.tag === 'div' && /^Picture from this computer/.test(e._text))[0];

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
  assert.deepEqual(picker.get(), {cover_photo_id: '12', cover_page_id: '111', cover_sha256: '', cover_local_sha256: ''});
  assert.equal(stateText().textContent, 'Photo 12, Page 111');

  // Remove пустит обе половины.
  find('Remove').fire('click');
  assert.deepEqual(picker.get(), {cover_photo_id: '', cover_page_id: '', cover_sha256: '', cover_local_sha256: ''});

  // Вставленный Photo ID берёт Страницу из контекста; очистка поля снимает пару.
  const input = box.all().find(e => e.tag === 'input' && e.type === 'text');
  input.value = ' 999 '; input.fire('input');
  assert.deepEqual(picker.get(), {cover_photo_id: '999', cover_page_id: '111', cover_sha256: '', cover_local_sha256: ''});
  input.value = ''; input.fire('input');
  assert.deepEqual(picker.get(), {cover_photo_id: '', cover_page_id: '', cover_sha256: '', cover_local_sha256: ''});

  // set() кладёт сохранённую пару (и sha256 загруженного файла).
  picker.set('5', '6');
  assert.deepEqual(picker.get(), {cover_photo_id: '5', cover_page_id: '6', cover_sha256: '', cover_local_sha256: ''});
  picker.set('5', '6', 'b'.repeat(64));
  assert.deepEqual(picker.get(), {cover_photo_id: '5', cover_page_id: '6', cover_sha256: 'b'.repeat(64), cover_local_sha256: ''});

  // ACL-2571: the file input never waits for an account or a Page.
  const fileInput = box.all().find(e => e.tag === 'input' && e.type === 'file');
  const img = box.all().find(e => e.tag === 'img');
  assert.equal(fileInput.attrs.accept, 'image/jpeg,image/png');
  assert.equal(fileInput.disabled, false, 'the file input must not wait for a Page');
  assert(!box.textContent.includes('Upload to Page'), 'there is no upload-to-Page button any more');
  assert(!box.textContent.includes('Choose an account and a Page first'), 'no Page-first gate');
  ctx = {account: '', pageId: ''};
  picker.gate();
  assert.equal(fileInput.disabled, false);
  const nFetch = fetches.length;
  // Чужой тип / слишком большой файл - текст, без предпросмотра и без запроса.
  fileInput.files = [{name: 'a.gif', type: 'image/gif', size: 100}]; fileInput.fire('change');
  assert.equal(msgText(), 'The background image must be a JPEG or PNG file.');
  assert.equal(img.hidden, true); assert.equal(img.src, '');
  fileInput.files = [{name: 'big.jpg', type: 'image/jpeg', size: 8 * 1024 * 1024 + 1}]; fileInput.fire('change');
  assert.equal(msgText(), 'The background image is larger than 8 MB.');
  // Хороший файл без Страницы: предпросмотр из blob:, запроса нет, пара пуста.
  const jpg = {name: 'cover.jpg', type: 'image/jpeg', size: 5000};
  picker.set('5', '6');                                  // a Page photo is chosen ...
  fileInput.files = [jpg]; fileInput.fire('change');     // ... one picture: the file replaces it
  assert.equal(img.src, 'blob:local/cover.jpg'); assert.equal(img.hidden, false);
  assert.equal(img.style.display, 'block'); assert.equal(fileState(), 'Chosen (not saved yet)');
  assert.equal(picker.pending(), true);
  assert.deepEqual(picker.get(), {cover_photo_id: '', cover_page_id: '', cover_sha256: '', cover_local_sha256: ''});
  assert.equal(fetches.length, nFetch, 'the file went out without a save');
  // Отказ: имя в тексте, файл остаётся выбранным.
  reply = {ok: false, message: 'The background image must be a JPEG or PNG file.'};
  let r = await picker.ensureStored();
  assert.equal(r.ok, false);
  assert.equal(fetches.length, nFetch + 1);
  assert.equal(fetches[nFetch].url, '/api/form-templates/cover-file');
  assert.deepEqual(Object.keys(fetches[nFetch].f), ['file']);            // no account, no Page: the file alone
  assert.equal(fetches[nFetch].f.file, jpg);
  assert.equal(errText(), 'The background image must be a JPEG or PNG file.');
  assert.equal(picker.pending(), true, 'a failed save leaves the file pending');
  // Успех: только хэш; предпросмотр остаётся локальным; два вызова разом - один запрос.
  const sha = 'a'.repeat(64);
  reply = {ok: true, sha256: sha, bytes: 5000, mime: 'image/jpeg'};
  const both = await Promise.all([picker.ensureStored(), picker.ensureStored()]);
  assert(both[0].ok && both[1].ok);
  assert.equal(fetches.length, nFetch + 2, 'one file must be sent once');
  assert.deepEqual(picker.get(), {cover_photo_id: '', cover_page_id: '', cover_sha256: '', cover_local_sha256: sha});
  assert.equal(stateText2().textContent, 'Picture from this computer, uploaded to each Page at publish');
  assert(img.src.startsWith('blob:'), 'предпросмотр не из локального файла');
  assert.equal(fileState(), 'Stored on this computer'); assert.equal(errBox().hidden, true);
  assert.equal(picker.pending(), false);
  r = await picker.ensureStored();                       // already kept: no second request
  assert.equal(r.ok, true); assert.equal(fetches.length, nFetch + 2);
  // Вставленный вручную id снимает локальную картинку (одна картинка).
  input.value = '888'; input.fire('input');
  assert.deepEqual(picker.get(), {cover_photo_id: '888', cover_page_id: '', cover_sha256: '', cover_local_sha256: ''});
  assert.equal(img.hidden, true);
  // Remove убирает и предпросмотр, и всё остальное.
  fileInput.files = [jpg]; fileInput.fire('change');
  find('Remove').fire('click');
  assert.deepEqual(picker.get(), {cover_photo_id: '', cover_page_id: '', cover_sha256: '', cover_local_sha256: ''});
  assert.equal(img.hidden, true); assert.equal(img.src, '');
  assert.equal(img.style.display, 'none', 'an inline display would show a broken image without a file');
  assert.equal(fileInput.value, '', 'the file input must not keep the name of a dropped file');
  assert(revoked.length >= 1, 'blob: адрес не освобождён');

  // Сохранённая локальная картинка шаблона: предпросмотр с этого же дашборда, не из Meta.
  picker.set('', '', '', sha, 'tpl000000001');
  assert.equal(img.src, '/api/form-templates/cover-file?id=tpl000000001&sha=' + sha);
  assert.equal(img.hidden, false);
  assert.deepEqual(picker.get(), {cover_photo_id: '', cover_page_id: '', cover_sha256: '', cover_local_sha256: sha});
  // Файла нет на этом компьютере (шаблон из общей библиотеки): сказано прямо, хэш остаётся.
  img.onerror();
  assert.equal(fileState(), 'Background picture is on another computer \u2014 choose it again here');
  assert.equal(img.hidden, true);
  assert.equal(picker.get().cover_local_sha256, sha, 'the hash must stay until the buyer replaces the picture');
  assert.equal(picker.pending(), false, 'a missing file is not a pending file');
  // Без id шаблона (ещё не сохранён) картинки с сервера нет, но сказано то же.
  picker.set('', '', '', sha, '');
  assert.equal(img.src, ''); assert.equal(fileState(), 'Background picture is on another computer \u2014 choose it again here');
  // Фото Страницы в set() главнее: локальный хэш не держится рядом с парой.
  picker.set('5', '6', '', sha, 'tpl000000001');
  assert.deepEqual(picker.get(), {cover_photo_id: '5', cover_page_id: '6', cover_sha256: '', cover_local_sha256: ''});

  for (const f of fetches) assert(['/api/page-photos', '/api/form-templates/cover-file'].includes(f.url), 'лишний адрес: ' + f.url);

  // Пусто / отказ чтения со Страницы: понятный текст.
  ctx = {account: 'Acc', pageId: '111'};
  reply = {ok: false, state: 'unreadable', photos: [], message: 'No pictures could be read on this Page.'};
  find('Choose from Page').fire('click'); await flush();
  assert.equal(msgText(), 'No pictures could be read on this Page. Paste a Photo ID, or upload a picture once in a form in Ads Manager.');
  reply = {ok: false, state: 'failed', photos: [], message: 'Profile is busy'};
  find('Choose from Page').fire('click'); await flush();
  assert.equal(msgText(), 'Profile is busy');
  process.stdout.write('ok');
})().catch(e => { console.error(e.stack || e); process.exit(1); });
