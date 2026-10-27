const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
assert(html.trimStart().startsWith('<!DOCTYPE html>'), 'JavaScript/text leaked before the document');
const code = html.slice(html.indexOf('// Targeting library UI.'), html.indexOf('</script>', html.indexOf('// Targeting library UI.')));
const elements = {}, listeners = {}, requests = [];
const get = id => elements[id] ||= {id, value:'', style:{}, hidden:false, disabled:false, getAttribute(){return null;}};
const name = 'A\\" [odd] <script>';
let release, confirmed = true, saveOk = true;
let library = {connected:true, ok:true, error:'', synced_at:'2026-09-18 10:05:00 UTC'};
const context = {
 document:{getElementById:get, querySelectorAll(){return Object.values(elements);}, addEventListener(t,fn){listeners[t]=fn;}},
 location:{hash:''}, confirm(){return confirmed;}, aclConfirm(){return Promise.resolve(confirmed);},
 fetch:async (url, opts) => {
   if (opts) {
     requests.push(JSON.parse(opts.body));
     if (release) await new Promise(r => release = r);
     return saveOk ? {ok:true, json:async()=>({ok:true,profile:{name},scope:library.connected ? 'team' : 'local'})}
                   : {ok:false, json:async()=>({ok:false,error:'GitHub is unreachable: timed out Nothing was saved.'})};
   }
   return {ok:true,json:async()=>({ok:true,library,profiles:[{name,interests:'Finance',narrow:'Owners',added_by:'Anna',added_at:'2026-09-18'},
     {name:'Alias',interests:'Finance',narrow:'Owners'}],duplicates:{names:[name],sets:[[name,'Alias']]},
     deleted:[{name:'Gone <i>',deleted_by:'Boris',deleted_at:'2026-09-18 09:00:00 UTC',item:{interests:'Travel'}}]})};
 }
};
vm.runInNewContext(code, context);
const flush = () => new Promise(r=>setImmediate(r));
const click = (id, attrs={}) => listeners.click({target:{id,getAttribute(k){return attrs[k] || null;}}});
(async()=>{
 click('',{'data-view':'targets'}); await flush();
 assert(get('tg-list').innerHTML.includes('&lt;script&gt;'));
 assert(get('tg-list').innerHTML.includes('Same name twice:'));
 assert(get('tg-list').innerHTML.includes('Same set of interests:'));
 assert(get('tg-list').innerHTML.includes('Anna'), 'who added is not shown');
 assert(get('tg-status').textContent.includes('refreshed'), get('tg-status').textContent);
 assert(get('tg-deleted').innerHTML.includes('Boris'), 'who deleted is not shown');
 assert(!get('tg-deleted').innerHTML.includes('Gone <i>'), 'deleted name leaked raw');
 click('',{'data-tg-edit':name});
 assert.equal(get('tg-name').value,name); assert.equal(get('tg-narrow').value,'Owners');
 click('tg-cancel-btn'); assert.equal(get('tg-name').value,'');
 get('tg-name').value='New'; get('tg-interests').value='Travel';
 click('tg-add-btn'); await flush(); assert.equal(requests[0].action,'add');
 assert(get('tg-msg').textContent.includes('Everyone gets it'), get('tg-msg').textContent);
 // A second click while saving does not leave.
 release = true; click('',{'data-tg-restore':'Gone <i>'}); await flush();
 click('',{'data-tg-del':name}); assert.equal(requests.length,2);
 release(); release = null; await flush(); assert.equal(requests[1].action,'restore'); assert.equal(requests[1].name,'Gone <i>');
 confirmed = false; click('',{'data-tg-del':name}); await flush(); assert.equal(requests.length,2);
 confirmed = true;
 saveOk = false; get('tg-name').value='X'; get('tg-interests').value='Y';
 click('tg-add-btn'); await flush();
 assert(get('tg-msg').textContent.includes('Nothing was saved'), get('tg-msg').textContent);
 assert.equal(get('tg-name').value,'X', 'a failed save must keep what was typed');
 saveOk = true;
 library = {connected:false, ok:true, error:'', synced_at:''};
 click('tg-refresh-btn'); await flush();
 assert(get('tg-status').textContent.includes('not connected'), get('tg-status').textContent);
 get('tg-name').value='Local'; get('tg-interests').value='Local interest';
 click('tg-add-btn'); await flush();
 assert(get('tg-msg').textContent.includes('on this computer only'), get('tg-msg').textContent);
 assert(!get('tg-msg').textContent.includes('Everyone'));
})().catch(e=>{console.error(e);process.exitCode=1;});
