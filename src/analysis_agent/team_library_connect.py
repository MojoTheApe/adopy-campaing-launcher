"""Подключение общей библиотеки с дешборда: одно поле для токена (ACL-2038).

Обновление несёт всё, кроме токена: релизы и рабочий репозиторий видят
посторонние разработчики, так что токен, зашитый в сборку, был бы публичным.
Поэтому дешборд один раз просит токен, проверяет его одним чтением репозитория
библиотеки, кладёт в настройки машины (`%LOCALAPPDATA%\\ACL\\.env`, как и все
прочие секреты) и сам добавляет в библиотеку недостающее с этой машины.

Токен не должен попасть никуда, кроме файла настроек и заголовка запроса к
GitHub: ни в логи, ни в телеметрию и Sentry, ни в текст ошибки, ни в адрес, ни
в ответ страницы. Поэтому:
  * формат проверяется до любого запроса — иначе urllib упомянул бы значение в
    тексте ошибки о плохом заголовке;
  * любая неожиданная ошибка превращается в фиксированный текст, без `str(exc)`;
  * в ответ уходят только имя репозитория и счётчики.
"""
from __future__ import annotations

import os
import re

from config import paths
from src.analysis_agent import team_library as tl
from src.analysis_agent import team_library_seed as seed

TOKEN_KEY = "ACL_LIBRARY_TOKEN"
# Dashboard connection imports targets and ad texts; the shared CLI also seeds forms.
_CONNECT_KINDS = ("targets", "ad_texts")
#: GitHub выдаёт ghp_…, github_pat_… (буквы, цифры, подчёркивание), старые — hex.
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_\-]{20,255}")


class ConnectError(Exception):
    """Причина по-человечески; никогда не содержит значения токена."""


def current_repo() -> str:
    return (os.environ.get("ACL_LIBRARY_REPO") or "").strip() or tl.DEFAULT_REPO


def state() -> dict:
    """Подключена ли библиотека на этой машине. Токен наружу не отдаётся."""
    token = (os.environ.get(TOKEN_KEY) or "").strip()
    return {"connected": bool(token), "repo": current_repo()}


def _check_shape(token: str) -> str:
    token = (token or "").strip()
    if not token:
        raise ConnectError("Paste the library token first.")
    if not _TOKEN_SHAPE.fullmatch(token):
        raise ConnectError("That does not look like a GitHub token. Paste it exactly as it was given to you.")
    return token


def _local_snapshots() -> dict:
    return {kind: seed.KINDS[kind][0]() for kind in _CONNECT_KINDS}


#: Снимок, не доехавший до библиотеки: повторное «Connect» берёт его, а не копию,
#: которую открытие раздела уже подтянуло поверх местной.
_pending: dict = {}


def _seed_counts(local: dict) -> tuple[dict, str]:
    """Добавить недостающее: ничего не перезаписывается и не удаляется.

    (сколько добавлено по видам, текст первой ошибки). Вид, который не доехал,
    остаётся в `_pending` до следующей попытки."""
    out, error = {}, ""
    for kind in _CONNECT_KINDS:
        if kind not in local:
            continue
        try:
            n, _skipped = seed.seed(kind, add_missing=True, dry_run=False, local=local[kind])
        except tl.LibraryError as exc:
            error = error or str(exc)
            continue
        except Exception:
            error = error or "Adding this machine's items failed. Try Connect again."
            continue
        out[kind] = n
        _pending.pop(kind, None)
    return out, error


def connect(token: str) -> dict:
    """Проверить токен, сохранить на машине, добавить недостающее в библиотеку."""
    token = _check_shape(token)
    repo = current_repo()
    # Снимок местных целей и текстов — до сохранения токена: с токеном любое открытие
    # раздела подтягивает общую копию поверх местной.
    local = dict(_local_snapshots())
    for kind, pending in _pending.items():
        key = seed.KINDS[kind][1]
        current = local.get(kind, {})
        # Keep originals hidden by a remote refresh, alongside newly added local
        # records. A failed kind must not replace the entire current snapshot.
        local[kind] = {**current, **pending,
                       key: {**current.get(key, {}), **pending.get(key, {})}}
    _pending.clear()
    _pending.update(local)
    branch = (os.environ.get("ACL_LIBRARY_BRANCH") or "").strip() or tl.DEFAULT_BRANCH
    try:
        tl.GitHubBackend(repo, token, branch).check()
    except tl.LibraryError as exc:
        # Тексты LibraryError собираются без токена (см. `_fail`), но токен
        # подставляем в замену на всякий случай ещё раз.
        raise ConnectError(str(exc).replace(token, "[redacted]")) from None
    except Exception:
        raise ConnectError("Could not check the token. Try again in a moment.") from None
    old_token = os.environ.get(TOKEN_KEY, "")
    try:
        paths.update_machine_env({TOKEN_KEY: token})
    except Exception:
        raise ConnectError("The token is valid, but it could not be saved in this machine's settings.") from None
    tl.use_backend(None)
    added, error = _seed_counts(local)
    error = error.replace(token, "[redacted]")
    if error and not added:
        # Токен читает, но писать им нельзя (у fine-grained токена права на чтение
        # видны как права пользователя): не оставляем машину с токеном, которым
        # не сохранить ни одной правки.
        try:
            paths.update_machine_env({TOKEN_KEY: old_token})
        except Exception:
            pass
        if not old_token:
            os.environ.pop(TOKEN_KEY, None)
        tl.use_backend(None)
        raise ConnectError("The token was accepted for reading, but saving to the team library failed: "
                           + error + " Nothing was saved on this computer.")
    return {"ok": True, "repo": repo, "seeded": not error, "seed_error": error, "added": added}


def disconnect() -> dict:
    """Убрать токен из настроек машины. Локальные цели и тексты не трогаются."""
    try:
        paths.update_machine_env({TOKEN_KEY: ""})
    except Exception:
        raise ConnectError("Could not change this machine's settings.") from None
    os.environ.pop(TOKEN_KEY, None)
    tl.use_backend(None)
    return {"ok": True}


def summary(added: dict, failed: bool = False) -> str:
    """«12 targets and 40 ad texts added to the team library»."""
    t, a = int(added.get("targets", 0)), int(added.get("ad_texts", 0))
    if failed:
        return (f"Connected, but not everything was added ({t} target{'s' if t != 1 else ''}, "
                f"{a} ad text{'s' if a != 1 else ''} so far). Press Connect again to retry.")
    if not t and not a:
        return "Connected. Nothing from this machine needed adding — the team library already has it."
    return f"Connected. {t} target{'s' if t != 1 else ''} and {a} ad text{'s' if a != 1 else ''} added to the team library."
