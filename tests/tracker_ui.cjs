/* Node + Playwright. Synthetic HTTP routes only; never reaches a live tracker. */
const {chromium}=require('playwright');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
 const fixture=JSON.parse(execFileSync(process.env.TRACKER_TEST_PYTHON||'python3',[path.join(__dirname,'tracker_ui_fixture.py')],{encoding:'utf8'}));
 let status=fixture.status,data=fixture.data;
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  await page.addInitScript(()=>{const original=window.setInterval;window.setInterval=(fn,ms,...args)=>{if(ms>=15000){window.testRefresh=fn;return 1;}return original(fn,ms,...args);};});
  await page.route('http://tracker.test/**',r=>{const url=new URL(r.request().url());r.fulfill({status:200,contentType:url.pathname==='/'?'text/html':'application/json',body:url.pathname==='/'?fixture.page:JSON.stringify(url.pathname==='/status.json'?status:data)});});
  await page.goto('http://tracker.test/');await page.waitForFunction(()=>document.getElementById('sync-state').textContent.startsWith('Synced'));
  assert.equal(await page.locator('.task').count(),6);assert.equal(await page.locator('#view-all b').innerText(),'6');assert.equal(await page.locator('#view-backlog b').innerText(),'1');assert.equal(await page.locator('#mvp-count').innerText(),'100%');
  // Status navigation has exact slices; modules count that slice and no removed controls survive.
  assert.equal(await page.locator('#views, #mobile-views, #density-toggle, .st-metrics').count(),0);
  assert.equal(await page.locator('#pipeline button').count(),8);
  for(const stage of ['idea','backlog','todo','in_progress','review','merged']){
    await page.locator('#view-'+stage).click();assert.equal(await page.locator('.task').count(),1);
    assert.equal(await page.locator('#modules-accounts .st-nav-count').innerText(),'1');
    assert.equal(await page.locator('#view-'+stage).getAttribute('aria-pressed'),'true');
    await page.locator('[data-layout=board]').click();assert.equal(await page.locator('.st-lane').count(),1);
    await page.locator('[data-layout=list]').click();
  }
  await page.locator('#view-archive').click();assert.equal(await page.locator('.task').count(),0);
  await page.locator('#view-all').click();assert.equal(await page.locator('#modules-accounts .st-nav-count').innerText(),'6');
  await page.locator('#filters-toggle').click();assert.equal(await page.locator('#filters-panel').evaluate(e=>e.tagName),'SECTION');assert.equal(await page.locator('#filters-panel').isVisible(),true);assert.equal(await page.locator('#filters-toggle').getAttribute('aria-expanded'),'true');await page.locator('#filter-flags-design').check();await page.locator('#filter-priorities-high').check();assert.equal(await page.locator('.task').count(),1);
  await page.locator('#filter-statuses-review').check();assert.equal(await page.locator('.task').count(),0);
  await page.locator('#filter-statuses-in_progress').check();assert.equal(await page.locator('.task').count(),1);
  await page.keyboard.press('Escape');await page.locator('#summary-4').click();assert.equal(await page.locator('#task-detail').evaluate(e=>e.open),true);
  await page.keyboard.press('Escape');assert.equal(await page.evaluate(()=>document.activeElement.id),'summary-4');
  await page.setViewportSize({width:1920,height:1000});await page.locator('#search').fill('Task 4');await page.locator('#summary-4').click();
  const titleNode=await page.locator('#summary-4').elementHandle();
  data=structuredClone(data);data.tasks[3].body+=' Updated';data.digest='updated';status={...status,digest:'updated'};
  await page.evaluate(()=>window.testRefresh());assert.equal(await page.locator('#task-detail').evaluate(e=>e.open),true);assert.match(await page.locator('.task-body').innerText(),/Updated/);assert.equal(await page.locator('#search').inputValue(),'Task 4');assert.equal(await page.evaluate(()=>document.activeElement.id),'detail-close');
  // A completed task leaving the filter stays readable in the open detail panel.
  data.tasks[3].status='merged';data.digest='moved';status.digest='moved';await page.evaluate(()=>window.testRefresh());assert.equal(await page.locator('.task').count(),0);assert.match(await page.locator('#detail-content').innerText(),/Outside current filters/);
  await page.locator('#detail-close').click();data=structuredClone(fixture.data);status={...fixture.status};await page.evaluate(()=>window.testRefresh());assert.equal(await page.locator('.task').count(),1);
  status={...status,ok:false,error:'empty_response'};await page.evaluate(()=>window.testRefresh());assert.equal(await page.locator('.task').count(),1);assert.match(await page.locator('#sync-state').innerText(),/Empty update ignored/);
  status={...status,ok:true,digest:'empty'};data={...data,digest:'empty',tasks:[]};await page.evaluate(()=>window.testRefresh());assert.equal(await page.locator('.task').count(),1);
  data=structuredClone(fixture.data);status={...fixture.status};await page.evaluate(()=>window.testRefresh());await page.locator('#view-all').click();assert.equal(await page.locator('.task').count(),6);
  // Layout is independent from filtering and a card opens the same canonical ID.
  await page.locator('#modules-accounts').click();await page.locator('#search').fill('Task');await page.locator('[data-layout=board]').click();assert.equal(await page.locator('.task').count(),6);assert.equal(await page.locator('.st-lane').count(),6);assert.equal(await page.locator('#search').inputValue(),'Task');await page.locator('[data-layout=list]').click();assert.equal(await page.locator('.task').count(),6);
  // No replacement of unchanged rows on a fresh data snapshot.
  const stable=await page.locator('#summary-3').elementHandle();data=structuredClone(data);data.digest='same-rows';status.digest='same-rows';await page.evaluate(()=>window.testRefresh());assert.equal(await stable.evaluate(e=>e.isConnected),true);
  await page.locator('#mvp-metric').click();assert.match(await page.locator('#release-summary').innerText(),/Scope ready/);status.ok=false;await page.evaluate(()=>window.testRefresh());assert.match(await page.locator('#release-summary').innerText(),/Readiness unknown/);assert.equal(await page.locator('#release-summary.ready').count(),0);
  status.ok=true;await page.evaluate(()=>window.testRefresh());await page.locator('#filters-toggle').click();await page.locator('#filter-selectedRelease-2').check();assert.match(await page.locator('#release-summary').innerText(),/Scope not defined/);assert.equal(await page.locator('#mvp-count').innerText(),'—');await page.keyboard.press('Escape');
  // Required archived leaf tasks stay in the unresolved release scope; parents don't count.
  data=structuredClone(fixture.data);const archived={...data.tasks[1],number:7,id:'#7',archive:true,required:true,milestone:1,milestone_title:'MVP'};const parent={...data.tasks[1],number:8,id:'#8',required:true,initiative:true,milestone:1,milestone_title:'MVP'};data.tasks.push(archived,parent);data.milestones[0].summary={total:2,complete:1,remaining:1,blocked:0,unresolved:[],ready:false};data.digest='archive-scope';status={...fixture.status,digest:data.digest,issues:8};await page.evaluate(()=>window.testRefresh());await page.locator('#filters-toggle').click();await page.locator('#filter-selectedRelease-1').check();await page.keyboard.press('Escape');await page.locator('#mvp-metric').click();assert.equal(await page.locator('.task').count(),2);assert.equal(await page.locator('#task-8').count(),0);await page.locator('#scope-remaining').click();assert.equal(await page.locator('.task').count(),1);assert.equal(await page.locator('#task-7').count(),1);assert.equal(await page.locator('#mvp-count').innerText(),'50%');assert.equal(await page.locator('#view-all b').innerText(),'7');
  await page.locator('#view-all').click();assert.equal(await page.locator('.task').count(),7);await page.locator('#view-archive').click();assert.equal(await page.locator('.task').count(),1);
  await page.locator('#view-all').click();await page.locator('#modules-accounts').click();await page.getByRole('button',{name:'Dark',exact:true}).click();await page.reload();assert.equal(await page.locator('#modules-accounts').getAttribute('aria-pressed'),'true');assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
  await page.evaluate(()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.copied=value;}}}));await page.locator('#task-4 [data-copy] .st-id').click();assert.equal(await page.evaluate(()=>window.copied),'example-4');assert.equal(await page.locator('#task-detail').evaluate(e=>e.open),false);assert.equal(await page.locator('#copy-toast').innerText(),'Copied example-4');assert.equal(await page.locator('#task-4 .st-id').innerText(),'example-4');await page.locator('#task-4 [data-copy] svg').click();assert.equal(await page.evaluate(()=>window.copied),'example-4');await page.evaluate(()=>navigator.clipboard.writeText=async()=>{throw Error('denied')});await page.locator('#task-3 [data-copy]').click();assert.equal(await page.locator('#copy-toast').innerText(),'Copy manually: example-3');
  await page.emulateMedia({reducedMotion:'reduce'});await page.getByRole('button',{name:'Light',exact:true}).click();assert.equal(await page.locator('#signal-tracker-v2').evaluate(e=>getComputedStyle(e).transitionDuration),'0s');
  for(const width of [360,736,1024,1440,2560]){await page.setViewportSize({width,height:1000});await page.waitForTimeout(130);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);}
  await page.setViewportSize({width:390,height:844});await page.locator('[data-layout=board]').click();assert.equal(await page.locator('.st-lane').count(),1);await page.locator('#column-review').click();assert.equal(await page.locator('#task-5').count(),1);await page.locator('#nav-toggle').click();await page.locator('#mobile-modules-accounts').click();assert.equal(await page.locator('#navigation-panel').evaluate(e=>e.open),false);await page.locator('#filters-toggle').click();assert.equal(await page.locator('#filters-panel').isVisible(),true);assert.equal(await page.locator('#filters-panel').evaluate(e=>e.matches(':modal')),false);await page.keyboard.press('Escape');
  await page.locator('[data-layout=list]').click();await page.locator('#summary-4').click();assert.equal(await page.locator('#task-detail').evaluate(e=>getComputedStyle(e).animationName),'none');assert.equal(await page.locator('#task-detail').evaluate(e=>e.matches(':modal')),true);assert.equal(await page.locator('#task-detail img').count(),0);await page.keyboard.press('Escape');
  status={...fixture.status,digest:'clear',empty_confirmed:true};data={...fixture.data,digest:'clear',tasks:[]};await page.evaluate(()=>window.testRefresh());assert.equal(await page.locator('.task').count(),0);
  // Malformed successful payloads cannot replace the last good snapshot.
  data=structuredClone(fixture.data);status={...fixture.status};await page.evaluate(()=>window.testRefresh());
  data={...data,digest:'bad',modules:[null]};status.digest='bad';await page.evaluate(()=>window.testRefresh());assert.ok(await page.locator('.task').count()>0);assert.match(await page.locator('#sync-state').innerText(),/Connection lost/);
  await page.setViewportSize({width:1440,height:1000});await page.locator('#view-all').click();
  data=structuredClone(fixture.data);for(let n=20;n<150;n++)data.tasks.push({...structuredClone(data.tasks[1]),number:n,id:'example-'+n,title:'Long list task '+n});data.digest='long';status={...fixture.status,digest:'long',issues:data.tasks.length};await page.evaluate(()=>window.testRefresh());await page.evaluate(()=>scrollTo(0,2400));
  const anchor=await page.evaluate(()=>{const e=[...document.querySelectorAll('.task')].find(x=>x.getBoundingClientRect().bottom>0);return {id:e.id,top:e.getBoundingClientRect().top}});
  data.tasks.push({...structuredClone(data.tasks[3]),number:200,id:'example-200',title:'New work above the reading position'});data.digest='inserted';status.digest='inserted';status.issues=data.tasks.length;await page.evaluate(()=>window.testRefresh());assert.ok(Math.abs(await page.locator('#'+anchor.id).evaluate(e=>e.getBoundingClientRect().top)-anchor.top)<2);
  // An invalid fragment must not stop the UI, and old unsupported filters are cleared.
  data=structuredClone(fixture.data);status={...fixture.status};await page.evaluate(()=>{sessionStorage.clear();localStorage.clear();sessionStorage.setItem('tracker.v2.owner/example',JSON.stringify({view:'all',priority:'normal',module:'removed',statuses:['next'],theme:'dark'}));});await page.goto('http://tracker.test/#%');await page.reload();assert.equal(await page.locator('#view-all b').innerText(),'6');assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');assert.equal(await page.locator('#chips .st-chip').count(),0);
  // Removed Work/Plan and density preferences cannot reopen hidden views.
  await page.evaluate(()=>{sessionStorage.clear();localStorage.setItem('tracker.design.v2.owner/example.prefs',JSON.stringify({view:'work',density:'comfortable'}));});await page.reload();
  assert.equal(await page.locator('#view-all').getAttribute('aria-pressed'),'true');assert.equal(await page.locator('.task').count(),6);
  await page.emulateMedia({reducedMotion:'no-preference'});await page.locator('#summary-4').click();
  assert.equal(await page.locator('#task-detail').evaluate(e=>getComputedStyle(e).animationName),'st-detail-enter');
  await page.keyboard.press('Escape');await page.getByRole('button',{name:'Dark',exact:true}).click();
  assert.ok(await page.locator('#signal-tracker-v2').evaluate(e=>getComputedStyle(e).transitionDuration.split(',').every(v=>parseFloat(v)>=.3)));
  // The entire row surface opens details; individual controls keep their own behavior.
  await page.locator('#view-all').click();await page.setViewportSize({width:1920,height:1000});
  await page.locator('#task-4').click({position:{x:3,y:3}});assert.equal(await page.locator('#task-detail').evaluate(e=>e.open),true);
  await page.keyboard.press('Escape');let rowBox=await page.locator('#task-4').boundingBox();
  await page.locator('#task-4').click({position:{x:3,y:rowBox.height-3}});assert.match(await page.locator('#detail-title').innerText(),/Task 4/);
  await page.locator('#task-5 .st-stage').click();assert.match(await page.locator('#detail-title').innerText(),/Task 5/);
  await page.keyboard.press('Escape');
  await page.locator('#task-4').evaluate(e=>{const a=document.createElement('a');a.href='#separate';a.textContent='Separate link';a.id='separate-link';a.onclick=event=>{event.preventDefault();window.linkClicked=true;};e.append(a);});
  await page.locator('#separate-link').click();assert.equal(await page.evaluate(()=>window.linkClicked),true);assert.equal(await page.locator('#task-detail').evaluate(e=>e.open),false);await page.locator('#separate-link').evaluate(e=>e.remove());
  await page.locator('#summary-4').focus();await page.keyboard.press('Enter');
  await page.locator('#task-detail').evaluate(e=>Promise.all(e.getAnimations().map(a=>a.finished)));
  const handle=page.locator('#detail-resize'),grip=await handle.boundingBox(),initial=await page.locator('#task-detail').boundingBox();
  await page.mouse.move(grip.x+6,grip.y+180);await page.mouse.down();await page.mouse.move(grip.x-194,grip.y+180,{steps:10});await page.mouse.up();
  let wide=await page.locator('#task-detail').boundingBox();assert.ok(Math.abs(wide.width-initial.width-200)<2);assert.equal(await page.locator('#task-detail').evaluate(e=>e.open),true);
  await handle.press('ArrowRight');wide=await page.locator('#task-detail').boundingBox();assert.ok(Math.abs(wide.width-initial.width-168)<2);
  const savedWidth=wide.width;await page.locator('#search').click();assert.equal(await page.locator('#task-detail').evaluate(e=>e.open),false);assert.equal(await page.evaluate(()=>document.activeElement.id),'search');
  await page.locator('#summary-4').click();assert.ok(Math.abs((await page.locator('#task-detail').boundingBox()).width-savedWidth)<2);
  await handle.press('End');assert.ok((await page.locator('#task-detail').boundingBox()).width<=960);await handle.press('Home');assert.equal(Math.round((await page.locator('#task-detail').boundingBox()).width),360);
  await handle.press('ArrowLeft');await page.reload();assert.equal(Math.round((await page.locator('#task-detail').boundingBox()).width),392);
  await page.keyboard.press('Escape');await page.setViewportSize({width:1440,height:1000});await page.locator('#summary-4').click();
  await page.locator('#task-detail').evaluate(e=>Promise.all(e.getAnimations().map(a=>a.finished)));
  // A drag starting inside does not count as an outside click; an actual backdrop click does.
  const panel=await page.locator('#task-detail').boundingBox();await page.mouse.move(panel.x+80,160);await page.mouse.down();await page.mouse.move(300,160,{steps:8});await page.mouse.up();assert.equal(await page.locator('#task-detail').evaluate(e=>e.open),true);
  await page.mouse.click(300,160);assert.equal(await page.locator('#task-detail').evaluate(e=>e.open),false);
  await page.locator('#search').focus();assert.equal(await page.locator('#search').evaluate(e=>getComputedStyle(e).outlineStyle),'none');assert.ok((await page.locator('.st-search').boundingBox()).width<=280);
  await page.locator('#filters-toggle').click();await page.locator('#filter-priorities-medium').check();await page.locator('#filters-toggle').click();assert.equal(await page.locator('#filters-panel').isVisible(),false);
  await page.locator('#filters-toggle').click();assert.equal(await page.locator('#filter-priorities-medium').isChecked(),true);await page.keyboard.press('Escape');await page.locator('#view-all').click();
  await page.evaluate(async()=>{await document.fonts.ready;window.fontLoads=0;document.fonts.addEventListener('loading',()=>window.fontLoads++);});
  const typeBefore=await page.locator('#summary-4').evaluate(e=>{const r=document.createRange();r.selectNodeContents(e);return {font:getComputedStyle(e).fontFamily,width:r.getBoundingClientRect().width};});
  for(const theme of ['light','dark','system']){
    await page.locator('button[data-theme='+theme+']').click();
    assert.equal(await page.locator('#summary-4').evaluate(e=>getComputedStyle(e).color),await page.locator('#signal-tracker-v2').evaluate(e=>getComputedStyle(e).color));
    const typeAfter=await page.locator('#summary-4').evaluate(e=>{const r=document.createRange();r.selectNodeContents(e);return {font:getComputedStyle(e).fontFamily,width:r.getBoundingClientRect().width};});assert.deepEqual(typeAfter,typeBefore);
  }
  assert.equal(await page.evaluate(()=>window.fontLoads),0);
  const logo=page.locator('.st-logo');assert.equal(await logo.evaluate(e=>getComputedStyle(e).animationName),'st-logo-flow');
  const position=await logo.evaluate(e=>getComputedStyle(e).backgroundPosition);await page.waitForTimeout(180);assert.notEqual(await logo.evaluate(e=>getComputedStyle(e).backgroundPosition),position);
  await page.emulateMedia({reducedMotion:'reduce'});assert.equal(await logo.evaluate(e=>getComputedStyle(e).animationName),'none');
  await page.setViewportSize({width:390,height:844});await page.locator('#summary-4').click();assert.equal(await handle.isVisible(),false);assert.equal(Math.round((await page.locator('#task-detail').boundingBox()).width),390);await page.keyboard.press('Escape');
  assert.deepEqual(errors,[]);
  console.log('PASS: exact counts and archived release scope, filters, stable keyed refresh, details outside filter, empty-update protection, stale readiness, List/Board, storage, copy success/denial (mock), themes, responsive navigation/dialogs, injection safety');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
