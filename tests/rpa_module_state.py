# -*- coding: utf-8 -*-
"""Состояние модулей RPA между тестами (ACL-2073, ACL-2078).

`meta_dom` и `crop_step` держат на уровне модуля «последний отказ / причину /
память прогона»: `threading.local` на поток и словари/множества на процесс. В
продукте их сбрасывает новый поток задания, новое объявление или новый прогон;
тесты же идут подряд в одном главном потоке воркера pytest, и состояние,
оставленное одним тестом, читал следующий. 21.10 так падал
`test_media_attachment_identity.py` после b97 (`_tls.failure`, ACL-2073), 22.10 —
`test_additional_ad_without_creative_has_no_text.py` после него же
(`_upload_why_tls.why` = `library_has_no_copy`, ACL-2078).

`reset()` приводит каждый такой кусок к виду свежего потока свежего процесса.
Трогаются только уже импортированные модули: сброс не тянет Playwright в
чужие тесты. Новое изменяемое состояние модуля нужно вписать в `MUTABLE` (или,
если это неизменяемая таблица, в `CONSTANTS`) — иначе упадёт
`tests/test_rpa_module_state_is_reset_between_tests.py`.
"""
from __future__ import annotations

import copy
import sys
import threading

META_DOM = "src.fb_ads_rpa.meta_dom"
CROP_STEP = "src.fb_ads_rpa.crop_step"
RUN_TELEMETRY = "src.fb_ads_rpa.run_telemetry"

#: Модули, у которых сбрасываются все `threading.local` уровня модуля.
THREAD_LOCAL_MODULES = (META_DOM, CROP_STEP)

#: Изменяемые контейнеры уровня модуля и их значение при импорте.
MUTABLE: dict[str, dict[str, object]] = {
    META_DOM: {
        "_LAST_UPLOAD_DIAG": {},
        "_UNSTUCK_RELOADED": set(),
        "_STALL_EVIDENCE_TAKEN": {},
        "_OTHER_REVIEW_ERRORS_SEEN": set(),
        "_reuse_state": {"counter_missing": False},
        "_failed_uploads": {},
        "_uploaded_ok": set(),
        "_last_destination": {"why": "", "diag": {}, "instant_form": {}},
        "_ADOPTED_SHELL_IDS": set(),
    },
    CROP_STEP: {},
}

#: Словари уровня модуля, которые код только читает (таблицы соответствий).
CONSTANTS: dict[str, frozenset[str]] = {
    META_DOM: frozenset({
        "_PLACEMENT_CATEGORIES", "_SUBITEMS_BY_CATEGORY", "_SUBITEM_CATEGORY",
        "_AD_COPY_FIELDS", "_COUNTRY_SEARCH", "_COUNTRY_NAME_TO_ISO",
    }),
    CROP_STEP: frozenset(),
}

_CONTAINER_TYPES = (dict, set, list, bytearray)


def module_containers(module) -> dict[str, object]:
    """Изменяемые контейнеры уровня модуля (без dunder-имён)."""
    return {
        name: value for name, value in vars(module).items()
        if not name.startswith("__") and isinstance(value, _CONTAINER_TYPES)
    }


def module_thread_locals(module) -> dict[str, threading.local]:
    return {name: value for name, value in vars(module).items() if isinstance(value, threading.local)}


def _restore(module, name: str, initial: object) -> None:
    if not hasattr(module, name):
        # Имени в модуле нет (переименовали/убрали): не создавать его здесь,
        # иначе тест реестра не увидит устаревшее имя в `MUTABLE`.
        return
    current = getattr(module, name)
    fresh = copy.deepcopy(initial)
    # На месте: на объект могли сослаться через `from meta_dom import ...`.
    if isinstance(current, dict) and isinstance(fresh, dict):
        current.clear()
        current.update(fresh)
    elif isinstance(current, set) and isinstance(fresh, set):
        current.clear()
        current.update(fresh)
    else:
        setattr(module, name, fresh)


def reset() -> None:
    """Вернуть состояние уже импортированных модулей RPA к свежему потоку и процессу."""
    for mod_name in THREAD_LOCAL_MODULES:
        module = sys.modules.get(mod_name)
        if module is None:
            continue
        for local in module_thread_locals(module).values():
            # `threading.local.__dict__` — словарь текущего потока: после
            # очистки поток выглядит как только что запущенный.
            local.__dict__.clear()
        for name, initial in MUTABLE.get(mod_name, {}).items():
            _restore(module, name, initial)

    crop = sys.modules.get(CROP_STEP)
    if crop is not None:
        crop.set_plan(None)  # штатный сброс объявления (как в продукте)

    telemetry = sys.modules.get(RUN_TELEMETRY)
    if telemetry is not None:
        # `run_recorded_campaign_flow` ставит прогон в ContextVar главного
        # потока; память загрузок (`_failed_uploads`) ключуется по нему.
        telemetry.clear_current_run()
