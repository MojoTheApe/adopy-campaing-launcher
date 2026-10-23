"""Test fixtures for analysis_agent unit tests.

Provides a synthetic enriched DataFrame (post compute_metrics + enrich_structure)
plus a synthetic accounts list. Designed so:
  - 2 countries (Mexico, Brazil) with non-trivial CM/offer/device/interest mixes
  - At least one CM aggregate per country has 2+ deposits (Strong combo eligible)
  - At least one combo has 1 deposit + CR > 5% (Interesting combo eligible)
  - One row has NaN CM (tests the dropna=False group path)
  - One row has NaN Interest (tests the (unknown) bucket)
  - Dates span 14 days so trend windows have both recent and older data
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

# Make project root importable
_PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

# Тесты не докладывают об отказах наружу (ACL-354).
#
# Отчёты в Sentry подвешены к `run_telemetry.record()`, а его зовут десятки
# тестов — в том числе с `ok=False`, потому что именно отказы они и проверяют.
# Без этой строки полный прогон отправляет выдуманные отказы в общий приёмник:
# первый же прогон так и сделал («Sentry is attempting to send 1 pending
# events»), и в CI это повторялось бы на каждом коммите, пряча настоящие
# отказы с машин байеров под шумом.
#
# Пустая строка — штатный выключатель из `acl_sentry.dsn()`, а не заглушка
# ради тестов. Ставится на уровне модуля, до импорта тестовых модулей:
# фикстура успела бы сработать позже первого `init()`.
os.environ["ACL_SENTRY_DSN"] = ""
# ACL-2117: действия ждут конца паузы после остановки профиля (до 3 мин). В тестах, которые
# проверяют прежний отказ, ждать нельзя: 0 = сразу отказ. Тесты ожидания включают его сами.
os.environ["ACL_PROFILE_RESTART_MAX_WAIT_S"] = "0"
# ACL-1447: итог кампании — тоже штатным выключателем. Тесты, подменяющие
# `acl_sentry.init` двойником, иначе ловили бы ещё и итог из фонового потока
# («чистая кампания — ни одного события»). Тесты итога включают его сами.
os.environ["ACL_SENTRY_RUN_SUMMARY"] = "0"

# Тесты не пишут в живую командную библиотеку (ACL-1397).
#
# `team_library` включается, когда заданы ACL_LIBRARY_REPO и ACL_LIBRARY_TOKEN,
# а машинный `%LOCALAPPDATA%\ACL\.env` на рабочей машине их содержит: любой
# `load_env()` во время прогона подтянул бы живую библиотеку. Раньше защита
# держалась только на том, что прогон запускали с пустыми переменными руками.
# При параллельном прогоне (pytest-xdist) этот файл заново исполняет каждый
# процесс-воркер, так что пустые значения есть в каждом из них. Пустая строка,
# а не удаление: `load_dotenv` не перезаписывает уже заданную переменную.
# Тест, которому нужна настроенная библиотека, ставит синтетику через
# monkeypatch — позже, и действует его значение.
for _name in ("ACL_LIBRARY_REPO", "ACL_LIBRARY_TOKEN", "ACL_LIBRARY_BRANCH"):
    os.environ[_name] = ""

# Тесты не спрашивают MLX API про прокси профиля (ACL-1138).
#
# Гейт стоит в начале `open()` обоих провайдеров Multilogin, а десятки тестов
# открывают профиль через двойников. С токеном из машинного `.env` или
# синтетическим из monkeypatch каждый такой тест ходил бы в
# api.multilogin.com. Штатный выключатель; тесты гейта включают его сами.
os.environ["FB_RPA_PROXY_GATE"] = "0"


# Маркер файлов, которым нужен Windows (ACL-1582). Это метка отбора для CI
# (scripts/acl_test_platform.py), а не пропуск: на Linux такие файлы просто не
# попадают в задание. Причина обязательна — её требует guard
# (tests/test_acl_test_platform.py).
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "windows_only(reason): the test file needs Windows; CI runs it only in the Windows job (ACL-1582)",
    )


# Тесты не пишут в живую машинную папку ACL (ревью v3 PR #314).
#
# `config.paths.get_local_config_dir()` — это `%LOCALAPPDATA%\ACL`: там
# `draft_status.json` (что видит человек на дешборде), реестры таргетинга и
# текстов, карта аккаунтов. `test_three_accounts_publish_at_once.py` через
# `publish_jobs._finish` записал туда фиктивные `Doc19`, `DQ501`, а какой-то
# тест обновил `library_synced_at` в `targeting_registry.json`. Все пути
# машинной папки считаются при вызове, не при импорте, поэтому переменной
# окружения на время теста достаточно; тест, который ставит свою
# `ACL_CONFIG_DIR` (или убирает её, проверяя путь по умолчанию), делает это
# позже этой фикстуры, и действует его значение.
@pytest.fixture(autouse=True)
def _machine_config_off_the_live_folder(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path_factory.mktemp("acl_config")))
    yield


# Тесты не пишут в живой журнал телеметрии (PR #314, ревью).
#
# `run_telemetry.TELEMETRY_PATH` смотрит в `output/rpa_telemetry.jsonl` рабочей
# папки — тот же файл, который читают разбор прогонов и дешборд. Десятки
# тестов зовут `record()` (часто с `ok=False`, ради отказов), и без этой
# фикстуры выдуманные отказы оседали в настоящем журнале. Замок журнала
# (`<путь>.lock`, `acl_log.append_line`) идёт следом за путём. Тест, который
# ставит свой путь сам (`monkeypatch.setattr(rt, "TELEMETRY_PATH", ...)`),
# делает это позже этой фикстуры — его путь и действует. Папка своя у
# каждого теста и не совпадает с `tmp_path`: тесты, которые считают файлы в
# `tmp_path`, лишнего не увидят.
@pytest.fixture(autouse=True)
def _telemetry_off_the_live_journal(tmp_path_factory, monkeypatch):
    try:
        from src.fb_ads_rpa import run_telemetry
    except Exception:
        yield
        return
    monkeypatch.setattr(run_telemetry, "TELEMETRY_PATH",
                        tmp_path_factory.mktemp("telemetry") / "rpa_telemetry.jsonl")
    yield


# Тесты не закрывают окна профилей этой машины (ACL-1388/1562).
#
# Окно профиля, открытое руками, робот закрывает сам и сразу — мягкой
# остановкой через MLX API (ACL-1562). Тест, который доходит до этой ветки
# без своих заглушек, позвал бы Multilogin живой машины. Выключатель —
# штатный (`FB_RPA_REOPEN_HAND_WINDOW=0`, прежнее поведение: всегда стоп);
# тесты ACL-1388/1562 включают его сами.
@pytest.fixture(autouse=True)
def _hand_window_reopen_off(monkeypatch):
    monkeypatch.setenv("FB_RPA_REOPEN_HAND_WINDOW", "0")
    yield


# Каждый тест начинает с чистого состояния шага Crop и отказа по медиа (ACL-2073).
#
# `crop_step` хранит отказ Crop в thread-local (`_tls.failure`), `meta_dom` —
# отказ по медиа объявления (`_media_refusal_tls`) и флаг «у медиатеки нет
# счётчика» (`_reuse_state`). В продукте их сбрасывает каждое объявление
# (`set_plan`/`reset_attempt`), а тест, который зовёт `crop_step.apply` без
# этого, оставлял отказ в главном потоке воркера. 21.10: при `--dist loadfile`
# `test_bench_b97_picker_and_crop_1007.py` (оставляет
# `crop_square_card_missing`) попал в один воркер с
# `test_media_attachment_identity.py`, и первый же повтор из медиатеки там
# забрал чужой отказ через `_crop_refusal` -> `dirty` вместо `attached` — падение
# зависело только от того, какие файлы достались шарду 3 CI. Сбрасываются лишь
# уже импортированные модули: фикстура не тянет Playwright в чужие тесты.
#
# ACL-2078 (22.10): сбрасывалась лишь часть состояния — b97 оставлял
# `_upload_why_tls.why` = `library_has_no_copy`, и
# `test_additional_ad_without_creative_has_no_text.py` получал
# `ad2_creative_upload:library_has_no_copy` вместо своей причины. Теперь
# сбрасываются ВСЕ `threading.local` `meta_dom`/`crop_step`, изменяемые
# контейнеры уровня модуля и прогон телеметрии главного потока — список и
# правило «новое состояние вписать туда» в `tests/rpa_module_state.py`.
@pytest.fixture(autouse=True)
def _crop_and_media_refusal_state_per_test():
    from tests import rpa_module_state

    rpa_module_state.reset()
    yield
    rpa_module_state.reset()


# Тесты не закрывают профили Multilogin в конце задания (ACL-1444).
#
# Чистое задание закрывает профиль через MLX API. Тест, который доходит до
# конца задания с настоящим провайдером без своих заглушек, мог бы позвать
# MLX живой машины. Выключатель — штатный (`FB_RPA_KEEP_PROFILES_OPEN=1`,
# прежнее поведение); тесты ACL-1444 снимают его сами.
@pytest.fixture(autouse=True)
def _finished_profiles_stay_open(monkeypatch):
    monkeypatch.setenv("FB_RPA_KEEP_PROFILES_OPEN", "1")
    yield


# Тесты не читают настоящую память машины через очередь профилей (ACL-1603).
#
# С ACL-1603 даже первый профиль ждёт «профиль + запас» (6,5 ГБ свободных), и
# задание, прошедшее через глобальную `publish_jobs._slots` с настоящим
# `read_memory`, на занятом CI-раннере ждало бы вечно (Windows-набор упёрся в
# 30 минут). Подставная память — много; тесту нужна своя — он ставит
# `ProfileSlots(memory=...)` или `monkeypatch.setattr(pj._slots, "_memory", ...)`
# позже этой фикстуры. Сам `profile_slots.read_memory` не трогаем: тесты
# настоящей пробы проверяют именно его.
@pytest.fixture(autouse=True)
def _slot_queue_never_reads_the_host_memory(monkeypatch):
    try:
        from src.web import profile_slots as ps
        from src.web import publish_jobs as pj
    except Exception:
        yield
        return

    def plenty():
        return ps.MemoryReading(total_mb=64 * 1024, available_mb=48 * 1024)

    monkeypatch.setitem(ps.ProfileSlots.__init__.__kwdefaults__, "memory", plenty)
    # Свежая очередь на каждый тест: окна, оставленные прошлым тестом
    # (`keep`), не должны занимать места следующему.
    monkeypatch.setattr(pj, "_slots", ps.ProfileSlots(memory=plenty))
    yield


@pytest.fixture
def enriched_df() -> pd.DataFrame:
    """Return an enriched DataFrame as if compute_metrics + enrich_structure ran.

    Schema mirrors what build_morning_report() consumes downstream.
    """
    today = datetime(2026, 4, 18)

    def d(days_ago: int) -> datetime:
        return today - timedelta(days=days_ago)

    rows = [
        # ── Mexico — Evgeni Sedin — trader_pro — all device — interest=trading ──
        # 3 ads, 2 of them with 1 deposit each (Strong combo: 2+ deposits)
        _row("Mexico", "MX-acc-1", "Evgeni Sedin", "trader_pro", "all", "trading",
             "25", "Make money fast", 100, 1, 50.0, 80.0, d(0)),
        _row("Mexico", "MX-acc-1", "Evgeni Sedin", "trader_pro", "all", "trading",
             "25", "Make money fast", 90, 1, 45.0, 70.0, d(1)),
        _row("Mexico", "MX-acc-1", "Evgeni Sedin", "trader_pro", "all", "trading",
             "25", "Make money fast", 80, 0, 40.0, 0.0, d(2)),

        # ── Mexico — Evgeni Sedin — trader_pro — ios device — interest=trading ──
        # 1 ad, 1 deposit, high CR (Interesting combo: 1 deposit + CR > 5%)
        _row("Mexico", "MX-acc-2", "Evgeni Sedin", "trader_pro", "ios", "trading",
             "35+", "iOS exclusive offer", 10, 1, 30.0, 60.0, d(0)),

        # ── Mexico — Other CM — crypto_invest — all device — interest=crypto ──
        # 4 ads, total 3 deposits (Strong combo, contributes to "Other CM" aggregate)
        _row("Mexico", "MX-acc-3", "Other CM", "crypto_invest", "all", "crypto",
             "25", "Buy bitcoin now", 200, 2, 100.0, 150.0, d(1)),
        _row("Mexico", "MX-acc-3", "Other CM", "crypto_invest", "all", "crypto",
             "25", "Buy bitcoin now", 150, 1, 75.0, 80.0, d(3)),
        _row("Mexico", "MX-acc-3", "Other CM", "crypto_invest", "all", "crypto",
             "25", "Buy bitcoin now", 100, 0, 50.0, 0.0, d(8)),
        _row("Mexico", "MX-acc-3", "Other CM", "crypto_invest", "all", "crypto",
             "25", "Crypto for beginners", 50, 0, 25.0, 0.0, d(10)),

        # ── Mexico — NaN CM — trader_pro — all device — interest=NaN ──
        # 1 ad with NaN CM and NaN Interest (tests dropna=False + (unknown) bucket)
        _row("Mexico", "MX-acc-4", None, "trader_pro", "all", None,
             "25", "Generic offer", 30, 0, 15.0, 0.0, d(5)),

        # ── Brazil — Evgeni Sedin — trader_pro — all device — interest=trading ──
        # 2 ads, 1 deposit each (2 deposits → Strong)
        _row("Brazil", "BR-acc-1", "Evgeni Sedin", "trader_pro", "all", "trading",
             "25", "Brazilian special", 70, 1, 35.0, 50.0, d(0)),
        _row("Brazil", "BR-acc-1", "Evgeni Sedin", "trader_pro", "all", "trading",
             "25", "Brazilian special", 60, 1, 30.0, 45.0, d(2)),

        # ── Brazil — Other CM — crypto_invest — ios device — interest=crypto ──
        # 1 ad, 0 deposits (won't surface in combos but appears in CM aggregate)
        _row("Brazil", "BR-acc-2", "Other CM", "crypto_invest", "ios", "crypto",
             "35+", "Mobile crypto trading", 40, 0, 20.0, 0.0, d(7)),
    ]
    df = pd.DataFrame(rows)

    # Compute the per-row metric columns the way compute_metrics does
    df["CPL"] = df["$ Spend"] / df["# Leads"].replace(0, pd.NA)
    df["CPA"] = df["$ Spend"] / df["# FTDs"].replace(0, pd.NA)
    df["CR"] = (df["# FTDs"] / df["# Leads"].replace(0, pd.NA)) * 100.0
    df["Profit"] = df["$ Net"] - df["$ Spend"]
    df["ROI"] = df["$ Net"] / df["$ Spend"].replace(0, pd.NA)
    return df


@pytest.fixture
def accounts_rows() -> list:
    """Synthetic accounts list as returned by gsheet_accounts.fetch_account_rows()."""
    return [
        {
            "alias": "MX-acc-1",
            "account_label": "MX-acc-1",
            "country_code": "mx",
            "is_ready": True,
            "to_do": "",
            "cost": "100",
            "stock": "5",
        },
        {
            "alias": "BR-acc-1",
            "account_label": "BR-acc-1",
            "country_code": "br",
            "is_ready": True,
            "to_do": "",
            "cost": "80",
            "stock": "3",
        },
    ]


def _row(country, alias, cm, offer, device, interest, age, ad_title,
         leads, ftds, spend, net, date) -> dict:
    """Build one fully-enriched row dict."""
    return {
        "Country": country,
        "Country Final": country,
        "Country Code": country[:2].upper(),
        "Account Alias": alias,
        "Adset Name": f"{country[:2].upper()}_{offer}_{device}_creative_{interest or 'wide'}_{age}",
        "Ad Title": ad_title,
        "Ad Asset": offer,
        "# Leads": leads,
        "# FTDs": ftds,
        "$ Spend": spend,
        "$ Net": net,
        "CM": cm,
        "Date": date,
        # enrich_structure-derived columns
        "Device": device,
        "Interest": interest,
        "Age": age,
        "Creative": ad_title,
        "TextSegment": ad_title.split()[0].lower() if ad_title else "",
        "Offer": offer,
    }


# Браузерные тесты страницы отдают скрипты `/static/*.js` с диска (ACL-1800).
#
# Шаблоны подключают помощников как `<script src="/static/….js">` (сейчас
# `acm_commands.js` в таблице Home). Заглушки маршрутов Playwright в десятках
# тестов отвечают на любой запрос не-документа JSON-ом, и браузер исполнял бы
# JSON как скрипт («Unexpected token ':'»). Эта прослойка стоит перед каждой
# заглушкой `Page.route` / `BrowserContext.route`: скрипт под `src/web/static/`
# отдаётся как есть, всё остальное идёт в заглушку теста. Заглушки, которые
# уже отвечают на `/static/…` сами (и берут маршрут раньше), прослойку не ломают.
@pytest.fixture(autouse=True)
def _static_scripts_come_from_disk(monkeypatch):
    try:
        from playwright.sync_api import BrowserContext, Page
    except Exception:
        yield
        return
    from urllib.parse import urlsplit

    static = _PROJ_ROOT / "src" / "web" / "static"

    def wrap(handler):
        if not callable(handler):
            return handler

        def serve_or_stub(route, *rest):
            try:
                path = urlsplit(route.request.url).path
            except Exception:
                path = ""
            if path.startswith("/static/") and path.endswith(".js"):
                target = (static / path[len("/static/"):]).resolve()
                if static.resolve() in target.parents and target.is_file():
                    return route.fulfill(status=200, content_type="application/javascript",
                                         body=target.read_text(encoding="utf-8"))
            return handler(route, *rest)

        serve_or_stub.__wrapped__ = handler
        return serve_or_stub

    for cls in (Page, BrowserContext):
        original = cls.route

        def patched(self, url, handler, *args, _orig=original, **kwargs):
            return _orig(self, url, wrap(handler), *args, **kwargs)

        patched.static_wrap = wrap       # для test_static_scripts_from_disk
        monkeypatch.setattr(cls, "route", patched)
    yield
