"""ACL-1342 (аудит безопасности, S09): пять форм секрета, которые раньше
доезжали до общей телеметрии и Sentry нетронутыми.

Живая проверка 27.09 (ACL-SECURITY-AUDIT-RU.md) нашла, что `scrub()` не
трогает:
  * `{"refresh_token":"..."}` — JSON, двоеточие вместо `=`;
  * `client_secret=...` — имени "secret" не было в списке;
  * `api_key: sk-...` — ни "key" в списке, ни двоеточие как разделитель;
  * `proxy_pass=...` — было только точное "password", не "proxy_pass";
  * `{"password":"..."}` — снова JSON с двоеточием, а не `=`.

Каждый случай ниже проверяется на СТРОКЕ (как приходит из логов и текста
исключений — `run_telemetry.scrub`) и через `scrub_diag`, которым размечает
события `acl_sentry._before_send`.

Независимое ревью PR #343 (2026-09-28) воспроизвело ещё два бага в самом
фиксе выше — оба ниже, в отдельных секциях:
  * camelCase/PascalCase имена (`refreshToken`, `RefreshToken`,
    `clientSecret`) не совпадали вовсе — `re.I` снимает регистр букв, но не
    разницу между `refresh_token` и `refreshToken` (наличие подчёркивания);
  * экранированная кавычка внутри JSON-значения (`"password":"ab\"cd-..."`)
    обрывала маскирование на первом символе, совпавшем с закрывающей
    кавычкой, даже экранированном — хвост секрета утекал рядом с `<hidden>`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402


# ── пять форм из аудита, плюс варианты написания ──────────────────────────

AUDIT_CASES = [
    ('{"refresh_token":"abc123XYZsecretvalue"}', "abc123XYZsecretvalue"),
    ("client_secret=s3cr3t-VALUE-999", "s3cr3t-VALUE-999"),
    ("api_key: sk-abcdef123456secret", "sk-abcdef123456secret"),
    ("proxy_pass=hunter2proxysecret", "hunter2proxysecret"),
    ('{"password":"hunter2supersecret"}', "hunter2supersecret"),
]

FORMAT_VARIANTS = [
    ("{'refresh_token': 'abc123XYZsecretvalue'}", "abc123XYZsecretvalue"),  # python-repr, одинарные кавычки
    ("client_secret=s3cr3t-VALUE-999;rest=of;the;line", "s3cr3t-VALUE-999"),  # без пробелов, дальше ещё текст
    ("...and the api_key: sk-abcdef123456secret was logged by mistake", "sk-abcdef123456secret"),  # в середине предложения
    ('  "proxy_pass" : "hunter2proxysecret"  ', "hunter2proxysecret"),  # JSON-ключ с пробелами вокруг двоеточия
    ("Body: {'password': 'hunter2supersecret', 'user': 'demo'}", "hunter2supersecret"),  # repr внутри более длинного текста
]


@pytest.mark.parametrize("raw,secret", AUDIT_CASES)
def test_each_audit_shape_is_redacted(raw, secret):
    cleaned = rt.scrub(raw)
    assert secret not in cleaned, f"секрет пережил scrub(): {cleaned!r}"
    assert "<hidden>" in cleaned


@pytest.mark.parametrize("raw,secret", FORMAT_VARIANTS)
def test_format_variants_are_also_redacted(raw, secret):
    cleaned = rt.scrub(raw)
    assert secret not in cleaned, f"секрет пережил scrub(): {cleaned!r}"
    assert "<hidden>" in cleaned


def test_scrub_diag_hides_the_same_keys_in_a_real_dict():
    """`acl_sentry._before_send` размечает событие через `scrub_diag`, а не
    `scrub()` напрямую — словарь, а не строка лога."""
    diag = {
        "refresh_token": "abc123XYZsecretvalue",
        "client_secret": "s3cr3t-VALUE-999",
        "api_key": "sk-abcdef123456secret",
        "proxy_pass": "hunter2proxysecret",
        "password": "hunter2supersecret",
        "campaign": "SA_sa_All_Image_Mix",  # не секрет — должен остаться
    }
    cleaned = rt.scrub_diag(diag)
    for key in ("refresh_token", "client_secret", "api_key", "proxy_pass", "password"):
        assert cleaned[key] == "<hidden>", f"{key} не скрыт: {cleaned[key]!r}"
    assert cleaned["campaign"] == "SA_sa_All_Image_Mix"


def test_an_unrelated_key_that_only_contains_the_word_is_not_mangled():
    """`tokenized_count=5` — не секрет, значение не должно исчезнуть."""
    cleaned = rt.scrub("tokenized_count=5 rows processed")
    assert "tokenized_count=5" in cleaned


def test_existing_act_and_bearer_scrubbing_still_works():
    """Регрессия: новые паттерны не должны потеснить то, что уже проверялось
    в проде (act=, Bearer, uuid, длинные числа)."""
    assert rt.scrub("https://adsmanager.facebook.com/x?act=1234567890") == (
        "https://adsmanager.facebook.com/x?act=<hidden>"
    )
    assert rt.scrub("Authorization: Bearer abc.123-456_XYZ") == "Authorization: Bearer <hidden>"


# ── PR #343 ревью, баг 1: camelCase/PascalCase имена ключей ───────────────

CAMEL_CASE_CASES = [
    ('{"refreshToken":"abc123XYZsecretvalue"}', "abc123XYZsecretvalue"),
    ('{"RefreshToken":"abc123XYZsecretvalue"}', "abc123XYZsecretvalue"),
    ("clientSecret=s3cr3t-VALUE-999", "s3cr3t-VALUE-999"),
    ("ClientSecret=s3cr3t-VALUE-999", "s3cr3t-VALUE-999"),
    ('{"apiKey":"sk-abcdef123456secret"}', "sk-abcdef123456secret"),  # "работал случайно" по ревью — не должен перестать
    ("proxyPass=hunter2proxysecret", "hunter2proxysecret"),
]


@pytest.mark.parametrize("raw,secret", CAMEL_CASE_CASES)
def test_camel_and_pascal_case_key_names_are_redacted(raw, secret):
    cleaned = rt.scrub(raw)
    assert secret not in cleaned, f"camelCase-имя ключа пережило scrub(): {cleaned!r}"
    assert "<hidden>" in cleaned


def test_camel_case_dict_keys_are_hidden_by_scrub_diag_too():
    """Не только строка из лога — тот же камень преткновения был у
    `scrub_diag` на НАСТОЯЩЕМ python-словаре с camelCase-ключом: значение
    само по себе не содержит имени ключа, так что regex `scrub()` там
    бессилен в принципе — маскировать обязан сам `scrub_diag` по ключу."""
    diag = {"refreshToken": "abc123XYZsecretvalue", "clientSecret": "s3cr3t-VALUE-999", "campaign": "ok"}
    cleaned = rt.scrub_diag(diag)
    assert cleaned["refreshToken"] == "<hidden>"
    assert cleaned["clientSecret"] == "<hidden>"
    assert cleaned["campaign"] == "ok"


# ── PR #343 ревью, баг 2: экранированная кавычка внутри JSON-значения ─────

def test_an_escaped_quote_inside_the_value_does_not_truncate_the_redaction():
    raw = '{"password":"ab\\"cd-secret-tail"}'
    cleaned = rt.scrub(raw)
    assert "cd-secret-tail" not in cleaned, f"хвост секрета утёк после экранированной кавычки: {cleaned!r}"
    assert "ab" not in cleaned.replace("<hidden>", ""), f"начало секрета тоже должно быть скрыто: {cleaned!r}"
    assert cleaned == '{"password":"<hidden>"}'


def test_an_escaped_single_quote_inside_a_repr_value_does_not_truncate_either():
    raw = "{'password': 'ab\\'cd-secret-tail'}"
    cleaned = rt.scrub(raw)
    assert "cd-secret-tail" not in cleaned, f"хвост секрета утёк после экранированной кавычки: {cleaned!r}"


# ── оба бага — через настоящий путь отправки в Sentry, не напрямую ────────

def test_both_bugs_are_fixed_through_the_real_acl_sentry_before_send_path():
    """`acl_sentry._before_send` — то, что реально видит каждое событие
    перед отправкой (`_scrub_deep` -> `scrub_diag` -> `scrub`). Ревью прямо
    просило проверить оба бага через этот путь, а не только вызовом
    `scrub()`/`scrub_diag()` напрямую — регресс мог вернуться в самой
    цепочке вызовов, а не в паттернах."""
    event = {
        "contexts": {
            "diag": {
                # баг 1: camelCase-ключ настоящего словаря
                "refreshToken": "abc123XYZsecretvalue",
                # баг 1: camelCase внутри вложенной JSON-строки
                "raw_response": '{"clientSecret":"s3cr3t-VALUE-999"}',
                # баг 2: экранированная кавычка внутри JSON-строки
                "raw_body": '{"password":"ab\\"cd-secret-tail"}',
                "campaign": "SA_sa_All_Image_Mix",
            }
        }
    }
    safe = acl_sentry._before_send(event, {})
    diag = safe["contexts"]["diag"]

    assert diag["refreshToken"] == "<hidden>"
    assert "s3cr3t-VALUE-999" not in diag["raw_response"]
    assert "<hidden>" in diag["raw_response"]
    assert "cd-secret-tail" not in diag["raw_body"], f"хвост утёк через реальный sentry-путь: {diag['raw_body']!r}"
    assert diag["campaign"] == "SA_sa_All_Image_Mix"
    # исходное событие не мутируется — тот же контракт, что уже проверяет
    # test_report_delivery_review.py::test_nested_secret_fields_are_removed_at_final_event_boundary
    assert event["contexts"]["diag"]["refreshToken"] == "abc123XYZsecretvalue"


# ── PR #343 ревью раунд 2: дефис, точка, пробел и префикс заголовка ───────

SECRET = "s3cr3tVALUE-777"

SEPARATOR_AND_PREFIX_CASES = [
    # JSON
    '{"x-api-key":"' + SECRET + '"}',
    '{"api-key":"' + SECRET + '"}',
    '{"REFRESH-TOKEN":"' + SECRET + '"}',
    '{"refresh.token":"' + SECRET + '"}',
    '{"refresh token":"' + SECRET + '"}',
    '{"X-Client-Secret":"' + SECRET + '"}',
    '{"xApiKey":"' + SECRET + '"}',
    '{"proxy-password":"' + SECRET + '"}',
    # python-repr
    "{'X-Refresh-Token': '" + SECRET + "'}",
    "{'x.api.key': '" + SECRET + "'}",
    # «голая» форма, в т.ч. заголовки HTTP
    "X-Api-Key: " + SECRET,
    "x-refresh-token=" + SECRET,
    "REFRESH-TOKEN=" + SECRET,
    "refresh.token=" + SECRET,
    "refresh token: " + SECRET,
    "x_api_key=" + SECRET,
    "xApiKey=" + SECRET,
    "Proxy-Password: " + SECRET,
]


@pytest.mark.parametrize("raw", SEPARATOR_AND_PREFIX_CASES)
def test_hyphen_dot_space_and_header_prefix_spellings_are_redacted(raw):
    cleaned = rt.scrub(raw)
    assert SECRET not in cleaned, f"написание ключа пережило scrub(): {cleaned!r}"
    assert "<hidden>" in cleaned


def test_the_key_name_itself_stays_visible():
    """Форма данных должна остаться читаемой — прячется значение, не имя."""
    assert rt.scrub('{"X-Api-Key":"' + SECRET + '"}') == '{"X-Api-Key":"<hidden>"}'
    assert rt.scrub("X-Refresh-Token: " + SECRET) == "X-Refresh-Token: <hidden>"


def test_authorization_bearer_is_still_handled_by_the_bearer_pattern():
    """Регрессия, которую уже раз ловили: заголовок с двоеточием не должен
    разбираться как key/value и отрезать слово "Bearer" вместо токена."""
    assert rt.scrub("Authorization: Bearer abc.123-456_XYZ") == "Authorization: Bearer <hidden>"
    assert rt.scrub("X-Authorization: Bearer abc.123-456_XYZ") == "X-Authorization: Bearer <hidden>"


@pytest.mark.parametrize("raw", [
    "tokenized_count=5 rows processed",
    "max_tokens=100",
    "monkeyapikey=banana",  # сплошная строчная каша — не camelCase-граница
    '{"campaign":"SA_sa_All_Image_Mix"}',
])
def test_names_that_only_look_similar_are_left_alone(raw):
    assert rt.scrub(raw) == raw


def test_scrub_diag_matches_separator_and_prefix_spellings_in_real_dicts():
    diag = {
        "X-Api-Key": SECRET,
        "refresh-token": SECRET,
        "refresh.token": SECRET,
        "x_client_secret": SECRET,
        "csrfToken": SECRET,
        "campaign": "ok",
    }
    cleaned = rt.scrub_diag(diag)
    for key in ("X-Api-Key", "refresh-token", "refresh.token", "x_client_secret", "csrfToken"):
        assert cleaned[key] == "<hidden>", f"{key} не скрыт: {cleaned[key]!r}"
    assert cleaned["campaign"] == "ok"


def test_scrub_diag_keeps_flags_and_counters_whose_name_only_ends_in_a_secret_word():
    """`has_token: True` — это «есть ли токен», а не токен. Совпадение только
    по окончанию имени прячет строки, но не флаги и числа; точное имя
    (`token`) прячется всегда, как и раньше."""
    cleaned = rt.scrub_diag({"has_token": True, "tokens_left": 3, "token": True})
    assert cleaned["has_token"] is True
    assert cleaned["tokens_left"] == 3
    assert cleaned["token"] == "<hidden>"


def test_separator_spellings_are_hidden_through_the_real_acl_sentry_path():
    event = {"contexts": {"diag": {
        "X-Api-Key": SECRET,
        "raw_headers": "X-Refresh-Token: " + SECRET,
        "raw_json": '{"refresh.token":"' + SECRET + '"}',
    }}}
    safe = acl_sentry._before_send(event, {})
    assert SECRET not in str(safe), f"секрет доехал до Sentry: {safe!r}"
