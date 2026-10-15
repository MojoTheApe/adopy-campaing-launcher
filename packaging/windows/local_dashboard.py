"""Где на этой машине мой дешборд и на какой порт встать, если его нет.

До ACL-1309 на этот вопрос отвечали одной проверкой: «на 9090 отвечает ACL».
Это «какой-то ACL», а не «мой». На машине с двумя людьми — два пользователя
Windows или удалённый стол — у каждого свой каталог настроек, а порт общий, и
лаунчер второго человека открывал дешборд первого: чужие аккаунты, чужие
цифры, и ни слова о том, что что-то не так. Та же слепота пропускала мастер
установки («дешборд уже работает»), валила в нём проверку порта и заставляла
обновление ждать остановки чужого дешборда.

Теперь «мой» значит: ACL на этом порту обслуживает тот же каталог настроек
(`config.paths.config_identity`). Всё остальное — чужой ACL или посторонняя
программа — не трогаем и встаём рядом, на следующий свободный порт.

Порт закреплён за человеком, а не за тем, кто раньше запустился: свой порт
человек помнит у себя (`dashboard_port.json` в каталоге настроек), а на
общей машине ещё и отмечает его в общем реестре (`%ProgramData%/ACL/ports`),
чтобы сосед, впервые запускаясь, пока хозяин порта спит, не занял его.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = 9090
#: Сколько портов подряд пробуем. Двадцать — с запасом на машину с
#: несколькими людьми и парой посторонних программ, и при этом перебор
#: занятых портов укладывается в секунды.
PORT_SPAN = 20
PORTS = range(DEFAULT_PORT, DEFAULT_PORT + PORT_SPAN)


def identity(config_directory: str | os.PathLike) -> str:
    from config.paths import config_identity

    return config_identity(config_directory)


def health(port: int, *, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def is_mine(payload: dict | None, mine: str) -> bool:
    """ACL, живой и обслуживает мой каталог настроек."""
    return bool(payload and payload.get("app") == "ACL" and payload.get("ok") is True
                and payload.get("config") == mine)


def port_in_use(port: int) -> bool:
    """Держит ли порт хоть кто-то — мгновенно.

    Не подключением: на Windows подключение к ЗАКРЫТОМУ порту не отказывает
    сразу, а ждёт повторов, и опрос двадцати портов занимал 19 секунд на
    каждом холодном старте (замер 24.09). Пытаемся занять порт сами:

    * `SO_EXCLUSIVEADDRUSE` — иначе Windows разрешает занять порт поверх
      чужого слушателя и ответ «свободен» был бы ложным;
    * `::` с `IPV6_V6ONLY=0` — такая привязка конфликтует со слушателем на
      любом адресе обоих семейств. Одна привязка к `0.0.0.0` не видела
      программу на `[::]` (ревью ACL-1309): Windows разрешает IPv4-сокету
      встать рядом с IPv6-слушателем, и мы встали бы поверх;
    * `0.0.0.0` — ещё раз отдельно: на машине без IPv6 и там, где система
      не даёт двойной стек;
    * без `listen`, поэтому брандмауэр Windows ни о чём не спрашивает.

    Только что освободившийся порт (TIME_WAIT) честно считается свободным.
    """
    attempts = [(socket.AF_INET, ("0.0.0.0", port))]
    if socket.has_ipv6:
        attempts.insert(0, (socket.AF_INET6, ("::", port)))
    for family, address in attempts:
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue  # IPv6 выключен в системе — остаётся IPv4
        with sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            if family == socket.AF_INET6:
                try:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass  # без двойного стека: IPv4 проверит следующая попытка
            try:
                sock.bind(address)
            except OSError as exc:
                if family == socket.AF_INET6 and exc.errno in _NO_IPV6_ERRNOS:
                    continue
                return True
    return False


_NO_IPV6_ERRNOS = {getattr(errno, name) for name in ("EADDRNOTAVAIL", "EAFNOSUPPORT")
                   if hasattr(errno, name)}


def recorded_port(config_directory: str | os.PathLike) -> int | None:
    """Порт, который мой сервер записал о себе в server.json, если записал."""
    try:
        state = json.loads((Path(config_directory) / "server.json").read_text(encoding="utf-8"))
        port = int(state["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return port if port in PORTS else None


def find_own(config_directory: str | os.PathLike, *, probe=health,
             in_use=port_in_use) -> tuple[int, dict] | None:
    """Мой работающий дешборд: (порт, ответ /health), либо None.

    Сперва смотрим туда, где мой сервер записал себя, потом на мой
    закреплённый порт — это обычный случай, и он обходится одним запросом.
    Записи могут устареть (сервер упал, не успев её убрать), поэтому они
    подсказка, а не ответ: всё решает `/health`.

    По HTTP спрашиваем только порты, которые кто-то держит: пустой порт не
    может быть моим, а спрашивать его на Windows — секунда ожидания.
    """
    mine = identity(config_directory)
    hints = [p for p in (recorded_port(config_directory), remembered_port(config_directory))
             if p is not None]
    for port in dict.fromkeys(hints + list(PORTS)):
        if not in_use(port):
            continue
        payload = probe(port)
        if is_mine(payload, mine):
            return port, payload
    return None


def free_port(*, in_use=port_in_use) -> int | None:
    """Первый свободный порт из нашего ряда. Чужое не останавливаем никогда."""
    for port in PORTS:
        if not in_use(port):
            return port
    return None


# --- Закреплённый порт ------------------------------------------------------
#
# Ревью ACL-1309: «кто первым запустился, тот и на 9090». Вечером владелец
# на 9090, утром Дан включил компьютер раньше — и уже он на 9090, а владелец
# на 9091: закладки и привычный адрес врут через день. Порт закреплён за
# человеком.
#
# Помнить свой порт мало: сосед, запустившийся впервые, пока хозяин 9090
# спит, займёт его и запомнит — и они будут меняться местами. Поэтому
# закреплённый порт отмечается ещё и в общем для машины реестре: файл на
# порт, внутри — отпечаток хозяина (не путь, как и в /health). Каталог
# %ProgramData% по умолчанию разрешает любому пользователю создавать файлы и
# только создателю — менять свои: соседскую отметку не перезаписать.

MEMORY_FILE = "dashboard_port.json"


def registry_dir() -> Path | None:
    """Общий для машины реестр закреплённых портов, или None, если его нет.

    `ACL_PORT_REGISTRY_DIR` — для тестов и особых установок; пустое значение
    выключает реестр.
    """
    explicit = os.environ.get("ACL_PORT_REGISTRY_DIR")
    if explicit is not None:
        return Path(explicit) if explicit.strip() else None
    program_data = os.environ.get("PROGRAMDATA")
    return Path(program_data) / "ACL" / "ports" if program_data else None


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def claim_owner(port: int) -> str | None:
    """Чей это порт по общему реестру: отпечаток хозяина, либо None."""
    folder = registry_dir()
    if folder is None:
        return None
    owner = (_read_json(folder / f"{port}.json") or {}).get("config")
    return owner if isinstance(owner, str) and owner else None


def remembered_port(config_directory: str | os.PathLike) -> int | None:
    """Мой закреплённый порт — из моей памяти, а без неё из общего реестра."""
    data = _read_json(Path(config_directory) / MEMORY_FILE) or {}
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        port = None
    if port in PORTS:
        return port
    mine = identity(config_directory)
    for candidate in PORTS:
        if claim_owner(candidate) == mine:
            return candidate
    return None


def _remember(config_directory: str | os.PathLike, port: int) -> None:
    """Закрепить порт за мной: в своей памяти и, если можно, в реестре.

    Ошибки записи не мешают запуску: дешборд на свободном порту лучше, чем
    никакого. Чужую отметку в реестре не перезаписываем никогда.
    """
    mine = identity(config_directory)
    memory = Path(config_directory) / MEMORY_FILE
    try:
        memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_text(json.dumps({"port": port}) + "\n", encoding="utf-8")
    except OSError:
        pass
    folder = registry_dir()
    if folder is None or claim_owner(port) is not None:
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / f"{port}.json", "x", encoding="utf-8") as handle:
            json.dump({"config": mine}, handle)
    except OSError:
        pass  # сосед успел первым, либо реестр недоступен — хватит памяти


def choose_port(config_directory: str | os.PathLike, *, remember: bool = True,
                in_use=port_in_use) -> int | None:
    """Порт, на котором встанет мой дешборд; None — встать некуда.

    1. Мой закреплённый порт, если он свободен.
    2. Иначе первый свободный, не закреплённый за соседом, — и он становится
       моим, если своего ещё не было.
    3. Иначе любой свободный, только на этот запуск: стоять на соседском
       месте, пока сосед не работает, лучше, чем не стоять вовсе.

    `remember=False` — только узнать ответ (шаг System check мастера), ничего
    не закрепляя: мастер должен назвать тот же порт, что займёт лаунчер.
    """
    mine = identity(config_directory)
    own = remembered_port(config_directory)
    if own is not None and not in_use(own):
        if remember:
            _remember(config_directory, own)  # дописать отметку в реестр, если её нет
        return own
    for port in PORTS:
        if in_use(port) or claim_owner(port) not in (None, mine):
            continue
        if remember and own is None:
            _remember(config_directory, port)
        return port
    return free_port(in_use=in_use)
