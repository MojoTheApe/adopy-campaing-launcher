"""ACL-2344, шаг P1/P2 (вариант A'): проба «лёгкой» рабочей вкладки.

Ничего не уходит в Meta: документ `__acl_work` на origin Ads Manager отдаёт сам
браузер через `Fetch.fulfillRequest`, любой другой перехваченный запрос
обрывается (`Fetch.failRequest`). Вместо токена передаётся случайная строка-заглушка.
Наружу (в Python, лог, телеметрию) возвращаются только булевы значения и коды причин.
"""
from __future__ import annotations

import base64
import secrets
import time
from typing import Any

from src.fb_api.cdp import CdpError, PageTab

LIGHT_ORIGIN = "https://adsmanager.facebook.com"
LIGHT_URL = LIGHT_ORIGIN + "/__acl_work"
WORLD = "acl_light_probe"
_BODY = b"<!doctype html><meta charset=utf-8><title>ACL</title>"

_LISTEN_JS = """(() => {
  const g = globalThis, ch = new BroadcastChannel(%(name)s);
  g.__lp = {got: null, used: false, bad: 0};
  g.__lpDone = new Promise(res => { ch.onmessage = e => {
    const d = e.data;
    if (g.__lp.used || !d || d.n !== %(nonce)s) { g.__lp.bad++; return; }
    g.__lp.used = true; g.__lp.got = d.t; ch.close();
    const a = new BroadcastChannel(%(ack)s); a.postMessage({ack: true}); a.close(); res(true);
  }; });
  return true; })()"""

_SEND_JS = """(() => {
  globalThis.__lpAck = new Promise(r => { const a = new BroadcastChannel(%(ack)s);
    a.onmessage = e => { r(e.data && e.data.ack === true); a.close(); }; });
  const ch = new BroadcastChannel(%(name)s);
  ch.postMessage({n: %(nonce)s, t: %(dummy)s}); setTimeout(() => ch.close(), 300);
  return Promise.race([globalThis.__lpAck, new Promise(r => setTimeout(() => r(false), %(wait_ms)d))]);
})()"""


def _js(value: str) -> str:
    import json
    return json.dumps(value)


def open_light_document(tab: PageTab, *, timeout: float = 15.0) -> dict:
    """Открыть синтетический документ в `tab`; вернуть {ok, reason, blocked, fulfilled}."""
    seen = {"fulfilled": 0, "blocked": 0}

    def on_event(msg: dict) -> None:
        if msg.get("method") != "Fetch.requestPaused":
            return
        params = msg.get("params") or {}
        rid = params.get("requestId")
        url = str((params.get("request") or {}).get("url") or "")
        try:
            if url == LIGHT_URL and params.get("resourceType") == "Document":
                seen["fulfilled"] += 1
                tab.call("Fetch.fulfillRequest", {
                    "requestId": rid, "responseCode": 200,
                    "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"},
                                        {"name": "Cache-Control", "value": "no-store"}],
                    "body": base64.b64encode(_BODY).decode()}, timeout=timeout)
            else:
                seen["blocked"] += 1          # ни одного запроса наружу
                tab.call("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"},
                         timeout=timeout)
        except CdpError:
            pass

    ok, reason = False, "light_doc_failed"
    try:
        tab.call("Fetch.enable", {"patterns": [{"urlPattern": LIGHT_URL, "requestStage": "Request"}]},
                 timeout=timeout)
        try:
            tab.call("Page.navigate", {"url": LIGHT_URL}, timeout=timeout, on_event=on_event)
        finally:
            try:
                tab.call("Fetch.disable", timeout=timeout)
            except CdpError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = (tab.call("Page.getFrameTree").get("frameTree") or {}).get("frame") or {}
            url = str(frame.get("url") or "")
            if url and url != "about:blank":
                if url != LIGHT_URL:
                    reason = "light_doc_wrong_origin"
                    break
                state = tab.evaluate_in(tab.create_isolated_world(WORLD + "_rs"), "document.readyState",
                                        timeout=5.0)
                if state == "complete":
                    ok, reason = True, ""
                    break
            time.sleep(0.2)
    except CdpError:
        reason = "light_doc_failed"
    if ok and (seen["fulfilled"] != 1 or seen["blocked"]):
        ok, reason = False, "light_doc_unexpected_request"
    return {"ok": ok, "reason": reason, "fulfilled": seen["fulfilled"], "blocked": seen["blocked"]}


def handover_dummy(work: PageTab, capture: PageTab, *, wait_s: float = 5.0) -> dict:
    """Передать заглушку capture-мир → work-мир через BroadcastChannel; только булевы."""
    name, nonce, ack = (secrets.token_urlsafe(16) for _ in range(3))
    dummy = "DUMMY-" + secrets.token_urlsafe(12)
    out: dict[str, Any] = {"handover_ok": False, "reason": "no_listener"}
    wctx = work.create_isolated_world(WORLD)
    cctx = capture.create_isolated_world(WORLD)
    work.evaluate_in(wctx, _LISTEN_JS % {"name": _js(name), "nonce": _js(nonce), "ack": _js(ack)})
    acked = capture.evaluate_in(
        cctx, _SEND_JS % {"name": _js(name), "nonce": _js(nonce), "ack": _js(ack),
                          "dummy": _js(dummy), "wait_ms": int(wait_s * 1000)}, timeout=wait_s + 5)
    held = work.evaluate_in(wctx, "globalThis.__lp.got === %s" % _js(dummy), timeout=5.0)
    # тот же мир по тому же имени: заглушка должна пережить повторное создание
    again = work.evaluate_in(work.create_isolated_world(WORLD), "globalThis.__lp.got === %s" % _js(dummy),
                             timeout=5.0)
    main_world = work.call("Runtime.evaluate", {"expression": "typeof globalThis.__lp",
                                               "returnByValue": True}).get("result", {}).get("value")
    out.update(handover_ok=bool(acked and held), acked=bool(acked), same_world_survives=bool(again),
               main_world_clean=(main_world == "undefined"))
    out["reason"] = "" if out["handover_ok"] else ("timeout" if not acked else "bad_nonce")
    return out


def run_p1(cdp_url: str) -> dict:
    """P1+A'-часть P2: две свои вкладки, обе на синтетическом документе, передача заглушки.

    Запрос к Meta не уходит; токен не читается; вкладки закрываются в конце.
    """
    work = PageTab.open(cdp_url)
    capture = None
    try:
        capture = PageTab.open(cdp_url)
        w = open_light_document(work)
        c = open_light_document(capture)
        result: dict[str, Any] = {"work_doc": w, "capture_doc": c}
        if w["ok"] and c["ok"]:
            result["handover"] = handover_dummy(work, capture)
        return result
    finally:
        for tab in (capture, work):
            if tab is not None:
                try:
                    tab.close()
                except Exception:  # noqa: BLE001
                    pass


def _brief(reply: Any) -> dict:
    """Только форма ответа Graph: статус, код ошибки, число записей. Ни id, ни данных."""
    if not isinstance(reply, dict):
        return {"shape": "none"}
    body = reply.get("body") if isinstance(reply.get("body"), dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    data = body.get("data")
    return {"status": reply.get("status"), "helper_error": reply.get("error"),
            "graph_error_code": err.get("code"), "graph_error_subcode": err.get("error_subcode"),
            "rows": len(data) if isinstance(data, list) else None,
            "has_usage_header": bool(reply.get("headers"))}


def run_p3(cdp_url: str, act_id: str) -> dict:
    """P3: реальный токен capture-вкладки → рабочая лёгкая вкладка, затем ДВА чтения Graph.

    Порядок: лёгкая вкладка (к Meta не ходит) → одна загрузка Ads Manager (как у Check plan) →
    хэндовер → capture закрыта → `/me/adaccounts` и чтение кабинета из рабочей вкладки, по одному
    разу. Любой login/checkpoint/190/ошибка — стоп без повторов. Токен наружу не возвращается.
    """
    from src.fb_api.token_capture import InPageGraph

    result: dict[str, Any] = {"stopped_at": None}
    work = capture = None
    try:
        work = PageTab.open(cdp_url)
        result["work_doc"] = open_light_document(work)
        if not result["work_doc"]["ok"]:
            result["stopped_at"] = "light_doc"
            return result
        capture = PageTab.open(cdp_url)
        ig_c, ig_w = InPageGraph(capture), InPageGraph(work)
        t0 = time.monotonic()
        ig_c.navigate(act_id)
        state = ig_c.wait_ready(timeout=90.0)
        result["capture"] = {"kind": state.get("kind"), "has_token": bool(state.get("has_token")),
                             "source": state.get("source"), "open_s": round(time.monotonic() - t0, 1)}
        if state.get("kind") in ("login", "checkpoint") or not state.get("has_token"):
            result["stopped_at"] = "capture"
            return result
        name, nonce, ack = (secrets.token_urlsafe(16) for _ in range(3))
        listening = ig_w._eval_helper(f".listen({_js(name)},{_js(nonce)},{_js(ack)})", timeout=10.0)
        acked = bool(listening) and bool(ig_c._eval_helper(
            f".handOver({_js(name)},{_js(nonce)},{_js(ack)},8000)", timeout=20.0))
        got = ig_w.state()
        result["handover"] = {"listening": bool(listening), "acked": acked,
                              "work_has_token": bool(got.get("has_token")), "source": got.get("source")}
        if not (acked and got.get("has_token")):
            result["stopped_at"] = "handover"
            return result
        capture.close()
        capture = None
        result["capture_closed"] = True
        r1 = ig_w.request("GET", "/me/adaccounts", {"fields": "id", "limit": "1"})
        result["read_me_adaccounts"] = _brief(r1)
        if r1.get("status") != 200:
            result["stopped_at"] = "read_me_adaccounts"
            return result
        r2 = ig_w.request("GET", "/act_" + act_id.replace("act_", ""), {"fields": "account_status"})
        result["read_act"] = _brief(r2)
        if r2.get("status") != 200:
            result["stopped_at"] = "read_act"
        return result
    except Exception as exc:  # noqa: BLE001 - отчёт без тела ошибки (там могут быть адреса)
        result["stopped_at"] = type(exc).__name__
        return result
    finally:
        for tab in (capture, work):
            if tab is not None:
                try:
                    tab.close()
                except Exception:  # noqa: BLE001
                    pass
