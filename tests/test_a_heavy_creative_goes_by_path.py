"""Тяжёлый креатив отдаётся браузеру путём, а не содержимым.

14.09, живой прогон, дословно из ошибки Playwright:

    FileChooser.set_files: Cannot transfer files larger than 50Mb
    to a browser not co-located with the server

Это ограничение клиента Playwright, не Facebook и не нашего кода: он шлёт
СОДЕРЖИМОЕ файла по CDP и отказывается делать это для больших файлов, когда
считает соединение удалённым. У нас оно таким и считается — CDP по TCP через
Multilogin, — хотя браузер стоит на этой же машине и файл ему доступен.

Отсюда обход: `DOM.setFileInputFiles` с путём. Через нас содержимое не идёт
вовсе, и порог перестаёт существовать.

Полтора дня до этого шаг молчал, и было выдвинуто три неверные версии. Одна из
них — «файл тяжёлый и не успевает загрузиться» — была обидно близкой: дело
действительно в весе, но в ПОРОГЕ, а не в скорости. Ждать дольше не помогло бы
никогда.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SRC = ROOT / "src" / "fb_ads_rpa" / "meta_dom.py"


def _helper() -> str:
    text = SRC.read_text(encoding="utf-8")
    start = text.index("def _set_file_via_cdp(")
    return text[start: text.index("\n#: След последней попытки", start)]


def _upload_block() -> str:
    text = SRC.read_text(encoding="utf-8")
    start = text.index("    landed = False\n    for _attempt in range(3):")
    return text[start: text.index("    if not landed:", start)]


# ── обход ─────────────────────────────────────────────────────────────

def test_the_browser_gets_a_path_not_the_bytes():
    helper = _helper()
    assert "DOM.setFileInputFiles" in helper, "файл снова передаётся содержимым"
    assert '"files": [path]' in helper
    assert "resolve()" in helper, "путь должен быть абсолютным — читает его браузер"


def test_it_looks_behind_shadow_boundaries():
    """На живой странице 14.09 `query_selector_all('input[type=file]')` давал
    ноль полей в тот момент, когда окно выбора уже было открыто. CDP умеет
    заглянуть за теневые границы, Playwright здесь — нет."""
    helper = _helper()
    assert '"pierce": True' in helper


def test_the_tree_is_walked_and_not_queried():
    """Первая попытка обхода провалилась именно здесь, и тест этого не поймал.

    `pierce=True` стоял на `DOM.getDocument`, а поиск шёл через
    `DOM.querySelectorAll` — который за теневые границы НЕ заглядывает: флаг
    влияет только на возвращаемое дерево. Живой прогон дал `cdp: false`.

    Проверка выше при этом была зелёной: она сторожила наличие флага, а не то,
    что мы им пользуемся. Поэтому здесь — про обход самого дерева.
    """
    # Комментарии снимаем: в них это имя упомянуто нарочно, как объяснение
    # прошлой ошибки. Сторож, спотыкающийся о собственное объяснение, — уже
    # было сегодня утром на проверке подключения аккаунта.
    code = "\n".join(
        line for line in _helper().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "DOM.querySelectorAll" not in code, (
        "поиск снова идёт запросом — он не видит теневых корней"
    )
    helper = _helper()
    assert "shadowRoots" in helper, "теневые корни не обходятся"
    assert "contentDocument" in helper, "содержимое iframe не обходится"
    assert 'nodeName' in helper and '"file"' in helper, "поля отбираются не по типу"


def test_how_many_inputs_were_found_is_recorded():
    """`cdp: false` не говорит, чего не хватило: полей не нашлось или нашлись,
    но не приняли файл. Это разные починки."""
    helper = _helper()
    assert '_diag["cdp_inputs"]' in helper


def test_sending_the_command_is_not_counted_as_success():
    """Ровно на подмене «отправили» вместо «приняли» этот шаг уже горел: до
    04.08 успехом считался сам `set_files`, и прогон уезжал в мастер, который
    невозможно было продолжить."""
    helper = _helper()
    body = helper[helper.index("for node_id in"):]
    assert "return True" in body
    assert "continue" in body, "неудача на одном поле обязана вести к следующему"
    assert helper.rstrip().endswith("pass"), "сессия CDP не отцепляется"


def test_the_session_is_detached_even_when_it_throws():
    helper = _helper()
    assert "finally:" in helper
    assert "session.detach()" in helper


# ── встраивание ───────────────────────────────────────────────────────

def test_only_heavy_files_take_the_new_road():
    """Обычные файлы ходят прежним путём. Правка обязана быть узкой: 3.5 и
    12.5 МБ грузились восемь раз из восьми, и менять для них ничего нельзя."""
    block = _upload_block()
    assert '_diag["size_mb"] > _PW_FILE_LIMIT_MB' in block
    light = block[block.index("else:", block.index("_PW_FILE_LIMIT_MB")):]
    assert "fc_info.value.set_files(creative_path)" in light[:200], (
        "лёгкий файл больше не идёт прежним путём"
    )


def test_the_threshold_sits_below_playwrights_own():
    """Порог у Playwright на содержимом, а не на размере файла. Упереться в
    него на 49.8 МБ было бы обидно."""
    text = SRC.read_text(encoding="utf-8")
    line = [l for l in text.splitlines() if l.startswith("_PW_FILE_LIMIT_MB")][0]
    value = int(line.split("=")[1].strip())
    assert value < 50, "порог не оставляет запаса"
    assert value >= 40, "порог занижен — лёгкие файлы уйдут в обход без нужды"


def test_a_failed_bypass_still_speaks():
    """Если обход не сработал — пусть откажет прежний путь, вслух и с текстом.
    Молчание здесь и стоило полутора дней."""
    block = _upload_block()
    bypass = block[block.index("_step[\"cdp\"]"):]
    bypass = bypass[: bypass.index("else:")]
    assert "set_files(creative_path)" in bypass, (
        "при неудаче обхода шаг снова замолчит"
    )


def test_the_bypass_is_recorded():
    block = _upload_block()
    assert '_step["cdp"]' in block, "в следе не видно, каким путём шёл файл"


def test_the_bypass_happens_after_the_chooser_opened():
    """Поле для файла появляется вместе с окном выбора — до клика его нет."""
    block = _upload_block()
    assert block.index('_step["chooser"] = True') < block.index("_set_file_via_cdp")
