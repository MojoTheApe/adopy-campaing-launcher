const fs=require('fs'), vm=require('vm'), assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8');
function block(start,end='\n}\n') {const a=html.indexOf(start);assert(a>=0,start);const b=html.indexOf(end,a);assert(b>a,end);return html.slice(a,b+end.length);}
const mode=process.argv[3];
const sandbox={window:{},Promise,Object,String,Error,JSON,FormData,AbortController,setTimeout,clearTimeout};
let preparations=0, submissions=0, confirmations=0;
if(mode==='lost-publish') {
 sandbox.window.aclPrepareConnection=async()=>{if(++preparations>1)throw new Error('profile busy with first job');};
 sandbox.fetch=async()=>{submissions++;if(submissions===1)throw new Error('reply lost');return {json:async()=>({job_id:'existing-job'})};};
 vm.createContext(sandbox);
 vm.runInContext('var CB_PUBLISH_TIMEOUT_MS=100;'+block('\nfunction cbBusyError(')+block('\nfunction cbPostPublish('),sandbox);
 (async()=>{
  function form(){const fd=new FormData();fd.set('account_label','Sample');fd.set('request_id','same-request');return fd;}
  await assert.rejects(sandbox.cbPostPublish(form()),e=>e.cbBusy===true);
  const result=await sandbox.cbPostPublish(form());
  assert.equal(result.job_id,'existing-job');assert.equal(preparations,1);assert.equal(submissions,2);
  console.log(JSON.stringify({ok:true,preparations,submissions}));
 })().catch(e=>{console.error(e);process.exitCode=1;});
} else {
 const yes=mode==='confirm-read';
 sandbox.window.aclConfirm=async()=>{confirmations++;return yes;};
 sandbox.fetch=async(url,options)=>{
  if(url==='/connection/prepare') {preparations++;const p=JSON.parse(options.body);return {json:async()=>p.confirm_reopen?{ok:true}:{ok:false,code:'open_by_hand'}};}
  submissions++;return {ok:true};
 };
 vm.createContext(sandbox);
 vm.runInContext(block('window.aclPrepareConnection = function','\n};')+block('window.aclConnectionFetch = function','\n};'),sandbox);
 (async()=>{
  if(yes)await sandbox.window.aclConnectionFetch('/account-spend/read',{method:'POST'},'Sample');
  else await assert.rejects(sandbox.window.aclConnectionFetch('/account-spend/read',{method:'POST'},'Sample'));
  assert.equal(confirmations,1);assert.equal(submissions,yes?1:0);assert.equal(preparations,yes?2:1);
  console.log(JSON.stringify({ok:true,preparations,submissions}));
 })().catch(e=>{console.error(e);process.exitCode=1;});
}
