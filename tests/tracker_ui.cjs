/* Run with Node + Playwright (Chrome installed); Python fixture uses only stdlib. */
const {chromium}=require('playwright');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
(async()=>{
 const fixture=JSON.parse(execFileSync(process.env.TRACKER_TEST_PYTHON||'python3',[path.join(__dirname,'tracker_ui_fixture.py')],{encoding:'utf8'}));
 let status=fixture.status,data=fixture.data;
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  await page.addInitScript(()=>{const original=window.setInterval;window.setInterval=(fn,ms,...args)=>{if(ms>=15000){window.testRefresh=fn;return 1;}return original(fn,ms,...args);};});
  await page.route('http://tracker.test/**',r=>{const url=new URL(r.request().url());r.fulfill({status:200,contentType:url.pathname==='/'?'text/html':'application/json',body:url.pathname==='/'?fixture.page:JSON.stringify(url.pathname==='/status.json'?status:data)});});
  await page.goto('http://tracker.test/');await page.waitForFunction(()=>document.getElementById('sync-state').textContent.startsWith('Synced'));
  await page.locator('[data-view=all]').click();assert.equal(await page.locator('.task').count(),6);
  await page.locator('#design').check();await page.locator('#priority').selectOption('high');assert.equal(await page.locator('.task').count(),1);
  await page.locator('#status-review').click();assert.equal(await page.locator('.task').count(),0);
  await page.locator('#status-in_progress').click();assert.equal(await page.locator('.task').count(),1);
  await page.locator('#status-all').click();assert.equal(await page.locator('#priority').inputValue(),'high');
  await page.locator('#summary-4').click();await page.locator('#search').fill('Task 4');
  data=structuredClone(data);data.tasks[3].body+=' Updated';data.digest='updated';status={...status,digest:'updated'};
  await page.evaluate(()=>window.testRefresh());assert.equal(await page.locator('#task-4').getAttribute('open'),'');assert.equal(await page.locator('#search').inputValue(),'Task 4');
  assert.equal(await page.evaluate(()=>document.activeElement.id),'search');
  status={...status,ok:false,error:'empty_response'};await page.evaluate(()=>window.testRefresh());
  assert.equal(await page.locator('.task').count(),1);assert.match(await page.locator('#sync-state').innerText(),/Empty update ignored/);
  // Even a seemingly successful empty snapshot cannot erase the current browser.
  status={...status,ok:true,digest:'empty'};data={...data,digest:'empty',tasks:[]};await page.evaluate(()=>window.testRefresh());assert.equal(await page.locator('.task').count(),1);
  data=fixture.data;status=fixture.status;await page.evaluate(()=>window.testRefresh());
  await page.locator('[data-view=all]').click();assert.equal(await page.locator('.task').count(),6);
  await page.locator('[data-view=release]').click();assert.match(await page.locator('#release-summary').innerText(),/Scope ready/);
  status={...status,ok:false};await page.evaluate(()=>window.testRefresh());assert.match(await page.locator('#release-summary').innerText(),/Readiness unknown/);assert.equal(await page.locator('#release-summary.ready').count(),0);
  status={...status,ok:true};await page.evaluate(()=>window.testRefresh());await page.locator('#release').selectOption('2');assert.match(await page.locator('#release-summary').innerText(),/No scope defined/);
  await page.locator('[data-view=all]').click();await page.locator('#module').selectOption('accounts');await page.locator('#summary-4').click();await page.reload();
  assert.equal(await page.locator('#module').inputValue(),'accounts');
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.equal(await page.locator('.task-detail img').count(),0);assert.deepEqual(errors,[]);
  console.log('PASS: combined filters, status OR, All reset, data refresh with focus/details, empty-update protection, stale/empty release readiness, session persistence, mobile, injection safety');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
