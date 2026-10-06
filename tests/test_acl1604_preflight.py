# -*- coding: utf-8 -*-
"""ACL-1604: проверка аккаунтов до «Publish all» — без открытия профилей.

05.10 5265 был разлогинен, у 9403A стоял чекпоинт, Elf403 открыли руками —
и все трое отвалились через час. Здесь: разбор по фальшивой телеметрии,
статусу, карте и токенам; эндпоинт; один браузерный тест списка «Not ready».
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.web import publish_preflight as pf  # noqa: E402

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


GOOD_TOKEN = jwt(time.time() + 3600)
ENTRY = SimpleNamespace(profile_id="p-1", multilogin_instance="A")


def verdict(**kw):
    kw.setdefault("entry", ENTRY)
    kw.setdefault("token", GOOD_TOKEN)
    return pf.classify("Facebook_5265", now=NOW, ttl_h=12, **kw)


def codes(v):
    return [r.code for r in v.blocking]


# --- карта и токен ----------------------------------------------------------

def test_good_account_is_ready():
    v = verdict()
    assert v.ready and not v.notes


def test_missing_map_entry_and_profile_block():
    assert codes(verdict(entry=None)) == [pf.NOT_IN_MAP]
    assert codes(verdict(entry=SimpleNamespace(profile_id="  ", multilogin_instance="A"))) == [pf.NO_PROFILE]


def test_dead_token_blocks_missing_or_expired():
    assert codes(verdict(token="")) == [pf.NO_TOKEN]
    assert codes(verdict(token=jwt(time.time() - 5))) == [pf.TOKEN_EXPIRED]
    assert verdict(token="not-a-jwt").ready   # настоящий отказ придёт при подключении


# --- телеметрия -------------------------------------------------------------

def test_recent_logged_out_blocks_with_time_and_hint():
    v = verdict(signals=[{"code": pf.LOGGED_OUT, "ts": iso(2)}])
    assert codes(v) == [pf.LOGGED_OUT]
    r = v.blocking[0]
    assert "logged out" in r.text() and "(seen " in r.text()
    assert "log in inside Multilogin" in r.hint


def test_old_signal_is_ignored():
    assert verdict(signals=[{"code": pf.LOGGED_OUT, "ts": iso(30)}]).ready


def test_later_successful_step_clears_the_signal():
    sig = [{"code": pf.LOGGED_OUT, "ts": iso(5)}, {"code": "", "ts": iso(4)}]
    assert verdict(signals=sig).ready
    # а трудность ПОСЛЕ успеха — снова держит
    sig = [{"code": "", "ts": iso(5)}, {"code": pf.NETWORK, "ts": iso(4)}]
    assert codes(verdict(signals=sig)) == [pf.NETWORK]


def test_later_status_read_clears_logged_out():
    sig = [{"code": pf.LOGGED_OUT, "ts": iso(5)}]
    assert verdict(signals=sig, status={"status": "clear", "at": iso(1)}).ready


def test_hand_window_that_could_not_reopen_blocks_but_open_profile_only_notes():
    v = verdict(signals=[{"code": pf.HAND_WINDOW_STUCK, "ts": iso(3)}])
    assert codes(v) == [pf.HAND_WINDOW_STUCK]
    v = verdict(open_profile=True)   # ACL-1606: открыт руками — переоткроется, не блок
    assert v.ready and [n.code for n in v.notes] == [pf.OPEN_NOW]


def test_status_disabled_restricted_block_review_only_notes():
    assert codes(verdict(status={"status": "disabled", "at": iso(1)})) == [pf.DISABLED]
    assert codes(verdict(status={"status": "restricted", "at": iso(1)})) == [pf.RESTRICTED]
    v = verdict(status={"status": "review", "at": iso(1)})
    assert v.ready and [n.code for n in v.notes] == [pf.REVIEW]


def test_checkpoint_from_status_text():
    v = verdict(status={"status": "restricted", "status_text": "Security checkpoint: confirm your identity",
                        "at": iso(1)})
    assert pf.CHECKPOINT in codes(v)


# --- разбор строк телеметрии ------------------------------------------------

def rows_for_5265():
    return [
        {"ts": iso(3), "step": "job_phase", "ok": True, "job": "j1", "diag": {"account": "Facebook_5265"}},
        {"ts": iso(2.9), "step": "profile_logged_out", "ok": False, "job": "j1", "note": "connect"},
        {"ts": iso(2.8), "step": "profile_hand_window", "ok": False, "job": "j2", "note": "restart_failed",
         "diag": {"account": "FacebookElf403"}},
        {"ts": iso(2.7), "step": "campaign_open", "ok": False, "job": "j3", "note": "Security checkpoint page",
         "diag": {"account": "9403A"}},
        {"ts": iso(2.6), "step": "profile_logged_out", "ok": False},   # без аккаунта и задания — никому
    ]


def test_signals_attribute_by_job_and_diag_account():
    sig = pf.signals_from_telemetry(rows_for_5265())
    assert [s["code"] for s in sig["facebook_5265"]] == [pf.LOGGED_OUT]
    assert [s["code"] for s in sig["facebookelf403"]] == [pf.HAND_WINDOW_STUCK]
    assert [s["code"] for s in sig["9403a"]] == [pf.CHECKPOINT]
    assert len(sig) == 3


def test_preflight_end_to_end_lists_not_ready_and_queues_good():
    amap = {"Facebook_5265": ENTRY, "9403A": ENTRY, "Good1": ENTRY,
            "Nope": SimpleNamespace(profile_id="", multilogin_instance="A")}
    out = pf.preflight(
        ["Facebook_5265", "9403A", "Good1", "Nope", "Ghost"], amap=amap,
        resolve=lambda m, n: m.get(n), token_for=lambda inst: GOOD_TOKEN,
        telemetry_rows=rows_for_5265, status_for=lambda a: {}, open_profile_ids=lambda: {"p-1"}, now=NOW)
    by = {v.account: v for v in out}
    assert codes(by["Facebook_5265"]) == [pf.LOGGED_OUT]
    assert codes(by["9403A"]) == [pf.CHECKPOINT]
    assert by["Good1"].ready
    assert codes(by["Nope"]) == [pf.NO_PROFILE]
    assert codes(by["Ghost"]) == [pf.NOT_IN_MAP]
    s = pf.summary(out)
    assert s["ready"] == ["Good1"] and s["opened_profiles"] is False
    assert s["text"].startswith("Not ready: Facebook_5265 — logged out of Facebook (seen ")


def test_preflight_makes_no_calls_for_an_empty_list():
    called = []
    pf.preflight([], amap={}, open_profile_ids=lambda: called.append(1) or set(), telemetry_rows=lambda: [])
    assert not called


# --- эндпоинт ---------------------------------------------------------------

def test_endpoint_and_telemetry_rows(monkeypatch, tmp_path):
    from src.fb_ads_rpa import run_telemetry as rt
    from src.web import app as web

    log = tmp_path / "t.jsonl"
    monkeypatch.setattr(rt, "TELEMETRY_PATH", log)
    monkeypatch.setattr(web, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))
    monkeypatch.setattr(pf, "_default_telemetry_rows", lambda: [])
    monkeypatch.setattr(pf, "_default_status", lambda a: {})
    monkeypatch.setattr(pf, "_default_open_profile_ids", lambda: set())

    r = asyncio.run(web.api_publish_preflight(accounts="Alpha, Beta,Alpha"))
    body = json.loads(r.body)
    assert body["not_ready"] == ["Alpha", "Beta"] and body["ready"] == []
    assert body["accounts"][0]["reasons"][0]["code"] == pf.NOT_IN_MAP
    assert body["opened_profiles"] is False

    class Req:
        async def json(self):
            return {"queued": ["Gamma"], "skipped": ["Alpha"], "run_anyway": ["Beta"]}

    assert json.loads(asyncio.run(web.api_publish_preflight_decision(Req())).body) == {"ok": True}
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [x["step"] for x in rows] == ["publish_preflight", "publish_preflight_decision"]
    assert rows[0]["ok"] is False and rows[0]["diag"]["not_ready"][0] == {"account": "Alpha", "codes": ["not_in_map"]}
    assert rows[1]["diag"]["skipped"] == ["Alpha"] and rows[1]["diag"]["run_anyway"] == ["Beta"]


def test_endpoint_without_accounts_is_empty(monkeypatch, tmp_path):
    from src.fb_ads_rpa import run_telemetry as rt
    from src.web import app as web

    monkeypatch.setattr(rt, "TELEMETRY_PATH", tmp_path / "t.jsonl")
    monkeypatch.setattr(web, "get_ad_account_map_path", lambda: str(tmp_path / "missing.csv"))
    body = json.loads(asyncio.run(web.api_publish_preflight(accounts="")).body)
    assert body["accounts"] == [] and body["text"] == ""
