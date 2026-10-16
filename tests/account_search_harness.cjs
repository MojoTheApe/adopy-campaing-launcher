const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const template = fs.readFileSync(process.argv[2], 'utf8');
const script = template.slice(template.indexOf('var _mlxCache = {}'), template.indexOf('function amapRenderHits('));
function fixture(fetch) {
  const inst = {value:'A'}, msg = {}, hits = {}, input = {value:'X84'};
  const box = {querySelector: sel => ({'.amap-inst':inst,'.amap-msg':msg,'.amap-hits':hits,'.amap-find':input}[sel])};
  input.closest = () => box;
  const renders = [], timers = new Map(); let next = 0;
  const ctx = {fetch, ACL_MLX_LABELS:{A:'Inhouse',B:'KLD'},
    setTimeout(fn) { timers.set(++next, fn); return next; },
    clearTimeout(id) { timers.delete(id); }};
  vm.createContext(ctx); vm.runInContext(script, ctx);
  ctx.amapRenderHits = (_box, _hits, profiles, instance) => renders.push({instance,names:profiles.map(p=>p.name)});
  async function flush() {
    const pending = [...timers.values()]; timers.clear(); pending.forEach(fn=>fn());
    await new Promise(setImmediate);
  }
  return {ctx,inst,msg,hits,input,renders,flush,btn:{closest:()=>box}};
}
(async()=>{
  const calls=[];
  const f=fixture(async (_url,opts)=>{
    calls.push(JSON.parse(opts.body));
    return {json:async()=>({ok:true,profiles:[{name:'KLD_X84'},{name:'KLD_X85'}]})};
  });
  f.ctx.amapFilter(f.input);
  f.input.value='X85'; f.ctx.amapFilter(f.input);
  assert.equal(calls.length,0,'typing must be debounced');
  await f.flush();
  assert.equal(calls.length,1);
  assert.equal(calls[0].search_text,'X85');
  assert.deepEqual(Array.from(f.renders.at(-1).names),['KLD_X85']);
  f.ctx.amapFilter(f.input); await f.flush();
  assert.equal(calls.length,1,'repeat query must use cache');
  // ACL-1151: кнопки Reload нет — устаревший ответ перечитывается сам.
  vm.runInContext('for (var k in _mlxCacheAt) _mlxCacheAt[k] -= AMAP_SEARCH_TTL_MS + 1;', f.ctx);
  f.ctx.amapFilter(f.input); await f.flush();
  assert.equal(calls.length,2,'a stale answer must be fetched again');
  f.ctx.amapFilter(f.input); await f.flush();
  assert.equal(calls.length,2,'a fresh answer comes from the cache');

  const resolves={};
  const race=fixture((_url,opts)=>new Promise(resolve=>{resolves[JSON.parse(opts.body).instance]=resolve;}));
  race.ctx.amapFilter(race.input); await race.flush();
  race.inst.value='B'; race.ctx.amapFilter(race.input); await race.flush();
  resolves.B({json:async()=>({ok:true,profiles:[{name:'KLD_X84'}]})});
  await new Promise(setImmediate);
  resolves.A({json:async()=>({ok:true,profiles:[{name:'INHOUSE_X84'}]})});
  await new Promise(setImmediate);
  assert.equal(race.renders.at(-1).instance,'B');
  assert.equal(race.renders.length,1,'late response must not repaint another instance');
  race.hits.innerHTML='old buttons';
  race.inst.value='A'; race.input.value=''; race.ctx.amapFilter(race.input);
  assert.equal(race.hits.innerHTML,'');
  assert.equal(race.hits.hidden,true);
})().catch(e=>{console.error(e);process.exit(1)});
