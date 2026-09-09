"""Tracker v2 integration contract replaces the retired snapshot/area renderer contract."""
import json
from types import SimpleNamespace
import pytest
from tracker import github, model, sync


def issue(number=1, **overrides):
    row=dict(number=number,title="project-101: Current title",state="open",labels=[{"name":x} for x in ("status:backlog","module:accounts","priority:medium")],body="Details\n- [x] Done part\n- [ ] Remaining")
    row.update(overrides);return row


class FakeAPI:
    root="repos/owner/repo"
    def __init__(self, rows=None): self.rows=rows if rows is not None else [issue()]
    def issues(self): return self.rows
    def request(self,*a,**k): return []


def test_github_tasks_need_no_historical_snapshot():
    tasks=model.tasks_from_issues([issue(),issue(2,title="New idea",labels=[])],model.load_config())
    assert len(tasks)==2 and tasks[0]['title']=="Current title"
    assert tasks[0]['status']=='backlog' and tasks[0]['priority']=='medium'
    assert tasks[0]['checks']==[{'done':True,'title':'Done part'},{'done':False,'title':'Remaining'}]
    assert tasks[1]['module']=='' and tasks[1]['warnings']


def test_closed_is_archived_not_completed():
    tasks=model.tasks_from_issues([issue(state='closed',labels=[])],model.load_config())
    assert tasks[0]['archive'] and tasks[0]['status']!='merged'


def test_issue_content_cannot_inject_html():
    data=model.payload([issue(title='</script><script>alert(1)</script>',body='<img src=x onerror="alert(1)">',html_url='javascript:alert(1)')],model.load_config())
    page,result=sync.render(data)
    assert '<script>alert(1)</script>' not in page and '<img src=x' not in page
    assert 'javascript:alert' not in page and result['digest'] in page


def test_fetch_all_pages_and_reject_malformed_or_duplicate_pages(monkeypatch):
    pr=issue(2,pull_request={})
    def run(args,**kwargs):
        assert '--paginate' in args and '--slurp' in args
        return SimpleNamespace(stdout=json.dumps([[issue(),pr],[issue(3)]]),returncode=0)
    monkeypatch.setattr(github.subprocess,'run',run)
    assert [i['number'] for i in github.GitHub('owner/repo').issues()]==[1,3]
    for pages in ([],[{}],[[issue()],[issue()]]):
        monkeypatch.setattr(github.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=json.dumps(pages),returncode=0))
        with pytest.raises(ValueError):github.GitHub('owner/repo').issues()


def test_partial_failure_preserves_board_and_safe_status(tmp_path):
    api=FakeAPI();config=model.load_config();sync.sync(api,config,tmp_path,queue={})
    old=(tmp_path/'index.html').read_text();status=json.loads((tmp_path/'status.json').read_text())
    def fail():raise RuntimeError('SENSITIVE')
    api.issues=fail
    with pytest.raises(RuntimeError,match='previous board preserved'):sync.sync(api,config,tmp_path,queue={})
    assert (tmp_path/'index.html').read_text()==old
    result=json.loads((tmp_path/'status.json').read_text())
    assert result['synced_at']==status['synced_at'] and not result['ok']
    assert 'SENSITIVE' not in (tmp_path/'status.json').read_text()


def test_empty_update_keeps_html_data_count_and_recovers(tmp_path):
    api=FakeAPI();config=model.load_config();sync.sync(api,config,tmp_path,queue={})
    html=(tmp_path/'index.html').read_text();data=(tmp_path/'data.json').read_text();old=json.loads((tmp_path/'status.json').read_text())
    api.rows=[]
    for _ in range(2):
        with pytest.raises(RuntimeError):sync.sync(api,config,tmp_path,queue={})
        assert (tmp_path/'index.html').read_text()==html and (tmp_path/'data.json').read_text()==data
        status=json.loads((tmp_path/'status.json').read_text())
        assert status['error']=='empty_response' and status['issues']==1 and status['digest']==old['digest'] and status['synced_at']==old['synced_at']
    api.rows=[issue(title='Recovered')];sync.sync(api,config,tmp_path,queue={})
    assert 'Recovered' in (tmp_path/'index.html').read_text()
    assert json.loads((tmp_path/'status.json').read_text())['ok']


def test_new_empty_project_and_explicit_clear(tmp_path):
    api=FakeAPI([]);config=model.load_config();sync.sync(api,config,tmp_path,queue={})
    assert json.loads((tmp_path/'status.json').read_text())['issues']==0
    api.rows=[issue()];sync.sync(api,config,tmp_path,queue={});api.rows=[]
    sync.sync(api,config,tmp_path,queue={},allow_empty=True)
    assert json.loads((tmp_path/'data.json').read_text())['tasks']==[]
    assert json.loads((tmp_path/'status.json').read_text())['empty_confirmed']


def test_ui_revision_changes_digest_but_sync_clock_does_not(tmp_path,monkeypatch):
    data=model.payload([issue()],model.load_config());_,first=sync.render(data);_,same=sync.render(data)
    assert first['digest']==same['digest']
    import shutil
    shutil.copytree(sync.WEB,tmp_path/'web');monkeypatch.setattr(sync,'WEB',tmp_path/'web')
    (sync.WEB/'style.css').write_text((sync.WEB/'style.css').read_text()+'\n/* new UI */')
    _,updated=sync.render(data);assert first['digest']!=updated['digest']


def test_issue_template_markers_remain_literal_data():
    import re
    body = "Document __APP__, __STYLE__ and __DATA__ without replacing their values."
    page, data = sync.render(model.payload([issue(body=body)], model.load_config()))
    embedded = re.search(r'<script id="tracker-data" type="application/json">(.*?)</script>', page, re.S)[1]
    assert json.loads(embedded)["tasks"][0]["body"] == body
    assert json.loads(embedded)["digest"] == data["digest"]
