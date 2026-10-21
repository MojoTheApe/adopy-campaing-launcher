// ACL-1202: the Accounts overview picker finds a profile by name, open or not.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const t = fs.readFileSync(process.argv[2], 'utf8');
const search = t.slice(t.indexOf('var _mlxCache = {}'), t.indexOf('function amapFilter('));
const pick = t.slice(t.indexOf('/* ACL-1202:begin */'), t.indexOf('/* ACL-1202:end */'));
const optStart = t.indexOf('  function profileOptions(current, suggested, extra) {');
const opt = t.slice(optStart, t.indexOf('  function checkLabel()', optStart));

function make(fetch, open) {
  const ctx = { fetch, openProfiles: open || [], Date,
    esc: v => String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;') };
  vm.createContext(ctx);
  vm.runInContext(search + pick + opt, ctx);
  return ctx;
}
const OPEN = [{ profile_id: 'o1', name: 'FacebookDQ501 - Duan', folder_id: 'f-open', key: 'Q501' }];
const reply = body => async () => ({ json: async () => body });

(async () => {
  // closed profile found by name, offered as closed, open one still first
  const calls = [];
  const c = make(async (_u, o) => { calls.push(JSON.parse(o.body)); return { json: async () => ({ ok: true, profiles: [
    { profile_id: 'c1', name: 'FacebookDoc19 - Eros', folder_id: 'f-closed', key: 'C19' },
    { profile_id: 'o1', name: 'FacebookDQ501 - Duan', folder_id: 'f-open', key: 'Q501' }] }) }; }, OPEN);
  const res = await c.aclPickerSearch('A', 'Doc19');
  assert.equal(res.state, 'found');
  assert.deepEqual(Array.from(res.profiles.map(p => p.profile_id)), ['c1']);   // ranked by the text typed
  const merged = c.aclPickerMerge(OPEN, res.profiles);
  assert.deepEqual(Array.from(merged, p => [p.profile_id, p.open]), [['o1', true], ['c1', false]]);
  assert.equal(merged[1].folder_id, 'f-closed', 'folder id carried through');
  const html = c.profileOptions('', '', res.profiles);
  assert.match(html, /FacebookDQ501 - Duan · open</);
  assert.match(html, /FacebookDoc19 - Eros · closed \(starts on Publish\)</);
  assert.ok(html.indexOf('DQ501') < html.indexOf('Doc19'), 'open first');

  // same text again: cache, no second request; a profile found AND open is one row
  await c.aclPickerSearch('A', 'Doc19');
  assert.equal(calls.length, 1, 'cached by (instance, text)');
  assert.equal(c.aclPickerMerge(OPEN, OPEN).length, 1);

  // suggestion keys on search results too; two with one key -> no guess; saved choice wins
  assert.equal(c.aclPickerSuggest('C19', merged, ''), 'c1');
  assert.equal(c.aclPickerSuggest('Q501', merged, ''), 'o1');
  assert.equal(c.aclPickerSuggest('C19', merged, 'o1'), '');
  const twins = c.aclPickerMerge(OPEN, [{ profile_id: 'c2', name: 'x', key: 'Q501' }]);
  assert.equal(c.aclPickerSuggest('Q501', twins, ''), '');
  assert.match(c.profileOptions('', 'c1', res.profiles), /value="c1" selected>[^<]*looks like this one/);

  // empty answer says so plainly
  const e = make(reply({ ok: true, profiles: [] }), OPEN);
  const none = await e.aclPickerSearch('A', 'DQ438');
  assert.equal(none.state, 'empty');
  assert.equal(none.message, 'DQ438 is not in Multilogin');

  // catalogue does not answer: not fatal, open profiles still offered, says so
  for (const f of [async () => { throw new Error('network down'); },
                   reply({ ok: false, error: 'MULTILOGIN_AUTOMATION_TOKEN_A not set' }),
                   reply(null)]) {
    const x = make(f, OPEN);
    const bad = await x.aclPickerSearch('A', 'DQ501');
    assert.equal(bad.state, 'failed');
    assert.match(bad.message, /catalogue did not answer/);
    assert.match(bad.message, /open profiles only/);
    assert.equal(bad.profiles.length, 0);
    assert.match(x.profileOptions('', '', bad.profiles), /DQ501 - Duan · open/);
  }
  // a saved profile that is in neither list is still shown
  assert.match(c.profileOptions('zzzzzzzz-1', '', []), /saved, profile not open/);
  // names are escaped
  const xss = make(reply({ ok: true, profiles: [] }), [{ profile_id: 'p', name: '<b>"x"' }]);
  assert.ok(!/<b>/.test(xss.profileOptions('', '', [])));
})().catch(e => { console.error(e); process.exit(1); });
