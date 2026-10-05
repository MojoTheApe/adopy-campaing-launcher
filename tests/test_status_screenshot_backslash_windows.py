# -*- coding: utf-8 -*-
"""Обратная косая в id снимка (ACL-1486): маршруты, которые что-то значат только на Windows.

Тесты доказывают, что `..\escape` не выводит из папки снимков. Это верно лишь
там, где `\` — разделитель пути: на Linux это обычный символ в имени файла и
предусловие «без защит файл нашёлся бы» не выполняется. Поэтому файл идёт на
Windows (ACL-1582); нейтральные маршруты остались в test_status_screenshot.py.
"""
from __future__ import annotations

import pytest

from src.analysis_agent import account_spend as sp
from tests.test_status_screenshot import (  # noqa: F401  (фикстуры local и client)
    OTHER_PNG, PNG, _Client, _encodings, _loose_names, _naive, client, local)

pytestmark = pytest.mark.windows_only(
    reason="backslash path traversal: \ is a path separator only on Windows (ACL-1486)")

BS = "\\"


def _outside_backslash(local) -> dict[str, str]:
    (local / "spend_shots").mkdir(exist_ok=True)
    (local / "escape.png").write_bytes(OTHER_PNG)
    (local / "spend_shots_evil").mkdir(exist_ok=True)
    (local / "spend_shots_evil" / "status_x.png").write_bytes(OTHER_PNG)
    far = local.parent / f"{local.name}-elsewhere"
    far.mkdir(exist_ok=True)
    (far / "escape.png").write_bytes(OTHER_PNG)
    far_id = str(far / "escape")  # drive path; sent below with forward slashes
    return {
        "parent-backslash": ".." + BS + "escape",
        "parent-through-subdir-backslash": "status_x" + BS + ".." + BS + ".." + BS + "escape",
        "sibling-backslash": ".." + BS + "spend_shots_evil" + BS + "status_x",
        "absolute-forward-slashes": far_id.replace(BS, "/"),
    }


ROUTES = ["parent-backslash", "parent-through-subdir-backslash", "sibling-backslash",
          "absolute-forward-slashes"]


@pytest.mark.parametrize("route", ROUTES)
def test_a_real_png_outside_the_folder_is_not_reachable_by_id(local, route):
    shot_id = _outside_backslash(local)[route]
    assert _naive(local, shot_id).read_bytes() == OTHER_PNG, "без защит файл нашёлся бы"
    assert sp.status_shot_path(shot_id) is None


@pytest.mark.parametrize("route", ROUTES)
def test_the_folder_check_holds_even_if_the_name_check_lets_a_path_through(local, monkeypatch, route):
    shot_id = _outside_backslash(local)[route]
    assert _naive(local, shot_id).read_bytes() == OTHER_PNG, "без защит файл нашёлся бы"
    _loose_names(monkeypatch)
    assert sp.status_shot_path(shot_id) is None
    (local / "spend_shots" / "status_ok__20261001-120000.png").write_bytes(PNG)
    assert sp.status_shot_path("status_ok__20261001-120000") is not None


@pytest.mark.parametrize("loose", [False, True], ids=["strict-names", "loose-names"])
@pytest.mark.parametrize("tail", ["", "/view"], ids=["image", "view"])
@pytest.mark.parametrize("route", ROUTES)
def test_the_endpoint_refuses_a_real_png_outside_the_folder(local, client, monkeypatch, route, tail, loose):
    shot_id = _outside_backslash(local)[route]
    assert _naive(local, shot_id).read_bytes() == OTHER_PNG, "без защит файл нашёлся бы"
    if loose:
        _loose_names(monkeypatch)
    for sent in _encodings(shot_id):
        r = client.get(f"/account-spend/status-shot/{sent}{tail}")
        assert r.status_code == 404, (sent, r.status_code)
        assert b"outside" not in r.content, sent
