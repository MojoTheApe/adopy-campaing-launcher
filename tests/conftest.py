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
