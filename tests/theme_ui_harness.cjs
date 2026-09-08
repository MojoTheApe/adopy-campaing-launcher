// Execute the actual theme scripts with a controllable browser preference.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]).filter(s => s.includes('acl.theme'));
assert.ok(scripts.length);
// The older template keeps the toggle in its large application script.
const blocks = scripts.map(s => {
  if (!s.includes('function amapPeriod')) return s;
  const marker = s.indexOf("var KEY = 'acl.theme'");
  return s.slice(s.lastIndexOf('(function () {', marker), s.indexOf('})();', marker) + 5);
});

function boot({stored = null, dark = true, readFails = false, writeFails = false} = {}) {
  const attrs = {}, buttonAttrs = {}, docEvents = {}, buttonEvents = {}, mediaEvents = [];
  let writes = 0;
  const button = {setAttribute: (k, v) => buttonAttrs[k] = v,
    addEventListener: (k, f) => buttonEvents[k] = f};
  const media = {matches: dark,
    addEventListener: (k, f) => { assert.equal(k, 'change'); mediaEvents.push(f); }};
  const context = vm.createContext({
    document: {readyState: 'loading',
      documentElement: {setAttribute: (k, v) => attrs[k] = v, getAttribute: k => attrs[k]},
      getElementById: id => id === 'theme-toggle' ? button : null,
      addEventListener: (k, f) => (docEvents[k] ||= []).push(f)},
    localStorage: {
      getItem: () => { if (readFails) throw Error('storage blocked'); return stored; },
      setItem: (k, v) => { if (writeFails) throw Error('quota'); stored = v; writes++; },
    },
    matchMedia: () => media,
  });
  blocks.forEach(s => vm.runInContext(s, context));
  const beforeReady = attrs['data-theme'];
  (docEvents.DOMContentLoaded || []).forEach(f => f());
  return {theme: () => attrs['data-theme'], checked: () => buttonAttrs['aria-checked'],
    stored: () => stored, writes: () => writes, beforeReady,
    click: () => buttonEvents.click(),
    media: dark => { media.matches = dark; mediaEvents.forEach(f => f({matches: dark})); }};
}

if (scenario === 'automatic') {
  for (const stored of [null, 'invalid']) {
    const a = boot({stored});
    assert.equal(a.beforeReady, 'dark');
    assert.equal(a.checked(), 'true');
    assert.equal(a.writes(), 0, 'opening the page must not pin a system preference');
    assert.equal(boot({stored: a.stored(), dark: false}).theme(), 'light');
  }
} else if (scenario === 'media_change') {
  const a = boot(); a.media(false);
  assert.equal(a.theme(), 'light'); assert.equal(a.checked(), 'false');
  a.media(true); assert.equal(a.theme(), 'dark'); assert.equal(a.writes(), 0);
} else if (scenario === 'explicit') {
  const a = boot({stored: 'light'}); assert.equal(a.beforeReady, 'light');
  a.media(true); assert.equal(a.theme(), 'light');
  a.click(); assert.equal(a.theme(), 'dark'); assert.equal(a.checked(), 'true');
  assert.equal(a.stored(), 'dark'); assert.equal(boot({stored: a.stored(), dark: false}).theme(), 'dark');
  a.media(false); assert.equal(a.theme(), 'dark');
} else if (scenario === 'unreadable_storage') {
  const a = boot({readFails: true}); assert.equal(a.beforeReady, 'dark');
  a.click(); assert.equal(a.theme(), 'light'); assert.equal(a.checked(), 'false');
} else if (scenario === 'unwritable_storage') {
  const a = boot({writeFails: true}); a.click();
  assert.equal(a.theme(), 'light'); a.media(true); assert.equal(a.theme(), 'light');
} else { throw Error('Unknown scenario: ' + scenario); }
