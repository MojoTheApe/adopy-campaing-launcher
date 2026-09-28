r"""PR #343, ревью раунд 2: `scrub()` не должен зависать на обрезанном секрете.

Значение в кавычках разбиралось как `(?:\\.|(?!\4).)*` — обратный слэш
подходил под обе альтернативы, n пар «слэш+символ» разбирались 2^n способами,
и без закрывающей кавычки движок перебирал их все. Замеры ревью:
`{'token': '` + JSON-экранированный путь Windows на 120 символов — 35.7 с,
100 КБ `\a\a…` — больше двух минут.

Сценарий настоящий: `page_capture.scrub_html()` режет HTML по потолку ДО
`scrub()`, и если разрез приходится внутрь `"lsd":"https:\/\/…"`, поток
робота встаёт. Через `scrub` же идут запись телеметрии и Sentry
`_before_send`.

Каждый случай — отдельный процесс с жёстким таймаутом: регэксп-движок держит
GIL, и зависший случай в том же процессе заморозил бы весь прогон pytest, а
не упал бы. Время меряется внутри дочернего процесса — без запуска
интерпретатора и импортов.

Строки собраны через `chr(92)`, а не литералами с обратными слэшами: так их
содержимое не зависит от того, как файл был записан.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BS = chr(92)  # обратный слэш

#: Потолок на один вызов `scrub()`. Исправленный шаблон укладывается в
#: ~0.03 с на 100 КБ; прежний на тех же данных — секунды и минуты.
LIMIT_S = 0.5
#: Жёсткий таймаут дочернего процесса (с запуском интерпретатора). Нужен,
#: чтобы на старом шаблоне тест падал, а не висел.
HARD_TIMEOUT_S = 20

CASES = {
    # Ревью: JSON-экранированный путь Windows внутри `'token'`, обрезан.
    "truncated_windows_path_in_token": (
        "{'token': '" + ("C:" + BS + BS + "Users" + BS + BS + "x" + BS + BS) * 8,
        "Users",
    ),
    # Ревью: HTML в стиле FB, `"lsd":"https:\/\/…"` с 20 `\/`, обрезан.
    "truncated_fb_lsd_with_escaped_slashes": (
        '{"lsd":"https:' + (BS + "/") * 2 + "www.facebook.com" + (BS + "/a") * 18,
        "facebook.com",
    ),
    # То же, 26 `\/`: время на старом шаблоне удваивалось с каждым `\/`, и
    # случай из ревью (20) на быстрой машине иногда укладывался в потолок —
    # этот не укладывается никогда, так что откат ловится детерминированно.
    "truncated_fb_lsd_with_26_escaped_slashes": (
        '{"lsd":"https:' + (BS + "/") * 2 + "www.facebook.com" + (BS + "/a") * 24,
        "facebook.com",
    ),
    # Ревью: 100 КБ пар `\a` без закрывающей кавычки.
    "adversarial_100kb_unclosed": (
        '{"password":"' + (BS + "a") * 50_000,
        BS + "a" + BS + "a" + BS + "a",
    ),
    # То же в «голой» форме `key="…` — у неё свой шаблон значения.
    "adversarial_100kb_unclosed_bare_form": (
        'password="' + (BS + "a") * 50_000,
        BS + "a" + BS + "a" + BS + "a",
    ),
}

_CHILD = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
from src.fb_ads_rpa import run_telemetry as rt
text, secret = json.loads(sys.stdin.read())
t0 = time.perf_counter()
out = rt.scrub(text)
dt = time.perf_counter() - t0
print(json.dumps({"dt": dt, "leaked": secret in out, "hidden": "<hidden>" in out}))
"""


def _scrub_in_child(text: str, secret: str) -> dict:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run(
            [sys.executable, "-c", _CHILD, str(ROOT)],
            input=json.dumps([text, secret]), capture_output=True, text=True,
            encoding="utf-8", timeout=HARD_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"scrub() не закончил за {HARD_TIMEOUT_S} с — катастрофический бэктрекинг")
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


#: Раунд 2, второй пункт: префикс перед секретным окончанием ключа
#: (`x-api-key`) и граница camelCase. Префикс ограничен 40 символами и ленив,
#: вложенных квантификаторов нет — эти входы проверяют, что так и осталось.
KEY_PREFIX_CASES = {
    "many_long_quoted_non_secret_keys": ('"' + "a-" * 20 + '":"v",') * 2_300,
    "unclosed_quoted_key_run": '"' + "a-b." * 25_000,
    "hyphen_run": "a-" * 50_000,
    "dot_run": "x." * 50_000,
    "camel_hump_run": "aB" * 50_000,
    "separator_colon_run": "token:" * 16_000,
}


@pytest.mark.parametrize("name", list(KEY_PREFIX_CASES))
def test_key_prefix_matching_stays_linear(name):
    got = _scrub_in_child(KEY_PREFIX_CASES[name], "\x00never-present\x00")
    assert got["dt"] < LIMIT_S, f"{name}: scrub() занял {got['dt']:.3f} с (потолок {LIMIT_S} с)"


@pytest.mark.parametrize("name", list(CASES))
def test_a_cut_secret_is_scrubbed_fast(name):
    text, secret = CASES[name]
    got = _scrub_in_child(text, secret)
    assert got["dt"] < LIMIT_S, f"{name}: scrub() занял {got['dt']:.3f} с (потолок {LIMIT_S} с)"


@pytest.mark.parametrize("name", list(CASES))
def test_a_cut_secret_is_still_hidden(name):
    """Не только быстро, но и спрятано: до фикса незакрытое значение не
    совпадало вовсе, и начало секрета уезжало открытым текстом."""
    text, secret = CASES[name]
    got = _scrub_in_child(text, secret)
    assert got["hidden"], f"{name}: маскирование не сработало"
    assert not got["leaked"], f"{name}: обрезанный секрет утёк"
