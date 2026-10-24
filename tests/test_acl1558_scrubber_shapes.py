"""ACL-1558: формы секрета, которые общий вычиститель пропускал (ревью PR #556, F4).

* голый токен Graph API `EAAB…`/`EAA…` без `token=` и без `Bearer`;
* учётка прокси `user:pass@host` / `user:pass@1.2.3.4:port`;
* ключи словаря в контекстах Sentry: номер кабинета, `act_`-номер, почта.

Все значения синтетические. Каждая форма проверяется там, где работает общий
вычиститель: строка (`scrub`), словарь (`scrub_diag`), настоящий путь Sentry
(`acl_sentry._before_send`), строки Graph-пилота (`fb_api.telemetry`).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import acl_sentry  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa import run_summary  # noqa: E402
from src.fb_api import telemetry as fb_tel  # noqa: E402

FAKE_TOKEN = "EAAB" + "Zx9FAKEtoken0" * 6
FAKE_SHORT_PREFIX = "EAA" + "q1W2e3R4t5Y6u7I8o9P0aSdF"


# ── голый токен ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    FAKE_TOKEN,
    f"graph call failed with {FAKE_TOKEN} after retry",
    f"token was ({FAKE_TOKEN}), see log",
    f"url=https://graph.facebook.com/me?fields=id&x={FAKE_TOKEN}&y=1",
    f"page said: {FAKE_SHORT_PREFIX}",
    f'{{"value": "{FAKE_TOKEN}"}}',
])
def test_a_bare_access_token_is_replaced(raw):
    out = rt.scrub(raw)
    assert FAKE_TOKEN[3:20] not in out and FAKE_SHORT_PREFIX[3:20] not in out, out
    assert "<token>" in out


def test_a_keyed_token_keeps_the_existing_hidden_placeholder():
    assert rt.scrub(f"access_token={FAKE_TOKEN}") == "access_token=<hidden>"
    assert rt.scrub(f"Authorization: Bearer {FAKE_TOKEN}") == "Authorization: Bearer <hidden>"


@pytest.mark.parametrize("text", [
    "EAAB is a prefix, not a token",
    "EAABshort123",
    "the EAAs ran fine",
    "XEAAB" + "a" * 30,  # часть другого слова
])
def test_words_that_only_start_like_a_token_are_left_alone(text):
    assert rt.scrub(text) == text


# ── учётка прокси ──────────────────────────────────────────────────────────

PROXY_CASES = [
    ("proxyuser:pr0xyPass@proxy.example.net", "proxy.example.net"),
    ("proxyuser:pr0xyPass@198.51.100.7:8080", "198.51.100.7:8080"),
    ("http://proxyuser:pr0xyPass@198.51.100.7:8080/", "http://<user>:<hidden>@198.51.100.7:8080/"),
    ("socks5://proxyuser:pr0xyPass@proxy.example.net:1080", "socks5://<user>:<hidden>@proxy.example.net:1080"),
    ("proxy=proxyuser:pr0xyPass@198.51.100.7:3128 failed", "proxy=<user>:<hidden>@198.51.100.7:3128 failed"),
    ("proxy 'proxyuser:p@ss:w0rd@198.51.100.7:3128' refused", "proxy '<user>:<hidden>@198.51.100.7:3128' refused"),
    ("[proxyuser:pr0xyPass@[2001:db8::1]:8080]", "[<user>:<hidden>@[2001:db8::1]:8080]"),
    # пароль с `/` (base64) прячется целиком
    ("http://proxyuser:pr0xyPass/w0rd+==@198.51.100.7:8080", "http://<user>:<hidden>@198.51.100.7:8080"),
    ("proxyuser:pr0xyPass@localhost:3128", "<user>:<hidden>@localhost:3128"),
    ("proxyuser:pr0xyPass@localhost", "<user>:<hidden>@localhost"),
    ("proxyuser:pr0xyPass@proxy-internal", "<user>:<hidden>@proxy-internal"),
    ("socks5://proxyuser:pr0xyPass@localhost", "socks5://<user>:<hidden>@localhost"),
    ("proxyuser:/pr0xyPass@proxy.example.net", "<user>:<hidden>@proxy.example.net"),
    ("http://proxyuser:/pr0xyPass@localhost", "http://<user>:<hidden>@localhost"),
    ("123:pr0xyPass@2x", "<user>:<hidden>@2x"),
    ("proxyuser" * 100 + ":pr0xyPass@localhost", "<user>:<hidden>@localhost"),
    ("proxyuser:" + "pr0xyPass" * 100 + "@localhost", "<user>:<hidden>@localhost"),
]


@pytest.mark.parametrize("raw,expected", PROXY_CASES)
def test_proxy_credentials_are_hidden_and_the_host_is_kept(raw, expected):
    out = rt.scrub(raw)
    assert "proxyuser" not in out and "pr0xyPass" not in out and "w0rd" not in out, out
    assert "<user>:<hidden>@" in out
    assert expected in out, out


@pytest.mark.parametrize("text", [
    "write to support@example.com please",
    "mailto:support@example.com",
    "step 12:30 done, retry at 13:00",
    r"saved to C:\Users\demo@corp\out.txt",  # linux-ok: scrubber input string, not a real path
    "https://example.com/path",
    "time 10:15 user@ host",
    "email:john@example.com sent",
    "ratio 16:9@2x",
    "https://user@example.com/x",
    "C:/Users/a@b.example.com",
    "custom://user@example.com/path",
    "http://host:8080/u/a@b.com",
    "git@github.com:org/repo",
])
def test_ordinary_text_with_colons_and_at_signs_is_not_over_scrubbed(text):
    assert "<user>" not in rt.scrub(text)


@pytest.mark.parametrize("raw", [case[0] for case in PROXY_CASES])
def test_proxy_credentials_are_hidden_at_every_shared_export_boundary(raw):
    event = {"contexts": {"diag": {"why": raw}}}
    sentry = acl_sentry._before_send(event, {})
    attachment = run_summary._attachment_row({"diag": {"why": raw}}, "synthetic-run")
    for out in (
        rt.scrub_diag({"why": raw}), sentry["contexts"]["diag"],
        attachment["diag"], fb_tel.scrub_value({"why": raw}),
    ):
        assert "proxyuser" not in repr(out) and "pr0xyPass" not in repr(out), out
        assert "<user>:<hidden>@" in repr(out), out


# ── ключи словаря ──────────────────────────────────────────────────────────

ID_KEY_DIAG = {
    "act_1234567": "ok",
    "act_1234567890123456": "disabled",
    "1234567": "ok",
    "123456789012345": "ok",
    "buyer.name@example.com": "profile A",
    "accounts": {"act_7654321": {"owner": "someone@example.org"}},
    "step": "publish",
    "attempt_2": 3,
}


def _all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_keys(item)


@pytest.mark.parametrize("leak", ["1234567", "123456789012345", "7654321", "buyer.name", "example.com"])
def test_scrub_diag_hides_ids_and_emails_in_dict_keys(leak):
    keys = list(_all_keys(rt.scrub_diag(ID_KEY_DIAG)))
    assert not any(leak in str(k) for k in keys), keys


def test_scrub_diag_keeps_every_entry_when_scrubbed_keys_collide():
    out = rt.scrub_diag(ID_KEY_DIAG)
    assert len(out) == len(ID_KEY_DIAG)
    assert sorted(v for v in out.values() if isinstance(v, str)) == sorted(
        v for v in ID_KEY_DIAG.values() if isinstance(v, str))
    assert out["step"] == "publish" and out["attempt_2"] == 3
    assert "act_<hidden>" in out and "act_<hidden>#2" in out and "<email>" in out


@pytest.mark.parametrize("scrubber", [rt.scrub_diag, fb_tel.scrub_value])
@pytest.mark.parametrize("count", [128, 256, 512])
def test_colliding_id_keys_take_linear_dictionary_work(monkeypatch, scrubber, count):
    """Count actual helper operations; no machine-speed or timeout assumption."""
    original = rt.put_unique
    operations = 0

    class MeasuredDict(dict):
        def __contains__(self, key):
            nonlocal operations
            operations += 1
            return super().__contains__(key)

    def measured(out, *args, **kwargs):
        # Instrument the real dictionary interface without changing its contents.
        observed = MeasuredDict(out)
        original(observed, *args, **kwargs)
        out.clear()
        out.update(observed)

    monkeypatch.setattr(rt, "put_unique", measured)
    clean = scrubber({str(100000 + i): i for i in range(count)})
    assert len(clean) == count and sorted(clean.values()) == list(range(count))
    assert operations <= 4 * count, (count, operations)


@pytest.mark.parametrize("scrubber", [rt.scrub_diag, fb_tel.scrub_value])
def test_collision_suffixes_preserve_literal_keys_and_reset_per_dictionary(scrubber):
    raw = {"<id>#2": "literal", "100000": 1, "100001": 2,
           "<id>#3": "other literal", "100002": 3}
    expected = {"<id>#2": "literal", "<id>": 1, "<id>#3": 2,
                "<id>#3#2": "other literal", "<id>#4": 3}
    assert scrubber(raw) == expected
    assert scrubber({"left": raw, "right": raw}) == {"left": expected, "right": expected}


@pytest.mark.parametrize("key,leak", [
    ("acct 123456789", "123456789"),
    ("12345678-x", "12345678"),
    ("ad_act_1234567", "1234567"),
    ("ACT_99", "ACT_99"),
    ("Act_1234567:state", "1234567"),
])
def test_ids_inside_longer_keys_are_hidden(key, leak):
    out = rt.scrub_diag({key: 1})
    assert not any(leak in str(k) for k in out), out
    assert list(out.values()) == [1]


def test_integer_keys_holding_an_account_id_are_hidden():
    out = rt.scrub_diag({123456789: "x", 1234567890123: "y", 7: "small", True: "flag"})
    assert sorted(map(str, out)) == sorted(["<id>", "<digits>", "7", "True"]), out
    assert out[7] == "small" and out[True] == "flag"


@pytest.mark.parametrize("key", ["step","campaign_name", "ad_123", "v2", "2026-10-17", "SA_sa_All_Image_Mix"])
def test_ordinary_keys_are_unchanged(key):
    assert rt.scrub_key(key) == key


def test_dict_keys_are_scrubbed_through_the_real_acl_sentry_path():
    event = {"contexts": {"diag": dict(ID_KEY_DIAG)}}
    safe = acl_sentry._before_send(event, {})
    keys = list(_all_keys(safe["contexts"]["diag"]))
    for leak in ("1234567", "7654321", "buyer.name", "example.org"):
        assert not any(leak in str(k) for k in keys), (leak, keys)
    assert "1234567" in event["contexts"]["diag"]  # исходное событие не мутируется


def test_sentry_context_values_with_all_three_shapes_are_scrubbed():
    event = {"contexts": {"diag": {
        "why": f"graph said {FAKE_TOKEN}",
        "proxy": "http://proxyuser:pr0xyPass@198.51.100.7:8080",
    }}}
    diag = acl_sentry._before_send(event, {})["contexts"]["diag"]
    assert FAKE_TOKEN not in diag["why"] and "<token>" in diag["why"]
    assert "pr0xyPass" not in diag["proxy"] and "198.51.100.7:8080" in diag["proxy"]


# ── Graph-пилот: та же граница ─────────────────────────────────────────────

def test_graph_pilot_scrub_value_covers_the_same_shapes():
    out = fb_tel.scrub_value({
        "buyer.name@example.com": {"act_1234567": f"proxyuser:pr0xyPass@198.51.100.7:8080 {FAKE_TOKEN}"},
        "1234567": "x",
        "step": "insights",
    })
    flat = repr(out)
    for leak in ("buyer.name", "1234567", "proxyuser", "pr0xyPass", FAKE_TOKEN[3:20]):
        assert leak not in flat, (leak, flat)
    assert out["step"] == "insights"
    assert "198.51.100.7:8080" in flat
