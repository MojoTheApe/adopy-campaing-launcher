(() => {
  'use strict';
  let data = JSON.parse(document.getElementById('tracker-data').textContent);
  const $ = id => document.getElementById(id);
  const storageKey = 'tracker.v2.' + data.project.repository;
  const defaults = () => ({view:'work', statuses:[], module:'', priority:'', type:'', release:'', design:false, required:false, blocked:false, search:'', scope:'remaining', open:{}, groups:{}, theme:'', scroll:0});
  let state = defaults();
  try { state = {...state, ...JSON.parse(sessionStorage.getItem(storageKey) || '{}')}; } catch (_) {}
  if (!['work','plan','release','all','archive'].includes(state.view)) state.view = 'work';
  if (!Array.isArray(state.statuses)) state.statuses = [];
  if (!state.open || typeof state.open !== 'object') state.open = {};
  if (!state.groups || typeof state.groups !== 'object') state.groups = {};
  let fresh = false, refreshing = false;
  const labels = {idea:'Idea',backlog:'Backlog',todo:'Todo',in_progress:'In progress',review:'Review',merged:data.completion_label};
  const save = () => { try { sessionStorage.setItem(storageKey, JSON.stringify(state)); } catch (_) {} };
  const node = (tag, text, className) => { const n=document.createElement(tag); if(text !== undefined) n.textContent=text; if(className) n.className=className; return n; };
  const cap = x => x ? x[0].toUpperCase()+x.slice(1) : 'Unassigned';
  const badge = (text, cls='') => node('span', text, 'badge '+cls);
  const moduleName = id => data.modules.find(m=>m.id===id)?.name || 'Unclassified';
  function theme() { document.documentElement.dataset.theme = state.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark':'light'); }
  theme();
  $('theme').onclick=()=>{ state.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';theme();save(); };
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{ if(!state.theme)theme(); });
  document.title=data.project.name; $('project-name').textContent=data.project.name;
  $('github-link').href='https://github.com/'+data.project.repository+'/issues';
  $('release-tab').textContent=data.release_view_label;
  function options(id, rows) {
    const first=$(id).options[0].cloneNode(true); $(id).replaceChildren(first);
    rows.forEach(([value,text])=>{const o=node('option',text);o.value=value;$(id).append(o);});
    $(id).value=state[id];
  }
  function filters() {
    options('module', [...data.modules.map(m=>[m.id,m.name]),['unclassified','Unclassified']]);
    options('release',data.milestones.map(m=>[String(m.number),m.title]));
    for(const id of ['search','priority','type']) $(id).value=state[id];
    for(const id of ['design','required','blocked']) $(id).checked=state[id];
    $('release-scope').value=state.scope;
  }
  function clearFilters(){ Object.assign(state,{statuses:[],module:'',priority:'',type:'',release:'',design:false,required:false,blocked:false,search:'',scope:'remaining'});filters(); }
  function chooseRelease(){if(!state.release)state.release=String((data.milestones.find(m=>m.title===data.default_release)||data.milestones[0])?.number||'');$('release').value=state.release;}
  function view(name){state.view=name;if(name==='all'){clearFilters();state.groups={};}if(name==='release')chooseRelease();save();render();}
  document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>view(b.dataset.view));
  $('reset').onclick=()=>{clearFilters();if(state.view==='release')chooseRelease();save();render();};
  for(const id of ['module','priority','type','release','design','required','blocked','search']) {
    $(id).addEventListener(id==='search'?'input':'change',()=>{state[id]=$(id).type==='checkbox'?$(id).checked:$(id).value;if(state.view==='release')chooseRelease();save();render();});
  }
  $('release-scope').onchange=()=>{state.scope=$('release-scope').value;save();render();};
  function inView(t) {
    if(state.view==='archive')return t.archive;
    if(t.archive && !(state.view==='release' && t.required))return false;
    if(state.view==='work')return ['todo','in_progress','review'].includes(t.status);
    if(state.view==='plan')return ['idea','backlog'].includes(t.status);
    if(state.view==='release') {
      if(String(t.milestone)!==state.release)return false;
      if(state.scope==='remaining')return t.required && !t.initiative && (t.status!=='merged'||t.archive);
      if(state.scope==='blocked')return t.required && (t.blocked || pendingDeps(t).length);
      if(state.scope==='complete')return t.status==='merged';
    }
    return true;
  }
  function matches(t, status=true) {
    if(!inView(t))return false;
    if(status && state.statuses.length && !state.statuses.includes(t.status))return false;
    if(state.module && (t.module||'unclassified')!==state.module)return false;
    if(state.priority && (t.priority||'unassigned')!==state.priority)return false;
    if(state.type && t.type!==state.type)return false;
    if(state.release && String(t.milestone)!==state.release)return false;
    for(const id of ['design','required','blocked'])if(state[id]&&!t[id])return false;
    const q=String(state.search).trim().toLowerCase();
    return !q || [t.title,t.id,'#'+t.number,t.body].join(' ').toLowerCase().includes(q);
  }
  function pendingDeps(t) {
    return t.dependencies.filter(dep=>{const other=data.tasks.find(x=>x.id.toLowerCase()===dep.toLowerCase() || '#'+x.number===dep);return !other||other.archive||other.status!=='merged';});
  }
  function renderStatus() {
    $('statuses').replaceChildren();
    const add=(id,label,count)=>{const b=node('button',label+' · '+count);b.dataset.status=id;b.id='status-'+id;b.setAttribute('aria-pressed',String(id==='all'?!state.statuses.length:state.statuses.includes(id)));b.onclick=()=>{state.statuses=id==='all'?[]:state.statuses.includes(id)?state.statuses.filter(x=>x!==id):[...state.statuses,id];save();render();};$('statuses').append(b);};
    const pool=data.tasks.filter(t=>matches(t,false));add('all','All statuses',pool.length);
    Object.entries(labels).forEach(([id,label])=>add(id,label,pool.filter(t=>t.status===id).length));
  }
  function card(t) {
    const d=node('details',undefined,'task');d.id='task-'+t.number;d.dataset.issue=t.number;d.open=Boolean(state.open[t.number]);
    const s=node('summary');s.id='summary-'+t.number;
    s.append(node('span',t.id,'task-id'),node('span',t.title,'task-title'));
    const tags=node('span',undefined,'tags');tags.append(badge(t.non_code && t.status==='merged'?'Complete':labels[t.status],'status s-'+t.status));
    if(t.design)tags.append(badge('Design'));
    if(t.required)tags.append(badge((t.milestone_title||'Release')+' required'));
    if(t.blocked)tags.append(badge('Blocked','blocked'));
    if(t.warnings.length)tags.append(badge('Check metadata','warning'));
    if(t.initiative)tags.append(badge('Initiative'));
    if(t.archive)tags.append(badge('Archived','warning'));
    s.append(tags,node('span',t.priority?cap(t.priority):'—','priority p-'+t.priority),node('span','›','chevron'));d.append(s);
    const detail=node('div',undefined,'task-detail');
    detail.append(node('p',[moduleName(t.module),cap(t.type),t.milestone_title].filter(Boolean).join(' · '),'meta'));
    if(t.warnings.length)detail.append(node('p',t.warnings.join(' · '),'warning'));
    if(t.dependencies.length){const dep=node('p','Dependencies: ');t.dependencies.forEach((id,index)=>{if(index)dep.append(node('span',', '));const other=data.tasks.find(x=>x.id.toLowerCase()===id.toLowerCase()||'#'+x.number===id);const link=node('a',id+(other?.status==='merged'?' ✓':''));link.href=other?other.url:t.url;link.target='_blank';link.rel='noopener';dep.append(link);});detail.append(dep);}
    if(t.checks.length){const ul=node('ul',undefined,'checks');t.checks.forEach(c=>{const li=node('li'),cb=node('input');cb.type='checkbox';cb.checked=c.done;cb.disabled=true;cb.setAttribute('aria-label',c.title);li.append(cb,document.createTextNode(c.title));ul.append(li);});detail.append(ul);}
    if(t.body)detail.append(node('pre',t.body));
    const foot=node('div',undefined,'detail-footer'),a=node('a','Open in GitHub ↗');a.href=t.url;a.target='_blank';a.rel='noopener';foot.append(a,node('span',t.updated_at?'Updated '+new Date(t.updated_at).toLocaleDateString('en-GB'):''));detail.append(foot);d.append(detail);
    d.addEventListener('toggle',()=>{state.open[t.number]=d.open;save();});return d;
  }
  function group(key,title,rows) {
    if(!rows.length)return;
    const d=node('details',undefined,'group');d.open=state.groups[key]!==false;
    const s=node('summary');s.id='group-'+key;s.append(node('h3',title),node('span',String(rows.length)));d.append(s,...rows.map(card));
    d.addEventListener('toggle',()=>{state.groups[key]=d.open;save();});$('tasks').append(d);
  }
  function releaseSummary() {
    const box=$('release-summary');box.hidden=state.view!=='release';box.classList.remove('ready');box.replaceChildren();if(box.hidden)return;
    const m=data.milestones.find(m=>String(m.number)===state.release),s=m?.summary;
    const title=!s?.total?'No scope defined':!fresh?'Readiness unknown · sync delayed':s.ready?'Scope ready':'Scope in progress';
    box.append(node('strong',(m?.title||data.release_view_label)+' · '+title));
    box.append(node('p',s?.total?`${s.complete} of ${s.total} required tasks complete · ${s.remaining} remaining · ${s.blocked} blocked · ${s.unresolved.length} unfinished dependencies`:'Assign a release milestone and mark its essential tasks as Required to define the scope.'));
    if(s?.ready&&fresh)box.classList.add('ready');
  }
  function render() {
    const focused=document.activeElement?.id,scroll=window.scrollY;
    const active=data.tasks.filter(t=>!t.archive);
    $('overview').replaceChildren();
    [['In progress',active.filter(t=>t.status==='in_progress').length],['Ready in Todo',active.filter(t=>t.status==='todo'&&!t.blocked&&!pendingDeps(t).length).length],['Review',active.filter(t=>t.status==='review').length],[data.completion_label,active.filter(t=>t.status==='merged').length]].forEach(([label,value])=>{const m=node('div',undefined,'metric');m.append(node('strong',String(value)),node('span',label));$('overview').append(m);});
    document.querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.view===state.view)));
    $('release-options').hidden=state.view!=='release';renderStatus();releaseSummary();
    const titles={work:['Current work','Active work, review, and the next selected tasks.'],plan:['Plan the next step','Develop ideas, then select a manageable batch from Backlog.'],release:[data.release_view_label+' scope','The tasks that matter for the selected release.'],all:['All tasks','Every active task, organized by module.'],archive:['Archive','Cancelled and duplicate tasks are kept here for reference.']};
    [$('view-title').textContent,$('view-description').textContent]=titles[state.view];
    const rows=data.tasks.filter(t=>matches(t));$('result-count').textContent=rows.length+' tasks';$('tasks').replaceChildren();
    const run=data.queue?.run; $('batch-note').hidden=state.view!=='work'||!run;
    if(run)$('batch-note').textContent='Batch '+run.id.slice(0,8)+' · '+run.issues.length+' selected tasks · '+run.state+(data.queue.active?' · working on #'+data.queue.active.issue:'');
    const order=data.queue?.run?.state==='running'?data.queue.run.issues:(data.queue?.todo_order||[]);
    const priority={high:0,medium:1,low:2,'':1};rows.sort((a,b)=>{
      const ai=order.indexOf(a.number),bi=order.indexOf(b.number);
      return ((ai<0?1e6:ai)-(bi<0?1e6:bi)) || priority[a.priority]-priority[b.priority] || String(a.todo_at||a.created_at).localeCompare(String(b.todo_at||b.created_at)) || a.number-b.number;
    });
    if(state.view==='work'){for(const status of ['in_progress','review','todo'])group(status,labels[status],rows.filter(t=>t.status===status));}
    else {for(const m of [...data.modules,{id:'',name:'Unclassified'}])group(m.id||'unclassified',m.name,rows.filter(t=>t.module===m.id));}
    if(!rows.length){const box=node('div',undefined,'empty');box.append(node('h3','No tasks in this view'),node('p',state.view==='work'?'Choose tasks from Backlog and move them to Todo to form your next batch. You can see the full project in All.':'Try clearing filters or choose All to see tasks across every status.'));const b=node('button','Show all tasks');b.onclick=()=>view('all');box.append(b);$('tasks').append(box);}
    if(focused&&$(focused))$(focused).focus({preventScroll:true});window.scrollTo(0,scroll);
  }
  async function refresh() {
    if(refreshing)return;refreshing=true;
    try{
      const response=await fetch('/status.json',{cache:'no-store'});if(!response.ok)throw Error();
      const s=await response.json(),age=Date.now()-Date.parse(s.synced_at);
      fresh=Boolean(s.ok&&Number.isFinite(age)&&age>=-60000&&age<=Math.max(180000,data.refresh_seconds*3000));
      if(!fresh){$('sync-state').textContent=s.error==='empty_response'?'Empty update ignored · showing the last successful update':'Sync delayed · showing the last successful update';releaseSummary();return;}
      if(s.digest!==data.digest){
        const r=await fetch('/data.json',{cache:'no-store'});if(!r.ok)throw Error();const next=await r.json();
        if(next.schema_version!==2||next.digest!==s.digest||!Array.isArray(next.tasks)||next.project.repository!==data.project.repository||(!next.tasks.length&&data.tasks.length))throw Error();
        if(next.revision!==data.revision){state.scroll=window.scrollY;state.focus=document.activeElement?.id;save();location.reload();return;}
        data=next;filters();render();
      }
      $('sync-state').textContent='Synced '+new Date(s.synced_at).toLocaleTimeString('en-GB')+' · '+s.issues+' tasks';releaseSummary();
    }catch(_){fresh=false;$('sync-state').textContent='Connection lost · showing the last successful update';releaseSummary();}
    finally{refreshing=false;}
  }
  filters();if(state.view==='release')chooseRelease();render();
  requestAnimationFrame(()=>{window.scrollTo(0,state.scroll||0);if(state.focus&&$(state.focus))$(state.focus).focus({preventScroll:true});});
  let scrollTimer;window.addEventListener('scroll',()=>{clearTimeout(scrollTimer);scrollTimer=setTimeout(()=>{state.scroll=window.scrollY;save();},200);},{passive:true});
  refresh();setInterval(refresh,data.refresh_seconds*1000);
})();
