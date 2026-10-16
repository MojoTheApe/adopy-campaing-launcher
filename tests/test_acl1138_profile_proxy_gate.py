"""ACL-1138: профиль без прокси в Multilogin не открывается.

Разведка 16.10 по MLX API: в пространстве A 34 профиля из 1848 без прокси,
в B трое из 520 — среди них рекламные. Источник правды — конфигурация
профиля в MLX (`/profile/search` с `profile_ids`), флаг `--proxy-server`
в командной строке только пишется в лог.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.fb_ads_rpa import profile_proxy_gate as gate
from src.fb_ads_rpa import profile_trouble


PID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    gate._ok_cache.clear()
    gate._unknown_cache.clear()
    monkeypatch.delenv("FB_RPA_PROXY_GATE", raising=False)
    monkeypatch.setattr(gate, "_report_unchecked", lambda *a: None)
    yield
    gate._ok_cache.clear()
    gate._unknown_cache.clear()


def _entry(pid: str = PID):
    return SimpleNamespace(profile_id=pid, multilogin_instance="A")


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


def _search_returning(*profiles, status: int = 200):
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        return _Resp(status, {"data": {"profiles": list(profiles)}})

    return post, calls


# ── чтение конфигурации из MLX API ───────────────────────────────────────

@pytest.mark.parametrize("ptype", ["socks5", "http"])
def test_proxied_profile_of_each_seen_type_is_recognised(ptype):
    post, calls = _search_returning({"id": PID, "proxy": {"type": ptype, "host": "h", "port": 1}})
    cfg = gate.fetch_proxy_config(PID, "tok", post=post)
    assert cfg.known and cfg.has_proxy and cfg.proxy_type == ptype
    url, kw = calls[0]
    assert url == gate.SEARCH_URL
    # По id, а не по имени: search_text по id ничего не находит (разведка 16.10).
    assert kw["json"]["profile_ids"] == [PID]
    assert kw["headers"]["Authorization"] == "Bearer tok"


@pytest.mark.parametrize("proxy", [None, {}, {"type": "http", "host": ""}])
def test_profile_without_proxy_is_recognised(proxy):
    post, _ = _search_returning({"id": PID, "proxy": proxy})
    cfg = gate.fetch_proxy_config(PID, "tok", post=post)
    assert cfg.known and not cfg.has_proxy


def test_unknown_when_api_fails_or_profile_is_missing():
    post, _ = _search_returning(status=501)
    assert not gate.fetch_proxy_config(PID, "tok", post=post).known
    post, _ = _search_returning({"id": "other", "proxy": None})
    assert not gate.fetch_proxy_config(PID, "tok", post=post).known

    def boom(url, **kw):
        raise ConnectionError("down")

    assert not gate.fetch_proxy_config(PID, "tok", post=boom).known
    assert not gate.fetch_proxy_config(PID, "", post=boom).known


# ── гейт ─────────────────────────────────────────────────────────────────

def test_profile_without_proxy_refuses_the_run_with_what_to_do():
    fetch = lambda pid, tok: gate.ProxyConfig(known=True, has_proxy=False)
    with pytest.raises(profile_trouble.ProfileStop) as exc:
        gate.ensure_profile_has_proxy(_entry(), "Doc1", fetch=fetch, token="t")
    assert exc.value.code == profile_trouble.NO_PROXY
    assert exc.value.code in profile_trouble.ACCOUNT_STOPS
    text = str(exc.value)
    assert "no proxy" in text and "Add the proxy to the profile in Multilogin" in text


def test_proxied_profile_passes_and_is_remembered():
    calls = []

    def fetch(pid, tok):
        calls.append(pid)
        return gate.ProxyConfig(known=True, has_proxy=True, proxy_type="http")

    clock = iter([0.0, 10.0, 10.0 + gate.OK_TTL_S + 1, 10.0 + gate.OK_TTL_S + 1]).__next__
    for _ in range(3):
        gate.ensure_profile_has_proxy(_entry(), "Doc1", fetch=fetch, token="t", now=clock)
    assert calls == [PID, PID]  # второй раз — из кэша, третий — кэш истёк


def test_refusal_is_not_cached_so_a_fixed_profile_runs_next_time():
    answers = iter([gate.ProxyConfig(known=True, has_proxy=False),
                    gate.ProxyConfig(known=True, has_proxy=True)])
    fetch = lambda pid, tok: next(answers)
    with pytest.raises(profile_trouble.ProfileStop):
        gate.ensure_profile_has_proxy(_entry(), "Doc1", fetch=fetch, token="t")
    assert gate.ensure_profile_has_proxy(_entry(), "Doc1", fetch=fetch, token="t").has_proxy


def test_failed_check_does_not_block_a_healthy_run():
    fetch = lambda pid, tok: gate.ProxyConfig(known=False, reason="MLX API HTTP 501")
    cfg = gate.ensure_profile_has_proxy(_entry(), "Doc1", fetch=fetch, token="t")
    assert cfg is not None and not cfg.known
    assert PID not in gate._ok_cache


def test_unknown_result_is_held_briefly_so_an_outage_costs_one_timeout():
    calls = []

    def fetch(pid, tok):
        calls.append(pid)
        return gate.ProxyConfig(known=False, reason="timeout")

    # запись в 0; 0 и 30 — из памяти; после UNKNOWN_TTL_S — снова спрашиваем.
    clock = iter([0.0, 0.0, 30.0, gate.UNKNOWN_TTL_S + 1, gate.UNKNOWN_TTL_S + 1]).__next__
    for _ in range(4):
        gate.ensure_profile_has_proxy(_entry(), "Doc1", fetch=fetch, token="t", now=clock)
    assert calls == [PID, PID]


def test_unknown_workspace_asks_both_and_uppercase_id_matches(monkeypatch):
    monkeypatch.setattr(gate, "_token_for", lambda inst: f"tok-{inst}")
    seen = []

    def fetch(pid, tok):
        seen.append(tok)
        if tok == "tok-A":
            return gate.ProxyConfig(known=False, reason="profile not found in this Multilogin workspace")
        return gate.ProxyConfig(known=True, has_proxy=False)

    with pytest.raises(profile_trouble.ProfileStop):
        gate.ensure_profile_has_proxy(SimpleNamespace(profile_id=PID, multilogin_instance=""),
                                      "Doc1", fetch=fetch)
    assert seen == ["tok-A", "tok-B"]

    post, _ = _search_returning({"id": PID.upper(), "proxy": None})
    cfg = gate.fetch_proxy_config(PID, "tok", post=post)
    assert cfg.known and not cfg.has_proxy


def test_machine_switch_turns_the_gate_off(monkeypatch):
    monkeypatch.setenv("FB_RPA_PROXY_GATE", "0")

    def fetch(pid, tok):
        raise AssertionError("must not be called")

    assert gate.ensure_profile_has_proxy(_entry(), "Doc1", fetch=fetch, token="t") is None


def test_cmdline_flag_detection():
    assert gate.cmdline_has_proxy_flag(["chrome.exe", "--proxy-server=socks5://x:1"])
    assert not gate.cmdline_has_proxy_flag(["chrome.exe", "--remote-debugging-port=1"])
    assert not gate.cmdline_has_proxy_flag(None)


# ── встроено в оба провайдера до прицепа и запуска ───────────────────────

def _refuse(entry, label):
    raise profile_trouble.ProfileStop(profile_trouble.NO_PROXY, "test")


def test_attach_provider_refuses_before_touching_the_profile(monkeypatch):
    from src.fb_ads_rpa import browser

    monkeypatch.setattr(browser.profile_proxy_gate, "ensure_profile_has_proxy", _refuse)

    def untouched(*a, **k):
        raise AssertionError("profile must not be looked up, attached or started")

    monkeypatch.setattr(browser, "_find_mlx_profile_dir", untouched)
    with pytest.raises(profile_trouble.ProfileStop) as exc:
        browser.MultiloginAttachOnlyProvider().open(_entry(), "Doc1")
    assert exc.value.code == profile_trouble.NO_PROXY


def test_direct_provider_refuses_before_touching_the_profile(monkeypatch):
    from src.fb_ads_rpa import browser

    monkeypatch.setattr(browser.profile_proxy_gate, "ensure_profile_has_proxy", _refuse)
    provider = browser.MultiloginDirectCDPProvider()

    def untouched(*a, **k):
        raise AssertionError("profile must not be looked up, attached or started")

    monkeypatch.setattr(provider, "_find_profile_user_data_dir", untouched)
    with pytest.raises(profile_trouble.ProfileStop):
        provider.open(_entry(), "Doc1")


def test_mlx_provider_refuses_before_starting(monkeypatch):
    from src.fb_ads_rpa import browser

    monkeypatch.setattr(browser.profile_proxy_gate, "ensure_profile_has_proxy", _refuse)
    import src.meta_campaign_agent.multilogin_config as mc

    def untouched(*a, **k):
        raise AssertionError("MLX must not be asked to start the profile")

    monkeypatch.setattr(mc, "get_multilogin_instance", untouched)
    with pytest.raises(profile_trouble.ProfileStop):
        browser.MultiloginBrowserProvider().open(_entry(), "Doc1")


def test_attach_existing_only_refuses(monkeypatch):
    from src.fb_ads_rpa import browser

    monkeypatch.setattr(browser.profile_proxy_gate, "ensure_profile_has_proxy", _refuse)
    provider = browser.MultiloginDirectCDPProvider()
    monkeypatch.setattr(provider, "_try_attach_existing",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not attach")))
    with pytest.raises(profile_trouble.ProfileStop):
        provider.attach_existing_only(PID, "C:/x")


def test_graph_api_open_profile_refuses_before_process_or_mlx(monkeypatch):
    from src.fb_api import profile as fb_profile
    from src.fb_ads_rpa import profile_proxy_gate as pg

    monkeypatch.setattr(pg, "ensure_profile_has_proxy", _refuse)

    def untouched(*a, **k):
        raise AssertionError("neither the local process nor MLX may be touched")

    with pytest.raises(fb_profile.ProfileError) as exc:
        fb_profile.open_profile(PID, "f", "B", launcher_factory=untouched, local_probe=untouched)
    assert exc.value.code == profile_trouble.NO_PROXY
    assert "Add the proxy to the profile in Multilogin" in str(exc.value)


def test_spend_attach_turns_the_refusal_into_not_attachable():
    from src.analysis_agent import account_spend

    class Direct:
        def attach_existing_only(self, *a, **k):
            raise profile_trouble.ProfileStop(profile_trouble.NO_PROXY, "x")

    with pytest.raises(account_spend.ProfileNotAttachable) as exc:
        account_spend._attach_existing(Direct(), PID, "C:/x", new_page=True)
    assert exc.value.code == profile_trouble.NO_PROXY


def test_publisher_shortcut_does_not_bypass_a_profile_stop():
    import inspect

    from src.fb_ads_rpa import rpa_publisher

    src = inspect.getsource(rpa_publisher.RpaPublisher._ensure_browser)
    shortcut = src[src.index("_open_multilogin_direct(entry, account_label)"):]
    assert shortcut.index("except profile_trouble.ProfileStop") < shortcut.index("trying normal chain")


def test_warm_pool_does_not_adopt_or_start_a_no_proxy_profile(monkeypatch):
    from src.fb_api import warm
    from src.fb_ads_rpa import profile_proxy_gate as pg

    monkeypatch.setattr(pg, "ensure_profile_has_proxy", _refuse)
    reg = warm.WarmRegistry.__new__(warm.WarmRegistry)
    reg._scan = lambda ids: (_ for _ in ()).throw(AssertionError("must not scan, adopt or start"))
    res = reg.open_one("Doc1", SimpleNamespace(profile_id=PID, folder_id="f", multilogin_instance="B"))
    assert res["ok"] is False and res["code"] == profile_trouble.NO_PROXY


def test_warm_checkout_refuses_a_session_registered_before_the_gate(monkeypatch):
    from src.fb_api import warm
    from src.fb_ads_rpa import profile_proxy_gate as pg

    monkeypatch.setattr(pg, "ensure_profile_has_proxy", _refuse)
    reg = warm.WarmRegistry.__new__(warm.WarmRegistry)
    sess = SimpleNamespace(profile_id=PID, owner=0,
                           lock=SimpleNamespace(acquire=lambda: (_ for _ in ()).throw(AssertionError("no lock"))))
    reg.session = lambda pid: sess
    with pytest.raises(warm.WarmError) as exc:
        reg.checkout(PID, account="Doc1", act="act_1", mode="read", make_graph=lambda t: None)
    assert exc.value.code == profile_trouble.NO_PROXY


def test_unknown_reasons_from_both_workspaces_are_kept(monkeypatch):
    monkeypatch.setattr(gate, "_token_for", lambda inst: f"tok-{inst}")
    answers = {"tok-A": "profile not found in this Multilogin workspace", "tok-B": "no MLX token"}
    fetch = lambda pid, tok: gate.ProxyConfig(known=False, reason=answers[tok])
    cfg = gate.ensure_profile_has_proxy(SimpleNamespace(profile_id=PID, multilogin_instance=""),
                                        "Doc1", fetch=fetch)
    assert "not found" in cfg.reason and "no MLX token" in cfg.reason
