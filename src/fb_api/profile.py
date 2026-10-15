"""Профиль Multilogin для Graph-вызовов: поднять или прицепиться, без Playwright.

Запуск — тем же `MultiloginLauncher` (MLX launcher API), что у робота и
дешборда; он на `requests`, без Playwright. Аккаунт вне Multilogin не
открываем никогда: нет порта отладки от MLX — нет работы.

Остановки нет вовсе (решение координатора, ревью PR #759, S2): пилот только
цепляется к работающему профилю или поднимает его, и НИКОГДА не останавливает.
Список «запущенных» MLX может не показать профиль робота, дешборда или
человека (схема ответа, гонка) — тогда «наш» профиль оказался бы чужим.
Поднятый пилотом профиль остаётся открытым, пилот просит закрыть его в
Multilogin руками. kill — тем более никогда.

Профиль, открытый руками в MLX Desktop, не трогаем: у него нет порта CDP,
MLX ответит «уже запущен» без порта — это отказ с понятной причиной.

Сначала — процессы на этой машине (как `attach_existing_only` робота): живой
пилот 5414 показал, что MLX `/profile/running` может не перечислить профиль,
чей Mimic уже работает с портом отладки (а `start` тогда отвечает 400 «browser
process is running»). Ищем процесс Mimic ИМЕННО этого профиля (по его
`--user-data-dir`, как `browser._discover_owned_debug_port`, без Playwright) и
его порт, который этот процесс действительно слушает. Нашли — цепляемся, профиль
«уже работал, не наш». Процесс есть, порта нет — окно открыто руками: отказ.
Только если процесса нет — MLX `/profile/running`, затем `start`.
"""
from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


#: Сколько ждём, пока закрывающийся профиль отпустит порт, и шаг опроса.
CLOSING_WAIT_S = 6.0
CLOSING_POLL_S = 2.0


class ProfileError(RuntimeError):
    """Профиль не поднят и не найден работающим с портом отладки.

    `code` — устойчивое имя отказа для экрана (ACL-1760): "open_by_hand" —
    окно открыто руками, порта нет, ничего не остановлено; "profile_closing" —
    порт ещё указан в командной строке, но уже не слушается (профиль закрывается).
    """

    def __init__(self, message: str = "", code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass
class OpenedProfile:
    profile_id: str
    folder_id: str
    instance: str
    port: str
    ws_endpoint: str | None
    started_by_us: bool
    launcher: Any = None
    #: Мы вызывали MLX `start` (ACL-1760): профиль поднят этим заходом, и пока
    #: он открыт, его место в очереди по памяти должно оставаться занятым.
    started_now: bool = False

    @property
    def cdp_url(self) -> str:
        ws = self.ws_endpoint or ""
        if ws and not ws.startswith("/"):
            ws = "/" + ws
        return f"http://127.0.0.1:{self.port}{ws}"


def _default_launcher(instance: str) -> Any:
    from src.meta_campaign_agent.multilogin_config import get_multilogin_instance
    from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncher

    return MultiloginLauncher(get_multilogin_instance(instance))


def running_state(launcher: Any, profile_id: str) -> tuple[str, str | None]:
    """('running', порт) / ('not_running', None) / ('unknown', None).

    Те же адреса «запущенных профилей», что у `MultiloginLauncher`; разница —
    «не ответил» отличается от «профиля нет в списке».
    """
    import requests

    answered = False
    for endpoint in (
        f"{launcher.config.launcher_base_v2}/profile/running",
        f"{launcher.config.launcher_base_v1}/profile/running",
        f"{launcher.config.launcher_base_v2}/profiles/running",
    ):
        try:
            r = requests.get(endpoint, headers=launcher._headers(), timeout=10)
            if r.status_code != 200:
                continue
            items = r.json()
            if isinstance(items, dict):
                items = items.get("data") or []
            if not isinstance(items, list):
                continue
            answered = True
            for item in items:
                if not isinstance(item, dict):
                    continue
                if (item.get("profile_id") or item.get("id") or "") == profile_id:
                    port = item.get("port") or item.get("debug_port")
                    return "running", (str(port) if port else None)
        except Exception:
            continue
    return ("not_running", None) if answered else ("unknown", None)


def _mlx_home() -> Path:
    return Path(os.environ.get("MLX_HOME", str(Path.home() / "mlx")))


def find_profile_dir(profile_id: str) -> Path | None:
    """Папка профиля в `~/mlx/profiles/…/<profile_id>` (как `browser._find_mlx_profile_dir`)."""
    root = _mlx_home() / "profiles"
    if not root.is_dir() or not profile_id:
        return None
    for path in root.rglob(profile_id):
        if path.is_dir():
            return path
    return None


def _long_path(value: str) -> str:
    """Запасное раскрытие 8.3 (`GetLongPathNameW`), если realpath оставил `~`.

    Только Windows; любая ошибка — путь как есть.
    """
    if os.name != "nt":
        return value
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetLongPathNameW(value, buf, len(buf))  # type: ignore[attr-defined]
        if 0 < n < len(buf):
            return buf.value
    except Exception:
        pass
    return value


def _norm(value: str) -> str:
    # Короткие имена 8.3 Windows раскрываем (realpath), сравниваем без регистра.
    value = (value or "").strip('"')
    try:
        value = os.path.realpath(value)
    except (OSError, ValueError):
        value = os.path.abspath(value)
    if "~" in value:
        value = _long_path(value)
    return os.path.normcase(value).replace("\\", "/").rstrip("/").lower()


def _accepting(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _arg_value(args: list, flag: str) -> str:
    val = next((a.split("=", 1)[1] for a in args if isinstance(a, str) and a.startswith(flag + "=")), "")
    if not val and flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            val = str(args[i + 1])
    return val


def local_debug_port(profile_id: str, *, process_iter: Callable[[], Iterable[Any]] | None = None,
                     profile_dir: Path | str | None = None,
                     accepting: Callable[[int], bool] = _accepting,
                     listens: Callable[[Any, int], bool] | None = None) -> tuple[str, int | None]:
    """Mimic ЭТОГО профиля на машине: ("port", N) / ("closing", N) / ("no_port", None) / ("none", None).

    Совпадение только по своему профилю: `--user-data-dir` равен папке профиля
    (или её последний компонент — это `profile_id`). Порт — из
    `--remote-debugging-port` (или `DevToolsActivePort`), и его должен СЛУШАТЬ
    этот процесс. Дочерние процессы Chrome (`--type=…`) пропускаются: у них нет
    порта, и «открыт руками» из них не выводится. Браузерный процесс без порта
    вовсе — открыт руками ("no_port"); порт указан, но не слушается — профиль
    закрывается ("closing"): это не «руками».
    """
    pid = (profile_id or "").strip().lower()
    if not pid:
        return "none", None
    want_dir = _norm(str(profile_dir)) if profile_dir else ""
    if process_iter is None:
        try:
            import psutil  # type: ignore
        except Exception:
            return "none", None  # нечем посмотреть — идём через MLX

        def process_iter():
            return psutil.process_iter(["name", "exe", "cmdline"])

        def listens(proc, port):
            try:
                return any(c.status == psutil.CONN_LISTEN and c.laddr.port == port
                           for c in proc.net_connections(kind="tcp"))
            except Exception:
                return False
    listens = listens or (lambda proc, port: True)

    found_process = False
    stale_port = 0
    for proc in process_iter():
        try:
            info = getattr(proc, "info", None) or {}
            exe = str(info.get("exe") or "").lower().replace("\\", "/")
            if "mimic" not in exe and "/mlx/" not in exe:
                continue
            args = list(info.get("cmdline") or [])
            udd = _arg_value(args, "--user-data-dir")
            if not udd:
                continue
            if any(isinstance(a, str) and a.startswith("--type=") for a in args):
                continue  # renderer/gpu/utility/crashpad: не браузерный процесс
            nudd = _norm(udd)
            if not ((want_dir and nudd == want_dir) or nudd.rsplit("/", 1)[-1] == pid):
                continue
            found_process = True
            raw = _arg_value(args, "--remote-debugging-port")
            port = int(raw) if raw.isdigit() else 0
            if port == 0:
                for f in (Path(udd) / "DevToolsActivePort", Path(udd) / "Default" / "DevToolsActivePort"):
                    try:
                        if f.is_file():
                            first = f.read_text(encoding="utf-8").splitlines()[0].strip()
                            port = int(first) if first.isdigit() else 0
                            break
                    except (OSError, IndexError, ValueError):
                        continue
            if port > 0 and listens(proc, port) and accepting(port):
                return "port", port
            if port > 0:
                stale_port = port
        except Exception:
            continue
    if stale_port:
        return "closing", stale_port
    return ("no_port", None) if found_process else ("none", None)


def open_profile(profile_id: str, folder_id: str = "", instance: str = "A", *,
                 launcher_factory: Callable[[str], Any] | None = None,
                 local_probe: Callable[[str], tuple[str, int | None]] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 closing_wait_s: float = CLOSING_WAIT_S) -> OpenedProfile:
    """Профиль с портом отладки от MLX: уже работающий или поднятый нами."""
    profile_id = (profile_id or "").strip()
    folder_id = (folder_id or "").strip()
    instance = (instance or "A").strip().upper() or "A"
    if not profile_id:
        raise ProfileError("no Multilogin profile id for this account")

    # 1. Процесс Mimic этого профиля на машине (как attach_existing_only робота).
    if local_probe is None:
        def local_probe(pid: str) -> tuple[str, int | None]:
            return local_debug_port(pid, profile_dir=find_profile_dir(pid))
    kind, local_port = local_probe(profile_id)
    # Профиль закрывается: несколько секунд ждём, что порт пропадёт вместе с
    # процессом (тогда идём в MLX) или оживёт; потом отказ, ничего не трогаем.
    waited = 0.0
    while kind == "closing" and waited < closing_wait_s:
        sleep(CLOSING_POLL_S)
        waited += CLOSING_POLL_S
        kind, local_port = local_probe(profile_id)
    if kind == "closing":
        raise ProfileError(
            "the profile is closing or starting; retry in a minute. Nothing was stopped.",
            code="profile_closing",
        )
    if kind == "port" and local_port:
        return OpenedProfile(profile_id, folder_id, instance, str(local_port), None, False, None)
    if kind == "no_port":
        raise ProfileError(
            "the profile is open but has no automation port (opened by hand in MLX Desktop?). "
            "Close it in MLX Desktop or open it from the dashboard, then retry. Nothing was stopped.",
            code="open_by_hand",
        )

    # 2. Процесса нет — спрашиваем MLX и поднимаем.
    launcher = (launcher_factory or _default_launcher)(instance)
    state, port = running_state(launcher, profile_id)
    if state == "running":
        if not port:
            raise ProfileError(
                "the profile is open but has no automation port (opened by hand in MLX Desktop?). "
                "Close it in MLX Desktop or open it from the dashboard, then retry. Nothing was stopped.",
                code="open_by_hand",
            )
        return OpenedProfile(profile_id, folder_id, instance, port, None, False, launcher)

    # ACL-1812 (ревью #813, B1): запуск ВСЕГДА проходит через общий журнал остановок.
    # Закрыли и сразу подняли заново — вероятная причина двух разлогинов (ACL-1811).
    from src.fb_api import stop_log as _stop_log

    why = _stop_log.refusal(profile_id)
    if why:
        raise ProfileError(why, code="restart_cooldown")

    started = launcher.start_profile(folder_id or None, profile_id, headless=False)
    if not started.port:
        raise ProfileError("Multilogin did not return a debug port; nothing was stopped.")
    return OpenedProfile(
        profile_id=profile_id,
        folder_id=started.folder_id or folder_id,
        instance=instance,
        port=str(started.port),
        ws_endpoint=started.ws_endpoint,
        # Только для сообщения человеку: остановки всё равно нет.
        started_by_us=(state == "not_running"),
        launcher=launcher,
        # Владелец — только при настоящем старте: ответ «уже запущен» (409) или
        # неизвестный исход остаётся чужим и нами не закрывается (ревью ACL-1760, P1).
        started_now=getattr(started, "newly_started", False) is True,
    )


def release_profile(opened: OpenedProfile | None) -> str:
    """Профиль не останавливается никогда; ответ — что сказать человеку."""
    if opened is None:
        return "nothing"
    if opened.started_by_us:
        return "left_open_started_by_pilot"
    return "left_running"
