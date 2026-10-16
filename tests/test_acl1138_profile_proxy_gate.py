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


PID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    gate._ok_cache.clear()
    monkeypatch.delenv("FB_RPA_PROXY_GATE", raising=False)
    yield
    gate._ok_cache.clear()


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
