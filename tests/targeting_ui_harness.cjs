const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
assert(html.trimStart().startsWith('<!DOCTYPE html>'), 'JavaScript/text leaked before the document');
const code = html.slice(html.indexOf('// Targeting library UI.'), html.indexOf('</script>', html.indexOf('// Targeting library UI.')));
const elements = {}, listeners = {}, requests = [];
const get = id => elements[id] ||= {id, value:'', style:{}, hidden:false, disabled:false, getAttribute(){return null;}};
const name = 'A\\" [odd] <script>';
let release, confirmed = true;
const context = {
 document:{getElementById:get, querySelectorAll(){return Object.values(elements);}, addEventListener(t,fn){listeners[t]=fn;}},
 location:{hash:''}, confirm(){return confirmed;},
 fetch:async (url, opts) => {
   if (opts) {
     requests.push(JSON.parse(opts.body));
     if (release) await new Promise(r => release = r);
     if (JSON.parse(opts.body).action === 'export') return {ok:true,json:async()=>({ok:true,exported:2,backup:'synthetic-backup.json'})};
     return {ok:true, json:async()=>({ok:true,shared:false,shared_error:'offline',profile:{name}})};
   }
   return {ok:true,json:async()=>({ok:true,profiles:[{name,interests:'Finance',narrow:'Owners',shared:false,shared_error:'offline'},
     {name:'Alias',interests:'Finance',narrow:'Owners'}],sheet:{},duplicates:{names:[name],sets:[[name,'Alias']]}})};
 }
};
vm.runInNewContext(code, context);
const flush = () => new Promise(r=>setImmediate(r));
const click = (id, attrs={}) => listeners.click({target:{id,getAttribute(k){return attrs[k] || null;}}});
(async()=>{
 click('tg-export-btn'); await flush(); assert.equal(requests.length,0);
 click('',{'data-view':'targets'}); await flush();
 assert(get('tg-list').innerHTML.includes('&lt;script&gt;'));
 assert(get('tg-list').innerHTML.includes('Same name twice:'));
 assert(get('tg-list').innerHTML.includes('Same set of interests:'));
 click('',{'data-tg-edit':name});
 assert.equal(get('tg-name').value,name); assert.equal(get('tg-narrow').value,'Owners');
 click('tg-cancel-btn'); assert.equal(get('tg-name').value,'');
 get('tg-name').value='New'; get('tg-interests').value='Travel';
 click('tg-add-btn'); await flush(); assert.equal(requests[0].action,'add');
 assert(get('tg-msg').textContent.includes('Sharing failed'));
 release = true; click('',{'data-tg-share':name}); await flush();
 click('',{'data-tg-del':name}); assert.equal(requests.length,2);
 release(); release = null; await flush(); assert.equal(requests[1].action,'share');
 confirmed = false; click('tg-export-btn'); await flush(); assert.equal(requests.length,2);
 confirmed = true; click('tg-export-btn'); await flush();
 assert.equal(requests[2].action,'export'); assert.equal(requests[2].confirmation,'replace_shared_library');
 assert(get('tg-msg').textContent.includes('synthetic-backup.json'));
})().catch(e=>{console.error(e);process.exitCode=1;});
