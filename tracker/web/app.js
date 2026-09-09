(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const root = $('signal-tracker-v2');
  let data = JSON.parse($('tracker-data').textContent);
  const key = 'tracker.design.v2.' + data.project.repository;
  const stages = ['idea', 'backlog', 'todo', 'in_progress', 'review', 'merged'];
  const priorities = ['high', 'medium', 'low', 'unassigned'];
  const types = ['bug', 'feature', 'improvement', 'research', 'operations', 'unassigned'];
  const views = ['all', ...stages, 'release', 'archive'];
  const defaults = () => ({view:'all', layout:'list', theme:'system', detailWidth:440, filtersOpen:false, module:'', priorities:[], types:[], design:false, required:false, blocked:false, search:'', scope:'all', selected:null, column:'in_progress', scroll:0, boardScroll:0, focus:''});
  const read = (storage, name) => {try {return JSON.parse(storage.getItem(name) || '{}') || {};} catch (_) {return {};}};
  let state = defaults(), migrated = false;
  try {
    const saved = read(sessionStorage, key), prefs = read(localStorage, key+'.prefs');
    Object.assign(state, prefs, saved);
    if (!sessionStorage.getItem(key) && !localStorage.getItem(key+'.prefs')) {
      const old = read(sessionStorage, 'tracker.v2.'+data.project.repository);
      if (Object.keys(old).length) {
        Object.assign(state, old, {priorities:old.priority?[old.priority]:[], types:old.type?[old.type]:[], selected:Number(Object.keys(old.open||{}).find(n=>old.open[n]))||null, theme:old.theme||'system', scope:old.scope||'all'});
        migrated = true;
      }
    }
  } catch (_) {}
  function sanitize() {
    const allowed = {view:views, layout:['list','board'], theme:['light','dark','system'], scope:['all','required','remaining','blocked','complete']};
    for (const [k, values] of Object.entries(allowed)) if (!values.includes(state[k])) state[k]=defaults()[k];
    // Stage and release scope live in navigation; discard retired hidden filters.
    delete state.statuses;delete state.releases;delete state.selectedRelease;
    for (const [k, values] of Object.entries({priorities, types})) state[k]=Array.isArray(state[k])?[...new Set(state[k].filter(v=>values.includes(v)))]:[];
    if (!['', 'unclassified', ...data.modules.map(m=>m.id)].includes(state.module)) state.module='';
    state.search=typeof state.search==='string'?state.search:'';
    for (const flag of ['design','required','blocked','filtersOpen']) state[flag]=state[flag]===true;
    state.selected=Number.isSafeInteger(Number(state.selected)) && Number(state.selected)>0?Number(state.selected):null;
    if (!stages.includes(state.column)) state.column='in_progress';
    state.detailWidth=Number.isFinite(state.detailWidth)?Math.min(960,Math.max(360,state.detailWidth)):440;
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
  const typeLabel = x => !x || x==='unassigned' ? 'Not classified' : cap(x);
  const label = s => ({idea:'Idea',backlog:'Backlog',todo:'To do',in_progress:'In progress',review:'Review',merged:data.completion_label})[s];
  const viewLabel = v => ({all:'All tasks',release:data.release_view_label,archive:'Archive'})[v] || label(v);
  const moduleName = m => data.modules.find(x=>x.id===m)?.name || 'Unclassified';
  const release = () => data.milestones.find(m=>m.title===data.default_release)||data.milestones[0];
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
    if(stages.includes(state.view))return t.status===state.view;
    if(state.view==='release') {
      if(t.milestone!==release()?.number)return false;
      if(state.scope!=='all'&&(!t.required||t.initiative))return false;
      if(state.scope==='remaining')return t.required && !t.initiative && !completed(t);
      if(state.scope==='blocked')return t.required && !t.initiative && blocked(t);
      if(state.scope==='complete')return completed(t);
    }
    return true;
  }
  function matches(t) {
    if(!inView(t))return false;
    if(state.module&&(t.module||'unclassified')!==state.module)return false;
    if(state.priorities.length&&!state.priorities.includes(t.priority||'unassigned'))return false;
    if(state.types.length&&!state.types.includes(t.type||'unassigned'))return false;
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
  function clearFilters() {Object.assign(state,{module:'',priorities:[],types:[],design:false,required:false,blocked:false,search:'',scope:'all'});$('search').value='';}
  function chooseView(v) {clearFilters();state.view=v;state.scope=v==='release'?'required':'all';state.boardScroll=0;closeDialog($('navigation-panel'));save();render();}
  function statusBadge(t) {const s=node('span',undefined,'st-stage');s.dataset.status=t.status;s.append(node('span',undefined,'st-stage-dot'),document.createTextNode(t.non_code&&t.status==='merged'?'Complete':label(t.status)));return s;}
  function copyButton(t) {
    const b=button('', 'st-copy', {id:'copy-'+t.number,'data-copy':t.id,'aria-label':'Copy task ID '+t.id,'title':'Copy task ID'});
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');svg.setAttribute('fill','none');svg.setAttribute('stroke','currentColor');svg.setAttribute('stroke-width','1.6');
    for(const attrs of [{x:8,y:8,width:12,height:12,rx:2},{x:3,y:3,width:12,height:12,rx:2}]){const r=document.createElementNS(svg.namespaceURI,'rect');for(const [k,v] of Object.entries(attrs))r.setAttribute(k,v);svg.prepend(r);}
    b.append(node('span',t.id,'st-id'),svg);return b;
  }
  const tag = (text, cls='') => node('span',text,'task-tag '+cls);
  const focusValues = t => [[t.design,'Design','design'],[t.required,'Required','release-tag'],[blocked(t),'Blocked','blocked']].filter(([active])=>active);
  function taskCard(t, layout) {
    const row=node(layout==='board'?'article':'div',undefined,layout==='board'?'task st-board-card':'task st-row');row.id='task-'+t.number;row.dataset.issue=t.number;
    if(layout==='list')row.setAttribute('role','row');
    const main=node('div',undefined,'st-task-main'), id=node('div',undefined,'st-idline');id.append(copyButton(t));
    if(t.warnings.length)id.append(tag('Check metadata','warning'));
    if(t.archive)id.append(tag('Archived','warning'));
    if(t.initiative)id.append(tag('Initiative'));
    const title=button(t.title,'st-task-title',{'data-task':t.number,id:'summary-'+t.number});
    main.append(id,title);row.append(main);
    const property=(name,cls,content)=>{const cell=node('div',undefined,'st-cell '+cls);cell.dataset.label=name;cell.append(content);if(layout==='list')cell.setAttribute('role','cell');return cell;};
    const priority=node('span',cap(t.priority),'st-priority');priority.dataset.priority=t.priority;
    const rel=t.milestone_title?tag(t.milestone_title,'release-tag'):t.required?tag('No release','warning'):node('span','—','st-unset');
    const focus=node('div',undefined,'st-focus-values');focus.append(...focusValues(t).map(([,name,cls])=>tag(name,cls)));if(!focus.childNodes.length)focus.append(node('span','—','st-unset'));
    const cells=[property('Status','st-status-cell',statusBadge(t)),property('Module','st-module-cell',node('span',moduleName(t.module),'st-module')),property('Priority','st-priority-cell',priority),property('Release','st-release-cell',rel),property('Type','st-type-cell',node('span',typeLabel(t.type),'st-type')),property('Focus','st-focus-cell',focus)];
    if(layout==='board'){const bottom=node('div',undefined,'st-card-bottom');bottom.append(...cells.slice(1));row.append(bottom);}else{main.setAttribute('role','cell');row.append(...cells);}
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
  function renderNavigation() {
    const modules=[{id:'',name:'All modules'},...data.modules];if(data.tasks.some(t=>!t.module))modules.push({id:'unclassified',name:'Unclassified'});
    const section=data.tasks.filter(inView),counts=modules.map(m=>section.filter(t=>!m.id||(t.module||'unclassified')===m.id).length);
    for(const id of ['modules','mobile-modules']) {
      const sig=JSON.stringify([modules,state.module,counts]);if($(id).dataset.signature===sig)continue;
      $(id).replaceChildren(...modules.map((m,i)=>{const b=button('','',{'data-module':m.id,'aria-pressed':String(state.module===m.id),id:id+'-'+(m.id||'all')});b.append(node('span',undefined,'st-module-dot'),node('span',m.name,'st-module-name'),node('span',String(counts[i]),'st-nav-count'));return b;}));$(id).dataset.signature=sig;
    }
    const title=state.module?moduleName(state.module):viewLabel(state.view);
    $('view-title').textContent=title;$('project-name').textContent=data.project.name;$('project-context').textContent=data.project.name;
    $('github-link').href='https://github.com/'+data.project.repository+'/issues';document.title=data.project.name+' · '+title;
    document.querySelectorAll('[data-layout]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.layout===state.layout)));
  }
  function renderOverview() {
    const active=data.tasks.filter(t=>!t.archive),m=release(),s=m?.summary;
    $('release-metric-label').textContent=m?.title||data.release_view_label;
    const percent=s?.total?(s.complete===s.total?100:Math.floor(s.complete/s.total*100)):null;
    $('mvp-count').textContent=percent===null?'—':percent+'%';
    $('mvp-note').textContent=percent===null?'Scope not defined':`${s.complete} / ${s.total} required tasks${!fresh?' · saved snapshot':''}`;
    $('mvp-metric').setAttribute('aria-pressed',String(state.view==='release'));
    $('mvp-metric').classList.toggle('is-stale',!fresh);
    $('mvp-progress').hidden=percent===null;$('mvp-progress').max=s?.total||1;$('mvp-progress').value=s?.complete||0;
    const counts={all:active.length,archive:data.tasks.length-active.length,...Object.fromEntries(stages.map(s=>[s,active.filter(t=>t.status===s).length]))};
    const sig=JSON.stringify([counts,data.completion_label,state.view]);
    if($('pipeline').dataset.signature!==sig){$('pipeline').replaceChildren(...['all',...stages,'archive'].map(v=>{
      const b=button('','st-signal-border',{'data-view':v,'aria-pressed':String(state.view===v),id:'view-'+v});
      if(stages.includes(v)){const dot=node('span',undefined,'st-stage-dot');dot.dataset.status=v;b.append(dot);}
      const name=v==='all'?'All':v==='merged'?(data.completion_label==='In master'?'Master':data.completion_label):viewLabel(v);
      b.append(node('span',name),node('b',String(counts[v])));return b;
    }));$('pipeline').dataset.signature=sig;}
    $('release-context').hidden=state.view!=='release';
    const scopeRows=[['required','Required tasks'],['remaining','Remaining'],['blocked','Blockers'],['complete','Completed'],['all','Entire milestone']];
    const scopeSig=JSON.stringify([state.scope,state.view]);if($('release-scopes').dataset.signature!==scopeSig){$('release-scopes').replaceChildren(...scopeRows.map(([v,l])=>button(l,'',{'data-scope':v,'aria-pressed':String(state.scope===v),id:'scope-'+v})));$('release-scopes').dataset.signature=scopeSig;}
    const box=$('release-summary');box.classList.toggle('ready',Boolean(s?.ready&&fresh));
    const summary=!s?.total?'Scope not defined':!fresh?'Readiness unknown · sync delayed':s.ready?'Scope ready':'Scope in progress';
    box.replaceChildren(node('strong',(m?.title||data.release_view_label)+' · '+summary));
    if(s?.total)box.append(node('span',` ${s.complete} of ${s.total} required tasks complete · ${s.remaining} remaining · ${s.unresolved.length} unfinished dependencies`));
    else box.append(node('span',' Assign essential tasks to this milestone and mark them Required.'));
  }
  function renderFilters() {
    $('filters-panel').hidden=!state.filtersOpen;$('filters-toggle').setAttribute('aria-expanded',String(state.filtersOpen));
    const hasUnclassified=data.tasks.some(t=>!t.type)||state.types.includes('unassigned');
    const signature=JSON.stringify([hasUnclassified]);
    if(signature!==filterSignature) {
      const group=(name, options, key)=>{const field=node('fieldset'),legend=node('legend',name),items=node('div',undefined,'st-options');field.append(legend,items);options.forEach(([v,l])=>{const lab=node('label'),input=node('input');input.type='checkbox';input.name=key;input.value=v;input.dataset.facet=key;input.id='filter-'+key+'-'+v;lab.append(input,document.createTextNode(l));items.append(lab);});return field;};
      $('filter-fields').replaceChildren(group('Priority',priorities.map(v=>[v,cap(v)]),'priorities'),group('Type',types.filter(v=>v!=='unassigned'||hasUnclassified).map(v=>[v,typeLabel(v)]),'types'),group('Focus',[['design','Design'],['required','Required'],['blocked','Blocked']],'flags'));
      filterSignature=signature;
    }
    for(const input of $('filter-fields').querySelectorAll('input')) {const k=input.dataset.facet;input.checked=k==='flags'?state[input.value]:state[k].includes(input.value);}
    const chips=[];
    const chip=(k,v,l)=>{const b=button(l+' ×','st-chip',{'data-remove':k,'data-value':v,'aria-label':'Remove '+l+' filter',id:'chip-'+k+'-'+v});chips.push(b);};
    if(state.module)chip('module',state.module,moduleName(state.module));if(state.search)chip('search','', 'Search: '+state.search);
    for(const v of state.priorities)chip('priorities',v,cap(v));for(const v of state.types)chip('types',v,typeLabel(v));
    for(const k of ['design','required','blocked'])if(state[k])chip(k,'',cap(k));
    if(chips.length)chips.push(button('Clear filters','st-clear',{'data-clear':'true',id:'clear-chips'}));
    const chipSignature=JSON.stringify(chips.map(c=>c.outerHTML));if($('chips').dataset.signature!==chipSignature){$('chips').replaceChildren(...chips);$('chips').dataset.signature=chipSignature;}
    const count=state.priorities.length+state.types.length+Number(state.design)+Number(state.required)+Number(state.blocked);
    $('filters-count').textContent=String(count);$('filters-count').hidden=!count;
    $('filters-toggle').dataset.filtered=String(count>0);
    const filterLabel='Filters'+(count?' · '+count+' active':'');
    $('filters-toggle').setAttribute('aria-label',filterLabel);$('filters-toggle').title=filterLabel;
  }
  function renderRows(rows) {
    const holder=$('tasks');
    if(!rows.length) {
      const msg=state.view==='archive'&&!data.tasks.some(inView)?'No archived tasks':!data.tasks.length?'No tasks yet':'No tasks match these filters';
      const signature=state.view+msg;
      if(holder.dataset.empty!==signature){const box=node('div',undefined,'st-empty');box.append(node('h2',msg));box.append(button('Show all tasks','st-control',{'data-view':'all'}));holder.replaceChildren(box);holder.dataset.empty=signature;}
      listLayout='';$('board-navigation').hidden=true;return;
    }
    delete holder.dataset.empty;
    if(listLayout!==state.layout){holder.replaceChildren();cardCache.clear();listLayout=state.layout;}
    $('layout-note').textContent=state.layout==='list'?'Active work first':'Grouped by status · scroll for more columns';
    $('board-navigation').hidden=state.layout==='list';
    if(state.layout==='list') {
      let list=holder.querySelector('.st-list');
      if(!list){list=node('div',undefined,'st-list');list.setAttribute('role','table');list.setAttribute('aria-label','Tasks');const header=node('div',undefined,'st-list-head');header.setAttribute('role','row');['Task','Status','Module','Priority','Release','Type','Focus'].forEach(l=>{const cell=node('span',l);cell.setAttribute('role','columnheader');header.append(cell);});list.append(header);holder.append(list);}
      reconcile(list,[list.firstElementChild,...rows.map(getCard)]);
    } else {
      let cols=stages.includes(state.view)?[state.view]:stages;
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
    const dl=node('dl');for(const [k,v] of [['Module',moduleName(t.module)],['Priority',cap(t.priority)],['Type',typeLabel(t.type)],['Release',t.milestone_title||'Not assigned'],['Focus',focusValues(t).map(([,name])=>name).join(' · ')||'—']])dl.append(node('dt',k),node('dd',v));contents.append(dl);
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
    if(sig!==detailSignature){const y=$('detail-content').scrollTop;const copy=copyButton(t);copy.id='detail-copy-'+t.number;$('detail-id').replaceChildren(copy);$('detail-content').replaceChildren(detailData(t,!matches(t),!current));$('detail-content').scrollTop=y;detailSignature=sig;}
    positionDetail();
  }

  function positionDetail() {
    if(!state.selected)return;const d=$('task-detail');const dock=window.innerWidth>=1680;
    applyDetailWidth();
    if(d.open&&d.dataset.docked===String(dock))return;
    const focused=document.activeElement,oldScroll=$('detail-content').scrollTop;
    if(d.open)d.close();d.dataset.docked=String(dock);root.classList.toggle('detail-open',dock);
    if(dock)d.show();else d.showModal();
    $('detail-content').scrollTop=oldScroll;if(focused&&d.contains(focused))focused.focus({preventScroll:true});
  }
  function openTask(number, source) {
    const t=taskByNumber(Number(number));if(!t)return;
    const switching=$('task-detail').open&&state.selected!==t.number;
    openerId=source?.id||'summary-'+number;state.selected=t.number;lastDetail=t;detailSignature='';updateDetail();$('detail-feedback').textContent='';$('detail-close').focus({preventScroll:true});
    if(switching&&!matchMedia('(prefers-reduced-motion: reduce)').matches)$('detail-content').animate([{opacity:0,transform:'translateY(6px)'},{opacity:1,transform:'translateY(0)'}],{duration:220,easing:'ease-out'});
    history.replaceState(null,'','#task-'+t.number);save();
  }
  function closeDetail(restoreFocus=true) {
    finishResize(true);
    state.selected=null;root.classList.remove('detail-open');$('task-detail').close();detailSignature='';lastDetail=null;
    if(/^#task-\d+$/.test(location.hash))history.replaceState(null,'',location.pathname+location.search);
    if(restoreFocus){const target=$(openerId)||$('search');target.focus({preventScroll:true});}save();
  }
  $('task-detail').addEventListener('cancel',e=>{e.preventDefault();closeDetail();});
  function captureAnchor() {
    // A task below visible controls is not the user's current reading position.
    if($('tasks').getBoundingClientRect().top>=0)return null;
    const visible=[...$('tasks').querySelectorAll('.task')].find(e=>e.getBoundingClientRect().bottom>0);return visible?{id:visible.id,top:visible.getBoundingClientRect().top}:null;
  }
  function render({preserve='viewport'}={}) {
    const focus=document.activeElement,focusId=focus?.id,y=window.scrollY,anchor=preserve===true?captureAnchor():null,horizontal=$('tasks').scrollLeft;
    renderNavigation();renderOverview();renderFilters();const rows=sortedRows();renderRows(rows);
    $('result-count').textContent=`Showing ${rows.length} of ${data.tasks.filter(inView).length} tasks`;
    const run=data.queue?.run;$('batch-note').hidden=!['todo','in_progress','review'].includes(state.view)||!run;
    if(run)$('batch-note').textContent=`${run.issues.length} tasks in the selected batch${data.queue.active?' · working on #'+data.queue.active.issue:''}`;
    if(state.selected)updateDetail();
    if(focus?.isConnected)focus.focus({preventScroll:true});else if(focusId&&$(focusId))$(focusId).focus({preventScroll:true});
    if(anchor&&$(anchor.id))window.scrollTo(0,window.scrollY+$(anchor.id).getBoundingClientRect().top-anchor.top);else if(preserve)window.scrollTo(0,y);
    $('tasks').scrollLeft=horizontal;
  }
  const themeMedia=matchMedia('(prefers-color-scheme: dark)');
  let themeTimer;
  function theme(animate=false) {
    if(animate)document.documentElement.classList.add('st-theme-transition');
    document.documentElement.dataset.theme=state.theme==='system'?(themeMedia.matches?'dark':'light'):state.theme;
    document.querySelectorAll('button[data-theme]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.theme===state.theme)));
    if(animate){clearTimeout(themeTimer);themeTimer=setTimeout(()=>document.documentElement.classList.remove('st-theme-transition'),420);}
  }
  themeMedia.addEventListener('change',()=>{if(state.theme==='system')theme(true);});
  function closeDialog(d) {if(d.open)d.close();}
  function showFilters(open=!state.filtersOpen) {state.filtersOpen=open;renderFilters();save();}
  let toastTimer;const copyTimers=new WeakMap();
  function copyFeedback(id, ok, source) {
    const message=ok?'Copied '+id:'Copy manually: '+id;
    $('detail-feedback').textContent=message;$('copy-toast').textContent=message;$('copy-toast').hidden=false;
    if(ok&&source.isConnected){source.dataset.copied='true';clearTimeout(copyTimers.get(source));copyTimers.set(source,setTimeout(()=>delete source.dataset.copied,1500));}
    clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('copy-toast').hidden=true;},3000);
  }
  root.addEventListener('click',event=>{
    const b=event.target.closest('button');
    if(!b){
      const row=event.target.closest('.task[data-issue]'),selection=window.getSelection();
      if(row&&!event.target.closest('a,input,select,textarea,summary,[role=button],[role=link],[contenteditable=true],[data-no-open]')&&!(selection&&!selection.isCollapsed&&row.contains(selection.anchorNode))){event.stopPropagation();openTask(row.dataset.issue,$('summary-'+row.dataset.issue));}
      return;
    }
    if(b.dataset.copy){const id=b.dataset.copy;try{navigator.clipboard.writeText(id).then(()=>copyFeedback(id,true,b),()=>copyFeedback(id,false,b));}catch(_){copyFeedback(id,false,b);}return;}
    if(b.dataset.task){event.stopPropagation();openTask(b.dataset.task,b);return;}
    if(b.id==='detail-close'){closeDetail();return;}
    if(b.dataset.close==='filters-panel'){showFilters(false);$('filters-toggle').focus();return;}
    if(b.dataset.close){closeDialog($(b.dataset.close));(b.dataset.close==='navigation-panel'?$('nav-toggle'):$('filters-toggle')).focus();return;}
    if(b.id==='nav-toggle'){$('navigation-panel').showModal();return;}
    if(b.id==='filters-toggle'){showFilters();return;}
    if(b.dataset.view){chooseView(b.dataset.view);return;}
    if(Object.hasOwn(b.dataset,'module')){state.module=b.dataset.module;closeDialog($('navigation-panel'));save();render();return;}
    if(b.dataset.layout){state.layout=b.dataset.layout;save();render({preserve:true});return;}
    if(b.dataset.column){state.column=b.dataset.column;save();render({preserve:true});return;}
    if(b.dataset.theme){state.theme=b.dataset.theme;theme(true);save();return;}
    if(b.dataset.scope){state.scope=b.dataset.scope;save();render();return;}
    if(b.dataset.clear||b.id==='reset'){clearFilters();save();render();return;}
    if(b.dataset.remove){const k=b.dataset.remove;if(Array.isArray(state[k]))state[k]=state[k].filter(v=>v!==b.dataset.value);else state[k]=typeof state[k]==='boolean'?false:'';$('search').value=state.search;save();render();}
  });
  $('filter-fields').addEventListener('change',e=>{const input=e.target,k=input.dataset.facet;if(!k)return;if(k==='flags')state[input.value]=input.checked;else state[k]=input.checked?[...new Set([...state[k],input.value])]:state[k].filter(v=>v!==input.value);save();render();});
  $('search').addEventListener('input',e=>{state.search=e.target.value;save();render();});
  // A backdrop/outside click closes details; a drag that began inside never does.
  const outsideDetail=e=>{const d=$('task-detail'),r=d.getBoundingClientRect();return !d.contains(e.target)||e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom;};
  let outsidePress=null;
  document.addEventListener('pointerdown',e=>{outsidePress={outside:$('task-detail').open&&outsideDetail(e),x:e.clientX,y:e.clientY};});
  document.addEventListener('click',e=>{
    if(!outsidePress?.outside||!$('task-detail').open||!outsideDetail(e)||Math.hypot(e.clientX-outsidePress.x,e.clientY-outsidePress.y)>5)return;
    const focus=e.target.closest('button,input,select,textarea,a[href],[tabindex]');
    closeDetail(!focus);if(focus?.isConnected)focus.focus({preventScroll:true});outsidePress=null;
  });
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){if($('task-detail').open){e.preventDefault();closeDetail();}else if(state.filtersOpen){e.preventDefault();showFilters(false);$('filters-toggle').focus();}}});

  let resizing=null;
  function detailLimits() {
    if(innerWidth<600)return {min:innerWidth,max:innerWidth};
    return {min:360,max:Math.max(360,Math.min(960,innerWidth-(innerWidth>=1680?824:32)))};
  }
  function applyDetailWidth() {
    const {min,max}=detailLimits(),width=Math.min(max,Math.max(min,state.detailWidth)),handle=$('detail-resize');
    root.style.setProperty('--detail-width',width+'px');
    handle.setAttribute('aria-valuemin',min);handle.setAttribute('aria-valuemax',max);handle.setAttribute('aria-valuenow',Math.round(width));handle.setAttribute('aria-valuetext',Math.round(width)+' pixels');
    return width;
  }
  function finishResize(cancel=false) {
    if(!resizing)return;
    const previous=resizing;resizing=null;
    if(cancel)state.detailWidth=previous.preference;
    root.classList.remove('is-resizing');
    if($('detail-resize').hasPointerCapture(previous.pointer))$('detail-resize').releasePointerCapture(previous.pointer);
    applyDetailWidth();save();
  }
  $('detail-resize').addEventListener('pointerdown',e=>{
    if(e.button!==0||innerWidth<600)return;
    e.preventDefault();e.currentTarget.focus({preventScroll:true});
    resizing={pointer:e.pointerId,x:e.clientX,width:applyDetailWidth(),preference:state.detailWidth};
    root.classList.add('is-resizing');e.currentTarget.setPointerCapture(e.pointerId);
  });
  $('detail-resize').addEventListener('pointermove',e=>{if(!resizing||e.pointerId!==resizing.pointer)return;const {min,max}=detailLimits();state.detailWidth=Math.min(max,Math.max(min,resizing.width+resizing.x-e.clientX));applyDetailWidth();});
  $('detail-resize').addEventListener('pointerup',()=>finishResize());
  $('detail-resize').addEventListener('pointercancel',()=>finishResize(true));
  $('detail-resize').addEventListener('lostpointercapture',()=>finishResize());
  $('detail-resize').addEventListener('keydown',e=>{
    if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();
    const {min,max}=detailLimits(),step=e.shiftKey?64:32;
    state.detailWidth=e.key==='Home'?min:e.key==='End'?max:Math.min(max,Math.max(min,applyDetailWidth()+(e.key==='ArrowLeft'?step:-step)));applyDetailWidth();save();
  });
  $('detail-resize').addEventListener('dblclick',()=>{state.detailWidth=440;applyDetailWidth();save();});
  window.addEventListener('blur',()=>finishResize(true));
  let resizeTimer,scrollTimer;
  window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{finishResize(true);render({preserve:true});},100);});
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
