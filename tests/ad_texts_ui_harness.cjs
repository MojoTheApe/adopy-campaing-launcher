// Раздел Ad titles, прогнанный по-настоящему: код страницы исполняется, а не
// вычитывается глазами. Проверки текстом не поймали бы ни экранирование, ни
// отбор — там всё собирается строкой уже во время работы.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const MARK = '// ── Ad titles: библиотека рекламных текстов ──';
const start = html.indexOf(MARK);
assert(start !== -1, 'кода раздела Ad titles на странице нет');
const code = html.slice(start, html.indexOf('</script>', start));

// Настоящие тексты из вкладки: стрелки, кавычки, эмодзи и абзацы. Ровно то,
// на чём разметка карточки и рвётся.
const rows = [
  {name:'Registro <"raro">', headline:'Registre-se agora>>>', body:'Linha um\nLinha dois', description:'★★★★⯪4.6k', asset:'', language:'ES', source:'sheet'},
  {name:'AR how much', headline:'للمزيد', body:'كم لديك', description:'', asset:'', language:'AR', source:'sheet'},
  {name:'Без языка', headline:'No language', body:'Body', description:'', asset:'mix', language:'', source:'dashboard'},
];

const elements = {}, listeners = {}, requests = [];
const get = id => elements[id] ||= {
  id, value:'', style:{}, hidden:false, disabled:false, innerHTML:'', textContent:'',
  getAttribute(){ return null; }, scrollIntoView(){},
};
let confirmed = true, saveOk = true;
let library = {connected:true, ok:true, error:'', synced_at:'2026-09-18 10:05:00 UTC'};
const context = {
  document: {
    getElementById: get,
    querySelectorAll(){ return Object.values(elements); },
    addEventListener(type, fn){ (listeners[type] ||= []).push(fn); },
    dispatchEvent(event){ (listeners[event.type] || []).forEach(fn => fn(event)); return true; },
  },
  location: {hash:''},
  CustomEvent: class { constructor(type, options={}) { this.type=type; this.detail=options.detail; } },
  confirm(){ return confirmed; },
  aclConfirm(){ return Promise.resolve(confirmed); },
  fetch: async (url, opts) => {
    if (opts) {
      requests.push(JSON.parse(opts.body));
      return saveOk
        ? {ok:true, json:async()=>({ok:true, text:{name:'x'}, scope:library.connected ? 'team' : 'local'})}
        : {ok:false, json:async()=>({ok:false, error:'Такой текст уже есть — «AR how much».'})};
    }
    return {ok:true, json:async()=>({
      ok:true, texts:rows, duplicates:{names:[], texts:[]},
      library:library,
      deleted:[{name:'Old <b>', deleted_by:'Anna', deleted_at:'2026-09-18 10:00:00 UTC', item:{headline:'H', body:'B'}}],
    })};
  },
};
vm.runInNewContext(code, context);

const flush = () => new Promise(r => setImmediate(r));
const fire = (type, target) => (listeners[type] || []).forEach(fn => fn({target}));
const click = (id, attrs = {}) => fire('click', {id, getAttribute(k){ return attrs[k] || null; }});
const type = (id, value) => { get(id).value = value; fire('input', get(id)); };

(async () => {
  context.document.dispatchEvent(new context.CustomEvent('acl:viewchange', {detail:{view:'ad-texts'}}));
  await flush();
  const list = () => get('at-list').innerHTML;

  // Разметка не рвётся об угловые скобки и кавычки из настоящего текста.
  // Проверяется отсутствие сырого вида, а не наличие экранированного: имя
  // попадает в карточку дважды — в data-атрибут кнопки и в заголовок, — и
  // одного экранированного вхождения достаточно, чтобы проверка «есть
  // экранированное» прошла на непочиненном коде. Так она однажды и прошла.
  assert(!list().includes('<"raro">'), 'имя попало в разметку сырым: ' + list().slice(0, 300));
  assert(!list().includes('agora>>>'), 'заголовок попал в разметку сырым');
  assert(list().includes('Registre-se agora&gt;&gt;&gt;'), 'заголовок не показан вовсе');

  // Расфасовка: язык — заголовок группы, пустой язык — отдельная группа в конце.
  assert(list().includes('>AR<'), 'группы AR нет');
  assert(list().includes('Language not set'), 'тексты без языка потерялись');
  assert(list().indexOf('Language not set') > list().indexOf('>ES<'), 'безъязыкие должны быть последними');
  assert.equal(get('at-count').textContent, '3 texts');

  // Строка состояния говорит, откуда список и насколько он свежий.
  assert(get('at-status').textContent.includes('refreshed 2026-09-18 10:05:00 UTC'), get('at-status').textContent);
  // Удалённое видно с автором, экранировано и восстанавливается.
  assert(get('at-deleted').innerHTML.includes('removed by Anna'), 'нет автора удаления');
  assert(!get('at-deleted').innerHTML.includes('Old <b>'), 'имя удалённого попало в разметку сырым');

  // Поиск идёт по всем полям, включая тело.
  type('at-search', 'linha dois');
  assert.equal(get('at-count').textContent, '1 of 3 texts');
  assert(list().includes('Registro'), 'поиск по телу не нашёл текст');
  type('at-search', 'كم لديك');
  assert(list().includes('AR how much'), 'поиск по арабскому телу не работает');
  type('at-search', '');

  // Отбор по языку.
  get('at-lang-filter').value = 'AR';
  fire('change', get('at-lang-filter'));
  assert.equal(get('at-count').textContent, '1 of 3 texts');
  assert(!list().includes('Registro'), 'отбор по языку пропустил чужой текст');
  get('at-lang-filter').value = '';
  fire('change', get('at-lang-filter'));

  // Правка подставляет все поля, включая описание и язык.
  click('', {'data-at-edit':'Registro <"raro">'});
  assert.equal(get('at-body').value, 'Linha um\nLinha dois', 'тело потеряло перенос строки при правке');
  assert.equal(get('at-description').value, '★★★★⯪4.6k');
  assert.equal(get('at-language').value, 'ES');
  assert.equal(get('at-add-btn').textContent, 'Save');

  // Отмена возвращает форму к добавлению, а не оставляет чужое имя в поле.
  click('at-cancel-btn');
  assert.equal(get('at-name').value, '');
  assert.equal(get('at-add-btn').textContent, 'Add');

  // Добавление отправляет то, что набрано, всеми шестью полями.
  get('at-name').value = 'Новый';
  get('at-headline').value = 'Заголовок';
  get('at-body').value = 'Первый абзац\n\nВторой абзац';
  get('at-description').value = '★★★★';
  get('at-language').value = 'ar';
  click('at-add-btn');
  await flush();
  assert.equal(requests[0].action, 'add');
  assert.equal(requests[0].body, 'Первый абзац\n\nВторой абзац', 'абзацы не доехали до сервера');
  assert.equal(requests[0].language, 'ar');
  // Список перечитывается сразу после сохранения — и отчёт о переносе не
  // должен затирать результат действия человека. Ровно так и было на первом
  // живом прогоне: нажал Add, а на экране «updated 316 · skipped 11…».
  assert.equal(get('at-msg').textContent, 'Added. Everyone gets it.',
    'сообщение о сохранении затёрто отчётом о переносе: ' + get('at-msg').textContent);

  // Отказ сервера виден на экране, а не только в консоли.
  saveOk = false;
  get('at-name').value = 'Повтор';
  get('at-headline').value = 'A';
  get('at-body').value = 'B';
  click('at-add-btn');
  await flush();
  assert(get('at-msg').textContent.includes('AR how much'),
    'причина отказа не показана: ' + get('at-msg').textContent);
  saveOk = true;

  // Удаление спрашивает, и «нет» значит нет.
  const before = requests.length;
  confirmed = false;
  click('', {'data-at-del':'AR how much'});
  await flush();
  assert.equal(requests.length, before, 'удаление ушло на сервер без подтверждения');
  confirmed = true;
  click('', {'data-at-del':'AR how much'});
  await flush();
  assert.equal(requests[requests.length - 1].action, 'delete');
  click('', {'data-at-restore':'Old <b>'});
  await flush();
  assert.equal(requests[requests.length - 1].action, 'restore');
  assert.equal(requests[requests.length - 1].name, 'Old <b>');

  // GitHub недоступен: список по копии, и это сказано прямо.
  library = {connected:true, ok:false, error:'GitHub is unreachable: timed out', synced_at:'2026-09-18 10:05:00 UTC'};
  click('at-refresh-btn');
  await flush();
  assert(get('at-status').textContent.includes('unreachable'), get('at-status').textContent);
  assert(get('at-status').textContent.includes('cannot be saved'), get('at-status').textContent);
  library = {connected:false, ok:true};
  click('at-refresh-btn'); await flush();
  get('at-name').value = 'Local'; get('at-headline').value = 'Local headline';
  click('at-add-btn'); await flush();
  assert(get('at-msg').textContent.includes('on this computer only'), get('at-msg').textContent);
  assert(!get('at-msg').textContent.includes('Everyone'));
})().catch(e => { console.error(e); process.exitCode = 1; });
