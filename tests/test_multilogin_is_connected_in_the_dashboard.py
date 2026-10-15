# -*- coding: utf-8 -*-
"""Подключение к Multilogin живёт в дешборде, а не в установщике (ACL-1303).

24.09, первая настоящая установка с опубликованного релиза: на шаге 5 из 7
мастер просил вставить «Multilogin access code». Токены Multilogin никому не
выдают — их получает сам тот, кто входит, — и у нового байера этой строки
нет и взять её неоткуда. Сперва мастер научили входить по почте и паролю.

А потом выяснилось, почему этому месту в установщике не место вовсе.
Проверка связи отвечала «HTTP 403» на заведомо рабочий токен: резал
Cloudflare (ошибка 1010) — мастер ходил через `urllib` без `User-Agent`.
С обычным заголовком на его месте обнаружилось 401: оба сохранённых токена
мертвы, хотя внутри написано «годен ещё две недели». То есть Multilogin
гасит выданный токен, когда в то же рабочее пространство входят снова, — а
логин у команды один на всех. Установка десяти человек подряд оставила бы
работающим одного, последнего, и узнали бы об этом на первой публикации.

Поэтому шаг убран. Человек подключается в дешборде, когда садится работать:
там он видит результат сразу и там же переподключается, если его токен
погасили. Установщик до Multilogin не дотрагивается — и ничей чужой токен
не гасит.

Здесь остались тесты самого входа: им пользуется дешборд.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging" / "windows"))

from src.web import multilogin_auth  # noqa: E402


class Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


class Multilogin:
    """Multilogin, записывающий, что ему прислали."""

    def __init__(self, *, signin=None, token=None):
        self.signin = signin or Response(200, {"data": {"token": "bearer-xyz"}})
        self.token = token or Response(200, {"data": {"token": "automation-abc"}})
        self.posted: list[dict] = []
        self.got: list[str] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posted.append({"url": url, "json": json})
        return self.signin

    def get(self, url, headers=None, timeout=None):
        self.got.append(url)
        return self.token


def test_the_password_is_hashed_and_never_travels_as_itself():
    """Multilogin требует MD5 — это их формат, а не наша защита. Но пароль
    как есть не должен уходить никуда, и это проверяется отдельно."""
    mlx = Multilogin()
    result = multilogin_auth.sign_in("buyer@adopy.com", "hunter2", requests_module=mlx)
    assert result.ok and result.automation_token == "automation-abc"
    sent = mlx.posted[0]["json"]
    assert sent["email"] == "buyer@adopy.com"
    assert sent["password"] == "2ab96390c7dbe3439de74d0c9b0b1767"  # md5("hunter2")
    assert "hunter2" not in str(mlx.posted), "пароль ушёл как есть"


def test_signing_in_without_the_right_to_mint_a_token_is_a_refusal():
    """Вход и право выдать токен автоматизации — разные вещи: второе есть
    только у владельца рабочего пространства. Правильный пароль участника
    команды проходит первую ступень и упирается во вторую, а без токена
    робот не откроет ни одного профиля — то есть это отказ, а не мелочь."""
    mlx = Multilogin(token=Response(403, {"status": {"message": "not the workspace owner"}}))
    result = multilogin_auth.sign_in("member@adopy.com", "pw", requests_module=mlx)
    assert result.ok is True and result.automation_token == ""
    assert "not the workspace owner" in result.token_error
    assert result.working_token == "bearer-xyz", "сессионный токен всё же получен"


def test_a_wrong_password_says_so_in_multilogins_own_words():
    mlx = Multilogin(signin=Response(401, {"status": {"message": "wrong credentials"}}))
    result = multilogin_auth.sign_in("buyer@adopy.com", "nope", requests_module=mlx)
    assert result.ok is False and "wrong credentials" in result.error
    assert result.automation_token == ""


def test_no_network_is_told_apart_from_a_refusal():
    """Человеку важно знать, он ошибся паролем или у него нет интернета."""
    class Offline(Multilogin):
        def post(self, *a, **k):
            raise OSError("getaddrinfo failed")

    result = multilogin_auth.sign_in("buyer@adopy.com", "pw", requests_module=Offline())
    assert result.ok is False and "Could not reach Multilogin" in result.error


def test_an_empty_form_does_not_call_multilogin_at_all():
    mlx = Multilogin()
    result = multilogin_auth.sign_in("", "", requests_module=mlx)
    assert result.ok is False and not mlx.posted


# ── Мастер до Multilogin не дотрагивается ─────────────────────────────────

def test_the_wizard_never_asks_about_multilogin():
    """Ни поля, ни кнопки, ни проверки. Всё это однажды уже стояло здесь и
    заводило человека в тупик; вернуть его обратно легче, чем кажется."""
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    for gone in ("def _multilogin", "Multilogin password", "access code",
                 "def sign_in_to_multilogin", "def test_multilogin_access",
                 "MULTILOGIN_AUTOMATION_TOKEN"):
        assert gone not in source, f"шаг Multilogin вернулся в установщик: {gone}"
    assert "\"Multilogin\"," not in source, "Multilogin снова стоит в списке шагов"


def test_setup_is_six_steps_and_the_screens_match_them():
    """Список шагов и список рисовальщиков — две половины одного; разъехавшись,
    они показывают человеку не тот экран, и молча.

    Считать их поштучно мало: при перестановке двух соседних счёт сходится, а
    заголовок «Your name» встаёт над выбором файлов, и `_validate_step` разбирает
    книгу статистики на чужом шаге (ревью 24.09). Сверяем поимённо и по порядку.
    """
    import re

    import setup_wizard as wizard

    assert wizard.SetupWizard.STEPS == (
        "Welcome", "System check", "Your name", "Local files", "Readiness", "Finish")
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    block = source[source.index("renderers: tuple["):]
    block = block[:block.index(")\n")]
    assert re.findall(r"self\.(_\w+),", block) == [
        "_welcome", "_system", "_identity", "_files", "_readiness", "_finish"]


def test_the_installer_never_writes_a_multilogin_token():
    """Токен сохраняет дешборд. Если установщик снова научится его писать,
    повторная установка затрёт чужую работу — а выдать новый может только
    Multilogin, погасив при этом предыдущий."""
    config = (ROOT / "packaging" / "windows" / "machine_config.py").read_text(encoding="utf-8")
    assert "MULTILOGIN_AUTOMATION_TOKEN" not in config
    assert "multilogin_tokens" not in config


def _draw(screen: str) -> list[tuple[str, str]]:
    """Нарисовать экран мастера и вернуть, что человек увидит: (текст, цвет).

    Читать вместо этого исходник как текст — соблазнительно и бесполезно:
    закомментированная строка в файле остаётся, и тест зеленеет над пустым
    экраном (ревью 24.09). Tk для этих двух экранов не нужен — им хватает
    четырёх полей и подменённого `_text`.
    """
    import setup_wizard as wizard

    w = wizard.SetupWizard.__new__(wizard.SetupWizard)
    w.system_checks = [SimpleNamespace(ok=True, label="Runtime", detail="ok")]
    w.configuration = SimpleNamespace(accounts=("one",))
    w.stats_path = SimpleNamespace(get=lambda: "stats.xlsx")
    w.creatives_path = SimpleNamespace(get=lambda: "creatives")
    w.version_text = "Adopy Campaign Manager 1.2.3"
    w.body = object()
    drawn: list[tuple[str, str]] = []
    w._text = lambda value, *, bold=False, color="#273248": drawn.append((value, color))
    w.tk = SimpleNamespace(Button=lambda *a, **k: SimpleNamespace(pack=lambda *a, **k: None))
    getattr(w, screen)()
    return drawn


def test_the_last_screens_say_where_multilogin_is_connected():
    """Человек, закрывший мастер, обязан знать, что осталось сделать: без
    подключения робот не откроет ни одного профиля, и первая публикация
    упадёт на «TOKEN_A is not set» — сообщение, которое ему ничего не скажет.

    Слова «Multilogin» и «Accounts» на этих экранах были и раньше (в строке
    про безопасный режим и в совете привязать профили), так что искать их
    бессмысленно: мимо пройдёт как раз то, что мы добавили. Ищем фразы,
    которые больше взяться неоткуда, — и в нарисованном экране, а не в файле.
    """
    for screen, phrase in (("_readiness", "Multilogin is connected in the dashboard"),
                           ("_finish", "press Connect Multilogin")):
        shown = " ".join(text for text, _ in _draw(screen))
        assert phrase in shown, f"{screen}: не сказано, где подключается Multilogin"


def test_readiness_does_not_read_as_all_done():
    """Список на Readiness — четыре зелёных галочки, и глазами это читается
    «готово», а абзац под ним — как совет. Multilogin стоит в том же списке,
    но помечен как «ещё впереди»: не провал (на установке его и не должно
    быть), но и не то, о чём можно не знать (ревью 24.09).

    Цвет здесь — часть смысла, а не оформление: позеленев, строка скажет
    ровно обратное тому, ради чего поставлена.
    """
    drawn = _draw("_readiness")
    rows = [(text, color) for text, color in drawn if "Multilogin" in text]
    row = next((r for r in rows if r[0].startswith("•")), None)
    assert row is not None, "Multilogin пропал из списка на экране готовности"
    text, color = row
    assert "connected in the dashboard" in text and "after setup" in text
    green = [c for t, c in drawn if t.startswith("✓")]
    assert color not in green, "строка позеленела и читается как «уже готово»"
    checks = [i for i, (t, _) in enumerate(drawn) if t.startswith(("✓", "✕"))]
    assert drawn.index(row) > max(checks), "строка стоит выше списка проверок"
    assert drawn.index(row) == len(drawn) - 1 or "Safety:" not in drawn[drawn.index(row) + 1][0], (
        "под жёлтой строкой идёт зелёная про безопасный режим — глаз склеит их "
        "и прочитает нижнюю как подтверждение верхней (ревью 24.09)"
    )


def test_the_way_out_of_the_page_is_never_squeezed_off_the_screen():
    """Пакер вытесняет то, что уложено последним. Кнопки «Next» и «Back»
    лежали после тела страницы — и на невысоком экране уходили за край
    вместе с ним, оставляя человека в тупике (24.09)."""
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    assert 'self.footer.pack(side="bottom", fill="x")' in source
    assert source.index("self.footer = tk.Frame") < source.index("self.body = tk.Frame"), (
        "подвал снова укладывается после тела"
    )


# ── Вставка в поля мастера (24.09) ────────────────────────────────────────
#
# Владелец не смог вставить почту в поле: Tkinter вешает вставку на латинскую
# «v», а при русской раскладке в событии приходит «м» — правило не
# срабатывает. У команды раскладка русская, так что заполнить поле можно было
# только руками. Смотрим на физическую клавишу, а не на букву.

def test_paste_works_in_any_keyboard_layout():
    import setup_wizard as wizard

    # 86 — физическая клавиша V; при русской раскладке она же даёт «м».
    assert wizard.clipboard_event(86, 0x4) == "<<Paste>>"
    assert wizard.clipboard_event(67, 0x4) == "<<Copy>>"
    assert wizard.clipboard_event(88, 0x4) == "<<Cut>>"
    assert wizard.clipboard_event(65, 0x4) == "<<SelectAll>>"


def test_a_letter_without_control_is_just_a_letter():
    """Иначе буква «v» в имени кампании вставляла бы буфер обмена."""
    import setup_wizard as wizard

    assert wizard.clipboard_event(86, 0) == ""
    assert wizard.clipboard_event(999, 0x4) == ""


def test_the_wizard_turns_the_shortcuts_on_and_offers_a_right_click_menu():
    """Правая кнопка нужна не меньше: человеку, у которого Ctrl+V не сработал,
    больше нечем вставить вовсе."""
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    assert "enable_clipboard_shortcuts(root, tk)" in source
    body = source[source.index("def enable_clipboard_shortcuts"):]
    body = body[:body.index(chr(10) + "def ")]
    assert '<Control-KeyPress>' in body and '<Button-3>' in body
    for label in ("Cut", "Copy", "Paste"):
        assert f'"{label}"' in body
