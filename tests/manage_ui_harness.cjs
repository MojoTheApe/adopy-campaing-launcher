// Run actual navigation/account handlers against the rendered panel identities.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const blocks = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const source = blocks.join('\n');
function fn(name, optional = false) {
  const m = new RegExp('^( *)function ' + name + '\\(', 'm').exec(source);
  if (!m && optional) return '';
  assert.ok(m, 'missing executable handler ' + name);
  const rest = source.slice(m.index);
  const end = new RegExp('^' + m[1] + '}', 'm').exec(rest);
  assert.ok(end, 'function boundary ' + name);
  return rest.slice(0, end.index + end[0].length);
}
function node(id = '', country = '', classes = []) {
  const set = new Set(classes), events = {};
  return {id, dataset: {country}, style: {}, open: false, textContent: '', children: [],
    classList: {add: x => set.add(x), remove: x => set.delete(x), contains: x => set.has(x),
      toggle: (x, value) => { if (value === undefined) value = !set.has(x); value ? set.add(x) : set.delete(x); }},
    addEventListener: (name, f) => (events[name] ||= []).push(f),
    fire: function(name, e = {}) { (events[name] || []).forEach(f => f.call(this, e)); },
    appendChild: function(x) { this.children.push(x); }, after() {}, remove() {},
    scrollIntoView: function() { this.scrolled = true; },
  };
}
const panels = [node('view-home', '', ['view-panel'])], builders = [], links = [], countryBlocks = [], countryBtns = [];
const ids = new Map(), storage = new Map(), events = {}, winEvents = {};
for (const m of html.matchAll(/<div id="(view-(?:targets|ad-texts|accounts))" class="view-panel"/g)) {
  panels.push(node(m[1], '', ['view-panel']));
}
for (const m of html.matchAll(/<div id="(view-country-[^"]+)" class="view-panel" data-country="([^"]+)"/g)) {
  panels.push(node(m[1], m[2], ['view-panel']));
}
for (const m of html.matchAll(/<details class="manage-builder" id="([^"]+)"/g)) {
  const country = m[1].slice('manage-builder-'.length);
  const builder = node(m[1], country);
  const end = html.indexOf('<div id="view-country-', m.index);
  const fragment = html.slice(m.index, end < 0 ? html.indexOf('</main>', m.index) : end);
  builder.rows = [...fragment.matchAll(/class="cb-acc-row" data-acc-label="([^"]+)"/g)]
    .map(m => ({dataset: {accLabel: m[1]}}));
  builder.querySelectorAll = s => s === '.cb-acc-row' ? builder.rows
    : s === '.cb-acc-title' ? builder.rows.map(r => ({textContent: r.dataset.accLabel})) : [];
  builder.querySelector = s => s === '.cb-acc-title' ? builder.querySelectorAll(s)[0]
    : builder.rows.find(r => s.includes('"' + r.dataset.accLabel + '"')) || null;
  builders.push(builder);
  const block = node('', country), button = node('', country);
  button.closest = () => block; countryBlocks.push(block); countryBtns.push(button);
}
assert.equal(builders.length, 2, 'the fixture must render two actual builders');
for (const m of html.matchAll(/class="sidebar-link[^"]*" data-view="([^"]+)"/g)) {
  const link = node(); link.dataset.view = m[1]; links.push(link);
}
const homeLink = node('', '', ['sidebar-home']); homeLink.dataset.view = 'home'; links.push(homeLink);
panels.concat(builders).forEach(n => ids.set(n.id, n));
for (const country of ['mexico', 'brazil', 'missing']) {
  const input = node('ml-manual-profile-' + country); input.value = 'synthetic-profile';
  ids.set(input.id, input); ids.set('ml-manual-status-' + country, node());
}
let confirms = [], profileLoads = 0;
const context = {
  document: {readyState: 'loading', getElementById: id => ids.get(id) || null,
    querySelectorAll: s => ({'.view-panel': panels, '.sidebar-link[data-view]': links,
      '.sidebar-country': countryBlocks, '.sidebar-country-btn': countryBtns,
      'details.manage-builder[id]': builders, '.overview-row[data-view]': [],
      '.cb-acc-title': builders.flatMap(b => b.querySelectorAll('.cb-acc-title'))}[s] || []),
    querySelector: s => s === '.view-panel.active' ? panels.find(p => p.classList.contains('active')) || null
      : s === '.sidebar-home' ? homeLink : ids.get(s.slice(1)) || null,
    createElement: () => node(),
    addEventListener: (e, f) => (events[e] ||= []).push(f),
    dispatchEvent: e => (events[e.type] || []).forEach(f => f(e)),
  },
  location: {hash: '#home'}, history: {replaceState: (a, b, hash) => context.location.hash = hash},
  addEventListener: (e, f) => (winEvents[e] ||= []).push(f),
  CustomEvent: function(type, init) { this.type = type; this.detail = init.detail; },
  localStorage: {getItem: k => storage.get(k) || null, setItem: (k, v) => storage.set(k, v)},
  ACCOUNT_ALIAS_MAP: {mexico: {known: 'MX-acc-1'}, brazil: {known: 'BR-acc-1'}},
  CSS: {escape: v => v}, aclEscHtml: v => v, setTimeout: () => {},
  _safeConfirm: (title, body) => confirms.push(body),
  loadProfilesForCountryForms: () => profileLoads++,
};
context.window = context; vm.createContext(context);
const run = code => vm.runInContext(code, context);
const nav = blocks.find(b => b.includes('MERGED_INTO_MANAGE'));
assert.ok(nav);
if (scenario === 'accounts') {
  run(fn('aclCountryBuilder', true) + fn('accountOptionsFor') + fn('findAccountRow'));
  const options = context.accountOptionsFor('mexico');
  assert.deepEqual(JSON.parse(JSON.stringify(options)), [{label: 'MX-acc-1', loaded: true}]);
  assert.equal(context.findAccountRow('mexico', 'MX-acc-1').dataset.accLabel, 'MX-acc-1');
  assert.equal(context.findAccountRow('mexico', 'BR-acc-1'), null);
  assert.equal(context.findAccountRow('missing', 'MX-acc-1'), null);
} else if (scenario === 'manual_scope') {
  run(fn('aclCountryBuilder', true) + fn('mlManualSave'));
  context.mlManualSave('mexico');
  assert.equal(confirms.length, 1, 'one local account must not become a cross-country picker');
  assert.ok(confirms[0].includes('MX-acc-1')); assert.ok(!confirms[0].includes('BR-acc-1'));
  confirms = []; context.mlManualSave('missing'); assert.equal(confirms.length, 0);
} else if (scenario === 'navigation') {
  run(nav);
  for (const suffix of ['goals', 'cm', 'accounts', 'open']) {
    context.location.hash = '#country-mexico-' + suffix;
    (winEvents.hashchange || []).forEach(f => f());
    assert.equal(context.document.querySelector('.view-panel.active').id, 'view-country-mexico-manage');
  }
  assert.equal(ids.get('manage-builder-mexico').open, true, 'legacy builder link must reveal the builder');
  assert.equal(ids.get('manage-builder-brazil').open, false);
  assert.equal(context.aclOpenCountryBuilder('brazil'), true);
  assert.equal(context.document.querySelector('.view-panel.active').id, 'view-country-brazil-manage');
  assert.equal(ids.get('manage-builder-brazil').open, true);
  assert.equal(context.aclOpenCountryBuilder('missing'), false);
  for (const view of ['targets', 'ad-texts', 'accounts']) {
    context.location.hash = '#' + view;
    winEvents.hashchange.forEach(f => f());
    assert.equal(context.document.querySelector('.view-panel.active').id, 'view-' + view);
  }
} else if (scenario === 'profiles') {
  run(nav);
  const start = source.indexOf('  function loadActiveCountryProfiles()');
  assert.ok(start >= 0, 'profile hydration must be attached to navigation');
  run(source.slice(start, source.indexOf('  function addFromCountry(', start)));
  countryBtns[0].fire('click'); assert.equal(profileLoads, 1);
  context.location.hash = '#country-brazil-accounts';
  winEvents.hashchange.forEach(f => f()); assert.equal(profileLoads, 2);
  context.aclOpenCountryBuilder('mexico'); assert.equal(profileLoads, 3);
  events.DOMContentLoaded.forEach(f => f()); assert.equal(profileLoads, 4);
} else if (scenario === 'persistence') {
  const marker = source.indexOf("var KEY = 'acl.manage.builder'");
  assert.ok(marker >= 0);
  const start = source.lastIndexOf('(function () {', marker);
  const end = source.indexOf('})();', marker) + 5;
  storage.set('acl.manage.builder', '["manage-builder-brazil"]');
  run(source.slice(start, end)); events.DOMContentLoaded.forEach(f => f());
  assert.equal(builders[0].open, false); assert.equal(builders[1].open, true);
  builders[0].open = true; builders[0].fire('toggle');
  assert.deepEqual(JSON.parse(storage.get('acl.manage.builder')), builders.map(b => b.id));
  builders[1].open = false; builders[1].fire('toggle');
  assert.deepEqual(JSON.parse(storage.get('acl.manage.builder')), [builders[0].id]);
} else { throw Error('Unknown scenario: ' + scenario); }
