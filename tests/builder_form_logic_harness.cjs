// ACL-2178: the builder's "Create and save form" panel and its answer conditions.
// The panel code is taken live from the page; the DOM is a tiny fake with exactly what that code touches
// (class selectors, closest, createElement, textContent, attributes). The question rows are built here by
// hand: `questionHTML` needs an HTML parser, which this harness does not have.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');

function slice(from, to) {
  const i = html.indexOf(from);
  assert(i > 0, 'not found: ' + from);
  const j = html.indexOf(to, i);
  assert(j > i, 'end not found: ' + to);
  return html.slice(i, j);
}

class El {
  constructor(tag) {
    this.tag = tag; this.cls = []; this.kids = []; this.parent = null; this.attrs = {};
    this.value = ''; this.checked = false; this.hidden = false; this._text = ''; this.onchange = null;
  }
  set className(v) { this.cls = String(v).split(/\s+/).filter(Boolean); }
  get className() { return this.cls.join(' '); }
  set textContent(v) { this._text = String(v); this.kids = []; }
  get textContent() { return this._text; }
  appendChild(c) { c.parent = this; this.kids.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  remove() { if (this.parent) this.parent.kids = this.parent.kids.filter((k) => k !== this); this.parent = null; }
  closest(sel) {
    const c = sel.replace('.', '');
    for (let n = this; n; n = n.parent) if (n.cls.includes(c)) return n;
    return null;
  }
  all(c, out = []) { this.kids.forEach((k) => { if (k.cls.includes(c)) out.push(k); k.all(c, out); }); return out; }
  querySelectorAll(sel) { return this.all(sel.replace('.', '')); }
  querySelector(sel) { return this.all(sel.replace('.', ''))[0] || null; }
  get options() { return this.kids.filter((k) => k.tag === 'option'); }
}
// A select takes a value only if it has such an option, as a browser does: any other value reads back as ''.
// (A select without option elements, such as the question-type one here, is a plain field.)
Object.defineProperty(El.prototype, 'value', {
  get() {
    if (this.tag !== 'select' || !this.options.length) return this._v === undefined ? '' : this._v;
    const o = this.options.find((x) => x._v === this._chosen);
    return o ? o._v : '';
  },
  set(v) { this._v = String(v); this._chosen = String(v); },
  configurable: true });
const mk = (tag, cls, props) => { const e = new El(tag); e.className = cls || ''; Object.assign(e, props || {}); return e; };

const layoutCode = slice('window.cbLayout = (function () {', '\n})();') + '\n})();';
const panelCode = slice('function formPanel(el) {', 'window.cbFormCreateOpen = ');
const sandbox = { console, document: { createElement: (t) => new El(t) }, confirmAnswer: true };
sandbox.window = sandbox;
sandbox.confirm = () => sandbox.confirmAnswer;
vm.createContext(sandbox);
vm.runInContext(layoutCode, sandbox);
vm.runInContext(panelCode, sandbox);
const W = sandbox;

// ── a panel with the controls the code reads ────────────────────────────
function question(label, type, options) {
  const q = mk('div', 'cb-fc-q');
  const l = mk('input', 'cb-fc-q-label', { value: label }); q.appendChild(l);
  const ty = mk('select', 'cb-fc-q-type', { value: type }); q.appendChild(ty);
  const op = mk('textarea', 'cb-fc-q-options', { value: (options || []).join('\n') }); q.appendChild(op);
  const afterBox = mk('label', 'cb-fc-q-after-box', { hidden: true }); q.appendChild(afterBox);
  afterBox.appendChild(mk('input', 'cb-fc-q-after'));
  q.appendChild(mk('div', 'cb-fc-q-logic', { hidden: true }));
  return q;
}
const panel = mk('div', 'cb-form-create');
const on = mk('input', 'cb-fc-logic-on'); panel.appendChild(on);
const help = mk('div', 'cb-fc-logic-help', { hidden: true }); panel.appendChild(help);
const list = mk('div', 'cb-fc-questions'); panel.appendChild(list);
const nonlead = mk('div', 'cb-fc-nonlead', { hidden: true }); panel.appendChild(nonlead);
['title', 'text', 'site', 'button'].forEach((k) => nonlead.appendChild(mk('input', 'cb-fc-nl-' + k)));
list.appendChild(question('Budget', 'multiple_choice', ['Yes', 'No']));
list.appendChild(question('Company', 'short_answer'));
list.appendChild(question('Phone model', 'short_answer'));
const sels = (i) => list.kids[i].querySelectorAll('.cb-fc-logic-sel');
const choose = (i, answer, value) => {
  const s = sels(i).find((x) => x.getAttribute('data-answer') === answer);
  s.value = value; s.onchange();
};
const tplOf = () => W.cbFormPanelTemplate(panel);

// 1. Off by default: no keys of the feature in the template, rows hidden.
let t = tplOf();
assert.deepEqual(t.questions[0], { label: 'Budget', type: 'multiple_choice', options: ['Yes', 'No'] });
assert(!('non_lead_end' in t) && !('after_answer' in t.questions[1]));
assert.equal(sels(0).length, 0);

// 2. Turning it on builds one row per answer, each starting at "continue"; the help text shows.
on.checked = true; W.cbFormLogicToggle(on);
assert.equal(help.hidden, false);
assert.deepEqual(sels(0).map((s) => [s.getAttribute('data-answer'), s.value]), [['Yes', 'next'], ['No', 'next']]);
assert.equal(list.kids[1].querySelector('.cb-fc-q-after-box').hidden, false, '"Only shown after an answer" missing');
assert.equal(list.kids[1].querySelector('.cb-fc-q-logic').hidden, true, 'a short answer got answer rows');
// The choices of a row: continue, later questions only, submit, close.
assert.deepEqual(sels(0)[0].options.map((o) => o.value), ['', 'next', 'q2', 'q3', 'submit', 'close']);

// 3. Choices land in the template as the distinct stored type, keyed by answer text.
choose(0, 'Yes', 'q3'); choose(0, 'No', 'close');
t = tplOf();
assert.deepEqual(t.questions[0], { label: 'Budget', type: 'multiple_choice_logic', options: ['Yes', 'No'], logic: { Yes: 'q3', No: 'close' } });
assert.equal(nonlead.hidden, false, 'the non-lead page must show while an answer closes the form');
assert.equal(W.cbLayout.validateTemplate(Object.assign({}, t, { privacy_url: 'https://example.com/p', thank_you_text: 'ok',
  name: 'n', headline: 'h', contact_fields: ['email'] })).join('|').includes('Non-lead end page: title is required'), true);
nonlead.querySelectorAll('.cb-fc-nl-title')[0].value = 'Sorry';
nonlead.querySelectorAll('.cb-fc-nl-text')[0].value = 'Not for you';
nonlead.querySelectorAll('.cb-fc-nl-site')[0].value = 'https://example.com/';
t = tplOf();
assert.deepEqual(t.non_lead_end, { title: 'Sorry', text: 'Not for you', website_url: 'https://example.com/', button_text: '' });

// 4. "Only shown after an answer" rides along.
list.kids[2].querySelector('.cb-fc-q-after').checked = true;
assert.strictEqual(tplOf().questions[2].after_answer, true);

// 5. Editing the options keeps the choice of an answer that still exists; a new answer is unchosen.
const optBox = list.kids[0].querySelector('.cb-fc-q-options');
optBox.value = 'Yes'+'\n'+'No'+'\n'+'Maybe';
W.cbFormLogicRefresh(optBox);
assert.deepEqual(sels(0).map((s) => [s.getAttribute('data-answer'), s.value]), [['Yes', 'q3'], ['No', 'close'], ['Maybe', '']]);
assert.equal(W.cbLayout.validateTemplate(Object.assign({}, tplOf(), { name: 'n', headline: 'h', contact_fields: ['email'],
  privacy_url: 'https://example.com/p', thank_you_text: 'ok' })).includes('Question 1: choose what happens after the answer "Maybe"'), true);
optBox.value = 'Yes'+'\n'+'No'; W.cbFormLogicRefresh(optBox);

// 6. Removing question 2 moves the jump to question 3 up to question 2; a jump to the removed one is cleared.
W.cbFormQuestionRemove(mk('button', '', { parent: list.kids[1] }));
assert.equal(list.kids.length, 2);
assert.equal(sels(0).find((s) => s.getAttribute('data-answer') === 'Yes').value, 'q2', 'the jump did not follow its question');
choose(0, 'Yes', 'q2');
W.cbFormQuestionRemove(mk('button', '', { parent: list.kids[1] }));
assert.equal(sels(0).find((s) => s.getAttribute('data-answer') === 'Yes').value, '', 'a jump to a removed question survived');

// 6b. ACL-2178 review: a jump to the question right after the removed one survives in a question below it.
// Q1 MC, Q2 short, Q3 MC [A, B] with "A" -> question 4, Q4 short; remove Q1: Q3 becomes Q2 and "A" must be q3.
while (list.kids.length) list.kids[0].remove();
list.appendChild(question('Q1', 'multiple_choice', ['Yes', 'No']));
list.appendChild(question('Q2', 'short_answer'));
list.appendChild(question('Q3', 'multiple_choice', ['A', 'B']));
list.appendChild(question('Q4', 'short_answer'));
W.cbFormLogicRefresh(panel);
choose(2, 'A', 'q4');
assert.equal(sels(2).find((s) => s.getAttribute('data-answer') === 'A').value, 'q4');
W.cbFormQuestionRemove(mk('button', '', { parent: list.kids[0] }));
assert.equal(list.kids.length, 3);
assert.deepEqual(sels(1).map((s) => [s.getAttribute('data-answer'), s.value]), [['A', 'q3'], ['B', '']]);
// Put the panel back for the checks below.
while (list.kids.length) list.kids[0].remove();
list.appendChild(question('Budget', 'multiple_choice', ['Yes', 'No']));
list.appendChild(question('Phone model', 'short_answer'));
W.cbFormLogicRefresh(panel);
choose(0, 'Yes', 'q2'); choose(0, 'No', 'close');

// 7. Turning it off asks first when something is set; "no" keeps it on, "yes" sends plain questions.
W.confirmAnswer = false; on.checked = false; W.cbFormLogicToggle(on);
assert.equal(on.checked, true, 'declining the question must keep the conditions');
W.confirmAnswer = true; on.checked = false; W.cbFormLogicToggle(on);
t = tplOf();
assert.equal(t.questions[0].type, 'multiple_choice');
assert(!('logic' in t.questions[0]) && !('non_lead_end' in t), 'keys of the feature stayed after turning it off');
assert.equal(help.hidden, true);
console.log('ok');
