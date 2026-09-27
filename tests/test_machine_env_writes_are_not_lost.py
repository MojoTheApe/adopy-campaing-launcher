# -*- coding: utf-8 -*-
"""Два писателя машинного `.env` не стирают друг друга (ревью PR #339, ACL-1335).

Запись атомарна (временный файл + replace), но чтение-изменение-запись без
замка теряло обновление: оба писателя читали старый файл, второй replace
затирал первого. С самопродлевающимся входом это обычный случай — поворот
refresh-токена слота A из потока робота, слота B из другого, плюс сохранение
из страницы. Окно гонки здесь нарочно расширено: запись притормаживает.
"""
from __future__ import annotations

import threading
import time

from config import paths


def test_parallel_updates_keep_every_key(monkeypatch):
    real_write = paths._atomic_write

    def slow_write(path, data):
        time.sleep(0.02)
        real_write(path, data)

    monkeypatch.setattr(paths, "_atomic_write", slow_write)
    keys = [f"ACL_TEST_WRITER_{i}" for i in range(6)]
    # Писатель кладёт значения и в окружение — monkeypatch вернёт его назад.
    for name in keys + ["ACL_TEST_SEED"]:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    paths.update_machine_env({"ACL_TEST_SEED": "1"})

    barrier = threading.Barrier(len(keys))
    errors: list[BaseException] = []

    def write(key):
        barrier.wait()
        try:
            paths.update_machine_env({key: "v"})
        except BaseException as exc:  # pragma: no cover - это и есть провал
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    text = open(paths.get_machine_env_path(), encoding="utf-8").read()
    missing = [k for k in keys if f'{k}="v"' not in text]
    assert not missing, f"записи потеряны: {missing}"
