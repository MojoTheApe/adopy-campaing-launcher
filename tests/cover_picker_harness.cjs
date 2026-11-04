// ACL-2188: выбор фоновой картинки (window.formCoverPicker), прогнанный по-настоящему на мини-DOM.
// Код берётся живьём из шаблона страницы. Проверяется то, что нельзя вычитать глазами: одно чтение
// на нажатие, строки только текстом, ни одного <img>, Remove пустит обе половины пары.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const a = html.indexOf('window.formCoverPicker = function');
const b = html.indexOf('function coverContext', a);
assert(a > 0 && b > a, 'formCoverPicker не найден');
const code = html.slice(a, b);

const tags = [];
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.listeners = {}; this.style = {}; this.attrs = {};
    this._text = ''; this.value = ''; this.disabled = false; this.className = '';
    tags.push(tag);
  }
  set innerHTML(v) { throw new Error('innerHTML must not be used for picker rows'); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this._text = String(v); this.children = []; }
  setAttribute(k, v) { this.attrs[k] = v; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  fire(t) { (this.listeners[t] || []).forEach(fn => fn({target: this})); }
  all() { return [this, ...this.children.flatMap(c => c.all())]; }
}
const fetches = [];
let reply = null;
const context = {
  document: {createElement: tag => new El(tag)},
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
  const stateText = () => box.all().filter(e => e.tag === 'div' && /^Photo \d/.test(e._text))[0];

  // Тексты из спецификации.
  const all = box.textContent;
  for (const t of ['Background image (optional)',
    'A picture already on this Page, shown on the intro screen. To add a new one, upload it once in a form in Ads Manager.',
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
  assert(!tags.includes('img'), 'на экране появился <img>');

  // Выбор строки.
  find('Cover of form "<b>Evil</b> form" — 12').fire('click');
  assert.deepEqual(picker.get(), {cover_photo_id: '12', cover_page_id: '111'});
  assert.equal(stateText().textContent, 'Photo 12, Page 111');

  // Remove пустит обе половины.
  find('Remove').fire('click');
  assert.deepEqual(picker.get(), {cover_photo_id: '', cover_page_id: ''});

  // Вставленный Photo ID берёт Страницу из контекста; очистка поля снимает пару.
  const input = box.all().find(e => e.tag === 'input');
  input.value = ' 999 '; input.fire('input');
  assert.deepEqual(picker.get(), {cover_photo_id: '999', cover_page_id: '111'});
  input.value = ''; input.fire('input');
  assert.deepEqual(picker.get(), {cover_photo_id: '', cover_page_id: ''});

  // set() кладёт сохранённую пару.
  picker.set('5', '6');
  assert.deepEqual(picker.get(), {cover_photo_id: '5', cover_page_id: '6'});

  // Пусто / отказ: понятный текст.
  reply = {ok: false, state: 'unreadable', photos: [], message: 'No pictures could be read on this Page.'};
  find('Choose from Page').fire('click'); await flush();
  assert.equal(msgText(), 'No pictures could be read on this Page. Paste a Photo ID, or upload a picture once in a form in Ads Manager.');
  reply = {ok: false, state: 'failed', photos: [], message: 'Profile is busy'};
  find('Choose from Page').fire('click'); await flush();
  assert.equal(msgText(), 'Profile is busy');
  assert.equal(fetches.length, 3);
  process.stdout.write('ok');
})().catch(e => { console.error(e.stack || e); process.exit(1); });
