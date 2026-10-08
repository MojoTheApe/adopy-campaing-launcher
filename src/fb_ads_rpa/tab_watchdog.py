"""Сторож вкладки: стек зависшего вызова и разблокировка через CDP браузера (ACL-1702).

Молчаливые зависания шли на разных шагах и на сборке 1010 с ACL-1641:
KLD_BFL_B97 (0ab4aecb8e3b, 07.10) — 18 мин тишины после `ad_destination`;
B96 — 8 мин в `ad3_destination`; Doc120 — 5,6 мин после `next_to_adset`.
Ограничивать вызовы по одному (ACL-1641, ACL-1673) бесконечно: любой вызов
Playwright без таймаута на зависшем рендерере держит поток задания.
`step_stuck` (ACL-1614) только сигналит.

Здесь две вещи.

1. **Стек.** На `step_stuck` — стек потока задания в
   `output/stuck_stacks/<run>_<ts>.txt` вместе с последним шагом и адресом
   вкладки. Важно: sync Playwright ждёт ответа, переключаясь в greenlet
   диспетчера, и `sys._current_frames()[поток]` показывает тогда лишь цикл
   событий (`select`). Настоящий вызов — в кадре приостановленного главного
   greenlet потока (`gr_frame`); его запоминаем при старте задания.

2. **Разблокировка.** Поток стоит в ОДНОМ вызове Playwright дольше
   `ACL_STUCK_UNBLOCK_S` (240 с) — поток сторожа открывает СВОЁ соединение
   CDP к браузеру профиля (сырой websocket на тот же порт отладки) и
   закрывает ЭТУ вкладку (`Target.closeTarget`, свой таймаут на вызов).
   Перезагрузки и terminate нет (ревью PR #704): reload переоткрыл бы
   редактор или выбросил начатое создание, terminate на зависшем рендерере
   не отвечает.

   Почему не объекты Playwright задания: sync Playwright привязан к потоку
   (greenlet и цикл событий потока задания). Вызов из чужого потока — это
   переключение в чужой greenlet (greenlet.error) или, хуже, работа с циклом
   событий, который крутит другой поток; закрытие соединения из чужого
   потока раскрутило поток задания в sync-цикле с GIL в руках (ACL-1614).
   Сырой websocket — отдельное TCP-соединение к Chromium: общего состояния с
   Playwright задания нет, команды домена Target исполняет сам браузер, без
   рендерера. Соединение Playwright задания не трогаем вовсе — заблокированный
   вызов получает обычную ошибку от драйвера и возвращается сам.

   Перед действием вкладка помечается «зависла» (`page_liveness.tab_gone` её
   видит) — стражи ACL-1673 останавливают работу объявления; паузы и проверки
   сроков потока кампании бросают `FlowBudgetExceeded("tab_hung")` (жёсткая
   остановка, мимо `except Exception`), а поток кампании (`take_hung`)
   называет итог `tab_hung:<шаг>`. Не трогаем: другие вкладки и
   профили, сам браузер и профиль Multilogin; ничего не делаем при
   перехвате человеком или ожидании обучения (`pacing`). Ни одного вызова к
   объектам Playwright из потока сторожа — только чтение полей.

`ACL_STUCK_UNBLOCK=off` выключает разблокировку (стек пишется всё равно).
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import traceback
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

#: Сколько секунд поток может стоять в одном вызове Playwright без строки
#: телеметрии, прежде чем сторож разблокирует вкладку. Ревью PR #704: выше
#: 180 с таймаута загрузки файла (`_PW_SET_FILES_MS`), с запасом.
UNBLOCK_S = 240.0
#: Таймаут каждого вызова CDP сторожа (подключение, getTargets, attach, …).
CALL_S = 5.0
#: Сколько ждать после каждого способа, вернулся ли заблокированный вызов.
SETTLE_S = 10.0
#: Больше разблокировок на одно задание не делаем: что-то не так системно.
MAX_PER_JOB = 3
#: Файлов стеков храним не больше стольких и не дольше стольких дней.
STACKS_KEEP = 200
STACKS_KEEP_DAYS = 14.0

_HUNG_ATTR = "_acl_hung_step"


def enabled() -> bool:
    """Разблокировка включена (по умолчанию да); `ACL_STUCK_UNBLOCK=off` — нет."""
    value = (os.environ.get("ACL_STUCK_UNBLOCK") or "").strip().lower()
    return value not in ("off", "0", "false", "no", "disabled")


def unblock_after_s() -> float:
    try:
        value = float((os.environ.get("ACL_STUCK_UNBLOCK_S") or "").strip())
    except ValueError:
        return UNBLOCK_S
    return value if value > 0 else UNBLOCK_S


def stacks_dir() -> Path:
    """`output/stuck_stacks` рядом с журналом телеметрии (`<workspace>/output`).

    От `run_telemetry.TELEMETRY_PATH`, а не заново от рабочей папки: тесты
    подменяют этот путь (conftest), и стеки уходят туда же, а не в `output/`.
    """
    from src.fb_ads_rpa import run_telemetry

    return Path(run_telemetry.TELEMETRY_PATH).parent / "stuck_stacks"


# ── Поток задания ─────────────────────────────────────────────────────────────

@dataclass
class JobThread:
    """Поток задания, каким его видит сторож: id и главный greenlet."""

    tid: int
    greenlet_ref: Any = None   # weakref на главный greenlet потока или None

    def greenlet(self):
        ref = self.greenlet_ref
        return ref() if ref is not None else None


#: id потока задания → адрес CDP браузера, к которому он подключён.
_ENDPOINTS: dict[int, str] = {}
#: id потока задания → шаг, на котором сторож разблокировал его вкладку.
_HUNG: dict[int, str] = {}
#: id потоков заданий (адрес CDP запоминаем только за ними).
_JOB_TIDS: set[int] = set()
_lock = threading.Lock()


def this_thread() -> JobThread:
    """Запомнить ТЕКУЩИЙ поток как поток задания (зовёт сам поток задания)."""
    ref = None
    try:
        import greenlet as _gl

        ref = weakref.ref(_gl.getcurrent())
    except Exception:
        ref = None
    tid = threading.get_ident()
    with _lock:
        _JOB_TIDS.add(tid)
    return JobThread(tid=tid, greenlet_ref=ref)


def note_endpoint(cdp_url: str) -> None:
    """Подключились к браузеру — запомнить адрес CDP для потока задания.

    Ревью PR #704 (m3): только за потоком задания (`this_thread`); прочие
    потоки (чтение расходов и т. п.) сюда не пишут — их id Windows раздаёт
    заново, и запись осталась бы висеть.
    """
    if not cdp_url:
        return
    tid = threading.get_ident()
    with _lock:
        if tid in _JOB_TIDS:
            _ENDPOINTS[tid] = str(cdp_url)


def forget_endpoint() -> None:
    """Соединение задания сброшено (переподключение, конец) — адрес забыть."""
    with _lock:
        _ENDPOINTS.pop(threading.get_ident(), None)


def endpoint_of(tid: int) -> str:
    with _lock:
        return _ENDPOINTS.get(tid, "")


def forget_thread(tid: int) -> None:
    with _lock:
        _ENDPOINTS.pop(tid, None)
        _HUNG.pop(tid, None)
        _JOB_TIDS.discard(tid)


def take_hung() -> str:
    """Шаг, на котором сторож разблокировал вкладку ЭТОГО потока ("" — не было).

    Зовёт поток кампании: после разблокировки итог — `tab_hung:<шаг>`, без
    повтора кампании. Отметка снимается — следующая кампания идёт с чистого.
    """
    with _lock:
        return _HUNG.pop(threading.get_ident(), "")


def peek_hung() -> str:
    """Шаг разблокировки этого потока, не снимая отметки. Дёшево: без замка
    (одно чтение dict под GIL) — зовётся в каждой паузе потока кампании."""
    return _HUNG.get(threading.get_ident(), "")


def page_marked_hung(page) -> bool:
    """Вкладку разблокировал сторож — для страж «вкладки нет» (ACL-1673)."""
    try:
        return bool(vars(page).get(_HUNG_ATTR))
    except TypeError:
        return False


def clear_page_mark(page) -> None:
    """Снять отметку: кампания кончилась `tab_hung`, вкладка (если жива) снова наша."""
    try:
        vars(page).pop(_HUNG_ATTR, None)
    except TypeError:
        pass


def _mark(job: JobThread, page, step: str) -> None:
    with _lock:
        _HUNG[job.tid] = step or "unknown"
    if page is not None:
        try:
            # Только запись в __dict__ объекта — не вызов Playwright.
            vars(page)[_HUNG_ATTR] = step or "unknown"
        except TypeError:
            pass


# ── Где стоит поток ──────────────────────────────────────────────────────────

def top_frame(job: JobThread):
    """Верхний кадр настоящей работы потока задания.

    Главный greenlet приостановлен (поток ждёт ответа драйвера) — его
    `gr_frame`; иначе — кадр самого потока.
    """
    g = job.greenlet()
    if g is not None:
        try:
            frame = g.gr_frame
        except Exception:
            frame = None
        if frame is not None:
            return frame, True
    return sys._current_frames().get(job.tid), False


def _is_sync_base(frame) -> bool:
    name = frame.f_code.co_filename.replace("\\", "/")
    return name.endswith("playwright/_impl/_sync_base.py") and frame.f_code.co_name == "_sync"


def _is_sync_api_obj(obj) -> bool:
    return type(obj).__module__.startswith("playwright.sync_api")


def _impl_page(impl):
    """Страница (impl) объекта Playwright по его полям, без вызовов драйвера."""
    for _ in range(4):
        if impl is None:
            return None
        if type(impl).__name__ == "Page" and hasattr(impl, "_main_frame"):
            return impl
        nxt = getattr(impl, "_page", None)
        if nxt is None:
            nxt = getattr(impl, "_frame", None)
        impl = nxt
    return None


def _sync_page_of(impl_page):
    if impl_page is None:
        return None
    try:
        from playwright._impl._impl_to_api_mapping import API_ATTR

        return getattr(impl_page, API_ATTR, None)
    except Exception:
        return None


def page_url(page) -> str:
    """Адрес вкладки, который Playwright помнит у себя (поле, не вызов)."""
    impl = getattr(page, "_impl_obj", page)
    try:
        return str(getattr(getattr(impl, "_main_frame", None), "_url", "") or "")
    except Exception:
        return ""


@dataclass
class BlockedCall:
    """Вызов Playwright, в котором стоит поток задания."""

    task_ref: Any                  # weakref на asyncio.Task вызова
    api: str = ""                  # «Page.evaluate», «Locator.click», …
    page: Any = None               # sync Page вкладки
    url: str = ""
    where: str = ""                # место в нашем коде: файл:строка функция

    def task(self):
        return self.task_ref() if self.task_ref is not None else None

    def done(self) -> bool:
        task = self.task()
        if task is None:
            return True
        try:
            return bool(task.done())
        except Exception:
            return True

    def same(self, other: "BlockedCall | None") -> bool:
        return other is not None and self.task() is not None and other.task() is self.task()


def blocked_call(job: JobThread) -> BlockedCall | None:
    """Поток стоит в вызове sync Playwright — какой это вызов и чья вкладка.

    Только чтение кадров и полей объектов; к драйверу не обращается.
    """
    frame, suspended = top_frame(job)
    if frame is None or not suspended:
        return None
    sync = None
    f = frame
    while f is not None:
        if _is_sync_base(f):
            sync = f
            break
        f = f.f_back
    if sync is None:
        return None
    try:
        task = sync.f_locals.get("task")
        task_ref = weakref.ref(task) if task is not None else None
    except Exception:
        task_ref = None
    if task_ref is None:
        return None
    call = BlockedCall(task_ref=task_ref)
    page = None
    f = sync.f_back
    while f is not None:
        try:
            local = f.f_locals
            me = local.get("self")
        except Exception:
            local, me = {}, None
        if not call.api and me is not None and _is_sync_api_obj(me):
            call.api = f"{type(me).__name__}.{f.f_code.co_name}"
            page = _sync_page_of(_impl_page(getattr(me, "_impl_obj", None)))
        name = f.f_code.co_filename.replace("\\", "/")
        if "/playwright/" not in name:
            if not call.where:
                call.where = f"{Path(name).name}:{f.f_lineno} {f.f_code.co_name}"
            if page is None:
                cand = local.get("page")
                if cand is not None and _is_sync_api_obj(cand) and type(cand).__name__ == "Page":
                    page = cand
            if page is not None and call.where:
                break
        f = f.f_back
    call.page = page
    call.url = page_url(page) if page is not None else ""
    return call


def format_stack(job: JobThread) -> str:
    frame, suspended = top_frame(job)
    if frame is None:
        return "(the job thread has no frames - it has ended)\n"
    head = ("job thread is waiting inside a Playwright call (main greenlet suspended)\n"
            if suspended else "job thread is running Python (no Playwright call in flight)\n")
    text = head + "".join(traceback.format_stack(frame))
    if suspended:
        own = sys._current_frames().get(job.tid)
        if own is not None:
            text += "\n-- thread frame (Playwright dispatcher) --\n" + "".join(traceback.format_stack(own))
    return text


def dump_stack(job: JobThread, *, run_id: str, step: str, reason: str,
               idle_s: float | None = None, out_dir: Path | None = None) -> Path | None:
    """Стек потока задания — в `output/stuck_stacks/<run>_<ts>.txt`. Не бросает."""
    try:
        call = blocked_call(job)
        url = call.url if call is not None else ""
        lines = [
            f"reason: {reason}",
            f"run: {run_id or '-'}",
            f"time_utc: {datetime.now(timezone.utc).isoformat()}",
            f"last_step: {step or '-'}",
        ]
        if idle_s is not None:
            lines.append(f"idle_s: {round(float(idle_s), 1)}")
        if call is not None:
            lines.append(f"blocked_call: {call.api or '?'} at {call.where or '?'}")
        lines.append(f"page_url: {_safe_url(url) or '-'}")
        body = "\n".join(lines) + "\n\n" + format_stack(job)
        return _write(body, run_id, out_dir)
    except Exception:
        return None


def dump_exception(exc: BaseException, *, run_id: str, step: str, reason: str,
                   detail: dict | None = None, page=None, out_dir: Path | None = None) -> Path | None:
    """Стек остановки, брошенной в САМОМ потоке задания (срок кампании/шага, ACL-1702).

    Зовёт поток задания в `except`: трассировка исключения доходит до места,
    где сработал срок (какой цикл ожидания или вызов его съел), плюс внешний
    стек до точки перехвата. Файл — тот же `output/stuck_stacks/<run>_<ts>.txt`.
    Не бросает.
    """
    try:
        lines = [
            f"reason: {reason}",
            f"run: {run_id or '-'}",
            f"time_utc: {datetime.now(timezone.utc).isoformat()}",
            f"last_step: {step or '-'}",
        ]
        for key, value in (detail or {}).items():
            lines.append(f"{key}: {_scrub(str(value))}")
        if page is not None:
            lines.append(f"page_url: {_safe_url(page_url(page)) or '-'}")
        # Ревью PR #704 (m5): текст исключений Playwright несёт полные адреса с
        # act/business id — чистим и его.
        body = ("\n".join(lines) + "\n\n-- where the stop was raised --\n"
                + _scrub("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
                + "\n-- caught at --\n" + "".join(traceback.format_stack()))
        return _write(body, run_id, out_dir)
    except Exception:
        return None


_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_ID_RE = re.compile(r"(?<![\w.])\d{8,}(?![\w.])")


def _scrub(text: str) -> str:
    """Адреса — без запроса и якоря; длинные числа (act/business/ad id) — `<id>`."""
    text = _URL_RE.sub(lambda m: _safe_url(m.group(0)), text or "")
    try:
        from src.fb_ads_rpa import run_telemetry

        text = run_telemetry.scrub(text)
    except Exception:
        pass
    return _ID_RE.sub("<id>", text)


def _write(body: str, run_id: str, out_dir: Path | None) -> Path:
    folder = out_dir or stacks_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_run = "".join(ch for ch in (run_id or "norun") if ch.isalnum() or ch in "-_")[:40]
    path = folder / f"{safe_run}_{stamp}.txt"
    path.write_text(body, encoding="utf-8")
    _prune(folder)
    return path


def _prune(folder: Path, keep: int = STACKS_KEEP, keep_days: float = STACKS_KEEP_DAYS) -> None:
    """Ревью PR #704 (m5): не больше `keep` новейших файлов и не старше `keep_days`."""
    try:
        files = sorted(folder.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
        cutoff = time.time() - keep_days * 86400
        for i, f in enumerate(files):
            if i >= keep or f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def _safe_url(url: str) -> str:
    """Адрес без запроса и якоря: в них бывают токены (`access_token`, …)."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}" if p.scheme else url.split("?", 1)[0]
    except Exception:
        return ""


# ── Сырой клиент CDP (своё соединение, свой поток) ──────────────────────────

class CdpError(RuntimeError):
    pass


class RawCdp:
    """Минимальный websocket-клиент CDP браузера: одно соединение, синхронно.

    Своё TCP-соединение к порту отладки, без Playwright: им пользуется только
    поток сторожа. Каждый вызов — со своим сроком (`timeout`); соединение
    без Origin (Chrome 111+ отвергает чужие Origin, а без него — пускает).
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b""
        self._id = 0

    # -- подключение
    @classmethod
    def connect(cls, cdp_url: str, timeout: float = CALL_S) -> "RawCdp":
        host, port, path = _browser_ws(cdp_url, timeout)
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   f"Sec-WebSocket-Version: 13\r\n\r\n")
            sock.settimeout(timeout)
            sock.sendall(req.encode())
            raw = b""
            deadline = time.monotonic() + timeout
            while b"\r\n\r\n" not in raw:
                if time.monotonic() > deadline:
                    raise TimeoutError("websocket handshake timed out")
                chunk = sock.recv(4096)
                if not chunk:
                    raise CdpError("websocket handshake: connection closed")
                raw += chunk
            head, rest = raw.split(b"\r\n\r\n", 1)
            status = head.split(b"\r\n", 1)[0]
            if b" 101 " not in status + b" ":
                raise CdpError(f"websocket handshake refused: {status[:80]!r}")
            me = cls(sock)
            me._buf = rest
            return me
        except BaseException:
            sock.close()
            raise

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"", timeout=1.0)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass

    # -- вызов
    def call(self, method: str, params: dict | None = None, *, session_id: str = "",
             timeout: float = CALL_S) -> dict:
        self._id += 1
        msg: dict[str, Any] = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        deadline = time.monotonic() + timeout
        self._send_frame(0x1, json.dumps(msg).encode(), timeout=timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{method}: no answer in {timeout:.0f} s")
            data = self._recv_message(deadline)
            try:
                got = json.loads(data)
            except ValueError:
                continue
            if got.get("id") != self._id:
                continue   # событие или чужой ответ
            if "error" in got:
                err = got.get("error") or {}
                raise CdpError(f"{method}: {err.get('message', err)}")
            return got.get("result") or {}

    # -- кадры websocket
    def _send_frame(self, opcode: int, payload: bytes, *, timeout: float) -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.settimeout(max(0.05, timeout))
        self._sock.sendall(head + mask + body)

    def _read(self, n: int, deadline: float) -> bytes:
        while len(self._buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("CDP read timed out")
            self._sock.settimeout(left)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout as exc:
                raise TimeoutError("CDP read timed out") from exc
            if not chunk:
                raise CdpError("CDP connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv_message(self, deadline: float) -> bytes:
        parts: list[bytes] = []
        while True:
            b0, b1 = self._read(2, deadline)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2, deadline))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8, deadline))[0]
            mask = self._read(4, deadline) if b1 & 0x80 else b""
            payload = self._read(n, deadline) if n else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise CdpError("CDP connection closed by the browser")
            if opcode == 0x9:
                self._send_frame(0xA, payload, timeout=max(0.05, deadline - time.monotonic()))
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts)


def _browser_ws(cdp_url: str, timeout: float) -> tuple[str, int, str]:
    """(хост, порт, путь websocket браузера) из адреса CDP задания.

    Multilogin отдаёт `http://127.0.0.1:<порт>/devtools/browser/<id>` — путь
    уже есть. Голый `http://127.0.0.1:<порт>` — путь из `/json/version`.
    """
    url = cdp_url if "://" in cdp_url else "http://" + cdp_url
    p = urlparse(url)
    host = p.hostname or "127.0.0.1"
    port = int(p.port or (443 if p.scheme in ("https", "wss") else 80))
    if p.path.startswith("/devtools/browser/"):
        return host, port, p.path
    import urllib.request

    with urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=timeout) as resp:
        info = json.loads(resp.read().decode("utf-8", "replace"))
    ws = urlparse(str(info.get("webSocketDebuggerUrl") or ""))
    if not ws.path.startswith("/devtools/browser/"):
        raise CdpError("the browser did not name its websocket")
    return host, port, ws.path


# ── Разблокировка ────────────────────────────────────────────────────────────

def _settled(done: Callable[[], bool], wait_s: float) -> bool:
    end = time.monotonic() + wait_s
    while True:
        if done():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.1)   # сон отпускает GIL


def unblock_tab(cdp_url: str, url: str, done: Callable[[], bool], *,
                connect: Callable[..., Any] | None = None,
                call_s: float = CALL_S, settle_s: float = SETTLE_S) -> dict:
    """Разблокировать ОДНУ вкладку (цель с адресом `url`): `Target.closeTarget`.

    Ревью PR #704 (решение координатора): только закрытие. `Page.reload`
    переоткрыл бы редактор (и одноэкранный `simple_create`, где в адресе нет
    `/edit`) или выбросил начатое создание на списке кампаний (ACL-1313);
    `Runtime.terminateExecution` на зависшем рендерере не отвечал и не помогал
    (замер на Chromium 1228). Кампания всё равно кончается `tab_hung`, а
    следующая подключается к профилю заново.

    Цель — среди вкладок (`type == "page"`) по точному адресу; нет её или их
    несколько — не делаем ничего: чужие вкладки не трогаем. Последнюю вкладку
    браузера не закрываем голой: Chrome с последним окном закрылся бы целиком
    (а с ним профиль Multilogin) — сперва рядом открывается пустая фоновая
    (`Target.createTarget`, тот же контекст). Браузер не закрываем никогда.
    """
    out: dict[str, Any] = {"ok": False, "method": "", "tried": [], "why": ""}
    if not cdp_url:
        out["why"] = "no_endpoint"
        return out
    if not url:
        out["why"] = "no_url"
        return out
    connect = connect or RawCdp.connect
    started = time.monotonic()
    try:
        cdp = connect(cdp_url, timeout=call_s)
    except Exception as exc:
        out["why"] = f"connect:{type(exc).__name__}"
        return out
    try:
        try:
            infos = (cdp.call("Target.getTargets", timeout=call_s) or {}).get("targetInfos") or []
        except Exception as exc:
            out["why"] = f"targets:{type(exc).__name__}"
            return out
        # Цели с `subtype` (prerender и т. п.) — не вкладки: ни нашей, ни
        # «ещё одной вкладки», которая держала бы браузер открытым (ревью PR #704).
        pages = [t for t in infos if t.get("type") == "page" and not t.get("subtype")]
        mine = [t for t in pages if t.get("url") == url]
        if len(mine) != 1:
            out["why"] = "no_target" if not mine else "ambiguous_target"
            return out
        target = str(mine[0].get("targetId") or "")
        tried: list[str] = out["tried"]
        if len(pages) == 1:
            params: dict[str, Any] = {"url": "about:blank", "background": True}
            if mine[0].get("browserContextId"):
                params["browserContextId"] = mine[0]["browserContextId"]
            try:
                cdp.call("Target.createTarget", params, timeout=call_s)
                tried.append("spare_tab:sent")
            except Exception as exc:
                # Без запасной вкладки последнюю не закрываем: браузер ушёл бы.
                tried.append(f"spare_tab:{type(exc).__name__}")
                out["why"] = "last_tab"
                return out
        t0 = time.monotonic()
        try:
            cdp.call("Target.closeTarget", {"targetId": target}, timeout=call_s)
            tried.append("close:sent")
        except Exception as exc:
            tried.append(f"close:{type(exc).__name__}")
            if not done():
                return out
        if done() or _settled(done, settle_s):
            out.update(ok=True, method="close", settle_s=round(time.monotonic() - t0, 2))
        else:
            tried.append("close:no_effect")
        return out
    finally:
        out["seconds"] = round(time.monotonic() - started, 2)
        try:
            cdp.close()
        except Exception:
            pass


# ── Решение сторожа: когда разблокировать ───────────────────────────────────

def _human_holds() -> str:
    """Перехват человеком или ожидание обучения — "" если нет."""
    try:
        from src.fb_ads_rpa import pacing

        if pacing.get_teach_state().get("waiting"):
            return "teach_wait"
        if pacing.is_takeover_active():
            return "takeover"
    except Exception:
        return ""
    return ""


@dataclass
class Watch:
    """Состояние сторожа одного задания (живёт в его потоке-стороже)."""

    job: JobThread
    seen: BlockedCall | None = None
    seen_since: float = 0.0
    done_tasks: list = field(default_factory=list)   # weakref уже разблокированных вызовов
    count: int = 0


def unblock_tick(watch: Watch, *, idle_s: float, step: str, now: float,
                 unblock_s: float | None = None,
                 act: Callable[[BlockedCall], dict] | None = None) -> dict | None:
    """Одна проверка: пора ли разблокировать. Возвращает итог действия или None.

    Пора, когда: разблокировка включена; задание молчит не меньше
    `unblock_s`; поток стоит в ОДНОМ и том же вызове Playwright (тот же Task)
    не меньше `unblock_s` по наблюдению сторожа (или с последней строки
    телеметрии — что раньше); человек не держит браузер; этот вызов ещё не
    разблокировали; разблокировок на задание меньше `MAX_PER_JOB`.
    """
    limit = unblock_after_s() if unblock_s is None else unblock_s
    call = blocked_call(watch.job)
    if call is None:
        watch.seen = None
        return None
    if not call.same(watch.seen):
        watch.seen, watch.seen_since = call, now
    if not enabled() or watch.count >= MAX_PER_JOB:
        return None
    if idle_s < limit:
        return None
    # Сколько стоим в ЭТОМ вызове — по наблюдению сторожа (он смотрит с начала
    # задания, раз в круг опроса): нижняя оценка. Цикл коротких вызовов без
    # строк телеметрии — каждый раз новый Task, и до порога он не дойдёт.
    if now - watch.seen_since < limit:
        return None
    if any(ref() is call.task() for ref in watch.done_tasks):
        return None
    held = _human_holds()
    if held:
        return {"skipped": held}
    watch.done_tasks.append(call.task_ref)
    watch.count += 1
    _mark(watch.job, call.page, step)
    if act is None:
        cdp_url = endpoint_of(watch.job.tid)
        result = unblock_tab(cdp_url, call.url, call.done)
    else:
        result = act(call)
    result = dict(result)
    if not result.get("ok") and not result.get("tried"):
        # Ничего не отправили (нет адреса CDP, цели, соединения) — вкладку не
        # трогали, отметку снимаем: вернётся вызов сам — кампания идёт дальше.
        with _lock:
            _HUNG.pop(watch.job.tid, None)
        if call.page is not None:
            clear_page_mark(call.page)
        result["marked"] = False
    else:
        result["marked"] = True
    result.setdefault("api", call.api)
    result.setdefault("where", call.where)
    result["url"] = _safe_url(call.url)
    return result
