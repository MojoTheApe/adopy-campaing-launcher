const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const template = fs.readFileSync(process.argv[2],'utf8');
const script = template.slice(template.indexOf('var _mlxCache = {}'), template.indexOf('function amapRenderHits('));
function fixture(fetch) {
  const inst = {value:'A'}, msg = {}, hits = {}, input = {value:'X84'};
  const box = {querySelector: sel => ({'.amap-inst':inst,'.amap-msg':msg,'.amap-hits':hits,'.amap-find':input}[sel])};
  input.closest = () => box;
  const renders=[];
  const ctx={fetch, ACL_MLX_LABELS:{A:'Inhouse',B:'KLD'}};
  vm.createContext(ctx); vm.runInContext(script,ctx);
  ctx.amapRenderHits = (_box, _hits, profiles, instance) => renders.push({instance,names:profiles.map(p=>p.name)});
  return {ctx,inst,msg,hits,input,renders,btn:{closest:()=>box}};
}
(async()=>{
  const calls=[];
  const f=fixture(async (_url,opts)=>{
    calls.push(JSON.parse(opts.body));
    return {json:async()=>({ok:true,profiles:[{name:'KLD_X84'},{name:'KLD_X85'}]})};
  });
  f.ctx._mlxCache.A=[{name:'KLD_X84'},{name:'KLD_X85'}];
  await f.ctx.amapFind(f.btn);
  f.input.value='X85'; f.ctx.amapFilter(f.input);
  await new Promise(setImmediate);
  assert.equal(calls[0].search_text, '');
  assert.equal(f.ctx._mlxCache.A.length,2);
  assert.deepEqual(Array.from(f.renders.at(-1).names),['KLD_X85']);

  const resolves={};
  const race=fixture((_url,opts)=>new Promise(resolve=>{resolves[JSON.parse(opts.body).instance]=resolve;}));
  race.ctx.amapFilter(race.input);
  race.inst.value='B'; race.ctx.amapFilter(race.input);
  resolves.B({json:async()=>({ok:true,profiles:[{name:'KLD_X84'}]})});
  await new Promise(setImmediate);
  resolves.A({json:async()=>({ok:true,profiles:[{name:'INHOUSE_X84'}]})});
  await new Promise(setImmediate);
  assert.equal(race.inst.value,'B'); assert.equal(race.renders.at(-1).instance,'B');
  assert.equal(race.renders.length, 1);

  // Switching clears the previous clickable hits before a new response arrives.
  race.hits.innerHTML = 'old buttons';
  race.inst.value='A'; race.input.value=''; race.ctx.amapFilter(race.input);
  assert.equal(race.hits.innerHTML, '');
  assert.equal(race.hits.hidden, true);
})().catch(e=>{console.error(e);process.exit(1)});
