"""ACL-2344, шаг P1/P2 (вариант A'): проба «лёгкой» рабочей вкладки.

Ничего не уходит в Meta: документ `__acl_work` на origin Ads Manager отдаёт сам
браузер через `Fetch.fulfillRequest`, ЛЮБОЙ другой запрос вкладки (шаблон `*`)
обрывается (`Fetch.failRequest`); перехват не снимается, пока жива вкладка. Вместо токена передаётся случайная строка-заглушка.
Модуль живёт вне `src/fb_api`: там строка `Network.enable` запрещена тестом (в продакшн-коде нет перехвата
сети и токенов). Здесь Network включается ТОЛЬКО на вкладке пробы: без него `setBypassServiceWorker` не
действует. События Network не читаются, токены не трогаются.
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
_CSP = "default-src 'none'; script-src 'none'; worker-src 'none'"
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


def open_light_document(tab: PageTab, *, timeout: float = 30.0, settle_s: float = 60.0) -> dict:
    """Открыть синтетический документ в `tab`; вернуть {ok, reason, blocked, fulfilled}.

    Перехват (`Fetch.enable`, шаблон `*`) включён на всё время жизни вкладки: он НЕ выключается
    после загрузки. Документ отдаёт браузер, всякий другой запрос обрывается; события
    `Fetch.requestPaused` обрабатываются при каждом вызове вкладки (`tab.on_event`) и в `tab.pump`.
    Причина отказа — код вида `light_doc_failed:<метод CDP>`, без текста ошибки и данных.
    """
    seen = {"fulfilled": 0, "blocked": 0, "errors": 0}
    tab.light_seen = seen

    def on_event(msg: dict) -> None:
        if msg.get("method") != "Fetch.requestPaused":
            return
        params = msg.get("params") or {}
        rid = params.get("requestId")
        url = str((params.get("request") or {}).get("url") or "")
        try:
            if url == LIGHT_URL and params.get("resourceType") == "Document" and not seen["fulfilled"]:
                seen["fulfilled"] += 1
                tab.call("Fetch.fulfillRequest", {
                    "requestId": rid, "responseCode": 200,
                    "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"},
                                        {"name": "Cache-Control", "value": "no-store"},
                                        # документ инертен: ни скриптов, ни воркеров, ни подресурсов
                                        *([{"name": "Content-Security-Policy", "value": _CSP}] if _CSP else [])],
                    "body": base64.b64encode(_BODY).decode()}, timeout=timeout)
            else:
                seen["blocked"] += 1          # ни одного запроса наружу
                tab.call("Fetch.failRequest", {"requestId": rid, "errorReason": "BlockedByClient"},
                         timeout=timeout)
        except (CdpError, TimeoutError):
            seen["errors"] += 1

    step = "Fetch.enable"
    ok, reason = False, "light_doc_failed"
    try:
        tab.on_event = on_event
        # service worker профиля не должен отвечать/ходить в сеть мимо перехвата;
        # без Network.enable setBypassServiceWorker не действует (проверено на локальном SW)
        step = "Network.enable"
        tab.call("Network.enable", timeout=timeout)
        step = "Network.emulateNetworkConditions"
        # запасной рубеж: даже освобождённый запрос упрётся в «офлайн»
        tab.call("Network.emulateNetworkConditions", {"offline": True, "latency": 0,
                                                      "downloadThroughput": -1, "uploadThroughput": -1},
                 timeout=timeout)
        step = "Network.setBypassServiceWorker"
        tab.call("Network.setBypassServiceWorker", {"bypass": True}, timeout=timeout)
        step = "Fetch.enable"
        tab.call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                 timeout=timeout)
        step = "Page.navigate"
        tab.call("Page.navigate", {"url": LIGHT_URL}, timeout=timeout)
        deadline = time.monotonic() + settle_s
        while time.monotonic() < deadline:
            step = "Page.getFrameTree"
            frame = (tab.call("Page.getFrameTree", timeout=timeout).get("frameTree") or {}).get("frame") or {}
            url = str(frame.get("url") or "")
            if url and url != "about:blank":
                if url != LIGHT_URL:
                    reason = "light_doc_wrong_origin"
                    break
                step = "Runtime.evaluate"
                state = tab.evaluate_in(tab.create_isolated_world(WORLD + "_rs"), "document.readyState",
                                        timeout=timeout)
                if state == "complete":
                    ok, reason = True, ""
                    break
            step = "pump"
            tab.pump(0.2)
        else:
            reason = "light_doc_timeout"
    except (CdpError, TimeoutError):
        reason = "light_doc_failed:" + step
    if ok and seen["fulfilled"] != 1:
        ok, reason = False, "light_doc_unexpected_request"
    return {"ok": ok, "reason": reason, "fulfilled": seen["fulfilled"], "blocked": seen["blocked"]}


def settle_before_close(tab: PageTab, seconds: float = 0.5) -> None:
    """Разобрать оставшиеся события перехвата до закрытия вкладки: ничего не остаётся «на паузе»."""
    try:
        tab.pump(seconds)
    except (CdpError, TimeoutError):
        pass


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


# ── P5 (redone, ACL-2344 review): request headers of the Graph call, compared WITHOUT the token and WITHOUT
# any request reaching Meta. Both tabs run the same fetch as the helper (same options) to a sentinel path of
# the captured Graph host; a Fetch pattern for exactly that path pauses it, the headers are read and the
# request is failed before the network. No token is in that URL, `Network.*` is never enabled, and the
# pattern matches nothing else of the tab (Ads Manager's own Graph calls never reach Python).
HEADER_PROBE_PATH = "/__acl_header_probe"
#: Header values that are never kept (only their presence).
_HIDDEN_HEADERS = frozenset({"cookie", "authorization", "x-fb-lsd", "x-asbd-id", "x-fb-friendly-name"})
_HEADER_FETCH_JS = """fetch(%(url)s, {method: 'GET', credentials: 'include', mode: 'cors', cache: 'no-store',
  redirect: 'error'}).then(() => 'sent', () => 'failed')"""


def _scrub_header(name: str, value: str) -> str:
    low = name.lower()
    if low in _HIDDEN_HEADERS:
        return "<present>"
    if low in ("referer", "origin"):
        from urllib.parse import urlsplit
        parts = urlsplit(str(value))
        # origin and whether a path follows - never the path (it may carry the account id)
        return f"{parts.scheme}://{parts.hostname}" + ("/<path>" if parts.path not in ("", "/") or parts.query else "/")
    return str(value)


def request_headers(tab: Any, graph_host: str, *, timeout: float = 20.0) -> dict:
    """Headers of one helper-like fetch from `tab` to the sentinel of `graph_host`; the request never leaves.

    A light tab keeps its origin interception: both patterns are on while the probe runs, and only the
    sentinel is handled here. Returns {name: scrubbed value} or {} when nothing was seen.
    """
    url = "https://" + graph_host + HEADER_PROBE_PATH
    seen: dict = {}
    doc = getattr(tab, "light", None)
    before = tab.on_event

    def on_event(msg: dict) -> None:
        params = msg.get("params") or {}
        req = params.get("request") or {}
        if msg.get("method") == "Fetch.requestPaused" and str(req.get("url") or "").split("?")[0] == url:
            if not seen:
                seen.update({str(k).lower(): _scrub_header(str(k), v) for k, v in (req.get("headers") or {}).items()})
            try:
                tab.call("Fetch.failRequest", {"requestId": params.get("requestId"), "errorReason": "BlockedByClient"},
                         timeout=10.0)
            except (CdpError, TimeoutError):
                pass
        elif before is not None:
            before(msg)

    patterns = [{"urlPattern": url + "*", "requestStage": "Request"}]
    if doc is not None:
        from src.fb_api import light_tab as _lt
        patterns.append({"urlPattern": _lt._origin_pattern(), "requestStage": "Request"})
    tab.on_event = on_event
    try:
        tab.call("Fetch.enable", {"patterns": patterns}, timeout=timeout)
        ctx = tab.create_isolated_world(WORLD + "_hdr")
        tab.evaluate_in(ctx, _HEADER_FETCH_JS % {"url": _js(url)}, timeout=timeout)
    finally:
        tab.on_event = before
        if doc is not None:
            doc.intercept()                       # the light tab's own interception, unchanged
        else:
            try:
                tab.call("Fetch.disable", timeout=timeout)
            except (CdpError, TimeoutError):
                pass
    return seen


def compare_headers(am: dict, light: dict) -> dict:
    """Only names and the scrubbed Referer/Origin/Sec-Fetch values go out."""
    names = sorted(set(am) | set(light))
    differ = [n for n in names if am.get(n) != light.get(n)]
    keep = [n for n in names if n in ("referer", "origin") or n.startswith("sec-fetch-")]
    return {"seen_ads_manager": bool(am), "seen_light": bool(light), "same": [n for n in names if n not in differ],
            "differ": differ, "values": {n: {"ads_manager": am.get(n), "light": light.get(n)} for n in keep}}


def run_p5(cdp_url: str, act_id: str, profile_id: str = "", *, _gate: bool = True) -> dict:
    """P5 live: ONE Ads Manager load in a capture tab (as a Check plan does), a light tab, the sentinel fetch in
    both. No Graph call is made and no token is handed over. Any login/checkpoint stops it."""
    from src.fb_api import light_tab as _lt
    from src.fb_api.token_capture import InPageGraph

    if _gate:
        require_pilot(cdp_url, profile_id)
    out: dict[str, Any] = {"stopped_at": None}
    work = capture = None
    try:
        work, doc, reason = _lt.open_work_tab(cdp_url)
        if work is None:
            out["stopped_at"] = reason
            return out
        capture = PageTab.open(cdp_url)
        ig = InPageGraph(capture)
        ig.navigate(act_id)
        state = ig.wait_ready(timeout=90.0)
        out["capture"] = {"kind": state.get("kind"), "has_token": bool(state.get("has_token"))}
        if state.get("kind") != "ads_manager" or not state.get("has_token"):
            out["stopped_at"] = "capture"
            return out
        host = str(state.get("graph_host") or "graph.facebook.com")
        out["graph_host_kind"] = "public" if host == "graph.facebook.com" else "captured"
        out["headers"] = compare_headers(request_headers(capture, host), request_headers(work, host))
        return out
    except Exception as exc:  # noqa: BLE001 - only the class name: texts may carry addresses
        out["stopped_at"] = type(exc).__name__
        return out
    finally:
        for tab in (capture, work):
            if tab is not None:
                _lt.settle_and_close(tab)


#: Pilot accounts the owner allowed for the live ACL-2344 probes (account-map name, lower case).
PILOT_ACCOUNTS = ("doc120", "doc283")


# ── P1/P3 again, on the FINAL code (`src/fb_api/light_tab.py` + the helper of `InPageGraph`), after review.
# P1: the light document, origin requests answered locally, a reload noticed - nothing goes to Meta.
# P3: the production lease path (capture tab -> light tab -> hand-over -> capture closed) and the two reads a
# Check plan makes. Only codes, counts and HTTP statuses go out.

def _origin_fetch(tab: Any) -> str:
    ctx = tab.create_isolated_world(WORLD + "_origin")
    return str(tab.evaluate_in(ctx, "fetch('/__acl_probe_origin', {cache: 'no-store'})"
                                    ".then(() => 'sent', () => 'blocked')", timeout=20.0))


def run_final_p1(cdp_url: str, profile_id: str = "", *, _gate: bool = True) -> dict:
    """The light tab of the final code: our document, an origin fetch and a reload answered locally."""
    from src.fb_api import light_tab as _lt

    if _gate:
        require_pilot(cdp_url, profile_id)
    out: dict[str, Any] = {}
    work, doc, reason = _lt.open_work_tab(cdp_url)
    if work is None:
        return {"light_doc": reason}
    try:
        out["light_doc"] = "ok"
        out["intact_after_open"] = doc.document_intact() or "ok"
        out["origin_fetch"] = _origin_fetch(work)
        work.pump(0.5)
        before = doc.fulfilled
        work.call("Page.reload", {"ignoreCache": True}, timeout=20.0)   # as an F5 of the manager would
        deadline = time.monotonic() + 15.0
        while doc.fulfilled == before and time.monotonic() < deadline:
            work.pump(0.2)
        work.pump(1.0)
        out["reload_served_locally"] = doc.fulfilled == before + 1
        out["reload_noticed"] = doc.document_intact() == "light_doc_replaced"
        out["counts"] = {"fulfilled": doc.fulfilled, "blocked": doc.blocked, "errors": doc.errors}
        return out
    finally:
        _lt.settle_and_close(work)


def _read_summary(reply: Any) -> dict:
    reply = reply if isinstance(reply, dict) else {}
    body = reply.get("body") if isinstance(reply.get("body"), dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    out = {"status": reply.get("status"), "error": reply.get("error"), "graph_error_code": err.get("code")}
    if isinstance(body.get("data"), list):
        out["rows"] = len(body["data"])
    return out


def run_final_p3(cdp_url: str, act_id: str, profile_id: str = "", *, _gate: bool = True) -> dict:
    """The production light lease path once: ONE Ads Manager load, hand-over, capture closed, two reads."""
    from src.fb_api import light_tab as _lt
    from src.fb_api.token_capture import InPageGraph, act_digits

    if _gate:
        require_pilot(cdp_url, profile_id)
    out: dict[str, Any] = {"stopped_at": None}
    work = capture = None
    try:
        work, doc, reason = _lt.open_work_tab(cdp_url)
        if work is None:
            out["stopped_at"] = reason
            return out
        capture = PageTab.open(cdp_url)
        cap = InPageGraph(capture)
        cap.install()
        cap.navigate(act_id)
        state = cap.wait_ready(timeout=90.0)
        out["capture"] = {"kind": state.get("kind"), "has_token": bool(state.get("has_token"))}
        if state.get("kind") != "ads_manager" or not state.get("has_token"):
            out["stopped_at"] = "capture"
            return out
        graph = InPageGraph(work)
        graph.install()
        graph.bind_act(act_id)
        out["handover"] = graph.receive_from(cap) or "ok"
        try:
            cap._eval_helper(".retire()", timeout=10.0)
        except Exception:  # noqa: BLE001
            pass
        _lt.settle_and_close(capture)
        capture = None
        out["capture_closed"] = True
        if out["handover"] != "ok":
            out["stopped_at"] = "handover"
            return out
        st = graph.state()
        out["work_state"] = {"kind": st.get("kind"), "has_token": bool(st.get("has_token")), "light": st.get("light")}
        r1 = _read_summary(graph.request("GET", "/me/adaccounts", {"fields": "id", "limit": "1"}, timeout=15))
        out["read_adaccounts"] = r1
        if r1.get("status") != 200:
            out["stopped_at"] = "read_adaccounts"
            return out
        out["read_account"] = _read_summary(graph.request("GET", "/act_" + act_digits(act_id),
                                                          {"fields": "account_status"}, timeout=15))
        out["intact_after_reads"] = doc.document_intact() or "ok"
        out["counts"] = {"fulfilled": doc.fulfilled, "blocked": doc.blocked, "errors": doc.errors}
        return out
    finally:
        for t in (capture, work):
            _lt.settle_and_close(t)


def pilot_profile_ids() -> set:
    import csv

    from config.paths import get_ad_account_map_path

    try:
        with open(get_ad_account_map_path(), encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return set()
    ids = set()
    for key in PILOT_ACCOUNTS:
        found = {str(r.get("profile_id") or "").strip() for r in rows
                 if key in str(r.get("account_name") or "").lower()} - {""}
        if len(found) == 1:
            ids |= found
    return ids


def require_pilot(cdp_url: str, profile_id: str) -> None:
    """Loopback port and an explicit profile_id of one of `PILOT_ACCOUNTS`."""
    from urllib.parse import urlparse

    host = urlparse(cdp_url if "://" in cdp_url else "http://" + cdp_url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ProbeGateError("probe_not_loopback")
    if not profile_id or profile_id not in pilot_profile_ids():
        raise ProbeGateError("probe_not_pilot")


class ProbeGateError(PermissionError):
    """Проба разрешена только на профиле Doc120; причина — код без данных."""


def doc120_profile_id() -> str:
    """profile_id аккаунта Doc120 из карты аккаунтов машины; пусто, если не найден однозначно."""
    import csv

    from config.paths import get_ad_account_map_path

    try:
        with open(get_ad_account_map_path(), encoding="utf-8-sig", newline="") as f:
            ids = {str(r.get("profile_id") or "").strip() for r in csv.DictReader(f)
                   if "doc120" in str(r.get("account_name") or "").lower()}
    except OSError:
        return ""
    ids.discard("")
    return ids.pop() if len(ids) == 1 else ""


def require_doc120(cdp_url: str, profile_id: str) -> None:
    """Ворота пробы: loopback-порт и явный profile_id, равный profile_id Doc120 из карты аккаунтов.

    Офлайн привязать порт к профилю нельзя, поэтому profile_id — обязательный явный аргумент.
    """
    from urllib.parse import urlparse

    host = urlparse(cdp_url if "://" in cdp_url else "http://" + cdp_url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ProbeGateError("probe_not_loopback")
    expected = doc120_profile_id()
    if not expected:
        raise ProbeGateError("probe_doc120_unknown")
    if not profile_id or profile_id != expected:
        raise ProbeGateError("probe_not_doc120")


def run_p1(cdp_url: str, profile_id: str = "", *, _gate: bool = True) -> dict:
    """P1+A'-часть P2: две свои вкладки, обе на синтетическом документе, передача заглушки.

    Запрос к Meta не уходит; токен не читается; вкладки закрываются в конце.
    """
    if _gate:       # только тесты на своём браузере с мёртвым прокси отключают ворота
        require_doc120(cdp_url, profile_id)
    work = PageTab.open(cdp_url, call_s=60.0)
    capture = None
    try:
        capture = PageTab.open(cdp_url, call_s=60.0)
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
                    settle_before_close(tab)
                    tab.close()
                except Exception:  # noqa: BLE001
                    pass
