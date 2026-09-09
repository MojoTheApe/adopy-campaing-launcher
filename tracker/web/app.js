(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const root = $('signal-tracker-v2');
  let data = JSON.parse($('tracker-data').textContent);
  const key = 'tracker.design.v2.' + data.project.repository;
  const stages = ['idea', 'backlog', 'todo', 'in_progress', 'review', 'merged'];
  const priorities = ['high', 'medium', 'low', 'unassigned'];
  const types = ['bug', 'feature', 'improvement', 'research', 'operations', 'unassigned'];
  const views = ['all', 'work', 'plan', 'release', 'archive'];
  const defaults = () => ({view:'all', layout:'list', density:'comfortable', theme:'system', module:'', statuses:[], priorities:[], types:[], releases:[], design:false, required:false, blocked:false, search:'', scope:'all', selectedRelease:'', selected:null, column:'in_progress', scroll:0, boardScroll:0, focus:''});
  const read = (storage, name) => {try {return JSON.parse(storage.getItem(name) || '{}') || {};} catch (_) {return {};}};
  let state = defaults(), migrated = false;
  try {
    const saved = read(sessionStorage, key), prefs = read(localStorage, key+'.prefs');
    Object.assign(state, prefs, saved);
    if (!sessionStorage.getItem(key) && !localStorage.getItem(key+'.prefs')) {
      const old = read(sessionStorage, 'tracker.v2.'+data.project.repository);
      if (Object.keys(old).length) {
        Object.assign(state, old, {priorities:old.priority?[old.priority]:[], types:old.type?[old.type]:[], releases:old.release?[String(old.release)]:[], selectedRelease:old.release||'', selected:Number(Object.keys(old.open||{}).find(n=>old.open[n]))||null, theme:old.theme||'system', scope:old.scope||'all'});
        migrated = true;
      }
    }
  } catch (_) {}
  function sanitize() {
    const allowed = {view:views, layout:['list','board'], density:['comfortable','compact'], theme:['light','dark','system'], scope:['all','remaining','blocked','complete']};
    for (const [k, values] of Object.entries(allowed)) if (!values.includes(state[k])) state[k]=defaults()[k];
    for (const [k, values] of Object.entries({statuses:stages, priorities, types, releases:data.milestones.map(m=>String(m.number))})) state[k]=Array.isArray(state[k])?[...new Set(state[k].filter(v=>values.includes(v)))]:[];
    if (!['', 'unclassified', ...data.modules.map(m=>m.id)].includes(state.module)) state.module='';
    state.search=typeof state.search==='string'?state.search:'';
    for (const flag of ['design','required','blocked']) state[flag]=state[flag]===true;
    state.selected=Number.isSafeInteger(Number(state.selected)) && Number(state.selected)>0?Number(state.selected):null;
    if (!data.milestones.some(m=>String(m.number)===state.selectedRelease)) state.selectedRelease=String((data.milestones.find(m=>m.title===data.default_release)||data.milestones[0])?.number||'');
    if (!stages.includes(state.column)) state.column='in_progress';
    state.scroll=Number.isFinite(state.scroll)?Math.max(0,state.scroll):0;
    state.boardScroll=Number.isFinite(state.boardScroll)?Math.max(0,state.boardScroll):0;
  }
  sanitize();
  function save() {
    // Only UI preferences and identifiers; task descriptions remain in the authenticated snapshot.
    const clean = Object.fromEntries(Object.keys(defaults()).map(k=>[k,state[k]]));
    try {sessionStorage.setItem(key,JSON.stringify(clean));localStorage.setItem(key+'.prefs',JSON.stringify({...clean,search:'',selected:null,scroll:0,boardScroll:0}));} catch (_) {}
  }
  const node = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const button = (text, cls, attributes={}) => {const b=node('button',text,cls);b.type='button';for(const [k,v] of Object.entries(attributes))b.setAttribute(k,v);return b;};
  const cap = x => x ? x[0].toUpperCase()+x.slice(1) : 'Unassigned';
  const label = s => ({idea:'Idea',backlog:'Backlog',todo:'Todo',in_progress:'In progress',review:'Review',merged:data.completion_label})[s];
  const viewLabel = v => ({all:'All tasks',work:'Work',plan:'Plan',release:data.release_view_label,archive:'Archive'})[v];
  const moduleName = m => data.modules.find(x=>x.id===m)?.name || 'Unclassified';
  const release = () => data.milestones.find(m=>String(m.number)===state.selectedRelease);
  const completed = t => t.status==='merged' && !t.archive;
  let lookup = new Map();
  function indexTasks() {lookup=new Map(data.tasks.flatMap(t=>[[t.id.toLowerCase(),t],['#'+t.number,t]]));}
  indexTasks();
  const taskByNumber = n => lookup.get('#'+n);
  const pendingDeps = t => t.dependencies.filter(id=>!completed(lookup.get(id.toLowerCase())||{}));
  const blocked = t => t.blocked || pendingDeps(t).length>0;
  const safeLink = t => 'https://github.com/'+data.project.repository+'/issues/'+t.number;
  function inView(t) {
    if(state.view==='archive')return t.archive;
    if(t.archive && !(state.view==='release' && t.required))return false;
    if(state.view==='work')return ['todo','in_progress','review'].includes(t.status);
    if(state.view==='plan')return ['idea','backlog'].includes(t.status);
    if(state.view==='release') {
      if(String(t.milestone)!==state.selectedRelease)return false;
      if(state.scope==='remaining')return t.required && !t.initiative && !completed(t);
      if(state.scope==='blocked')return t.required && !t.initiative && blocked(t);
      if(state.scope==='complete')return completed(t);
    }
    return true;
  }
  function matches(t) {
    if(!inView(t))return false;
    if(state.statuses.length&&!state.statuses.includes(t.status))return false;
    if(state.module&&(t.module||'unclassified')!==state.module)return false;
    if(state.priorities.length&&!state.priorities.includes(t.priority||'unassigned'))return false;
    if(state.types.length&&!state.types.includes(t.type||'unassigned'))return false;
    if(state.releases.length&&!state.releases.includes(String(t.milestone)))return false;
    if(state.design&&!t.design||state.required&&!t.required||state.blocked&&!blocked(t))return false;
    const q=state.search.trim().toLowerCase();
    return !q||[t.id,'#'+t.number,t.title,t.body].join(' ').toLowerCase().includes(q);
  }
  function sortedRows() {
    const rank={in_progress:0,review:1,todo:2,backlog:3,idea:4,merged:5};
    const order=data.queue?.run?.state==='running'?data.queue.run.issues:(data.queue?.todo_order||[]);
    const position=n=>{const i=order.indexOf(n);return i<0?1e9:i;};
    const p={high:0,medium:1,low:2,'':1};
    return data.tasks.filter(matches).sort((a,b)=>rank[a.status]-rank[b.status] || (a.status==='todo'?(position(a.number)-position(b.number)):0) || p[a.priority]-p[b.priority] || (a.status==='todo'?String(a.todo_at||a.created_at).localeCompare(String(b.todo_at||b.created_at)):0) || a.number-b.number);
  }
  function clearFilters() {Object.assign(state,{module:'',statuses:[],priorities:[],types:[],releases:[],design:false,required:false,blocked:false,search:'',scope:'all'});$('search').value='';}
  function chooseView(v) {clearFilters();state.view=v;state.boardScroll=0;closeDialog($('navigation-panel'));save();render();}
  function statusBadge(t) {const s=node('span',undefined,'st-stage');s.dataset.status=t.status;s.append(node('span',undefined,'st-stage-dot'),document.createTextNode(t.non_code&&t.status==='merged'?'Complete':label(t.status)));return s;}
  function copyButton(t) {
    const b=button('', 'st-copy', {id:'copy-'+t.number,'data-copy':t.id,'aria-label':'Copy task ID '+t.id,'title':'Copy task ID'});
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');svg.setAttribute('fill','none');svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','1.6');
    for(const attrs of [{x:8,y:8,width:12,height:12,rx:2},{x:3,y:3,width:12,height:12,rx:2}]){const r=document.createElementNS(svg.namespaceURI,'rect');for(const [k,v] of Object.entries(attrs))r.setAttribute(k,v);svg.prepend(r);}
    b.append(svg);return b;
  }
  const tag = (text, cls='') => node('span',text,'task-tag '+cls);
  function taskCard(t, layout) {
    const row=node(layout==='board'?'article':'div',undefined,layout==='board'?'task st-board-card':'task st-row');row.id='task-'+t.number;row.dataset.issue=t.number;
    const main=node('div',undefined,'st-task-main'), id=node('div',undefined,'st-idline');id.append(node('span',t.id,'st-id'),copyButton(t));
    if(blocked(t))id.append(tag('Blocked','blocked'));
    if(t.design)id.append(tag('Design','design'));
    if(t.warnings.length)id.append(tag('Check metadata','warning'));
    if(t.archive)id.append(tag('Archived','warning'));
    if(t.initiative)id.append(tag('Initiative'));
    const title=button(t.title,'st-task-title',{'data-task':t.number,id:'summary-'+t.number});
    main.append(id,title);
    const mobile=node('div',undefined,'mobile-properties');mobile.append(node('span',moduleName(t.module)));if(t.required)mobile.append(tag((t.milestone_title||'Release')+' required','release-tag'));main.append(mobile);row.append(main);
    const properties=[statusBadge(t),node('span',moduleName(t.module),'st-module'),node('span',cap(t.priority),'st-priority')];properties[2].dataset.priority=t.priority;
    const rel=node('span',undefined,'st-release-cell');if(t.milestone_title)rel.append(tag(t.milestone_title+(t.required?' required':''),'release-tag'));else if(t.required)rel.append(tag('Required · no release','warning'));
    if(layout==='board'){const bottom=node('div',undefined,'st-card-bottom');bottom.append(properties[1],properties[2],rel);row.append(bottom);}else row.append(...properties,rel);
    return row;
  }
  let fresh=false,refreshing=false,lastSynced='',filterSignature='',listLayout='',lastDetail=null,detailSignature='',openerId='';
  const cardCache=new Map();
  function getCard(t) {
    const fingerprint=JSON.stringify([t,data.modules,data.completion_label,blocked(t),state.layout]);
    let entry=cardCache.get(t.number);
    if(!entry||entry.fingerprint!==fingerprint){entry={fingerprint,element:taskCard(t,state.layout)};cardCache.set(t.number,entry);}
    return entry.element;
  }
  function reconcile(container, children) {
    // Reuse unchanged rows; poll updates never blank the working surface first.
    const keep=new Set(children);
    for(const child of [...container.children])if(!keep.has(child))child.remove();
    children.forEach((child,i)=>{if(container.children[i]!==child)container.insertBefore(child,container.children[i]||null);});
  }
  function navButtons(container, mobile=false) {
    const list=views.map(v=>{const b=button(viewLabel(v),'',{'data-view':v,'aria-pressed':String(state.view===v),id:(mobile?'mobile-':'')+'view-'+v});if(v==='all'){const count=node('span',String(data.tasks.filter(t=>!t.archive).length),'st-nav-count');b.append(count);}return b;});
    // Navigation isn't recreated during a data poll unless its labels/counts change.
    const sig=JSON.stringify(list.map(b=>b.outerHTML));if(container.dataset.signature!==sig){container.replaceChildren(...list);container.dataset.signature=sig;}
  }
  function renderNavigation() {
    navButtons($('views'));navButtons($('mobile-views'),true);
    const modules=[{id:'',name:'All modules'},...data.modules];if(data.tasks.some(t=>!t.module))modules.push({id:'unclassified',name:'Unclassified'});
    for(const id of ['modules','mobile-modules']) {
      const sig=JSON.stringify([modules,state.module]);if($(id).dataset.signature===sig)continue;
      $(id).replaceChildren(...modules.map(m=>{const b=button('','',{'data-module':m.id,'aria-pressed':String(state.module===m.id),id:id+'-'+(m.id||'all')});b.append(node('span',undefined,'st-module-dot'),document.createTextNode(m.name));return b;}));$(id).dataset.signature=sig;
    }
    $('view-title').textContent=viewLabel(state.view);$('project-name').textContent=data.project.name;$('project-context').textContent=data.project.name;
    $('github-link').href='https://github.com/'+data.project.repository+'/issues';document.title=data.project.name+' · '+viewLabel(state.view);
    root.dataset.density=state.density;
    document.querySelectorAll('[data-layout]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.layout===state.layout)));
    $('density-toggle').setAttribute('aria-pressed',String(state.density==='compact'));
  }
  function renderOverview() {
    const active=data.tasks.filter(t=>!t.archive),m=release(),s=m?.summary;
    $('total-count').textContent=active.length;$('archive-count').textContent=(data.tasks.length-active.length)+' archived';$('backlog-count').textContent=active.filter(t=>t.status==='backlog').length;
    $('release-metric-label').textContent=(m?.title||data.release_view_label)+' remaining';
    $('mvp-count').textContent=s?.total?String(s.remaining):'—';
    $('mvp-note').textContent=!s?.total?'Scope not defined':`${s.complete} of ${s.total} complete${s.blocked?' · '+s.blocked+' blocked':''}${!fresh?' · last saved data':''}`;
    $('mvp-progress').hidden=!s?.total;$('mvp-progress').max=s?.total||1;$('mvp-progress').value=s?.complete||0;
    const sig=JSON.stringify([active.map(t=>t.status),data.completion_label]);
    if($('pipeline').dataset.signature!==sig){$('pipeline').replaceChildren(...['todo','in_progress','review','merged'].map(status=>{const b=button('','',{'data-metric':status,id:'metric-'+status});const mark=node('span',undefined,'st-stage-dot');mark.dataset.status=status;b.append(mark,document.createTextNode(label(status)),node('b',String(active.filter(t=>t.status===status).length)));return b;}));$('pipeline').dataset.signature=sig;}
    $('release-context').hidden=state.view!=='release';
    const scopeRows=[['all','Entire scope'],['remaining','Remaining required'],['blocked','Blockers'],['complete','Completed']];
    const scopeSig=JSON.stringify([state.scope,state.view]);if($('release-scopes').dataset.signature!==scopeSig){$('release-scopes').replaceChildren(...scopeRows.map(([v,l])=>button(l,'',{'data-scope':v,'aria-pressed':String(state.scope===v),id:'scope-'+v})));$('release-scopes').dataset.signature=scopeSig;}
    const box=$('release-summary');box.classList.toggle('ready',Boolean(s?.ready&&fresh));
    const summary=!s?.total?'Scope not defined':!fresh?'Readiness unknown · sync delayed':s.ready?'Scope ready':'Scope in progress';
    box.replaceChildren(node('strong',(m?.title||data.release_view_label)+' · '+summary));
    if(s?.total)box.append(node('span',` ${s.complete} of ${s.total} required tasks complete · ${s.remaining} remaining · ${s.unresolved.length} unfinished dependencies`));
    else box.append(node('span',' Assign essential tasks to this milestone and mark them Required.'));
  }
  function renderFilters() {
    const signature=JSON.stringify([state.view==='release',data.modules,data.milestones.map(m=>[m.number,m.title]),data.completion_label]);
    if(signature!==filterSignature) {
      const group=(name, options, key, radio=false)=>{const field=node('fieldset'),legend=node('legend',name),items=node('div',undefined,'st-options');field.append(legend,items);options.forEach(([v,l])=>{const lab=node('label'),input=node('input');input.type=radio?'radio':'checkbox';input.name=key;input.value=v;input.dataset.facet=key;input.id='filter-'+key+'-'+v;lab.append(input,document.createTextNode(l));items.append(lab);});return field;};
      $('filter-fields').replaceChildren(group('Status',stages.map(s=>[s,label(s)]),'statuses'),group('Priority',priorities.map(v=>[v,cap(v)]),'priorities'),group('Type',types.map(v=>[v,cap(v)]),'types'),group(state.view==='release'?'Selected release':'Release',data.milestones.map(m=>[String(m.number),m.title]),state.view==='release'?'selectedRelease':'releases',state.view==='release'),group('Focus',[['design','Design'],['required','Required'],['blocked','Blocked']],'flags'));
      filterSignature=signature;
    }
    for(const input of $('filter-fields').querySelectorAll('input')) {const k=input.dataset.facet;input.checked=k==='flags'?state[input.value]:k==='selectedRelease'?state.selectedRelease===input.value:state[k].includes(input.value);}
    const chips=[];
    const chip=(k,v,l)=>{const b=button(l+' ×','st-chip',{'data-remove':k,'data-value':v,'aria-label':'Remove '+l+' filter',id:'chip-'+k+'-'+v});chips.push(b);};
    if(state.module)chip('module',state.module,moduleName(state.module));if(state.search)chip('search','', 'Search: '+state.search);
    for(const v of state.statuses)chip('statuses',v,label(v));for(const v of state.priorities)chip('priorities',v,cap(v));for(const v of state.types)chip('types',v,cap(v));
    for(const v of state.releases)chip('releases',v,data.milestones.find(m=>String(m.number)===v)?.title||v);
    for(const k of ['design','required','blocked'])if(state[k])chip(k,'',cap(k));
    if(chips.length)chips.push(button('Clear filters','st-clear',{'data-clear':'true',id:'clear-chips'}));
    const chipSignature=JSON.stringify(chips.map(c=>c.outerHTML));if($('chips').dataset.signature!==chipSignature){$('chips').replaceChildren(...chips);$('chips').dataset.signature=chipSignature;}
    const count=state.statuses.length+state.priorities.length+state.types.length+state.releases.length+Number(state.design)+Number(state.required)+Number(state.blocked);
    $('filters-toggle').textContent=count?'Filters ('+count+')':'Filters';
  }
  function renderRows(rows) {
    const holder=$('tasks');
    if(!rows.length) {
      const msg=state.view==='work'&&!data.tasks.some(inView)?'No tasks selected for execution':state.view==='archive'&&!data.tasks.some(inView)?'No archived tasks':!data.tasks.length?'No tasks yet':'No tasks match these filters';
      const signature=state.view+msg;
      if(holder.dataset.empty!==signature){const box=node('div',undefined,'st-empty');box.append(node('h2',msg));if(state.view==='work')box.append(node('p','Select tasks from Backlog and move them to Todo.'));box.append(button('Show all tasks','st-control',{'data-view':'all'}));holder.replaceChildren(box);holder.dataset.empty=signature;}
      listLayout='';$('board-navigation').hidden=true;return;
    }
    delete holder.dataset.empty;
    if(listLayout!==state.layout){holder.replaceChildren();cardCache.clear();listLayout=state.layout;}
    $('layout-note').textContent=state.layout==='list'?'Active work first':'Grouped by status · scroll for more columns';
    $('board-navigation').hidden=state.layout==='list';
    if(state.layout==='list') {
      let list=holder.querySelector('.st-list');
      if(!list){list=node('div',undefined,'st-list');const header=node('div',undefined,'st-list-head');['Task','Status','Module','Priority','Release'].forEach((l,i)=>header.append(node('span',l,i===2?'st-module-head':i===3?'st-priority-head':'')));list.append(header);holder.append(list);}
      reconcile(list,[list.firstElementChild,...rows.map(getCard)]);
    } else {
      let cols=state.view==='work'?['todo','in_progress','review']:state.view==='plan'?['idea','backlog']:stages;
      if(state.statuses.length)cols=cols.filter(s=>state.statuses.includes(s));
      const focused=matchMedia('(max-width: 899px)').matches;
      if(!cols.includes(state.column))state.column=cols.find(s=>rows.some(t=>t.status===s))||cols[0];
      $('board-navigation').hidden=!focused;
      if(focused){$('layout-note').textContent='Focus column';const sig=JSON.stringify([cols,state.column,rows.map(t=>t.status)]);if($('board-navigation').dataset.signature!==sig){$('board-navigation').replaceChildren(...cols.map(s=>button(label(s)+' · '+rows.filter(t=>t.status===s).length,'',{'data-column':s,'aria-pressed':String(state.column===s),id:'column-'+s})));$('board-navigation').dataset.signature=sig;}cols=[state.column];}
      let board=holder.querySelector('.st-board');if(!board){board=node('div',undefined,'st-board');holder.append(board);}
      const lanes=cols.map(s=>{let lane=board.querySelector('[data-lane="'+s+'"]');if(!lane){lane=node('section',undefined,'st-lane');lane.dataset.lane=s;lane.setAttribute('aria-label',label(s));const head=node('div',undefined,'st-lane-head');head.append(statusBadge({status:s}),node('b'));lane.append(head,node('div',undefined,'lane-tasks'));}const members=rows.filter(t=>t.status===s);lane.querySelector('b').textContent=members.length;const body=lane.querySelector('.lane-tasks');if(members.length)reconcile(body,members.map(getCard));else{if(!body.querySelector('.st-lane-empty'))body.replaceChildren(node('p','No tasks in this stage','st-lane-empty'));}return lane;});
      reconcile(board,lanes);board.style.setProperty('--st-columns',cols.length);holder.classList.toggle('focused-board',focused);
    }
    holder.classList.toggle('board-scroll',state.layout==='board');
    for(const [n,entry] of cardCache)if(!rows.some(t=>t.number===n)) {if(!entry.element.isConnected)cardCache.delete(n);}
  }
  function detailData(t, outside, missing) {
    const contents=document.createDocumentFragment();contents.append(statusBadge(t),node('h2',t.title));contents.lastChild.id='detail-title';
    if(missing||outside)contents.append(node('p',missing?'Task unavailable · showing last saved details':'Outside current filters','detail-notice'));
    const dl=node('dl');for(const [k,v] of [['Module',moduleName(t.module)],['Priority',cap(t.priority)],['Type',cap(t.type)],['Release',t.milestone_title?(t.milestone_title+(t.required?' · Required':'')):t.required?'Required · no release':'Not assigned']])dl.append(node('dt',k),node('dd',v));contents.append(dl);
    if(t.warnings.length)contents.append(node('p',t.warnings.join(' · '),'detail-notice warning'));
    if(blocked(t))contents.append(node('p',t.blocked?'Blocked · see the task notes below':'Waiting for unfinished dependencies','detail-notice blocked'));
    if(t.dependencies.length){contents.append(node('h3','Dependencies'));const deps=node('div',undefined,'dependency-list');for(const id of t.dependencies){const other=lookup.get(id.toLowerCase());if(other)deps.append(button(id+(completed(other)?' · complete':' · pending'),'st-control',{'data-task':other.number}));else deps.append(node('span',id+' · unresolved','warning'));}contents.append(deps);}
    if(t.checks.length){contents.append(node('h3','Acceptance checklist'));const ul=node('ul',undefined,'checks');for(const c of t.checks){const li=node('li'),cb=node('input');cb.type='checkbox';cb.checked=c.done;cb.disabled=true;cb.setAttribute('aria-label',c.title);li.append(cb,node('span',c.title));ul.append(li);}contents.append(ul);}
    if(t.body){contents.append(node('h3','Description'),node('pre',t.body,'task-body'));}
    const links=[...new Set((t.body.match(/https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/(?:pull\/\d+|commit\/[a-f0-9]{7,40})/gi)||[]))];
    if(links.length){contents.append(node('h3','Pull requests and commits'));for(const url of links){const a=node('a',url);a.href=url;a.target='_blank';a.rel='noopener';a.className='git-reference';contents.append(a);}}
    const foot=node('div',undefined,'detail-footer'),a=node('a','Open in GitHub ↗');a.href=safeLink(t);a.target='_blank';a.rel='noopener';foot.append(a);if(t.updated_at)foot.append(node('span','Updated '+new Date(t.updated_at).toLocaleDateString('en-GB')));contents.append(foot);return contents;
  }
  function updateDetail() {
    const d=$('task-detail');if(!state.selected)return;
    const current=taskByNumber(state.selected),t=current||lastDetail;
    if(!t){state.selected=null;return;}
    lastDetail=t;
    const sig=JSON.stringify([t,!matches(t),!current,data.modules,pendingDeps(t)]);
    if(sig!==detailSignature){const y=$('detail-content').scrollTop;const copy=copyButton(t);copy.id='detail-copy-'+t.number;$('detail-id').replaceChildren(node('span',t.id,'st-id'),copy);$('detail-content').replaceChildren(detailData(t,!matches(t),!current));$('detail-content').scrollTop=y;detailSignature=sig;}
    positionDetail();
  }

  function positionDetail() {
    if(!state.selected)return;const d=$('task-detail');const dock=window.innerWidth>=1680;
    if(d.open&&d.dataset.docked===String(dock))return;
    const focused=document.activeElement,oldScroll=$('detail-content').scrollTop;
    if(d.open)d.close();d.dataset.docked=String(dock);root.classList.toggle('detail-open',dock);
    if(dock)d.show();else d.showModal();
    $('detail-content').scrollTop=oldScroll;if(focused&&d.contains(focused))focused.focus({preventScroll:true});
  }
  function openTask(number, source) {
    const t=taskByNumber(Number(number));if(!t)return;
    openerId=source?.id||'summary-'+number;state.selected=t.number;lastDetail=t;detailSignature='';updateDetail();$('detail-feedback').textContent='';$('detail-close').focus({preventScroll:true});
    history.replaceState(null,'','#task-'+t.number);save();
  }
  function closeDetail() {
    state.selected=null;root.classList.remove('detail-open');$('task-detail').close();detailSignature='';lastDetail=null;
    if(/^#task-\d+$/.test(location.hash))history.replaceState(null,'',location.pathname+location.search);
    const target=$(openerId)||$('search');target.focus({preventScroll:true});save();
  }
  $('task-detail').addEventListener('cancel',e=>{e.preventDefault();closeDetail();});
  function captureAnchor() {
    const visible=[...$('tasks').querySelectorAll('.task')].find(e=>e.getBoundingClientRect().bottom>0);return visible?{id:visible.id,top:visible.getBoundingClientRect().top}:null;
  }
  function render({preserve=false}={}) {
    const focus=document.activeElement,focusId=focus?.id,y=window.scrollY,anchor=preserve?captureAnchor():null,horizontal=$('tasks').scrollLeft;
    renderNavigation();renderOverview();renderFilters();const rows=sortedRows();renderRows(rows);
    $('result-count').textContent=`Showing ${rows.length} of ${data.tasks.filter(inView).length} tasks`;
    const run=data.queue?.run;$('batch-note').hidden=state.view!=='work'||!run;
    if(run)$('batch-note').textContent=`${run.issues.length} tasks in the selected batch${data.queue.active?' · working on #'+data.queue.active.issue:''}`;
    if(state.selected)updateDetail();
    if(focus?.isConnected)focus.focus({preventScroll:true});else if(focusId&&$(focusId))$(focusId).focus({preventScroll:true});
    if(anchor&&$(anchor.id))window.scrollTo(0,window.scrollY+$(anchor.id).getBoundingClientRect().top-anchor.top);else if(preserve)window.scrollTo(0,y);
    $('tasks').scrollLeft=horizontal;
  }
  const themeMedia=matchMedia('(prefers-color-scheme: dark)');
  function theme(animate=false) {
    if(animate)document.documentElement.classList.add('st-theme-transition');
    document.documentElement.dataset.theme=state.theme==='system'?(themeMedia.matches?'dark':'light'):state.theme;
    document.querySelectorAll('button[data-theme]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.theme===state.theme)));
    if(animate)setTimeout(()=>document.documentElement.classList.remove('st-theme-transition'),180);
  }
  themeMedia.addEventListener('change',()=>{if(state.theme==='system')theme(true);});
  function closeDialog(d) {if(!d.open)return;d.close();if(d.id==='filters-panel')$('filters-toggle').setAttribute('aria-expanded','false');}
  function showFilters() {
    const d=$('filters-panel'),b=$('filters-toggle');renderFilters();
    if(window.innerWidth<900){d.classList.add('mobile-sheet');d.style.removeProperty('top');d.style.removeProperty('right');d.style.removeProperty('max-height');d.showModal();}
    else{d.classList.remove('mobile-sheet');const r=b.getBoundingClientRect(),top=Math.max(12,Math.min(r.bottom+8,window.innerHeight-300));d.style.top=top+'px';d.style.maxHeight=(window.innerHeight-top-16)+'px';d.style.right=Math.max(16,window.innerWidth-r.right)+'px';d.show();}
    b.setAttribute('aria-expanded','true');d.querySelector('button').focus();
  }
  $('filters-panel').addEventListener('close',()=>{$('filters-toggle').setAttribute('aria-expanded','false');});
  let toastTimer;
  function copyFeedback(id, ok, source) {
    const message=ok?'Copied '+id:'Copy manually: '+id;
    $('detail-feedback').textContent=message;$('copy-toast').textContent=message;$('copy-toast').hidden=false;
    if(ok&&source.isConnected){const original=source.innerHTML;source.textContent='✓';source.setAttribute('aria-label','Copied '+id);setTimeout(()=>{if(source.isConnected){source.innerHTML=original;source.setAttribute('aria-label','Copy task ID '+id);}},1500);}
    clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('copy-toast').hidden=true;},3000);
  }
  root.addEventListener('click',event=>{
    const b=event.target.closest('button');if(!b)return;
    if(b.dataset.copy){const id=b.dataset.copy;try{navigator.clipboard.writeText(id).then(()=>copyFeedback(id,true,b),()=>copyFeedback(id,false,b));}catch(_){copyFeedback(id,false,b);}return;}
    if(b.dataset.task){openTask(b.dataset.task,b);return;}
    if(b.id==='detail-close'){closeDetail();return;}
    if(b.dataset.close){closeDialog($(b.dataset.close));(b.dataset.close==='navigation-panel'?$('nav-toggle'):$('filters-toggle')).focus();return;}
    if(b.id==='nav-toggle'){$('navigation-panel').showModal();return;}
    if(b.id==='filters-toggle'){if($('filters-panel').open)closeDialog($('filters-panel'));else showFilters();return;}
    if(b.dataset.view){chooseView(b.dataset.view);return;}
    if(Object.hasOwn(b.dataset,'module')){state.module=b.dataset.module;closeDialog($('navigation-panel'));save();render();return;}
    if(b.dataset.layout){state.layout=b.dataset.layout;save();render({preserve:true});return;}
    if(b.dataset.column){state.column=b.dataset.column;save();render({preserve:true});return;}
    if(b.dataset.theme){state.theme=b.dataset.theme;theme(true);save();return;}
    if(b.id==='density-toggle'){state.density=state.density==='compact'?'comfortable':'compact';save();render({preserve:true});return;}
    if(b.dataset.metric){clearFilters();state.view=b.dataset.metric==='release'?'release':'all';if(b.dataset.metric==='release')state.scope='remaining';else if(b.dataset.metric!=='all')state.statuses=[b.dataset.metric];save();render();return;}
    if(b.dataset.scope){state.scope=b.dataset.scope;save();render();return;}
    if(b.dataset.clear||b.id==='reset'){clearFilters();save();render();return;}
    if(b.dataset.remove){const k=b.dataset.remove;if(Array.isArray(state[k]))state[k]=state[k].filter(v=>v!==b.dataset.value);else state[k]=typeof state[k]==='boolean'?false:'';$('search').value=state.search;save();render();}
  });
  $('filter-fields').addEventListener('change',e=>{const input=e.target,k=input.dataset.facet;if(!k)return;if(k==='flags')state[input.value]=input.checked;else if(k==='selectedRelease')state.selectedRelease=input.value;else state[k]=input.checked?[...new Set([...state[k],input.value])]:state[k].filter(v=>v!==input.value);save();render({preserve:true});});
  $('search').addEventListener('input',e=>{state.search=e.target.value;save();render();});
  document.addEventListener('pointerdown',e=>{const d=$('filters-panel');if(d.open&&!d.matches(':modal')&&!d.contains(e.target)&&!$('filters-toggle').contains(e.target))closeDialog(d);});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){if($('task-detail').open){e.preventDefault();closeDetail();}else if($('filters-panel').open){closeDialog($('filters-panel'));$('filters-toggle').focus();}}});
  let resizeTimer,scrollTimer;
  window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if($('filters-panel').open)closeDialog($('filters-panel'));render({preserve:true});},100);});
  window.addEventListener('scroll',()=>{clearTimeout(scrollTimer);scrollTimer=setTimeout(()=>{state.scroll=window.scrollY;save();},150);},{passive:true});
  $('tasks').addEventListener('scroll',()=>{state.boardScroll=$('tasks').scrollLeft;save();},{passive:true});
  function hashTask() {let hash;try{hash=decodeURIComponent(location.hash.slice(1));}catch(_){return null;}return /^task-\d+$/.test(hash)?Number(hash.slice(5)):lookup.get(hash.toLowerCase())?.number;}
  window.addEventListener('hashchange',()=>{const n=hashTask();if(n)openTask(n);else if(state.selected)closeDetail();});
  async function fetchUpdate(url) {
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),15000);
    try{const response=await fetch(url,{cache:'no-store',signal:controller.signal});if(!response.ok)throw Error();return await response.json();}finally{clearTimeout(timeout);}
  }
  function validSnapshot(next,s) {
    const text=x=>typeof x==='string';
    const strings=x=>Array.isArray(x)&&x.every(text);
    const count=x=>Number.isSafeInteger(x)&&x>=0;
    const validTask=t=>t&&Number.isSafeInteger(t.number)&&t.number>0&&['id','title','body','module','priority','type'].every(k=>text(t[k]))&&stages.includes(t.status)&&['archive','required','initiative','blocked','design','non_code'].every(k=>typeof t[k]==='boolean')&&strings(t.warnings)&&strings(t.dependencies)&&Array.isArray(t.checks)&&t.checks.every(c=>c&&text(c.title)&&typeof c.done==='boolean');
    return next&&next.schema_version===2&&next.digest===s.digest&&next.project?.repository===data.project.repository&&text(next.project.name)&&text(next.revision)&&text(next.completion_label)&&text(next.release_view_label)&&Array.isArray(next.modules)&&next.modules.every(m=>m&&text(m.id)&&text(m.name))&&Array.isArray(next.milestones)&&next.milestones.every(m=>m&&count(m.number)&&text(m.title)&&m.summary&&['total','complete','remaining','blocked'].every(k=>count(m.summary[k]))&&Array.isArray(m.summary.unresolved)&&typeof m.summary.ready==='boolean')&&Array.isArray(next.tasks)&&next.tasks.every(validTask)&&new Set(next.tasks.map(t=>t.number)).size===next.tasks.length&&(!data.tasks.length||next.tasks.length||s.empty_confirmed===true);
  }
  async function refresh() {
    if(refreshing)return;refreshing=true;
    try {
      const s=await fetchUpdate('/status.json'),age=Date.now()-Date.parse(s.synced_at);
      fresh=Boolean(s.ok&&Number.isFinite(age)&&age>=-60000&&age<=Math.max(180000,data.refresh_seconds*3000));
      if(s.synced_at)lastSynced=s.synced_at;
      if(!fresh){$('sync-state').textContent=(s.error==='empty_response'?'Empty update ignored':'Sync delayed')+' · last saved '+(lastSynced?new Date(lastSynced).toLocaleTimeString('en-GB'):'data');renderOverview();return;}
      if(s.digest!==data.digest){
        const next=await fetchUpdate('/data.json');if(!validSnapshot(next,s))throw Error();
        if(next.revision!==data.revision){state.scroll=window.scrollY;state.focus=document.activeElement?.id||'';save();location.reload();return;}
        data=next;sanitize();indexTasks();render({preserve:true});
      }
      $('sync-state').textContent='Synced '+new Date(s.synced_at).toLocaleTimeString('en-GB');renderOverview();
    } catch (_) {fresh=false;$('sync-state').textContent='Connection lost · showing last saved data';renderOverview();}
    finally {refreshing=false;}
  }
  theme();$('search').value=state.search;state.selected=hashTask()||state.selected;render();save();
  requestAnimationFrame(()=>{window.scrollTo(0,state.scroll);$('tasks').scrollLeft=state.boardScroll;if(state.selected&&taskByNumber(state.selected))openTask(state.selected);if(state.focus&&$(state.focus))$(state.focus).focus({preventScroll:true});});
  if(migrated)$('copy-toast').textContent='Saved view restored';
  refresh();setInterval(refresh,data.refresh_seconds*1000);
})();
