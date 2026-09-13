// Таргетинг, набранный руками, доводится до выбора при уходе с поля.
//
// Скрытое поле с интересами заполнялось ТОЛЬКО кликом мышью по строке
// списка. Напечатал код, ушёл с поля — имя кампании собиралось из
// напечатанного и выглядело правильным, интересы оставались пустыми.
// 10 сентября так ушли в черновик обе кампании.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

const start = html.indexOf('window.cbTargetingCommit = function');
assert(start > 0, 'cbTargetingCommit не найден в шаблоне');
const end = html.indexOf('\n  };', start) + 5;
const code = html.slice(start, end);

function makeWrap(typed, hiddenValue) {
  const input  = { value: typed, closest: () => wrap };
  const hidden = { value: hiddenValue || '' };
  const hint   = { textContent: '' };
  const wrap = {
    querySelector(sel) {
      if (sel === '.cb-targeting-hidden') return hidden;
      if (sel === '.cb-targeting-hint') return hint;
      return null;
    },
    closest: () => null,   // слота нет — cbUpdateName не зовётся
  };
  input.closest = () => wrap;
  return { input, hidden, hint };
}

const REF = { targetings: [
  { code: 'Trading', label: 'Trading RU', fb_interest: 'Day trading, Investment' },
  { code: 'TradingX', label: 'Trading X', fb_interest: 'Forex' },
  { code: 'FX', label: '', fb_interest: 'Foreign exchange market' },
]};
const ctx = { REF, cbUpdateName(){}, window: {} };
vm.runInNewContext(code, ctx);
const commit = ctx.window.cbTargetingCommit;

// Напечатанный код доводится до конца.
let w = makeWrap('Trading');
assert.equal(commit(w.input), true, 'точное совпадение по коду не засчитано');
assert.equal(w.hidden.value, 'Day trading, Investment', 'интересы не подставлены');
assert.equal(w.input.value, 'Trading');

// Регистр не должен мешать: человек печатает как придётся.
w = makeWrap('fx');
assert.equal(commit(w.input), true);
assert.equal(w.hidden.value, 'Foreign exchange market');
assert.equal(w.input.value, 'FX', 'код не приведён к каноническому виду');

// Совпадение по подписи — тоже выбор.
w = makeWrap('Trading X');
assert.equal(commit(w.input), true);
assert.equal(w.hidden.value, 'Forex');

// Похожее — НЕ выбор. Соседние коды отличаются одной буквой, и подстановка
// «ближайшего» означала бы чужой таргетинг в живой кампании.
w = makeWrap('Tradin');
assert.equal(commit(w.input), false, 'неполный код принят за выбор');
assert.equal(w.hidden.value, '', 'подставлены чужие интересы');

w = makeWrap('Trad');
assert.equal(commit(w.input), false);

// Уже выбранное мышью не трогаем.
w = makeWrap('Trading', 'ручной набор интересов');
assert.equal(commit(w.input), true);
assert.equal(w.hidden.value, 'ручной набор интересов', 'перезаписан выбор пользователя');

// Пустое поле очищает скрытое: иначе останется таргетинг от прошлого выбора.
w = makeWrap('', 'старые интересы');
assert.equal(commit(w.input), false);
assert.equal(w.hidden.value, '', 'скрытое поле хранит отменённый выбор');
