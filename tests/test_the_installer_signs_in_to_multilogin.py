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
    assert step.index("Multilogin email") < step.index("I was given an access code"), (
        "поле токена стоит раньше входа — человек снова упрётся в то, чего у него нет"
    )


def test_the_two_slots_are_named_the_way_the_dashboard_names_them():
    """«Multilogin access code» и «… (optional)» не говорили ничего: человек
    с двумя аккаунтами не знал, куда что класть.

    А назвать их по-своему («Main», «Second») — тоже мимо: у этих аккаунтов
    уже есть имена, Inhouse и KLD, и человек видит их каждый день на
    дешборде. Второе имя одной вещи — это то, что придётся однажды
    переименовывать в двух местах (замечание владельца 24.09).
    """
    import setup_wizard as wizard

    from src.web.app import mlx_instance_labels

    assert dict(wizard.multilogin_slots()) == mlx_instance_labels()
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    assert "Main Multilogin account" not in source and "Second Multilogin account" not in source
    step = source[source.index("def _multilogin(self)"):]
    step = step[:step.index(chr(10) + "    def _readiness")]
    assert "(optional)" not in step


def test_renaming_an_account_changes_the_installer_too(monkeypatch):
    """Подмена через ACL_MLX_LABEL_A/B работает и в мастере — иначе имена
    разъедутся ровно в тот день, когда аккаунт переименуют."""
    import setup_wizard as wizard

    monkeypatch.setenv("ACL_MLX_LABEL_B", "Agency")
    assert dict(wizard.multilogin_slots())["B"] == "Agency"


def test_the_paste_field_is_hidden_until_it_is_asked_for():
    """Выдать automation token может только владелец рабочего пространства,
    поэтому вставка нужна одному человеку из десяти. Показывать поле всем —
    растить экран и предлагать то, чего у большинства нет; на ноутбуке из-за
    этого за край уходила кнопка «Next» (замечание владельца 24.09)."""
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    step = source[source.index("def _multilogin(self)"):]
    step = step[:step.index(chr(10) + "    def _readiness")]
    assert "I was given an access code instead" in step
    assert "if self.paste_mode.get():" in step, "поля токенов показываются всегда"
    assert step.index("Multilogin email") < step.index("I was given an access code")


def test_the_way_out_of_the_page_is_never_squeezed_off_the_screen():
    """Пакер вытесняет то, что уложено последним. Кнопки «Next» и «Back»
    лежали после тела страницы — и на невысоком экране уходили за край
    вместе с ним, оставляя человека в тупике (24.09)."""
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    assert 'self.footer.pack(side="bottom", fill="x")' in source
    assert source.index("self.footer = tk.Frame") < source.index("self.body = tk.Frame"), (
        "подвал снова укладывается после тела"
    )


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


# ── Ветки, которые пропускали человека дальше без токена ──────────────────
#
# Ревью 24.09 удалило по одной строке и получило зелёный набор. А без них
# мастер доходит до конца, пишет «готово», не сохраняет ни одного токена — и
# первая же публикация у байера падает на «TOKEN_A is not set». Это ровно тот
# молчаливый успех, ради устранения которого задача и заведена.

def _wizard():
    import setup_wizard
    return setup_wizard


def test_signing_in_without_a_token_is_not_a_success(monkeypatch):
    """Вошёл, но токен автоматизации не выдали — право на это есть только у
    владельца рабочего пространства. Робот без токена не откроет ни один
    профиль, значит это отказ."""
    wizard = _wizard()
    monkeypatch.setattr(
        multilogin_auth, "sign_in",
        lambda *a, **k: multilogin_auth.SignIn(
            ok=True, bearer="b", automation_token="",
            token_error="not the workspace owner"),
    )
    with pytest.raises(wizard.WizardValidationError) as exc:
        wizard.sign_in_to_multilogin("member@adopy.com", "pw")
    assert "not the workspace owner" in str(exc.value)
    assert "Ask the owner" in str(exc.value), "человеку не сказали, к кому идти"


def test_a_refused_sign_in_is_told_apart_from_a_missing_token(monkeypatch):
    """Неверный пароль и «нет прав выдать токен» — разные беды, и советы к
    ним разные. Пока ветку отказа не проверяли, ошибка пароля отвечала бы
    «попросите владельца войти сюда»."""
    wizard = _wizard()
    monkeypatch.setattr(
        multilogin_auth, "sign_in",
        lambda *a, **k: multilogin_auth.SignIn(ok=False, error="Sign-in failed [401]: wrong credentials"),
    )
    with pytest.raises(wizard.WizardValidationError) as exc:
        wizard.sign_in_to_multilogin("buyer@adopy.com", "nope")
    assert "wrong credentials" in str(exc.value)
    assert "Ask the owner" not in str(exc.value)


def test_signing_in_to_one_account_does_not_bless_the_other(monkeypatch):
    """У владельца два аккаунта Multilogin. Вход в первый засчитывал и
    второй, не проверив его: опечатка в его поле уехала бы в настройку как
    рабочий токен, а мастер написал бы «verified» (ревью 24.09)."""
    wizard = _wizard()

    class Var:
        def __init__(self, value=""):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    app = wizard.SetupWizard.__new__(wizard.SetupWizard)
    app.token_a, app.token_b = Var(""), Var("typo-from-the-other-account")
    app.mlx_email, app.mlx_password, app.mlx_slot = Var("o@adopy.com"), Var("pw"), Var("A")
    app.busy = False
    app._verified_tokens = None
    app.multilogin_ok = False
    app._after_render = ""
    app.status = Var("")
    captured = {}
    app._run_worker = lambda work, done: captured.update(work=work, done=done)

    monkeypatch.setattr(wizard, "sign_in_to_multilogin", lambda e, p: ("fresh-token-A", "Signed in"))
    app._start_multilogin_signin()
    captured["done"](captured["work"]())

    assert app.token_a.get() == "fresh-token-A"
    assert app.token_b.get() == "typo-from-the-other-account", "чужое поле затёрли"
    assert app.multilogin_ok is False, (
        "второй аккаунт объявлен проверенным, хотя его никто не проверял"
    )
    assert app.mlx_password.get() == "", "пароль остался в поле"


def test_signing_in_with_the_other_slot_empty_is_enough(monkeypatch):
    """Обратная сторона: если второго аккаунта нет, вход в первый должен
    засчитываться — иначе мастер не пройти вовсе."""
    wizard = _wizard()

    class Var:
        def __init__(self, value=""):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    app = wizard.SetupWizard.__new__(wizard.SetupWizard)
    app.token_a, app.token_b = Var(""), Var("")
    app.mlx_email, app.mlx_password, app.mlx_slot = Var("o@adopy.com"), Var("pw"), Var("A")
    app.busy = False
    app._verified_tokens = None
    app.multilogin_ok = False
    app._after_render = ""
    app.status = Var("")
    captured = {}
    app._run_worker = lambda work, done: captured.update(work=work, done=done)

    monkeypatch.setattr(wizard, "sign_in_to_multilogin", lambda e, p: ("fresh-token-A", "Signed in"))
    app._start_multilogin_signin()
    captured["done"](captured["work"]())
    assert app.multilogin_ok is True


def test_signing_in_to_both_accounts_keeps_both_verified(monkeypatch):
    """У владельца два аккаунта Multilogin, и он войдёт в оба подряд. Вход во
    второй не должен сбрасывать проверку первого: иначе мастер говорит
    «сохранено», галочка пропадает, а Next отказывается (ревью 24.09)."""
    wizard = _wizard()

    class Var:
        def __init__(self, value=""):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    app = wizard.SetupWizard.__new__(wizard.SetupWizard)
    app.token_a, app.token_b = Var(""), Var("")
    app.mlx_email, app.mlx_password, app.mlx_slot = Var("o@adopy.com"), Var("pw"), Var("A")
    app.busy = False
    app._verified_tokens = None
    app.multilogin_ok = False
    app._after_render = ""
    app.status = Var("")
    captured = {}
    app._run_worker = lambda work, done: captured.update(work=work, done=done)

    for slot, token in (("A", "token-inhouse"), ("B", "token-agency")):
        app.mlx_slot.set(slot)
        app.mlx_password.set("pw")
        monkeypatch.setattr(wizard, "sign_in_to_multilogin", lambda e, p, t=token: (t, "Signed in"))
        app._start_multilogin_signin()
        captured["done"](captured["work"]())

    assert app.token_a.get() == "token-inhouse" and app.token_b.get() == "token-agency"
    assert app.multilogin_ok is True, "вход во второй аккаунт сбросил проверку первого"


def test_ready_cannot_mean_no_token_at_all(monkeypatch):
    """Два пустых словаря равны друг другу, поэтому «проверено» могло
    означать «проверено, что токенов нет». Калитка обязана это ловить сама,
    а не полагаться на одну строку выше по течению (ревью 24.09)."""
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    gate = source[source.index("elif self.step == 4"):]
    gate = gate[:gate.index("def ", gate.index("elif self.step == 4"))] if "def " in gate else gate
    assert gate.count("not any(self._tokens().values())") >= 2, (
        "калитка пропускает машину без единого токена"
    )


def test_the_success_line_does_not_promise_what_has_not_happened():
    """Сохранение происходит на Finish. Пока сообщение стиралось в том же
    такте, неточность никого не трогала; теперь она видна."""
    source = (ROOT / "packaging" / "windows" / "setup_wizard.py").read_text(encoding="utf-8")
    assert "The access code is saved when you finish setup." in source
    assert "automation token saved for this computer" not in source


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
