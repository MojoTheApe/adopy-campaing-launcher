"""Shared connection entry paths: repeated Check and authorized manual recovery."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.analysis_agent import account_spend as sp, account_spend_api as api
from src.fb_api import profile as prof, warm
from tests.test_acl1812_warm_sessions import Rig, PID, ACT


def test_repeated_read_automatically_manages_existing_profile_and_reuses_one_tab(monkeypatch,tmp_path):
    rig=Rig(tmp_path)
    rig.scan_result[PID]=("port",9333)
    entry=SimpleNamespace(profile_id=PID,folder_id="",multilogin_instance="A",ad_account_id=ACT)
    monkeypatch.setattr(sp,"_entry_for",lambda _:entry)
    monkeypatch.setattr(sp,"_set_wait_note",lambda *a:None)
    monkeypatch.setattr(warm,"_REGISTRY",rig.reg)
    monkeypatch.setattr(api,"_probe_local",lambda _: ("port",9333))
    monkeypatch.setattr(api,"_open_gated",lambda *a:prof.OpenedProfile(PID,"","A","9333",None,False))
    monkeypatch.setattr(api,"_open_tab",rig._tab)
    monkeypatch.setattr(api,"_make_graph",lambda tab,**kw:rig.make(tab))
    api._COOLDOWN.clear()
    fixture=json.loads((Path(__file__).parent / "fixtures/ads_manager/fb_api_connection_lifecycle_2026_10_27.json").read_text())
    for _ in range(fixture["repeated_reads"]):
        with api._session("Acc") as sess:
            assert not sess.client.allow_writes
    assert len(rig.tabs)==fixture["healthy_owned_targets"], "Repeated Check must not open another Ads Manager tab"
    assert rig.tabs[0].navigations==1 and not rig.tabs[0].closed
    assert not rig.started and not rig.stopped


def test_opted_in_manual_profile_is_stopped_then_started_once(monkeypatch,tmp_path):
    monkeypatch.setenv("ACL_AUTO_REOPEN_MANUAL","1")
    monkeypatch.setenv("ACL_PROFILE_RESTART_COOLDOWN_S","0")
    rig=Rig(tmp_path)
    rig.scan_result[PID]=("no_port",None)
    entry=SimpleNamespace(profile_id=PID,folder_id="",multilogin_instance="A",ad_account_id=ACT)
    result=rig.reg.open_one("Acc",entry)
    assert result["ok"], result
    assert rig.stopped==["Acc"] and len(rig.started)==1


def test_transient_network_timeout_recovers_one_read_without_a_write():
    from src.fb_api.graph import GraphClient
    class Transport:
        def __init__(self): self.reads=0
        def request(self,method,path,params):
            assert method=="GET"
            self.reads+=1
            return {"error":"network_timeout"} if self.reads==1 else {"status":200,"body":{"id":"123"}}
        def state(self): return {"kind":"ads_manager","has_token":True}
    transport=Transport()
    client=GraphClient(transport,min_interval_s=0,sleep=lambda _:None)
    assert client.get("/123",{"fields":"id"})=={"id":"123"}
    assert transport.reads==2 and client.writes==0


@pytest.mark.parametrize("mode",["declined","foreign","busy","stop_failed","still_running","start_failed"])
def test_manual_recovery_stops_at_the_actual_failed_stage(monkeypatch,tmp_path,mode):
    from src.fb_api.connection import confirmed_reopen
    monkeypatch.setenv("ACL_PROFILE_RESTART_COOLDOWN_S","0")
    rig=Rig(tmp_path)
    rig.scan_result[PID]=("no_port",None)
    entry=SimpleNamespace(profile_id=PID,folder_id="",multilogin_instance="A",ad_account_id=ACT)
    if mode=="foreign": rig.reg._foreign[PID]={"owner_pid":999}
    if mode=="busy": rig.others=True
    if mode=="stop_failed":
        def fail(*a): raise RuntimeError("stop unavailable")
        rig.reg.set_closer(fail)
    if mode=="still_running": rig.reg.set_closer(lambda *a:"still_running")
    if mode=="start_failed": rig.start_error=RuntimeError("launcher unavailable")
    with confirmed_reopen(mode!="declined"):
        result=rig.reg.open_one("Acc",entry)
    assert not result["ok"]
    assert not rig.started, result
    if mode in {"declined","foreign","busy","stop_failed","still_running"}: assert not rig.stopped


def test_confirmation_is_scoped_and_does_not_enable_global_permission(tmp_path):
    from src.fb_api.connection import confirmed_reopen,may_reopen_manual,settings
    assert not may_reopen_manual()
    with confirmed_reopen(True): assert may_reopen_manual()
    assert not may_reopen_manual() and not settings()["auto_reopen_manual"]


def test_corrupt_settings_cannot_authorize_a_manual_restart(tmp_path):
    from config.paths import get_local_config_dir
    from pathlib import Path
    from src.fb_api.connection import may_reopen_manual
    p=Path(get_local_config_dir())/'connection_settings.json'
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text('{bad',encoding='utf-8')
    with pytest.raises(ValueError): may_reopen_manual()


@pytest.mark.parametrize("kind",["network_timeout","network"])
def test_repeated_network_failure_has_only_one_retry(kind):
    from src.fb_api.graph import GraphClient,PageFailed
    class Transport:
        def __init__(self): self.reads=0
        def request(self,*a): self.reads+=1; return {"error":kind}
        def state(self): return {"kind":"ads_manager","has_token":True}
    transport=Transport();client=GraphClient(transport,min_interval_s=0,sleep=lambda _:None)
    with pytest.raises(PageFailed): client.get('/123')
    assert transport.reads==2 and client.calls==2 and client.writes==0


@pytest.mark.parametrize("kind",["login","checkpoint","unknown"])
def test_read_retry_needs_a_fresh_healthy_page(kind):
    from src.fb_api.graph import GraphClient,GraphStop
    class Transport:
        def __init__(self): self.reads=0
        def request(self,*a): self.reads+=1; return {"error":"network_timeout"}
        def state(self): return {"kind":kind,"has_token":kind!="unknown"}
    transport=Transport();client=GraphClient(transport,min_interval_s=0,sleep=lambda _:None)
    with pytest.raises(GraphStop): client.get('/123')
    assert transport.reads==1 and client.writes==0


def test_fresh_frozen_tab_is_replaced_once_before_any_action(tmp_path):
    from src.fb_api.graph import PageUnresponsive
    rig=Rig(tmp_path);rig.warm_up()
    original=rig.make
    def make(tab):
        graph=original(tab)
        if len(rig.tabs)==1:
            def wait(**kw): raise PageUnresponsive("frozen")
            graph.wait_ready=wait
        return graph
    lease=rig.reg.checkout(PID,account="Acc",act=ACT,mode="read",make_graph=make)
    lease.release()
    assert len(rig.tabs)==2 and rig.tabs[0].closed and not rig.tabs[1].closed


def test_two_frozen_tabs_stop_without_an_unbounded_recovery_loop(tmp_path):
    from src.fb_api.graph import PageUnresponsive
    rig=Rig(tmp_path);rig.warm_up()
    def make(tab):
        graph=rig.make(tab)
        def wait(**kw): raise PageUnresponsive("frozen")
        graph.wait_ready=wait
        return graph
    with pytest.raises(PageUnresponsive): rig.reg.checkout(PID,account="Acc",act=ACT,mode="read",make_graph=make)
    assert len(rig.tabs)==2 and rig.tabs[0].closed and not rig.gate.active()


@pytest.mark.parametrize("slow_stage",["new_tab","account_switch"])
def test_connection_budget_covers_all_preparation_stages(tmp_path,slow_stage):
    from src.fb_api.graph import GraphStop
    rig=Rig(tmp_path);sess=rig.warm_up()
    if slow_stage=="new_tab":
        original=rig.reg._tab_factory
        def slow_tab(url):
            rig.now[0]+=60
            return original(url)
        rig.reg._tab_factory=slow_tab
        make=rig.make
    else:
        lease=rig.checkout();lease.release()
        sess.tab.url='https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=999'
        sess.graphs={}
        def make(tab):
            graph=rig.make(tab)
            navigate=graph.navigate
            def slow_nav(*a,**kw): rig.now[0]+=40; return navigate(*a,**kw)
            def slow_ready(**kw): rig.now[0]+=30; return {"kind":"ads_manager","has_token":True}
            graph.navigate=slow_nav;graph.wait_ready=slow_ready
            return graph
    with pytest.raises(GraphStop):
        rig.reg.checkout(PID,account="Acc",act=ACT,mode="read",make_graph=make,ready_timeout=45)
    assert not rig.gate.active()


@pytest.mark.parametrize("mode",["lost-publish","confirm-read","decline-read"])
def test_actual_frontend_confirmation_and_idempotent_submission(mode):
    import json,shutil,subprocess
    from pathlib import Path
    node=shutil.which("node")
    if not node: pytest.skip("Node unavailable")
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([node,str(root/'tests/acl2092_connection_harness.cjs'),str(root/'src/web/templates/report.html'),mode],capture_output=True,text=True,encoding='utf-8',timeout=10)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)["ok"]
