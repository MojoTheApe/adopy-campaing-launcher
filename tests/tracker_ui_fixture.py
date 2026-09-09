"""Synthetic browser fixture; no real project data or credentials."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tracker import model, sync
config=model.load_config();config['project']={'name':'Example Task Wall','repository':'owner/example','base_branch':'main'}
config['completion_label']='In main'
rows=[]
for n,status in enumerate(model.STATUSES,1):
    labels=['status:'+status,'module:accounts','priority:medium','type:feature']
    if n in (2,4):labels+=['facet:design']
    if n==4:labels=[x for x in labels if not x.startswith('priority:')]+['priority:high']
    if n==6:labels+=['release:required']
    rows.append({'number':n,'title':f'example-{n}: Task {n}','body':'Details <img src=x onerror=alert(1)>','state':'open','labels':[{'name':x} for x in labels],'milestone':{'number':1,'title':'MVP'} if n==6 else None})
page,data=sync.render(model.payload(rows,config,[{'number':1,'title':'MVP'},{'number':2,'title':'Later'}]))
print(json.dumps({'page':page,'data':data,'status':{'ok':True,'synced_at':sync.now(),'digest':data['digest'],'revision':data['revision'],'issues':6}}))
