"""Pages и Pixels аккаунта читаются одной сессией профиля (ACL-2850).

Один вход в профиль, один токен, два чтения только на чтение (`/{act}/adspixels` и Страницы). Нажатие
Pixel / Refresh pixels заодно заполняет кэш Страниц, нажатие Page / Refresh pages — кэш пикселей, если
кэш второго списка пуст или устарел. У каждого списка свой результат и своя ошибка. Таймеров и опроса нет.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterator

_KINDS = ("pixels", "pages")
_FAILED = {
    "pixels": "Could not load pixels. Refresh pixels to retry. ",
    "pages": "Could not load Facebook Pages. Refresh pages to retry. ",
}


def _module(kind: str):
    from src.web import account_pages, account_pixels

    return account_pixels if kind == "pixels" else account_pages


def _cached(kind: str, account: str):
    mod = _module(kind)
    return (mod.cached_pixels if kind == "pixels" else mod.cached_pages)(account)


def _remember(kind: str, account: str, result: dict) -> None:
    mod = _module(kind)
    (mod.remember_pixels if kind == "pixels" else mod.remember_pages)(account, result)


def _job(account: str, primary: str, force: bool, queued: float) -> dict:
    """Внутри очереди профиля: список `kind` -> результат (dict) или исключение."""
    from src.analysis_agent import account_spend_api as api

    began = time.monotonic()
    wait_s = began - queued     # ждали, пока освободится профиль (замок/очередь чтений)
    # Пока мы стояли в очереди, соседний запрос мог уже заполнить кэши: берём их, профиль не открываем.
    want = [k for k in _KINDS if (force and k == primary) or _cached(k, account) is None]
    if primary not in want:
        return {primary: _cached(primary, account), "_skipped": True}
    out: dict[str, Any] = {}
    notes: dict = {}
    capture_s = call_s = 0.0
    opened_at = time.monotonic()
    try:
        with api._session(account) as sess:
            capture_s = time.monotonic() - opened_at
            try:
                for kind in sorted(want, key=lambda k: k != primary):
                    started = time.monotonic()
                    try:
                        out[kind] = _module(kind)._read_in_session(sess, account)
                    except Exception as exc:      # noqa: BLE001 - у каждого списка своя ошибка
                        out[kind] = exc
                    call_s += time.monotonic() - started
            finally:
                notes = dict(getattr(sess, "notes", None) or {})
                notes.setdefault("calls", getattr(sess.client, "calls", 0))
    except Exception as exc:                      # сессия не открылась: обоим спискам одна причина
        if not capture_s:
            capture_s = time.monotonic() - opened_at
        for kind in want:
            out.setdefault(kind, exc)
    failed = next((v for v in out.values() if isinstance(v, BaseException)), None)
    try:
        api._telemetry("pixels" if "pixels" in want else "pages", account, failed is None,
                       wait_s + capture_s + call_s, notes,
                       error=api._failure(account, failed)[0] if failed else "",
                       extra={"wait_s": round(wait_s, 1), "capture_s": round(capture_s, 1),
                              "call_s": round(call_s, 1), "lists": "+".join(k for k in _KINDS if k in want)})
    except Exception:
        pass
    return out


def read_lists(account: str, primary: str, *, force: bool = False) -> dict:
    """Результат списка `primary`; кэш второго списка заполняется в той же сессии, если он пуст."""
    from src.analysis_agent import account_spend as accounts
    from src.analysis_agent import account_spend_api as api
    from src.fb_api.connection import user_started_action

    if not force:
        hit = _cached(primary, account)
        if hit is not None:
            return hit
    queued = time.monotonic()
    read = accounts.serialized_browser(key=accounts._profile_key, reading=True)(
        lambda label: _job(label, primary, force, queued))
    try:
        # ACL-2272: список открывает человек (Pixel / Page / Refresh); отрисовка - `cached_only`.
        with user_started_action():
            out = read(account)
    except Exception as exc:
        return _failed(account, primary, exc, api)
    for kind in _KINDS:
        got = out.get(kind)
        if isinstance(got, dict) and not out.get("_skipped"):
            _remember(kind, account, got)
    got = out.get(primary)
    if isinstance(got, BaseException):
        return _failed(account, primary, got, api)
    return got


def _failed(account: str, kind: str, exc: BaseException, api) -> dict:
    return {"ok": False, "state": "failed", kind: [], "account": account,
            "message": _FAILED[kind] + api._failure(account, exc)[0]}


def stream(account: str, primary: str, *, force: bool = False) -> Iterator[dict]:
    """{"progress": строка ожидания} по мере смены, затем {"result": ответ}. Без таймеров и опроса."""
    from src.analysis_agent import account_spend as accounts

    box: dict = {}
    done = threading.Event()

    def work() -> None:
        try:
            box["result"] = read_lists(account, primary, force=force)
        except BaseException as exc:     # noqa: BLE001 - человеку уходит ответ-ошибка
            box["result"] = {"ok": False, "state": "failed", primary: [], "account": account,
                             "message": _FAILED[primary] + str(exc)}
        finally:
            done.set()
            accounts.wake_wait_watchers()

    threading.Thread(target=work, name=f"read-{primary}-{account}", daemon=True).start()
    for note in accounts.wait_note_changes(account, done.is_set):
        yield {"progress": note}
    yield {"result": box["result"]}
