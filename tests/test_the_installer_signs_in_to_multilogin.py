# -*- coding: utf-8 -*-
"""Мастер установки спрашивает то, что у человека есть (ACL-1303).

24.09, первая настоящая установка с опубликованного релиза: на шаге 5 из 7
мастер просил вставить «Multilogin access code». Токены Multilogin никому не
выдают — их получает сам тот, кто входит, — и у нового байера этой строки
нет и взять её неоткуда. При этом дешборд рядом уже умел войти по почте и
паролю и получить токен сам.

Владелец, настраивавший всё своими руками, ни разу не вводил токен вручную:
он лежал в машинной настройке с прошлого раза. Поэтому дыру и увидели только
тогда, когда прошли установку с чистого листа, шаг за шагом.
"""
from __future__ import annotations

import sys
from pathlib import Path

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


# ── Сам мастер ────────────────────────────────────────────────────────────

def test_the_wizard_asks_for_an_email_and_a_password_not_a_token():
    """Первым на экране стоит то, что у человека есть. Вставка токена
    остаётся вторым вариантом — кому-то его выдали на руки."""
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    step = source[source.index("def _multilogin(self)"):]
    step = step[:step.index("\n    def _readiness")]
    assert "Multilogin email" in step and "Multilogin password" in step
    assert "Sign in to Multilogin" in step
    assert step.index("Multilogin email") < step.index("Already have an access code"), (
        "поле токена стоит раньше входа — человек снова упрётся в то, чего у него нет"
    )


def test_the_two_slots_say_which_account_they_are():
    """«Multilogin access code» и «… (optional)» не говорили ничего: человек
    с двумя аккаунтами не знал, куда что класть."""
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    assert 'MULTILOGIN_SLOTS = (("A", "Main Multilogin account"), ("B", "Second Multilogin account"))' in source
    step = source[source.index("def _multilogin(self)"):]
    step = step[:step.index("\n    def _readiness")]
    assert "(optional)" not in step


def test_the_wizard_keeps_the_token_and_forgets_the_password():
    """Пароль нужен на один запрос. В машинную настройку уходит только токен."""
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    hand = source[source.index("def _start_multilogin_signin"):]
    hand = hand[:hand.index("\n    def _start_multilogin_check")]
    assert "self.mlx_password.set(\"\")" in hand, "пароль остался в поле после входа"
    config = (ROOT / "packaging" / "windows" / "machine_config.py").read_text(encoding="utf-8")
    assert "password" not in config.lower(), "пароль попал в машинную настройку"


def test_the_wizard_and_the_dashboard_sign_in_through_the_same_code():
    """Две копии одного входа однажды разъедутся: починят одну, забудут
    вторую. Подробность про MD5 и адрес их API живёт в одном файле."""
    wizard = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    app = (ROOT / "src" / "web" / "app.py").read_text(encoding="utf-8")
    assert "from src.web.multilogin_auth import sign_in" in wizard
    assert "from src.web.multilogin_auth import MLX_API, sign_in" in app
    assert "hashlib" not in app.split("async def multilogin_signin")[1][:2000], (
        "дешборд снова хеширует пароль сам"
    )


def test_a_person_with_no_token_and_no_sign_in_is_told_what_to_do():
    """Прежний текст советовал «вставьте код, выданный поддержкой» — кода
    нет, поддержки в этом смысле тоже."""
    import setup_wizard as wizard

    with pytest.raises(wizard.WizardValidationError) as exc:
        wizard.test_multilogin_access({"A": "", "B": ""})
    assert "Sign in to Multilogin" in str(exc.value)
    assert "supplied by support" not in str(exc.value)
