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
"""
from __future__ import annotations

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
    * `0.0.0.0`, а не `127.0.0.1` — иначе не видна программа, слушающая все
      адреса;
    * без `listen`, поэтому брандмауэр Windows ни о чём не спрашивает.

    Только что освободившийся порт (TIME_WAIT) честно считается свободным.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return True
        return False


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

    Сперва смотрим туда, где мой сервер записал себя, — это обычный случай, и
    он обходится одним запросом. Запись может устареть (сервер упал, не успев
    её убрать), поэтому она подсказка, а не ответ: всё решает `/health`.

    По HTTP спрашиваем только порты, которые кто-то держит: пустой порт не
    может быть моим, а спрашивать его на Windows — секунда ожидания.
    """
    mine = identity(config_directory)
    hint = recorded_port(config_directory)
    for port in ([hint] if hint is not None else []) + [p for p in PORTS if p != hint]:
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
