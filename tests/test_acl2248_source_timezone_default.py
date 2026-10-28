"""ACL-2248: BI и книга статистики без объявленной зоны/границы дня — Asia/Jerusalem, calendar_day."""
import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.analysis_agent import bi_compare

TZ = bi_compare.METADATA_COLUMNS["timezone"]
DAY = bi_compare.METADATA_COLUMNS["day_boundary"]
ENV = ("BI_TIMEZONE", "STATS_TIMEZONE", "BI_DAY_BOUNDARY", "STATS_DAY_BOUNDARY")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)


def _frame(zones, days=None, source="bi"):
    frame = pd.DataFrame({"Account Alias": ["FacebookDoc101"] * len(zones), "$ Spend": [10.0] * len(zones)})
    frame = bi_compare.annotate_source(frame, source)
    frame[TZ] = list(zones)
    frame[DAY] = list(days if days is not None else [""] * len(zones))
    return frame


@pytest.mark.parametrize("source", ["bi", "workbook"])
def test_default_applied_when_env_unset(source):
    info = bi_compare.provenance(source)
    assert info["timezone"] == "Asia/Jerusalem" and info["day_boundary"] == "calendar_day"
    assert info["timezone_defaulted"] and info["day_boundary_defaulted"] and info["assumed"] is False
    assert "time zone: Asia/Jerusalem (default); day boundary: calendar day (default)" in info["note"]
    assert "Missing source metadata" not in info["note"] and "unknown" not in info["note"]


def test_empty_rows_and_missing_columns_use_default():
    assert bi_compare.provenance(frame=pd.DataFrame())["timezone"] == "Asia/Jerusalem"
    bare = bi_compare.annotate_source(pd.DataFrame({"$ Spend": [1.0]}), "bi").drop(columns=[TZ, DAY])
    info = bi_compare.provenance(frame=bare)
    assert info["timezone"] == "Asia/Jerusalem" and info["day_boundary"] == "calendar_day"


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("BI_TIMEZONE", "Europe/Berlin")
    monkeypatch.setenv("BI_DAY_BOUNDARY", "rolling_24h")
    info = bi_compare.provenance("bi")
    assert info["timezone"] == "Europe/Berlin" and info["timezone_defaulted"] is False
    assert info["day_boundary"] == "rolling_24h" and info["day_boundary_defaulted"] is False
    assert "time zone: Europe/Berlin;" in info["note"] and "(default)" in info["note"]  # валюта осталась default
    assert "time zone: Europe/Berlin (default)" not in info["note"]


def test_row_column_override_wins():
    info = bi_compare.provenance(frame=_frame(["America/New_York", "America/New_York"], ["calendar_day"] * 2))
    assert info["timezone"] == "America/New_York" and not info["timezone_defaulted"]
    assert not info["day_boundary_defaulted"]
    # объявленная строка + пустая строка: объявленное значение остаётся (как у валюты)
    partly = bi_compare.provenance(frame=_frame(["Asia/Jerusalem", ""]))
    assert partly["timezone"] == "Asia/Jerusalem" and not partly["timezone_defaulted"]


def test_mixed_values_stay_unknown():
    info = bi_compare.provenance(frame=_frame(["Europe/Berlin", "Asia/Tokyo"], ["calendar_day", "rolling_24h"]))
    assert info["timezone"] == "" and info["day_boundary"] == ""
    assert info["assumed"] is True and "time zone: unknown" in info["note"]
    assert "Missing source metadata" in info["note"]
    # явная зона + строка, где зона по умолчанию другая: это смесь, а не Jerusalem
    assert bi_compare.provenance(frame=_frame(["Europe/Berlin", ""]))["timezone"] == "" 


def test_invalid_env_name_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("BI_TIMEZONE", "+02:00")  # сдвиг не имя IANA
    assert bi_compare.provenance("bi")["timezone"] == "Asia/Jerusalem"


def test_default_is_iana_name_and_follows_dst():
    assert bi_compare.DEFAULT_SOURCE_TIMEZONE == "Asia/Jerusalem"
    zone = ZoneInfo(bi_compare.provenance("bi")["timezone"])
    winter = dt.datetime(2026, 1, 15, 12, tzinfo=zone).utcoffset()
    summer = dt.datetime(2026, 7, 15, 12, tzinfo=zone).utcoffset()
    assert (winter, summer) == (dt.timedelta(hours=2), dt.timedelta(hours=3))


def test_comparison_still_checks_account_own_zone():
    source = {"FacebookDoc101": 10.0}
    info = bi_compare.provenance(frame=_frame([""]))
    same = {"FacebookDoc101": {"state": "ok", "spend": 10.0, "currency": "USD",
                               "timezone": "Asia/Jerusalem", "day_boundary": "calendar_day"}}
    other = {"FacebookDoc101": dict(same["FacebookDoc101"], timezone="America/Los_Angeles")}
    unknown = {"FacebookDoc101": dict(same["FacebookDoc101"], timezone="")}
    for reading, state in ((same, "comparable"), (other, "day_boundary_mismatch"), (unknown, "unknown_day_boundary")):
        row = bi_compare.compare_spend(source, reading, source_context={"FacebookDoc101": info})[0]
        assert row["comparison_state"] == state
