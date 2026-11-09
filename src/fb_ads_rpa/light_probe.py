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
