const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
function between(start, end) {
  const a = html.indexOf(start), b = html.indexOf(end, a);
  assert(a >= 0 && b > a, `Missing handler: ${start}`);
  return html.slice(a, b);
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

async function library() {
  const listeners = {}, pending = [];
  let renders = 0;
  const context = {
    document: {addEventListener(type, fn) {(listeners[type] ||= []).push(fn);}},
    fetch: () => new Promise(resolve => pending.push(resolve)),
    cbpRenderAll: () => renders++,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(between('  var AD_TITLES =', '  // ── Ad preview mini-cards'), context);
  const update = texts => {
    for (const fn of listeners['acl:adtextschange'] || []) fn({detail: {texts}});
  };
  // Saving a local edit before the startup request completes must win.
  update([{name:'Saved', headline:'New', body:'Local body'}]);
  pending.shift()({ok:true, json:async()=>({rows:[{ad_title:'Saved', headline:'Stale', body:'Old'}]})});
  await flush();
  assert.equal(context.AD_TITLES[0].headline, 'New');
  update([]);
  assert.equal(context.AD_TITLES.length, 0, 'Deleted copy remained in the picker');
  for (const fn of listeners['acl:adtextschange'] || []) fn({});
  pending.shift()({ok:false, json:async()=>({error:'Library damaged <fix>'})});
  await flush();
  assert.equal(context.AD_TITLES_ERROR, 'Library damaged <fix>');
  assert(renders >= 3, 'Previews were not refreshed with the library');
}

async function tracking() {
  assert(html.includes('name="tracking_link"'), 'Rendered form has no link field');
  let confirmation, savedLink, builds = 0;
  const classes = new Set(['cb-preflight-collapsed']);
  const panel = {classList:{remove:name=>classes.delete(name)}};
  const input = {value:'', classList:{add:name=>classes.add(name),remove:name=>classes.delete(name)},
    closest:()=>panel, scrollIntoView(){}, addEventListener(){}, removeEventListener(){}};
  const account = {value:'Demo'};
  // У настоящей формы есть и querySelectorAll — двойник без него ронял
  // cbPublish, как только тот начал обходить поля таргетинга. Пустой список
  // означает «слотов нет», и предупреждению о таргетинге здесь не о чем
  // говорить: этот стенд про трекинговую ссылку.
  const form = {classList:{contains:()=>false}, querySelector(selector) {
    if (selector.includes('tracking_link')) return input;
    if (selector.includes('account_label')) return account;
    return {value: selector.includes('camp_count') ? '1' : 'Mexico'};
  }, querySelectorAll(){ return []; }};
  const context = {ACCOUNT_PREFLIGHT:{Demo:{link:'',link_status:'empty'}}, aclEscHtml:esc,
    _safeConfirm:(title,body,run,options)=>{confirmation={body,run,options};},
    _doCbPublish:()=>builds++,
    fetch:async(url,opts)=>{
      savedLink=JSON.parse(opts.body).link;
      return {ok:true,json:async()=>({ok:true,link_status:'tracking'})};
    },
  };
  context.window=context;
  vm.createContext(context);
  vm.runInContext(between('window.cbTrackingWarning =', 'function _doCbPublish'),context);
  vm.runInContext(between('  window.cbSaveTrackingLink =', '  // ── Save individual account map field'),context);
  context.cbPublish(form);
  assert(confirmation.body.includes('No tracking link'));
  confirmation.run();
  assert.equal(builds,1,'A warning blocked the draft confirmation');
  assert(!classes.has('cb-preflight-collapsed'));

  context.ACCOUNT_PREFLIGHT.Demo={link:'https://example.test/?x=1&y=2',link_status:'tracking'};
  input.value='https://example.test/new?x=1&y=2';
  context.cbPublish(form);
  assert(confirmation.body.includes('x=1&amp;y=2'));
  assert(confirmation.body.includes('/new?'));
  assert(confirmation.body.includes('This build will use'));
  assert(!confirmation.body.includes('&amp;amp;'));
  const badge={};
  const row={querySelector:selector=>selector.includes('tracking_link') ? input : badge};
  const button={closest:()=>row};
  context.cbSaveTrackingLink(button,'Demo');
  await flush();
  assert.equal(savedLink,input.value);
  assert.equal(context.cbTrackingWarning(form),null,'Save succeeded but warning still says unsaved');
  context.ACCOUNT_PREFLIGHT.Demo.link_status='jira';
  assert(context.cbTrackingWarning(form).text.includes('Jira'));
}

(async()=>{ await library(); await tracking(); console.log('Library races, deletion, load errors and tracking save/confirmation passed.'); })()
  .catch(error=>{console.error(error);process.exitCode=1;});
