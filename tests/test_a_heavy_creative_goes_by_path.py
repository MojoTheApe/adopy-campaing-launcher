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
    но не приняли файл. Это разные починки.

    След ведётся в словаре, который ПЕРЕДАЁТСЯ помощнику. Обращаться к
    локальной переменной вызывающей функции он не может — на этом обход и
    упал на живом прогоне с `NameError`, и ни один текстовый тест этого не
    заметил. Само поведение проверяется исполнением ниже.
    """
    helper = _helper()
    assert 'diag["cdp_inputs"]' in helper
    assert "creative_path: str, diag: dict" in helper, (
        "словарь следа снова не передаётся — помощник не видит его извне"
    )


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


def test_the_bypass_does_not_swallow_its_own_errors():
    """Первая версия этой функции глотала исключения молча — написанная в тот
    самый день, когда весь разбор упёрся ровно в такое же молчание шагом выше.

    Живой прогон вернул `cdp: false` и ни слова о причине; поля `cdp_inputs` в
    следе не было вовсе, то есть падало до него. Узнать где — нечем.
    """
    helper = _helper()
    assert "except Exception:\n        return False" not in helper, (
        "обход снова молчит о своём отказе"
    )
    for where in ("new_cdp_session", "getDocument/walk", "setFileInputFiles"):
        assert where in helper, f"не сказано, что упало на «{where}»"
    assert "полей input[type=file] нет" in helper, (
        "пустой поиск не отличается от ошибки поиска"
    )


# ── а теперь тесты, которые КОД ВЫПОЛНЯЮТ ─────────────────────────────
#
# 14.09: обход упал на живом прогоне с `NameError: name '_diag' is not
# defined` — помощник на уровне модуля обращался к локальной переменной
# вызывающей функции. Ни одна из двенадцати проверок выше этого не поймала:
# все они читают исходник, а не исполняют его.
#
# Ровно то, в чём я в тот же день упрекал чужие сторожа: «проверялось наличие
# слова, а не поведение». Ниже — поведение.

class _FakeSession:
    def __init__(self, doc, on_set=None):
        self.doc, self.on_set, self.sent = doc, on_set, []

    def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "DOM.getDocument":
            return {"root": self.doc}
        if method == "DOM.setFileInputFiles":
            if self.on_set:
                return self.on_set(params)
            return {}
        return {}

    def detach(self):
        self.detached = True


class _FakePage:
    def __init__(self, session):
        self.context = type("Ctx", (), {"new_cdp_session": lambda _s, _p: session})()


def _input_node(node_id=7):
    return {"nodeId": node_id, "nodeName": "INPUT",
            "attributes": ["type", "file"], "children": []}


def _call(doc, on_set=None):
    from src.fb_ads_rpa import meta_dom
    session = _FakeSession(doc, on_set)
    diag: dict = {}
    ok = meta_dom._set_file_via_cdp(_FakePage(session), __file__, diag)
    return ok, diag, session


def test_it_runs_at_all():
    """Тот самый NameError: помощник не видел `_diag` вызывающей функции."""
    ok, diag, _ = _call({"nodeId": 1, "nodeName": "#document", "children": []})
    assert ok is False
    assert "полей input[type=file] нет" in diag.get("cdp_error", ""), diag


def test_a_plain_input_is_found_and_fed():
    doc = {"nodeId": 1, "nodeName": "#document", "children": [_input_node()]}
    ok, diag, session = _call(doc)
    assert ok is True
    assert diag["cdp_inputs"] == 1
    method, params = session.sent[-1]
    assert method == "DOM.setFileInputFiles"
    assert params["nodeId"] == 7
    assert params["files"][0].endswith(".py"), "браузеру ушёл не путь"


def test_an_input_behind_a_shadow_root_is_found():
    """Ради этого обход и писался: на живой странице поля не было в основном
    дереве."""
    doc = {"nodeId": 1, "nodeName": "#document",
           "children": [{"nodeId": 2, "nodeName": "DIV", "children": [],
                         "shadowRoots": [{"nodeId": 3, "nodeName": "#document-fragment",
                                          "children": [_input_node(9)]}]}]}
    ok, diag, session = _call(doc)
    assert ok is True and diag["cdp_inputs"] == 1
    assert session.sent[-1][1]["nodeId"] == 9


def test_an_input_inside_an_iframe_is_found():
    doc = {"nodeId": 1, "nodeName": "#document",
           "children": [{"nodeId": 2, "nodeName": "IFRAME", "children": [],
                         "contentDocument": {"nodeId": 3, "nodeName": "#document",
                                             "children": [_input_node(11)]}}]}
    ok, _, session = _call(doc)
    assert ok is True and session.sent[-1][1]["nodeId"] == 11


def test_a_refusing_input_is_reported_not_swallowed():
    def refuse(_params):
        raise RuntimeError("node is detached")

    doc = {"nodeId": 1, "nodeName": "#document", "children": [_input_node()]}
    ok, diag, _ = _call(doc, on_set=refuse)
    assert ok is False
    assert "setFileInputFiles" in diag["cdp_error"] and "detached" in diag["cdp_error"]


def test_a_text_input_is_not_mistaken_for_a_file_one():
    doc = {"nodeId": 1, "nodeName": "#document",
           "children": [{"nodeId": 5, "nodeName": "INPUT",
                         "attributes": ["type", "text"], "children": []}]}
    ok, diag, _ = _call(doc)
    assert ok is False and diag["cdp_inputs"] == 0
