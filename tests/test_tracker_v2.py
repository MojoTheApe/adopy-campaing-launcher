import base64
import copy
import json
from types import SimpleNamespace
import pytest
from tracker import cli, commands, github, model, queue

CONFIG=model.load_config()


def issue(n=1,status='todo',**kwargs):
    row={'number':n,'title':f'project-{n}: Task {n}','state':'open','labels':[{'name':x} for x in ('status:'+status,'module:accounts','priority:medium','type:improvement')],'body':'','created_at':f'2026-01-{n:02d}T00:00:00Z'}
    row.update(kwargs);return row


class API:
    root='repos/owner/repo';repository='owner/repo'
    def __init__(self,rows=None):self.rows={i['number']:i for i in rows or [issue()]};self.state=None;self.sha=0;self.writes=[];self.race=False;self.fail_labels=False
    def issues(self):return copy.deepcopy(list(self.rows.values()))
    def issue(self,n):return copy.deepcopy(self.rows[n])
    def labels(self,n,add=(),remove=()):
        if self.fail_labels:raise github.APIError('uncertain write')
        old=model.names(self.rows[n]);self.rows[n]['labels']=[{'name':x} for x in sorted(old-set(remove)|set(add))];self.writes.append(('labels',n));return self.issue(n)
    def request(self,path,method='GET',payload=None,**kwargs):
        if '/contents/' in path:
            if method=='GET':
                if self.state is None:return None
                return {'content':base64.b64encode(json.dumps(self.state).encode()).decode(),'sha':str(self.sha)}
            if self.race or payload.get('sha')!=(str(self.sha) if self.sha else None):raise github.APIError('conflict',409)
            self.state=json.loads(base64.b64decode(payload['content']));self.sha+=1;self.writes.append(('state',copy.deepcopy(self.state)));return {}
        if '/git/ref/heads/' in path:return {'object':{'sha':'a'*40}}
        if method=='PATCH' and '/issues/' in path:
            n=int(path.rsplit('/',1)[1]);self.rows[n].update(payload);return self.issue(n)
        if method=='POST' and path.endswith('/issues'):
            n=max(self.rows)+1;row=issue(n,**{k:v for k,v in payload.items() if k!='labels'});row['labels']=[{'name':x} for x in payload['labels']];self.rows[n]=row;return self.issue(n)
        raise AssertionError((path,method,payload))


def store(api):return queue.StateStore(api,'master')


def test_claim_reserves_before_status_and_blocks_second_agent():
    api=API([issue(1),issue(2)]);queue.start(api,CONFIG,store(api),1)
    assert api.writes[0][0]=='state' and api.writes[1]==('labels',1)
    assert model.names(api.issue(1)) >= {'status:in_progress','priority:medium'}
    with pytest.raises(ValueError,match='owns'):queue.start(api,CONFIG,store(api),2)
    with pytest.raises(ValueError,match='owns'):queue.start(api,CONFIG,store(api),1)
    queue.start(api,CONFIG,store(api),1,resume=True)


def test_atomic_race_prevents_status_write():
    api=API();api.race=True
    with pytest.raises(ValueError,match='another session'):queue.start(api,CONFIG,store(api),1)
    assert not api.writes and 'status:todo' in model.names(api.issue(1))


def test_uncertain_label_write_keeps_claim_for_explicit_resume():
    api=API();api.fail_labels=True
    with pytest.raises(github.APIError):queue.start(api,CONFIG,store(api),1)
    assert api.state['active']['issue']==1
    api.fail_labels=False;queue.start(api,CONFIG,store(api),1,resume=True)
    assert 'status:in_progress' in model.names(api.issue(1))


def test_batch_snapshots_todo_skips_dependencies_and_never_pulls_backlog():
    api=API([issue(1,body='needs: #2'),issue(2),issue(3,'backlog')]);s=store(api)
    run=queue.begin(s,model.tasks_from_issues(api.issues(),CONFIG));assert run['issues']==[1,2]
    api.rows[4]=issue(4)
    assert queue.next_task(api,CONFIG,s)['issue']==2
    api.labels(2,{'status:merged'},{'status:in_progress'});queue.release(s,2)
    assert queue.next_task(api,CONFIG,s)['issue']==1
    api.labels(1,{'status:merged'},{'status:in_progress'});queue.release(s,1)
    assert queue.next_task(api,CONFIG,s)['state']=='complete'
    assert 'status:todo' in model.names(api.issue(4)) and 'status:backlog' in model.names(api.issue(3))


def test_blocker_preserves_stage_and_releases_execution():
    api=API();s=store(api);queue.start(api,CONFIG,s,1)
    commands.change(api,CONFIG,s,1,blocked=True,reason='Waiting; needs: #2')
    assert {'status:in_progress','flag:blocked'}<=model.names(api.issue(1)) and not api.state['active']
    with pytest.raises(ValueError,match='blocked'):queue.start(api,CONFIG,s,1)


def test_metadata_defaults_medium_and_preserves_existing_fields():
    api=API([issue(status='idea',labels=[{'name':x} for x in ['status:idea','module:accounts','custom:keep']])]);s=store(api)
    row=commands.change(api,CONFIG,s,1,status='backlog')
    assert model.names(row)=={'status:backlog','module:accounts','priority:medium','custom:keep'}
    commands.change(api,CONFIG,s,1,status='todo')
    assert api.state['todo_added']['1']
    with pytest.raises(ValueError):commands.change(api,CONFIG,s,1,status='merged')
    with pytest.raises(ValueError):commands.change(api,CONFIG,s,1,priority='normal')
    with pytest.raises(ValueError):commands.change(api,CONFIG,s,1,required=True)


def test_create_idea_unclassified_and_duplicate_retry():
    api=API();args=SimpleNamespace(title='New idea',status='idea',module=None,kind='feature',design=False,priority=None,body_file=None)
    result=commands.create(api,CONFIG,args)
    assert model.names(result)=={'status:idea','flag:needs_classification','type:feature'}
    with pytest.raises(ValueError,match='exists'):commands.create(api,CONFIG,args)


def test_release_requires_scope_completion_and_unblocked_valid_dependencies():
    rows=[issue(1,'merged',milestone={'number':1,'title':'MVP'},body='needs: #2'),issue(2,'todo')]
    rows[0]['labels'].append({'name':'release:required'})
    tasks=model.tasks_from_issues(rows,CONFIG)
    assert not model.release_summary(tasks,2)['ready']
    s=model.release_summary(tasks,1);assert not s['ready'] and s['unresolved']
    tasks[1]['status']='merged';assert model.release_summary(tasks,1)['ready']
    tasks[0]['blocked']=True;assert not model.release_summary(tasks,1)['ready']
    tasks[0]['blocked']=False;tasks[0]['archive']=True;assert not model.release_summary(tasks,1)['ready']
    tasks[0]['archive']=False;tasks[0]['initiative']=True;assert model.release_summary(tasks,1)['total']==0


def test_metadata_conflict_and_unchecked_completed_tasks_are_visible():
    row=issue(status='merged',body='- [ ] Not done')
    row['labels'].append({'name':'module:campaigns'})
    t=model.tasks_from_issues([row],CONFIG)[0]
    assert not t['archive'] and t['warnings'] and t['module']==''
    assert any('checklist' in w for w in t['warnings'])


def test_migration_preserves_content_and_roundtrips_and_rejects_concurrent_changes():
    row=issue(status='next');row['labels']=[{'name':x} for x in ['status:next','area:1','track:dashboard']]
    api=API([row]);before=api.issue(1);plan=commands.migration_plan(api,CONFIG,{'1':{'module':'accounts','type':'feature'}})
    commands.migrate(api,plan);assert {'status:backlog','priority:medium','module:accounts','track:dashboard'}<=model.names(api.issue(1))
    assert api.issue(1)['body']==before['body'] and api.issue(1)['title']==before['title']
    commands.migrate(api,plan);commands.migrate(api,plan,rollback=True)
    assert model.names(api.issue(1))==model.names(before)
    api.labels(1,{'status:review'},{'status:next'})
    with pytest.raises(ValueError,match='changed'):commands.migrate(api,plan)


def test_export_has_generic_config_no_old_tasks_or_live_deployment(tmp_path):
    args=SimpleNamespace(output=tmp_path/'new',repository='owner/new',name='New Project',base='main')
    cli.export(CONFIG,args)
    c=model.load_config(args.output/'tracker/config.json')
    assert c['project']['repository']=='owner/new' and c['modules'][0]['id']=='product'
    assert not list(args.output.rglob('*snapshot*')) and not list(args.output.rglob('*mapping*'))
    assert (args.output/'AGENTS.md').exists() and (args.output/'.github/ISSUE_TEMPLATE/task.yml').exists()


def test_non_code_completion_requires_review_and_records_evidence(tmp_path):
    row=issue(status='review');row['labels'].append({'name':'type:operations'});row['labels']=[x for x in row['labels'] if x['name']!='type:improvement']
    api=API([row]);evidence=tmp_path/'evidence.md';evidence.write_text('Verified domain resolves to the expected service.')
    args=cli.parser().parse_args(['complete','1','--evidence-file',str(evidence),'--verified-non-code'])
    cli.execute(args,CONFIG,api)
    result=api.issue(1);assert {'status:merged','resolution:non_code'}<=model.names(result)
    assert 'Verified domain' in result['body']
