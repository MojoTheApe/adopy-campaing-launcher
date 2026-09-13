"""Карта подключает аккаунт так же, как вкладка Accounts: выбери профиль.

Подключиться можно из двух мест, и они спрашивали разное. Вкладка просила
одно — какой профиль открыт, — а имя аккаунта брала из заголовка профиля и
рекламный кабинет из его истории браузера. Карта просила последние буквы
профиля, потом Reload, потом имя и кабинет руками. Тот же результат, больше
работы и больше мест ошибиться. Решение владельца: карта перенимает подход.

Главное здесь — не новая форма, а то, что её **нет**. Форм подключения в
этом файле уже было несколько, две сводили в одну отдельной задачей неделю
назад: копии расходятся молча, и расходятся в сторону, которую замечают по
чужому скриншоту. Поэтому вывод имени, формула ключа и сам запрос живут в
одном месте, а экраны отличаются только разметкой.

Поведение проверяется исполнением кода страницы (`builder_connect_harness`),
здесь — что второй реализации не завелось.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"
ADD = ROOT / "src" / "web" / "templates" / "_account_map_add.html"


def _shared() -> str:
    src = TPL.read_text(encoding="utf-8")
    start = src.index("window.aclConnect = (function () {")
    return src[start: src.index("\n})();", start) + 6]


def _map_js() -> str:
    src = TPL.read_text(encoding="utf-8")
    start = src.index("function amapProfBlock(el)")
    return src[start: src.index("function amapFilter(input, reload)", start)]


def test_the_page_code_behaves():
    """Вывод имени, состояния списка и состав запроса — на живом коде."""
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — исполнить код страницы нечем")
    r = subprocess.run(
        [node, str(ROOT / "tests" / "builder_connect_harness.cjs"), str(TPL)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr


def test_the_map_asks_for_a_profile():
    """Одно решение вместо четырёх полей."""
    add = ADD.read_text(encoding="utf-8")
    assert 'class="amap-prof"' in add, "выбора профиля в карте нет"
    assert "amapProfConnect" in add


def test_what_was_worked_out_is_shown_before_connecting():
    """Человек должен увидеть, что мы поняли, а не узнать это по строке,
    появившейся в карте."""
    assert 'class="amap-derived"' in ADD.read_text(encoding="utf-8")
    js = _map_js()
    assert "Account: <b>" in js and "Ad account: <code>" in js


def test_a_profile_that_says_nothing_offers_the_manual_path():
    """Строка без кабинета запишется, но в карте не появится: карта
    пропускает записи без `ad_account_id`. Молча исчезнувший аккаунт хуже
    отказа, поэтому запасной путь открывается сам."""
    js = _map_js()
    assert "could not be worked out" in js
    assert "manual.open = true" in js, "запасной путь надо искать по экрану"


def test_the_map_does_not_grow_its_own_connect_logic():
    """Второй реализации быть не должно — за этим и заводили общий модуль."""
    js = _map_js()
    assert "aclConnect" in js, "карта считает что-то сама"
    for forbidden in ("fetch(", "account-map-patch", "match(/([A-Za-z])"):
        assert forbidden not in js, f"в карте завелась своя механика: {forbidden}"


def test_the_key_formula_is_not_reinvented():
    """Формула ключа должна совпадать с серверной буква в букву: своя,
    похожая, дала бы другие совпадения, и вывод имени в карте разошёлся бы
    со вкладкой.

    Сравнивается с настоящим `profile_name_key`, а не с копией в тесте.
    """
    from src.fb_ads_rpa.mlx_profiles import profile_name_key

    js = _shared()
    assert re.search(r"match\(/\(\[A-Za-z\]\)\(\\d\{1,4\}\)\$/\)", js), (
        "в общем модуле не та формула хвоста"
    )
    # И сервер действительно считает так же — иначе совпадать будет нечему.
    assert profile_name_key("KLD_BFL_W27") == "W27"
    assert profile_name_key("FacebookMKTOBFLW27") == "W27"


def test_the_tab_uses_the_shared_module_too():
    """Иначе их снова двое, просто теперь один из них называется общим."""
    src = TPL.read_text(encoding="utf-8")
    start = src.index("var loaded = false, openProfiles = []")
    tab = src[start: src.index("// ── Таргеты: общая библиотека", start)]
    assert "window.aclConnect.nameFor" in tab
    assert "window.aclConnect.adFor" in tab
    assert "window.aclConnect.connect" in tab
    assert "window.aclConnect.key" in tab


def test_the_list_is_asked_for_once_and_only_when_needed():
    """Блоков подключения на странице под тридцать, а трогают один. Список
    приходит из Google Sheets: при загрузке страницы он встаёт в очередь за
    остальным и ждёт больше десяти секунд — платить этим при каждом
    открытии дешборда незачем."""
    src = TPL.read_text(encoding="utf-8")
    start = src.index("window.amapProfEnsure = function")
    block = src[start: src.index("})();", start)]
    assert "if (asked && !force) return;" in block, "список тянется на каждый взгляд"
    assert "mousedown" in src[start - 2000: start + 2000], "список не ждёт, пока за ним потянутся"
