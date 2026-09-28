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
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
