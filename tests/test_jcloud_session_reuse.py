"""ACL-2881: клиент JCloudFront держит одну сессию MCP и переиспользует её."""
from __future__ import annotations

import contextlib

import pytest

from src.jcloud import client as jc


class _Session:
    def __init__(self, log, fail_first=False):
        self.log, self.fail_first = log, fail_first

    async def call_tool(self, tool, args):
        self.log.append(("call", tool))
        if self.fail_first:
            self.fail_first = False
            raise ConnectionError("stale session")
        return type("R", (), {"structuredContent": {"ok": True, "n": len(self.log)}, "isError": False})()


@pytest.fixture
def opened(monkeypatch):
    monkeypatch.setenv("JCLOUD_MCP_URL", "http://127.0.0.1:18765/mcp")
    monkeypatch.setenv("JCLOUD_MCP_TOKEN", "tok-1234567890")
    log: list = []
    behaviour = {"fail_first_session": False, "fail_open": False}

    @contextlib.asynccontextmanager
    async def fake_open(url, token):
        if behaviour["fail_open"]:
            raise OSError("cannot connect")
        log.append(("open", token))
        bad = behaviour["fail_first_session"]
        behaviour["fail_first_session"] = False  # устаревает только первая сессия
        yield _Session(log, fail_first=bad)
        log.append(("close", token))

    monkeypatch.setattr(jc, "_open_session", fake_open)
    jc._worker.stop()
    yield log, behaviour
    jc._worker.stop()


def _opens(log):
    return [e for e in log if e[0] == "open"]


def test_two_calls_in_a_row_open_the_session_once(opened):
    log, _ = opened
    jc._call("search_landing_catalog", {})
    jc._call("search_landing_catalog", {})
    assert len(_opens(log)) == 1 and [e for e in log if e[0] == "call"] == [("call", "search_landing_catalog")] * 2


def test_a_stale_session_is_reopened_and_the_call_retried_once(opened):
    log, behaviour = opened
    behaviour["fail_first_session"] = True
    out = jc._call("search_landing_catalog", {})
    assert out["ok"] and len(_opens(log)) == 2


def test_a_second_failure_is_returned_as_the_error(opened, monkeypatch):
    log, behaviour = opened

    class Broken(_Session):
        async def call_tool(self, tool, args):
            raise ConnectionError("down")

    @contextlib.asynccontextmanager
    async def always_broken(url, token):
        log.append(("open", token))
        yield Broken(log)

    monkeypatch.setattr(jc, "_open_session", always_broken)
    with pytest.raises(ConnectionError):
        jc._call("search_landing_catalog", {})
    assert len(_opens(log)) == 2


def test_a_changed_token_opens_a_new_session(opened, monkeypatch):
    log, _ = opened
    jc._call("search_landing_catalog", {})
    monkeypatch.setenv("JCLOUD_MCP_TOKEN", "tok-OTHER-1234567")
    jc._call("search_landing_catalog", {})
    assert [e[1] for e in _opens(log)] == ["tok-1234567890", "tok-OTHER-1234567"]


def test_idle_session_is_closed_and_the_next_call_opens_a_new_one(opened, monkeypatch):
    log, _ = opened
    monkeypatch.setattr(jc, "IDLE_S", 0.2)
    jc._call("search_landing_catalog", {})
    import time
    time.sleep(0.6)
    jc._call("search_landing_catalog", {})
    assert len(_opens(log)) == 2


def test_failure_to_connect_is_reported_and_does_not_break_later_calls(opened):
    log, behaviour = opened
    behaviour["fail_open"] = True
    with pytest.raises(OSError):
        jc._call("search_landing_catalog", {})
    behaviour["fail_open"] = False
    assert jc._call("search_landing_catalog", {})["ok"]


def test_search_still_maps_a_connection_failure_to_unreachable(opened):
    log, behaviour = opened
    behaviour["fail_open"] = True
    assert jc.search("/x")["code"] == "unreachable"


# ───────────── зависшее открытие / закрытие (замечание ревью ACL-2881) ─────────────

def _hanging_open(monkeypatch, log, *, hang_times, uninterruptible=False):
    """Первые hang_times открытий зависают на рукопожатии; остальные дают рабочую сессию."""
    import asyncio

    state = {"n": 0}

    @contextlib.asynccontextmanager
    async def fake_open(url, token):
        state["n"] += 1
        if state["n"] <= hang_times:
            if uninterruptible:
                while True:
                    try:
                        await asyncio.sleep(3600)
                    except asyncio.CancelledError:
                        pass  # зависание, которое не снимается отменой
            await asyncio.sleep(3600)
        log.append(("open", token))
        yield _Session(log)

    monkeypatch.setattr(jc, "_open_session", fake_open)


def test_a_hanging_handshake_times_out_and_the_next_search_works(opened, monkeypatch):
    import time
    log, _ = opened
    monkeypatch.setattr(jc, "OPEN_S", 0.3)
    _hanging_open(monkeypatch, log, hang_times=1)
    t = time.time()
    assert jc.search("/x")["code"] == "timeout"
    assert time.time() - t < 3
    assert jc.search("/x")["ok"] is True


def test_a_handshake_that_ignores_cancellation_is_abandoned_for_a_fresh_thread(opened, monkeypatch):
    import threading
    log, _ = opened
    monkeypatch.setattr(jc, "OPEN_S", 0.2)
    monkeypatch.setattr(jc, "CALL_WAIT_S", 0.8)
    _hanging_open(monkeypatch, log, hang_times=1, uninterruptible=True)
    threads_before = {t.ident for t in threading.enumerate() if t.name == "jcloud-mcp"}
    assert jc.search("/x")["code"] == "timeout"
    assert jc.search("/x")["ok"] is True
    after = {t.ident for t in threading.enumerate() if t.name == "jcloud-mcp"}
    assert after - threads_before  # работает новый поток


def test_a_hanging_close_does_not_block_the_next_session(opened, monkeypatch):
    import asyncio
    log, _ = opened
    monkeypatch.setattr(jc, "CLOSE_S", 0.2)

    @contextlib.asynccontextmanager
    async def slow_close(url, token):
        log.append(("open", token))
        yield _Session(log)
        await asyncio.sleep(3600)

    monkeypatch.setattr(jc, "_open_session", slow_close)
    jc._call("search_landing_catalog", {})
    monkeypatch.setenv("JCLOUD_MCP_TOKEN", "tok-OTHER-1234567")  # заставляет закрыть и открыть заново
    assert jc._call("search_landing_catalog", {})["ok"]
    assert len(_opens(log)) == 2
