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

import itertools
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


# Тесты не ходят в общую библиотеку команды (ACL-1289).
#
# `src/analysis_agent/gsheet_accounts.py` зовёт `load_env()` на импорте, а его
# тянет за собой `src.web.app`. Значит любой тест, который импортирует дешборд,
# кладёт `%LOCALAPPDATA%\ACL\.env` этой машины в `os.environ` на весь прогон —
# вместе с `ACL_LIBRARY_REPO` и `ACL_LIBRARY_TOKEN`. После этого
# `team_library.configured()` истинно для всех следующих тестов, и
# `targeting_registry.add()` пишет не в подменённый `_store`, а в настоящий
# репозиторий библиотеки.
#
# Так и получалось: файл поодиночке проходил, в полном прогоне падал, а в
# библиотеке команды заводились тестовые таргеты. У CI машина чистая: сьют
# идёт на windows-latest (`.github/workflows/ci.yml`), лаунчер там не
# установлен, поэтому `%LOCALAPPDATA%\ACL\.env` не существует, а `.env`
# чекаута не коммитится. CI оставался зелёным — худшее сочетание: оно
# приучает не верить локальному прогону. Дело не в операционной системе:
# на такой же Windows с установленным лаунчером CI падал бы так же.
#
# Изоляция здесь, а не в каждом файле, потому что зависимость скрытая: она
# приходит от чужого импорта, и следующий такой файл о ней не догадается.
_LIBRARY_ENV = ("ACL_LIBRARY_REPO", "ACL_LIBRARY_TOKEN", "ACL_LIBRARY_BRANCH")
#: Без этой пары `backend()` не соберёт `GitHubBackend`. Отдельно от списка
#: выше, чтобы сторож не поехал, если тот когда-нибудь переставят местами.
_LIBRARY_REQUIRED = ("ACL_LIBRARY_REPO", "ACL_LIBRARY_TOKEN")

# Пустая строка, а не удаление, и на уровне модуля — как выключатель Sentry
# выше и по той же причине. Удалять здесь бесполезно: `load_env()` случится
# позже, при импорте тестовых модулей, и вернёт переменные обратно. А вот
# перебить уже выставленное значение он не может — `load_dotenv` по умолчанию
# идёт с `override=False`. Так закрывается единственное окно, до которого
# фикстурам не дотянуться: сбор тестов. `backend()` и `configured()` зовут
# `.strip()`, поэтому пустая строка для них то же самое, что отсутствие.
for _name in _LIBRARY_ENV:
    os.environ[_name] = ""


@pytest.fixture(scope="session", autouse=True)
def library_disconnected():
    """Общая библиотека отключена на весь прогон, с самого начала.

    Отдельно от машинной фикстуры и сессионной областью — потому что
    фикстуры уровня модуля и сессии поднимаются раньше функциональных.
    Иначе такая фикстура успела бы поработать по живой библиотеке до того,
    как включится защита.
    """
    from src.analysis_agent import team_library

    patch = pytest.MonkeyPatch()
    for name in _LIBRARY_ENV:
        patch.delenv(name, raising=False)

    # Последний рубеж. Снятых переменных хватает, пока их никто не вернёт —
    # а вернуть может `load_env()` из чекаутного `.env`, который читается,
    # когда машинного нет. Тогда тест должен упасть с понятным текстом, а не
    # отправить правку в библиотеку команды. Проверяем окружение до вызова:
    # отказ должен случиться раньше, чем соберётся настоящий `GitHubBackend`.
    # Тесты, которые сами собирают его с подменённым `_call`, идут мимо
    # `backend()` и этого не замечают.
    #
    # Ставится один раз на прогон, а не на каждый тест. Размен: тест, который
    # присвоит `team_library.backend` напрямую, минуя monkeypatch, снимет
    # сторожа до конца прогона. Сейчас таких нет, и сессионная область важнее:
    # без неё фикстуры уровня модуля работают вообще без защиты.
    real_backend = team_library.backend

    def refuse_the_real_library():
        if team_library._backend_override is None and all(
            (os.environ.get(name) or "").strip() for name in _LIBRARY_REQUIRED
        ):
            raise AssertionError(
                "A test tried to reach the real team library repository. The "
                "library_disconnected fixture in tests/conftest.py clears "
                "ACL_LIBRARY_REPO/ACL_LIBRARY_TOKEN; something restored them "
                "mid-test — most likely a load_env() that fell back to the "
                "checkout .env. Use team_library.use_backend(MemoryBackend())."
            )
        return real_backend()

    patch.setattr(team_library, "backend", refuse_the_real_library)
    yield
    patch.undo()


@pytest.fixture(autouse=True)
def isolated_machine_config(_machine_dirs, monkeypatch):
    """Каждому тесту — свой машинный каталог и чистое состояние библиотеки.

    `ACL_CONFIG_DIR` во временной папке уводит туда и реестры, и файлы
    блокировок, и путь к `.env` машины, так что `load_env()` внутри теста
    машинный файл уже не найдёт. Запасной `.env` чекаута он всё ещё прочитает
    — на этот случай и стоит сторож в `library_disconnected`. Папка своя, а не
    `tmp_path` теста, чтобы реестры не мешались с файлами самой проверки.
    """
    from src.analysis_agent import team_library

    made = next(_machine_dirs)
    made.mkdir()
    monkeypatch.setenv("ACL_CONFIG_DIR", str(made))
    for name in _LIBRARY_ENV:
        monkeypatch.delenv(name, raising=False)

    # Состояние модуля тоже общее на весь прогон: подменённое хранилище,
    # отметки о последнем чтении и статус синхронизации иначе переезжают из
    # одного теста в другой. monkeypatch вернёт их и после упавшего теста.
    monkeypatch.setattr(team_library, "_backend_override", None)
    monkeypatch.setattr(team_library, "_pulled", {})
    monkeypatch.setattr(team_library, "_status", {})


@pytest.fixture(scope="session")
def _machine_dirs(tmp_path_factory):
    """Нумерованные каталоги под общим корнем, по одному на тест.

    `tmp_path_factory.mktemp` на каждый тест обходится дорого: он пересчитывает
    весь корень, чтобы найти следующий номер, и на двух с половиной тысячах
    тестов это десятки секунд на пустом месте. Корень берётся один раз.

    Каталог создаёт вызывающий, а не генератор: упавший внутри `mkdir` закрыл
    бы его насовсем, и одна ошибка диска превратилась бы в `StopIteration` на
    всех оставшихся тестах.
    """
    root = tmp_path_factory.mktemp("acl-machines")
    return (root / str(n) for n in itertools.count())


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
