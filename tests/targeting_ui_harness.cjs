const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const code = html.slice(html.indexOf('// Targeting library UI.'), html.indexOf('</script>', html.indexOf('// Targeting library UI.')));
const elements = {}, listeners = {}, requests = [];
const get = id => elements[id] ||= {id, value:'', style:{}, hidden:false, disabled:false, getAttribute(){return null;}};
const name = 'A\\" [odd] <script>';
let release;
const context = {
 document:{getElementById:get, querySelectorAll(){return Object.values(elements);}, addEventListener(t,fn){listeners[t]=fn;}},
 location:{hash:''}, confirm(){return true;},
 fetch:async (url, opts) => {
   if (opts) {
     requests.push(JSON.parse(opts.body));
     if (release) await new Promise(r => release = r);
     return {ok:true, json:async()=>({ok:true,shared:false,shared_error:'offline',profile:{name}})};
   }
   return {ok:true,json:async()=>({ok:true,profiles:[{name,interests:'Finance',narrow:'Owners',shared:false,shared_error:'offline'}],sheet:{}})};
 }
};
vm.runInNewContext(code, context);
const flush = () => new Promise(r=>setImmediate(r));
const click = (id, attrs={}) => listeners.click({target:{id,getAttribute(k){return attrs[k] || null;}}});
(async()=>{
 click('',{'data-view':'targets'}); await flush();
 assert(get('tg-list').innerHTML.includes('&lt;script&gt;'));
 click('',{'data-tg-edit':name});
 assert.equal(get('tg-name').value,name); assert.equal(get('tg-narrow').value,'Owners');
 click('tg-cancel-btn'); assert.equal(get('tg-name').value,'');
 get('tg-name').value='New'; get('tg-interests').value='Travel';
 click('tg-add-btn'); await flush(); assert.equal(requests[0].action,'add');
 assert(get('tg-msg').textContent.includes('Sharing failed'));
 release = true; click('',{'data-tg-share':name}); await flush();
 click('',{'data-tg-del':name}); assert.equal(requests.length,2);
 release(); await flush(); assert.equal(requests[1].action,'share');
})().catch(e=>{console.error(e);process.exitCode=1;});
