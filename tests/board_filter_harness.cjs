// Executes the generated page JavaScript with a minimal DOM/localStorage harness.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const storage = new Map([['acl.wall.open', '["1"]']]);
const reads = [];
const area = {
  open: false, style: {}, listeners: {},
  getAttribute: () => '1',
  addEventListener: function (event, callback) { this.listeners[event] = callback; },
  querySelector: () => pin,
};
const pin = {
  open: false, listeners: {},
  dataset: {status: 'review'},
  classList: { toggle: () => {} },
  getAttribute: () => 'acl-101',
  addEventListener: function(event, callback) { this.listeners[event] = callback; },
};
const chip = {
  dataset: {filter: 'review'}, listeners: {},
  setAttribute: () => {},
  addEventListener: function(event, callback) { this.listeners[event] = callback; },
};
const allAttributes = {};
const allChip = {
  dataset: {filter: 'all'}, listeners: {},
  setAttribute: (key, value) => { allAttributes[key] = value; },
  addEventListener: function(event, callback) { this.listeners[event] = callback; },
};
const document = {
  querySelectorAll: (selector) => ({'details.area': [area], 'details.pin': [pin], '.chip': [allChip, chip], '.pin': [pin], '.cap': [], 'nav.areas a': []}[selector]),
};
const localStorage = {
  getItem: (key) => { reads.push(String(key)); return storage.get(String(key)) ?? null; },
  setItem: (key, value) => storage.set(key, value),
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {document, localStorage, location: {hash: ''}});
assert.equal(area.open, true);
chip.listeners.click();
assert.equal(area.open, true);
// Filtering expands areas automatically; a card is still opened by the reader.
pin.open = true;
pin.listeners.toggle();
assert.equal(storage.get('acl.wall.pins'), '["acl-101"]');
chip.listeners.click();
// Native details dispatches this toggle event after the script changes open.
area.listeners.toggle();
assert.equal(area.open, true);
assert.equal(storage.get('acl.wall.open'), '["1"]');
assert.ok(!reads.includes('undefined'));
// All must reveal cards even when the reader previously collapsed every area.
area.open = false;
area.listeners.toggle();
assert.equal(storage.get('acl.wall.open'), '[]');
chip.listeners.click();
allChip.listeners.click();
assert.equal(area.open, true);
assert.equal(allAttributes['aria-pressed'], 'true');
area.listeners.toggle();
assert.equal(storage.get('acl.wall.open'), '["1"]');
// Repeated All is idempotent; it must never become a nonexistent status filter.
allChip.listeners.click();
assert.equal(area.open, true);
assert.equal(allAttributes['aria-pressed'], 'true');
