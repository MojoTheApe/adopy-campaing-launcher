"""One append-only telemetry log for every RPA run — CLI and dashboard alike.

Replaces the ad-hoc debug dumps that grew per-bug during the 2026-07-25..29
debugging push (`debug_ad_copy_locator_miss.json`,
`debug_create_ad.json`, `debug_ad_level_soft_fails.txt`). Those each had a
different format, only one was append-only, and answering "how often does
this step actually fail, and with what diagnosis" meant reading PNG
screenshots one at a time.

Format: one JSON object per line (JSONL), append-only, never rewritten, so
concurrent runs and crashed runs can't lose or truncate history.

Each record:
    run_id     — groups records from one publish run
    ts         — UTC ISO timestamp
    step       — step name, normalised (no after_/fail_ prefix)
    ok         — True (step succeeded), False (failed), None (informational)
    campaign   — campaign name, when known
    duration_s — seconds since the previous step of the same run
    diag       — free-form dict with whatever the failing site knows
    note       — short free text
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
from src import acl_log
from config.paths import get_runtime_workspace
TELEMETRY_PATH = get_runtime_workspace() / "output" / "rpa_telemetry.jsonl"


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def normalise_step(label: str) -> tuple[str, bool | None]:
    """Split a screenshot label into (step, ok).

    The recorded flow already names every screenshot `after_<step>` on
    success and `fail_<step>` on failure, so those labels are a complete,
    zero-maintenance description of the run — no separate instrumentation
    to keep in sync with the steps themselves.
    """
    if label.startswith("after_"):
        return label[len("after_"):], True
    if label.startswith("fail_"):
        return label[len("fail_"):], False
    if label.startswith("success_"):
        return label[len("success_"):], True
    # «Пропущено» — третий исход, и он не то же самое, что удача или отказ.
    # Шаг детального таргетинга не выполнялся вовсе, когда в черновике не было
    # интересов, и не писал об этом ни строки: в потоке его просто не
    # существовало. 9 сентября это выглядело так, что интересы «не
    # проставились», а по телеметрии нельзя было отличить пропуск от отказа.
    # `ok=None` не попадает в долю неудач — пропуск не провал.
    if label.startswith("skip_"):
        return label[len("skip_"):], None
    return label, None


#: Что похоже на номер кабинета, id профиля или иной опознавательный знак.
#: Контекст отказа уезжает в общую таблицу телеметрии, которую читает вся
#: компания, — идентификаторы туда не едут.
#:
#: ACL-1342 S09: список имён секретов — раньше здесь ловилось только
#: `key=value` для `access_token`/`token`/`password`/`authorization`/
#: `fb_dtsg`/`lsd`, и то не для всех: `client_secret=`, `api_key=`,
#: `proxy_pass=` не совпадали (в списке не было ни "secret", ни "key", ни
#: точного "proxy_pass"), а `"password":"..."` — JSON- и python-repr-форма
#: с двоеточием вместо `=` — не совпадала вовсе, для любого из имён.
#:
#: PR #343, ревью: `re.I` снимает регистр букв, но не разницу между
#: `refresh_token` и `refreshToken` — camelCase-варианты (обычные для
#: JS-объектов на странице Facebook/сторонних SDK) физически не совпадали
#: ни с одним паттерном. `_flex_key()` делает подчёркивание между частями
#: составного имени необязательным — один шаблон ловит snake_case И
#: camelCase/PascalCase сразу, без отдельного списка вариантов на каждое имя.
#:
#: PR #343, ревью раунд 2: необязательным было только `_`, и целиком утекали
#: имена через дефис, точку и пробел (`REFRESH-TOKEN`, `refresh.token`,
#: `refresh token`) — а это как раз имена HTTP-заголовков: `x-api-key`,
#: `X-Api-Key`, `X-Refresh-Token`. Разделитель между частями теперь любой из
#: `[-_. ]` или никакой.
_KEY_SEP = r"[-_. ]?"


def _flex_key(name: str) -> str:
    return name.replace("_", _KEY_SEP)


_SECRET_KEY_NAMES = (
    "refresh_token", "access_token", "token", "client_secret", "api_key",
    "proxy_password", "proxy_pass", "passwd", "password", "authorization",
    "fb_dtsg", "lsd",
)
_SECRET_KEYS = "(?:" + "|".join(_flex_key(k) for k in _SECRET_KEY_NAMES) + ")"

#: Для «голой» формы `key: value` / `key=value` без кавычек "authorization"
#: исключён: заголовок обычно пишется как "Authorization: Bearer <токен>", и
#: с двоеточием в общем списке это разобралось бы как key="authorization",
#: value="Bearer" — отдельное слово "Bearer" вместо всего токена, который сам
#: по себе прячет паттерн `Bearer` ниже. Через `=` ("authorization=...")
#: поведение осталось прежним — этот случай ловится отдельно.
_SECRET_KEYS_BARE = "(?:" + "|".join(
    _flex_key(k) for k in _SECRET_KEY_NAMES if k != "authorization"
) + ")"

#: Секретное имя сверяется по ОКОНЧАНИЮ ключа: `x-api-key`, `X_Refresh_Token`,
#: `xApiKey`, `csrf_token` — префикс может быть любым.
#:
#: В кавычках (`"x-api-key": …`) ключ целиком стоит между кавычками, поэтому
#: префикс — ограниченная (≤ 40) ленивая серия символов имени перед секретным
#: окончанием: на каждой кавычке не больше 41 попытки, без вложенных
#: квантификаторов — линейно.
_QUOTED_KEY_PREFIX = r"[A-Za-z0-9_.\- ]{0,40}?"
#: Без кавычек префикс не захватывается, а начало имени требуется на границе:
#: либо перед ним не буква/цифра (`X-Api-Key`, `x_api_key` — `_` здесь тоже
#: граница, в отличие от `\b`), либо это «горб» camelCase (`xApiKey`,
#: `csrfToken`: строчная/цифра, затем заглавная; сверяется с учётом регистра
#: через `(?-i:…)`, иначе `re.I` превратил бы проверку в «любые две буквы» и
#: `monkeyapikey` резалось бы посередине).
_BARE_KEY_START = r"(?:(?<![A-Za-z0-9])|(?-i:(?<=[a-z0-9])(?=[A-Z])))"

#: Значение в кавычках после «голого» ключа. Закрывающая кавычка
#: необязательна и перевод строки внутрь не входит — см. `_QUOTED_VALUE`.
_BARE_VALUE = r'("(?:[^"\\\n]|\\.)*"?|\'(?:[^\'\\\n]|\\.)*\'?|[^\s,;&"\'<>\)\]\}]{1,16384})'

#: Значение в кавычках для JSON/repr-формы (группа 4 — открывающая кавычка).
#:
#: PR #343, ревью раунд 2 — катастрофический бэктрекинг. Прежнее
#: `(?:\\.|(?!\4).)*` было неоднозначным: обратный слэш подходил под ОБЕ
#: альтернативы (`\\.` брал пару `\x`, `(?!\4).` — один `\`), так что n пар
#: разбирались 2^n способами, и без закрывающей кавычки движок перебирал их
#: все. Замер: обрезанный `{'token': '` + JSON-экранированный путь Windows на
#: 120 символов — 35.7 с; 100 КБ `\a\a…` — больше двух минут. Реальный
#: спусковой крючок — `page_capture.scrub_html()`: он режет HTML по потолку
#: ДО `scrub()`, и разрез внутри `"lsd":"https:\/\/…` вешал поток робота.
#:
#: Теперь альтернативы взаимоисключающие: первая требует `\`, вторая его
#: исключает (`[^\\\n]`) — разбор единственный, время линейное. Перевод строки
#: значение не пересекает (как у прежнего `.`), так что незакрытое значение не
#: съедает остаток многострочного текста.
#:
#: Закрывающая кавычка (группа 6) необязательна: если значение обрезано —
#: именно тот случай из `scrub_html()` — до фикса регэксп просто не находил
#: совпадения, и начало секрета уезжало открытым текстом. Теперь прячется
#: всё до конца строки/текста.
_QUOTED_VALUE = r'((?:\\.|(?!\4)[^\\\n])*)(\4)?'


_PROXY_USER = re.compile(r"(?<![^\s/=,;(\[{<>\"'])([^\s:@/\\\"'<>=,;(\[{]+):")
_URI_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")
_RATIO_SCALE = re.compile(r"\d+:\d+@\d+(?:\.\d+)?x[,;)\]}]*", re.I)


def _scrub_proxy(match: re.Match) -> str:
    """Inspect each word once, without a credential-length escape hatch."""
    word = match[0]
    at = word.rfind("@")
    if at < 0 or at == len(word) - 1:
        return word
    if not (word[at + 1].isalnum() or word[at + 1] == "["):
        return word
    authority_start = None
    for user in _PROXY_USER.finditer(word, 0, at):
        name = user[1]
        password_start = user.end()
        if password_start == at:
            continue
        userinfo = user.start() == authority_start
        if not userinfo and word.startswith("//", password_start) and _URI_SCHEME.fullmatch(name):
            authority_start = password_start + 2
            continue
        # Once an outer URI introduced userinfo, its login/password cannot
        # become an ordinary mail label, Windows drive, nested scheme or ratio.
        if not userinfo:
            if name.lower() in {"mailto", "email", "e-mail"}:
                continue
            if len(name) == 1 and name.isalpha() and word.startswith("/", password_start):
                continue  # C:/Users/a@host is a Windows path.
            if _RATIO_SCALE.fullmatch(word, user.start()):
                return word
        if (word[max(0, user.start() - 3):user.start()] == "://"
                and re.match(r"\d{1,5}/", word[password_start:at])):
            return word  # A URL host:port/path@example.com, not userinfo.
        return word[:user.start()] + "<user>:<hidden>@" + word[at + 1:]
    return word


_SCRUB = (
    # JSON / python-repr словарь: "key":"value" или 'key': 'value' — с любыми
    # пробелами вокруг двоеточия, кавычки ключа и значения не обязаны
    # совпадать друг с другом. Экранированная кавычка (`\"`) внутри значения
    # поглощается парой `\\.` и не закрывает его раньше времени.
    (re.compile(rf'(["\'])({_QUOTED_KEY_PREFIX}{_SECRET_KEYS})\1(\s*:\s*)(["\']){_QUOTED_VALUE}', re.I),
     lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{m.group(4)}<hidden>{m.group(6) or ''}"),
    # key=value / key: value без кавычек вокруг имени, внутри произвольного
    # текста лога — то, что раньше проверял единственный `(...)=` паттерн.
    (re.compile(rf'{_BARE_KEY_START}({_SECRET_KEYS_BARE})(\s*[:=]\s*){_BARE_VALUE}', re.I),
     lambda m: f"{m.group(1)}{m.group(2)}<hidden>"),
    (re.compile(rf'{_BARE_KEY_START}(authorization)(\s*=\s*){_BARE_VALUE}', re.I),
     lambda m: f"{m.group(1)}{m.group(2)}<hidden>"),
    (re.compile(r"\bBearer [A-Za-z0-9._~+/-]{1,16384}", re.I), "Bearer <hidden>"),
    # ACL-1558: голый токен Graph API (`EAAB…`/`EAAG…`) без имени ключа и
    # без `Bearer` рядом. Ключевые формы выше уже заменили его на `<hidden>`,
    # сюда доходит только то, что они пропустили. Токен, приклеенный к
    # букве (`tokenEAAB…`), не ловится — иначе резались бы обычные слова с
    # `EAA` внутри; приклеенный к `_`, `=`, `/`, кавычке — ловится.
    (re.compile(r"(?<![A-Za-z0-9])EAA[A-Za-z0-9]{20,16384}"), "<token>"),
    # ACL-1558: учётка прокси `user:pass@host[:port]` (в т.ч. после `http://`).
    # Хост и порт остаются — по ним видно, какой прокси отказал; логин и
    # пароль прячутся всегда оба. Начало — только в начале «слова» (после
    # пробела, `/`, `=`, кавычки, скобки…), иначе каждая позиция длинной
    # строки начинала бы свой перебор. Каждый отдельный фрагмент без
    # пробела/кавычки проверяется один раз: последняя @, затем непересекающиеся
    # имена перед двоеточием. Длинный пароль не остаётся открытым из-за лимита.
    # `/`, `:` и `@` в пароле допустимы; схемы, пути и ratio отличаем отдельно.
    (re.compile(r"[^\s\\\"'<>]+"), _scrub_proxy),
    (re.compile(r"(act=)(?:act_)?\d{6,}"), r"\1<hidden>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{10,}\b"), "<digits>"),
)


#: ACL-103: адрес, логин и пароль BI (MySQL) живут только в машинном .env.
#: Текст ошибки драйвера содержит их дословно («Can't connect to MySQL server
#: on '<host>'», «Access denied for user '<user>'@…»), и по имени ключа их не
#: найти, поэтому прячем сами настроенные значения.
_BI_ENV_NAMES = ("BI_MYSQL_HOST", "BI_MYSQL_USER", "BI_MYSQL_PASSWORD")


def _hide_configured_bi(text: str) -> str:
    for name in _BI_ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if len(value) >= 3:
            text = re.sub(re.escape(value), "<hidden>", text, flags=re.I)
    return text


def _hide_configured_secrets(text: str) -> str:
    """ACL-1358: спрятать сами значения токенов этой машины (Multilogin, Google).

    Токен без имени ключа рядом (`eyJ…` в тексте ошибки, `refresh_token` в
    выдержке страницы) шаблоны не опознают; значения берутся из окружения и
    файлов токенов, в том числе из защищённого хранилища.
    """
    try:
        from config import token_vault

        return token_vault.hide_configured_secrets(text)
    except Exception:
        pass
    return text


def hide_secrets_deep(value):
    """`_hide_configured_secrets` по всем строкам вложенной структуры (ключи тоже)."""
    if isinstance(value, dict):
        return {(_hide_configured_secrets(k) if isinstance(k, str) else k): hide_secrets_deep(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [hide_secrets_deep(v) for v in value]
    return _hide_configured_secrets(value) if isinstance(value, str) else value


def scrub(text: str) -> str:
    """Убрать из строки всё, по чему опознаётся аккаунт или профиль."""
    out = _hide_configured_secrets(_hide_configured_bi(str(text or "")))
    for pattern, replacement in _SCRUB:
        out = pattern.sub(replacement, out)
    return out


#: ACL-1342 S09: та же расширенная граница, что и в _SECRET_KEYS — диагностика
#: приходит и настоящим словарём (не только строкой из логов), и по ключу
#: словаря значение не пройдёт через `scrub()` (в самом значении имени ключа
#: обычно нет, скрести регуляркой там нечего).
#:
#: PR #343, ревью: тот же camelCase-разрыв, что и в _SECRET_KEYS, но здесь
#: ключ — настоящий ключ Python-словаря, а не текст в строке, так что
#: `_flex_key()`/regex не применить — сравнение сделано побуквенно: обе
#: стороны (имя из словаря и имя из списка) сравниваются без подчёркиваний,
#: так что `refreshToken`, `refresh_token` и `REFRESH_TOKEN` бьют в одну и
#: ту же запись.
#:
#: PR #343, ревью раунд 2: разделителями бывают и `-`, `.`, пробел
#: (`X-Api-Key`, `refresh-token`, `refresh.token`), а у заголовков — префикс
#: (`x-api-key`). Имя нормализуется без `[-_. ]`, и секретом считается и
#: точное совпадение, и совпадение по окончанию. Для совпадения только по
#: окончанию значение прячется, если это не флаг/число/None: `has_token: True`
#: — диагностика («есть ли токен»), а не сам токен, и в Sentry должен остаться.
_SECRET_DICT_KEYS = {
    "token", "access_token", "refresh_token", "password", "passwd",
    "client_secret", "api_key", "apikey", "proxy_pass", "proxy_password",
    "authorization", "fb_dtsg", "lsd",
}
_KEY_NORM_DROP = re.compile(r"[-_. ]")
_SECRET_DICT_KEYS_NORM = {_KEY_NORM_DROP.sub("", k) for k in _SECRET_DICT_KEYS}


def _norm_key(key: object) -> str:
    return _KEY_NORM_DROP.sub("", str(key).lower())


def _hides_dict_value(key: object, item: object) -> bool:
    norm = _norm_key(key)
    if norm in _SECRET_DICT_KEYS_NORM:
        return True
    if isinstance(item, (bool, int, float)) or item is None:
        return False
    return any(norm.endswith(name) for name in _SECRET_DICT_KEYS_NORM)


#: ACL-1558: ключ словаря тоже бывает идентификатором — диагностика вида
#: «номер кабинета → состояние» или «почта → профиль». `scrub()` в ключе
#: ловит только ≥10 цифр подряд; короткие `act_<номер>`, номер кабинета
#: из 6–9 цифр и почта проходили в контексты Sentry как есть (ACL-1554
#: закрыл это только для вложения итога, `run_summary._scrub_strings`).
_KEY_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}\b")
_KEY_ACT = re.compile(r"(?<![A-Za-z0-9])act_\d+", re.I)
_KEY_DIGITS = re.compile(r"(?<![A-Za-z0-9])\d{6,}(?![A-Za-z0-9])")


def scrub_key(key: str) -> str:
    """Ключ словаря без номеров кабинетов, `act_`-номеров и почты."""
    out = scrub(key)
    out = _KEY_EMAIL.sub("<email>", out)
    out = _KEY_ACT.sub("act_<hidden>", out)
    return _KEY_DIGITS.sub("<id>", out)


def clean_key(key):
    """Строковый ключ — через `scrub_key`; целый (номер кабинета числом) —
    тоже, но остаётся числом, если прятать нечего."""
    if isinstance(key, str):
        return scrub_key(key)
    if isinstance(key, int) and not isinstance(key, bool):
        clean = scrub_key(str(key))
        return key if clean == str(key) else clean
    return key


def put_unique(out: dict, key, item, *, suffixes: dict) -> None:
    """Append without loss; suffix progress belongs to this output dictionary."""
    if key in out:
        n = suffixes.get(key, 2)
        while f"{key}#{n}" in out:
            n += 1
        suffixes[key] = n + 1
        key = f"{key}#{n}"
    out[key] = item


def scrub_diag(value):
    """Explicit step diagnostics share the same export boundary as page context."""
    if isinstance(value, dict):
        out: dict = {}
        suffixes: dict = {}
        for key, item in value.items():
            clean = "<hidden>" if _hides_dict_value(key, item) else scrub_diag(item)
            put_unique(out, clean_key(key), clean, suffixes=suffixes)
        return out
    if isinstance(value, (list, tuple)):
        return [scrub_diag(item) for item in value]
    return scrub(value) if isinstance(value, str) else value


def merge_diag(collected: dict | None, explicit: dict | None) -> dict | None:
    """Соединить общий снимок обстановки с тем, что шаг рассказал о себе сам.

    Два правила, и оба нужны:

    * **Точное важнее общего.** Если шаг назвал причину своим ключом, снимок
      обстановки его не затирает.
    * **Общее не теряется.** Шаг, рассказавший о себе, не должен из-за этого
      лишиться адреса страницы и заголовка — по ним отказ находят в снимках.

    Раньше это было условием `if diag is None` в теле `_shot`: шаг с
    собственной причиной оставался без адреса страницы. Правило вынесено
    сюда, чтобы его можно было проверить, а не угадывать по виду кода.
    """
    merged = {**(collected or {}), **(explicit or {})}
    return merged or None


def _process_session_id(pid: int) -> int | None:
    """Return a Windows session ID; inability to query is not session zero."""
    try:
        import ctypes
        from ctypes import wintypes

        query = ctypes.windll.kernel32.ProcessIdToSessionId
        query.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL
        session = wintypes.DWORD()
        if query(pid, ctypes.byref(session)):
            return session.value
    except Exception:
        pass
    return None


def screen_locked() -> bool | None:
    """Observe LogonUI in ACL's Windows session; unknown probes return None.

    A process in another signed-in/RDP session says nothing about the browser
    driven by this desktop app. The observation is a possible explanation for
    a stalled upload, not proof of the cause of a particular campaign error.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import psutil

        own_session = _process_session_id(os.getpid())
        if own_session is None:
            return None
        unknown = False
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() == "logonui.exe":
                session = _process_session_id(proc.pid)
                if session is None:
                    unknown = True
                elif session == own_session:
                    return True
        return None if unknown else False
    except Exception:
        return None


def window_state(page) -> dict:
    """Размер и состояние окна браузера; {} если прочитать нельзя. Не бросает."""
    try:
        w = page.evaluate(
            "() => ({inner: [innerWidth, innerHeight], outer: [outerWidth, outerHeight],"
            " dpr: devicePixelRatio, visibility: document.visibilityState,"
            " focus: document.hasFocus()})"
        )
    except Exception:
        return {}
    if not isinstance(w, dict):
        return {}
    try:
        w["minimized"] = bool(w["outer"][0] < 200 or w["outer"][1] < 100)
    except Exception:
        pass
    return w


def page_context(page, *, timeout_ms: int = 1200) -> dict:
    """Что было на странице в момент отказа — коротко и безопасно.

    До сих пор у отказов записывался только факт: «ad_copy: false». Причины не
    было ни у человека, ни у будущего агента. 9 сентября прогон дал шесть
    отказов, и по телеметрии нельзя было сказать про них ничего.

    Собирается в **одной** точке — там же, где пишется сама запись, — а не в
    сорока трёх местах вызова. Разойтись с ними нечему, и новый шаг получит
    контекст, ничего для этого не делая.

    Три правила, которые здесь важнее полноты:

    * **Никогда не бросает.** Строка лога не должна стоить кампании — то же
      правило, что у `record`.
    * **Ограничено по времени.** Ожидание на упавшем шаге платит человек,
      сидящий перед экраном.
    * **Вычищено.** Адрес Ads Manager содержит номер кабинета, и он не поедет
      в общую таблицу.
    """
    info: dict[str, Any] = {}
    try:
        info["url"] = scrub(str(getattr(page, "url", "") or ""))[:200]
    except Exception:
        pass
    for name, call in (
        ("title", lambda: page.title()),
        ("dialogs", lambda: page.get_by_role("dialog").count()),
    ):
        try:
            value = call()
            if isinstance(value, str):
                value = scrub(value)[:120]
            info[name] = value
        except Exception:
            continue
    # Окно браузера: размер, масштаб, свёрнуто ли. 17.09 04:20 сбои S15/Doc21
    # совпали со свёрнутыми окнами и масштабом 0.75, но раньше свёрнутые окна
    # проходили — без этой записи связь не проверить.
    window = window_state(page)
    if window:
        info["window"] = window
    # Верхний баннер Meta закрывает и сдвигает контролы — это самая частая
    # причина, по которой шаг «не нашёл» существующую кнопку.
    try:
        banner = page.get_by_role("banner").first
        if banner.is_visible(timeout=timeout_ms):
            info["banner"] = scrub(banner.inner_text(timeout=timeout_ms))[:120]
    except Exception:
        pass
    # Страница-ошибка самого браузера. Когда она открыта, все прочие выводы —
    # шум: шаг честно не найдёт кнопку, которой нет, и сообщит «не нашёл поле
    # имени кампании» вместо «нет связи». 10 сентября прогон встал на
    # ERR_SSL_PROTOCOL_ERROR — на проксированном профиле это почти всегда сам
    # прокси, и искать надо там, а не в разметке Meta.
    err = browser_error(page)
    if err:
        info["browser_error"] = err
    # Заблокированный экран останавливает страницу так же, как свёрнутое окно,
    # только сильнее. Без этой строки отказ читается как поломка разметки, а
    # человек уходит искать её в Ads Manager.
    if screen_locked():
        info["screen_locked"] = True
    return info


_ERROR_CODE = re.compile(r"\bERR_[A-Z0-9_]{3,40}\b")


def browser_error(page) -> str:
    """Код ошибки браузера, если открыта его собственная страница-ошибка.

    Chrome рисует её со своим узлом `#main-frame-error` и кодом внутри —
    `ERR_SSL_PROTOCOL_ERROR`, `ERR_PROXY_CONNECTION_FAILED` и подобными.
    Ищем узел, а не текст по всей странице: слово `ERR_…` может встретиться и
    в скриптах живой страницы, а нам нужен именно этот экран.
    """
    try:
        text = page.evaluate(
            """
            () => {
              const el = document.querySelector('#main-frame-error');
              if (!el) return '';
              const code = el.querySelector('.error-code');
              return (code ? code.textContent : el.textContent) || '';
            }
            """
        )
    except Exception:
        return ""
    match = _ERROR_CODE.search(text or "")
    return match.group(0) if match else ""


#: Какой прогон идёт **в этом потоке**. Контекстная переменная, а не словарь
#: на модуль: с acl-1210 два прогона идут разом в двух потоках, и общий
#: словарь подписал бы строки одного номером другого — а разбор любого
#: прогона это фильтр по `run_id`. Поток задания ставит своё значение при
#: старте; всё, что он вызывает, читает его даром, как и прежде.
_current_var: ContextVar[dict[str, str]] = ContextVar("acl_current_run")


def _current_state() -> dict[str, str]:
    try:
        return _current_var.get()
    except LookupError:
        state = {"run_id": "", "campaign": ""}
        _current_var.set(state)
        return state


def set_current_run(run_id: str = "", campaign: str = "") -> None:
    """Запомнить, какой прогон идёт сейчас — в этом потоке.

    Об отказах сообщают сорок три места, и не все они знают номер прогона:
    `create_additional_ad` 9 сентября записался с пустым `run_id` и потому
    не попал ни в один прогон — при разборе его пришлось искать по времени.
    Место, которое знает номер, называет его один раз; остальные получают
    его даром.
    """
    old = _current_state()
    _current_var.set({
        **old,
        "run_id": run_id or old.get("run_id", ""),
        "campaign": campaign or old.get("campaign", ""),
    })


def set_current_job(job_id: str) -> None:
    """Запомнить задание публикации дешборда, которое идёт в этом потоке.

    ACL-1315, повторное ревью PR #314: точка сборки в билдере сопоставляла
    строки телеметрии с кампанией только по имени — два задания с
    одноимёнными кампаниями на разных аккаунтах получали шаги и блокеры друг
    друга. Номер задания в строке (`job`) делает сопоставление точным.
    """
    state = dict(_current_state())
    state["job"] = job_id or ""
    _current_var.set(state)


def current_job_id() -> str:
    return _current_state().get("job") or ""


def clear_current_run() -> None:
    """Забыть прогон в этом потоке — для тестов и для конца задания."""
    _current_var.set({"run_id": "", "campaign": ""})


class _CurrentView:
    """`_current` as callers knew it — a dict-like view of THIS thread's run.

    Kept on purpose: open branches read `_telemetry._current.get("run_id")`
    (the upload memo of acl-1171), and the first live tree that merged
    acl-1210 next to it failed every upload with "no attribute '_current'"
    (run 86b7cd7edda9, 2026-09-16 14:03). New code should call
    `current_run_id()`; old code keeps working through this.
    """

    def get(self, key: str, default: str = "") -> str:
        return _current_state().get(key, default) or default

    def __getitem__(self, key: str) -> str:
        return _current_state()[key]

    def __setitem__(self, key: str, value: str) -> None:
        state = dict(_current_state())
        state[key] = value
        _current_var.set(state)

    def __contains__(self, key: str) -> bool:
        return key in _current_state()


_current = _CurrentView()


def current_run_id() -> str:
    return _current_state().get("run_id") or ""


def current_campaign() -> str:
    return _current_state().get("campaign") or ""


# ── ACL-1614: тишина задания и брошенные задания ────────────────────────────
#
# Когда последняя строка телеметрии задания — по ней `publish_jobs` видит, что
# робот застрял (`step_stuck`), и в чём именно (последний шаг). Брошенное по
# сроку отмены задание больше ничего не пишет: проснувшийся позже поток не
# должен дописывать строки в уже закрытое задание.
_clock = time.monotonic
_LAST_BY_JOB: dict[str, tuple[float, str]] = {}
#: ACL-1702: задание → номер его прогона (сторож пишет из своего потока, где
#: номера прогона нет: он в ContextVar потока задания).
_RUN_BY_JOB: dict[str, str] = {}
#: ACL-1702: строки сторожа задания — тишину задания они не сбрасывают.
WATCHER_STEPS = frozenset({"step_stuck", "tab_unblocked", "tab_unblock_skipped"})
_ABANDONED: set[str] = set()
_JOBS_LOCK = threading.Lock()
_JOBS_MAX = 256


def clock() -> float:
    return _clock()


def last_activity(job_id: str) -> tuple[float, str] | None:
    """(время по `clock()`, шаг) последней строки задания — или None."""
    with _JOBS_LOCK:
        return _LAST_BY_JOB.get(str(job_id))


def last_run_id(job_id: str) -> str:
    """Номер прогона последней строки задания (ACL-1702) — или ""."""
    with _JOBS_LOCK:
        return _RUN_BY_JOB.get(str(job_id), "")


def abandon_job(job_id: str) -> None:
    """Задание брошено: строки его потока больше не пишутся."""
    with _JOBS_LOCK:
        _ABANDONED.add(str(job_id))
        while len(_ABANDONED) > _JOBS_MAX:
            _ABANDONED.pop()


def forget_job(job_id: str) -> None:
    with _JOBS_LOCK:
        _LAST_BY_JOB.pop(str(job_id), None)
        _RUN_BY_JOB.pop(str(job_id), None)


def beat() -> None:
    """Задание живо без новой строки: время последней активности, шаг прежний."""
    job = current_job_id()
    if not job:
        return
    with _JOBS_LOCK:
        seen = _LAST_BY_JOB.get(job)
        _LAST_BY_JOB[job] = (_clock(), seen[1] if seen else "")


def _touch_job(job_id: str, step: str, run_id: str = "") -> None:
    with _JOBS_LOCK:
        _LAST_BY_JOB[job_id] = (_clock(), step)
        while len(_LAST_BY_JOB) > _JOBS_MAX:
            _LAST_BY_JOB.pop(next(iter(_LAST_BY_JOB)))
        if run_id:
            _RUN_BY_JOB[job_id] = run_id
            while len(_RUN_BY_JOB) > _JOBS_MAX:
                _RUN_BY_JOB.pop(next(iter(_RUN_BY_JOB)))


def record(
    step: str,
    ok: bool | None = None,
    *,
    run_id: str = "",
    campaign: str = "",
    duration_s: float | None = None,
    diag: dict[str, Any] | None = None,
    note: str = "",
    path: Path | None = None,
) -> None:
    """Append one record. Never raises — telemetry must not break a run."""
    # ACL-1614: брошенное задание (поток завис, Stop истёк по сроку) молчит.
    try:
        _job = current_job_id()
        if _job:
            if _job in _ABANDONED:
                return
            # Сама `step_stuck` тишину не сбрасывает: сигнал один на простой.
            # ACL-1702: и строки сторожа вкладки (`WATCHER_STEPS`) тоже.
            if step not in WATCHER_STEPS:
                _touch_job(_job, step, run_id or current_run_id())
    except Exception:
        pass
    try:
        p = path or TELEMETRY_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        rec: dict[str, Any] = {
            "run_id": run_id or current_run_id(),
            "ts": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "ok": ok,
        }
        campaign = campaign or current_campaign()
        if campaign:
            rec["campaign"] = campaign
        job = current_job_id()
        if job:
            rec["job"] = job
        if duration_s is not None:
            rec["duration_s"] = round(duration_s, 1)
        if diag:
            rec["diag"] = scrub_diag(diag)
        if note:
            # ACL-1358: примечание не чистится шаблонами (оно уже человеческий
            # текст), но значение токена этой машины в нём быть не может.
            rec["note"] = _hide_configured_secrets(note)
        # Через `acl_log`, а не `open(..., "a")`. Обоснование формата выше
        # («concurrent runs ... can't lose or truncate history») верно для
        # POSIX и неверно для Windows: там режим «a» эмулируется как «перейти
        # в конец, затем записать». Замер 15.09 — двенадцать писателей, 720
        # записей: целых 590, потеряно 130. Формат при этом правильный,
        # менять его не нужно — нужен замок.
        acl_log.append_line(p, json.dumps(rec, ensure_ascii=False))
    except Exception as exc:
        # Recording must not interrupt a run, but a lost record must be visible.
        # Avoid copying the event payload or credentials into the fallback log.
        try:
            logging.getLogger(__name__).error("Telemetry journal write failed (%s)", type(exc).__name__)
        except Exception:
            pass  # Even a broken logging handler must not abort browser work.

    # ACL-354 отправлял в Sentry каждую строку с `ok=False`. ACL-1318: так
    # больше не делаем — 25.09 список тикетов состоял из диагностики
    # (`advantage_plus_off: still-on` ×36, `click_target_covered: silent`
    # ×14, `create_took`, строки промежуточных попыток), и настоящий отказ
    # `ad2_copy` в нём терялся. Здесь строка только запоминается: какой шаг
    # в каком прогоне отказал и чем объяснил. В Sentry уходят лишь отказы,
    # которые портят кампанию, — их решает `sentry_signal` в конце попытки и
    # по итогу кампании, а контекст берёт отсюда.
    if ok is False:
        try:
            _remember_failure(run_id or current_run_id(), step, _why_of(note, diag), diag)
        except Exception:
            pass
    # ACL-1447: шаги прогона копятся и в памяти — из них строится итог
    # кампании для Sentry (`run_summary`), без чтения журнала с диска.
    try:
        _remember_step(run_id or current_run_id(), step, ok, duration_s,
                       _why_of(note, diag) if ok is not True else "")
    except Exception:
        pass


#: Последний отказ каждого шага в каждом прогоне: (run_id, step) -> {why, diag}.
#: Ограничен по размеру — процесс дешборда живёт сутками.
_FAILURES: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()
_FAILURES_LOCK = threading.Lock()
_FAILURES_MAX = 512


def _remember_failure(run_id: str, step: str, why: str, diag: dict[str, Any] | None) -> None:
    key = (run_id or "", step or "")
    with _FAILURES_LOCK:
        known = _FAILURES.pop(key, None) or {}
        _FAILURES[key] = {
            # Шаг часто пишет две строки: первая с причиной, вторая — общий
            # отказ со снимком страницы и пустой причиной (audience_location
            # 25.09 17:54). Причина первой не должна теряться.
            "why": why or known.get("why", ""),
            "diag": scrub_diag(diag) if diag else None,
        }
        while len(_FAILURES) > _FAILURES_MAX:
            _FAILURES.popitem(last=False)


def last_failure(step: str, run_id: str = "") -> dict[str, Any] | None:
    """Последний отказ шага `step` в прогоне (по умолчанию — текущем).

    `{"why": …, "diag": …}` — `diag` уже вычищен тем же `scrub_diag`, что и
    журнал. `None`, если шаг в этом прогоне не отказывал.
    """
    key = (run_id or current_run_id(), step or "")
    with _FAILURES_LOCK:
        found = _FAILURES.get(key)
        return dict(found) if found else None


#: Шаги каждого прогона по порядку (ACL-1447): run_id -> [{step, ok, duration_s, why}].
#: Ограничено и по числу прогонов, и по шагам в прогоне — процесс дешборда
#: живёт сутками, а итог кампании — не журнал.
_STEPS: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
_STEPS_LOCK = threading.Lock()
_STEPS_MAX_RUNS = 64
_STEPS_MAX_PER_RUN = 400


def _remember_step(run_id: str, step: str, ok: bool | None,
                   duration_s: float | None, why: str) -> None:
    if not run_id:
        return
    row: dict[str, Any] = {"step": step or "", "ok": ok}
    if duration_s is not None:
        row["duration_s"] = round(float(duration_s), 1)
    if why:
        row["why"] = str(why)[:300]
    with _STEPS_LOCK:
        rows = _STEPS.pop(run_id, None) or []
        if len(rows) < _STEPS_MAX_PER_RUN:
            rows.append(row)
        _STEPS[run_id] = rows
        while len(_STEPS) > _STEPS_MAX_RUNS:
            _STEPS.popitem(last=False)


def run_steps(run_id: str = "") -> list[dict[str, Any]]:
    """Шаги прогона `run_id` (по умолчанию — текущего) в порядке записи.

    `why` — причина как её записал шаг, ещё НЕ вычищенная: вычищает тот, кто
    отправляет (`run_summary`).
    """
    key = run_id or current_run_id()
    with _STEPS_LOCK:
        return [dict(r) for r in _STEPS.get(key, [])]


@contextmanager
def phase(name: str, *, campaign: str = "", diag: dict[str, Any] | None = None):
    """Строка `job_phase` с длительностью этапа задания (ACL-1313).

    От нажатия в дешборде до подключения к профилю уходило ~60 с, и
    сказать, на что, было нечем: телеметрия начиналась с первого шага в Ads
    Manager. Этапы начала задания (сборка черновиков, библиотека, ожидание
    профиля, подключение, первый переход) пишут себя сами. Вызывающий может
    дополнить `diag` по ходу — он отдаётся в `with … as info`.

    Исключение этапа не глотается; строка тогда с `ok=None` и типом ошибки —
    сам отказ сообщает тот, кто его поймает.
    """
    started = time.monotonic()
    info: dict[str, Any] = dict(diag or {})
    ok: bool | None = None
    try:
        yield info
        ok = True
    except BaseException as exc:
        info["error"] = type(exc).__name__
        raise
    finally:
        record("job_phase", ok, campaign=campaign, note=name,
               duration_s=time.monotonic() - started, diag=info or None)


def _why_of(note: str, diag: dict[str, Any] | None) -> str:
    """Чем шаг объяснил свой отказ.

    Сам шаг кладёт причину то в `note`, то в `diag['why']`, то в
    `diag['error']` — за полтора года так сложилось в сорока трёх местах, и
    сводить их к одному полю сейчас значило бы трогать все сразу. Здесь мы
    просто смотрим во все три по очереди.
    """
    if note:
        return str(note)
    for key in ("why", "error", "how"):
        value = (diag or {}).get(key)
        if value:
            return str(value)
    return ""


def load(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or TELEMETRY_PATH
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def summarize(path: Path | None = None, last_runs: int | None = None) -> list[dict[str, Any]]:
    """Per-step reliability, worst first — the question screenshots can't answer."""
    rows = load(path)
    if last_runs:
        seen: list[str] = []
        for r in rows:
            rid = r.get("run_id") or ""
            if rid and rid not in seen:
                seen.append(rid)
        keep = set(seen[-last_runs:])
        rows = [r for r in rows if (r.get("run_id") or "") in keep]

    stats: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("ok") is None:
            continue
        s = stats.setdefault(
            r["step"], {"step": r["step"], "ok": 0, "fail": 0, "slowest_s": 0.0, "last_diag": None}
        )
        if r["ok"]:
            s["ok"] += 1
        else:
            s["fail"] += 1
            if r.get("diag"):
                s["last_diag"] = r["diag"]
        d = r.get("duration_s")
        if isinstance(d, (int, float)) and d > s["slowest_s"]:
            s["slowest_s"] = float(d)

    out = list(stats.values())
    for s in out:
        total = s["ok"] + s["fail"]
        s["runs"] = total
        s["fail_rate"] = round(s["fail"] / total, 2) if total else 0.0
    out.sort(key=lambda s: (-s["fail_rate"], -s["fail"], s["step"]))
    return out


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Summarise RPA run telemetry.")
    ap.add_argument("--last-runs", type=int, default=None, help="only the N most recent runs")
    args = ap.parse_args()

    rows = summarize(last_runs=args.last_runs)
    if not rows:
        print(f"No telemetry yet at {TELEMETRY_PATH}")
        return
    print(f"{'step':38s} {'runs':>5s} {'fail':>5s} {'rate':>5s} {'slowest':>8s}")
    print("-" * 70)
    for s in rows:
        print(
            f"{s['step'][:38]:38s} {s['runs']:5d} {s['fail']:5d} "
            f"{s['fail_rate']:5.2f} {s['slowest_s']:7.1f}s"
        )
    worst = [s for s in rows if s["fail"] and s["last_diag"]]
    if worst:
        print("\nlast diagnosis per failing step:")
        for s in worst[:8]:
            print(f"  {s['step']}: {json.dumps(s['last_diag'], ensure_ascii=False)[:160]}")


if __name__ == "__main__":
    _main()
