# -*- coding: utf-8 -*-
"""Начало задания видно в телеметрии; библиотека не тянется дважды (ACL-1313).

От нажатия в дешборде до подключения к профилю уходило ~60 с, а телеметрия
начиналась с первого шага в Ads Manager. Теперь этапы начала пишут строки
`job_phase` с длительностью: build_drafts, library_pull, profile_queue,
browser_attach, open_campaigns.

И одно лишнее ожидание убрано: /preview-publish уже стянул библиотеку
(`pull(force=True)` обоих реестров), и запуск задания сразу за ним делал то же
самое ещё раз. Если библиотеку тянули успешно меньше минуты назад — повторно
не тянем.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import team_library  # noqa: E402
from src.fb_ads_rpa import enrich_draft  # noqa: E402
from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402
from src.fb_ads_rpa.rpa_publisher import RpaPublisher  # noqa: E402
from tests.profile_lease_fakes import fake_profile


@pytest.fixture
def rows(monkeypatch):
    got: list[dict] = []
    monkeypatch.setattr(rt, "record", lambda step, ok=None, **kw: got.append({"step": step, "ok": ok, **kw}))
    return got


def _phases(rows):
    return {r["note"]: r for r in rows if r["step"] == "job_phase"}


# ── phase ──────────────────────────────────────────────────────────────────

def test_a_phase_records_its_duration(rows):
    with rt.phase("library_pull", diag={"preview": False}) as info:
        info["pulled"] = ["targets"]
    row = _phases(rows)["library_pull"]
    assert row["ok"] is True
    assert row["duration_s"] >= 0
    assert row["diag"] == {"preview": False, "pulled": ["targets"]}


def test_a_failing_phase_is_recorded_and_raises(rows):
    with pytest.raises(RuntimeError):
        with rt.phase("browser_attach"):
            raise RuntimeError("MLX 401")
    row = _phases(rows)["browser_attach"]
    assert row["ok"] is None, "отказ этапа сообщает тот, кто поймает исключение"
    assert row["diag"]["error"] == "RuntimeError"


# ── библиотека ─────────────────────────────────────────────────────────────

@pytest.fixture
def library(monkeypatch):
    state = {"ok": True}
    monkeypatch.setattr(enrich_draft, "_forced_pull_at", {})
    monkeypatch.setattr(team_library, "status",
                        lambda kind: {"connected": True, "ok": state["ok"], "error": "", "synced_at": "t"})
    pulls: list[bool] = []

    def pull(*, force=False):
        pulls.append(force)
        return {"connected": True, "ok": state["ok"], "error": "", "synced_at": "now"}

    return {"state": state, "pulls": pulls, "pull": pull}


def test_a_library_pulled_seconds_ago_is_not_pulled_again(library):
    enrich_draft._forced_pull_at["targets"] = time.monotonic() - 5
    info: dict = {}
    got = enrich_draft._pull_unless_just_pulled("targets", library["pull"], info)
    assert library["pulls"] == []
    assert got["ok"] is True
    assert info == {"fresh": ["targets"]}


def test_a_library_pulled_long_ago_is_pulled(library):
    enrich_draft._forced_pull_at["targets"] = time.monotonic() - enrich_draft.LIBRARY_FRESH_S - 1
    info: dict = {}
    enrich_draft._pull_unless_just_pulled("targets", library["pull"], info)
    assert library["pulls"] == [True]
    assert info == {"pulled": ["targets"]}


def test_a_never_pulled_library_is_pulled(library):
    enrich_draft._pull_unless_just_pulled("targets", library["pull"], {})
    assert library["pulls"] == [True]


def test_a_library_in_error_is_pulled_again(library):
    enrich_draft._forced_pull_at["targets"] = time.monotonic() - 5
    library["state"]["ok"] = False
    enrich_draft._pull_unless_just_pulled("targets", library["pull"], {})
    enrich_draft._pull_unless_just_pulled("targets", library["pull"], {})
    assert library["pulls"] == [True, True], "неудачный поход не считается свежим"


def test_a_local_write_does_not_count_as_a_pull(library, monkeypatch):
    """Своя запись обновляет копию, но чужих правок не приносит — тянем."""
    monkeypatch.setattr(team_library, "_pulled", {"targets": time.monotonic()})
    enrich_draft._pull_unless_just_pulled("targets", library["pull"], {})
    assert library["pulls"] == [True]


def test_preview_then_publish_pulls_the_library_once(library, monkeypatch, tmp_path, rows):
    """Как в дешборде: /preview-publish, затем запуск задания."""
    from src.analysis_agent import ad_text_registry, targeting_registry
    from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft

    monkeypatch.setattr(targeting_registry, "pull", library["pull"])
    monkeypatch.setattr(ad_text_registry, "pull", library["pull"])
    account_map = tmp_path / "map.csv"
    account_map.write_text("account_label,ad_account_id,page_id\n", encoding="utf-8")

    def drafts():
        return [CampaignDraft("Campaign A", "MX", "Account", ad_sets=[
            AdSetDraft("Set A", targeting_label="Investing",
                       ads=[AdDraft(ad_title="t", ad_body="b")])])]

    for preview in (True, False):
        enrich_draft.enrich_campaign_drafts(
            drafts(), account_label="Account", upload_fb_path=str(tmp_path / "none.xlsx"),
            ad_account_map_path=str(account_map), creatives_root=str(tmp_path),
            preview_mode=preview)
    assert library["pulls"] == [True], "запуск задания снова тянул только что стянутую библиотеку"
    pulls = [r for r in rows if r["step"] == "job_phase" and r["note"] == "library_pull"]
    assert [r["diag"].get("pulled", r["diag"].get("fresh")) for r in pulls] == [["targets"], ["targets"]]
    assert "fresh" in pulls[1]["diag"]


# ── публикатор ─────────────────────────────────────────────────────────────

class _Draft:
    def __init__(self, name="Campaign A"):
        self.status = "DRAFT"
        self.errors = []
        self.name = name
        self.account_label = "TEST_ACCOUNT"


def _publisher() -> RpaPublisher:
    p = RpaPublisher.__new__(RpaPublisher)
    p._conn = None
    p._entry = None
    p._provider = MagicMock()
    p.hand_off = False
    p.dry_run = False
    p.selectors_path = None
    return p


def test_the_publisher_measures_the_queue_and_the_attach(rows, monkeypatch):
    from src.fb_ads_rpa import rpa_publisher as rp
    from src.fb_ads_rpa.draft import RpaPublishResult

    monkeypatch.setattr(rp, "load_ad_account_map", lambda _: {
        "TEST_ACCOUNT": SimpleNamespace(profile_id=fake_profile("TEST_ACCOUNT"))})
    p = _publisher()
    monkeypatch.setattr(rp, "slots_to_campaign_drafts", lambda slots, **kw: [_Draft(), _Draft("B")])
    monkeypatch.setattr(rp, "enrich_campaign_drafts", lambda drafts, **kw: drafts)
    built = p.build_drafts([{}], account_label="TEST_ACCOUNT", country="MX")
    conn = MagicMock(provider="multilogin")

    def ensure(label):
        p._conn = conn
        return conn

    flow = MagicMock()
    flow.return_value.publish_campaign.return_value = RpaPublishResult(campaign_name="A", success=True)
    with patch.object(p, "_ensure_browser", side_effect=ensure), patch.object(rp, "AdsManagerFlow", flow):
        for d in built:
            p.publish_draft(d)
    phases = [r["note"] for r in rows if r["step"] == "job_phase"]
    assert phases == ["build_drafts", "profile_queue", "browser_attach"], phases
    attach = _phases(rows)["browser_attach"]
    assert attach["diag"]["provider"] == "multilogin"
    assert attach["campaign"] == "Campaign A"


def test_the_first_navigation_is_measured(rows, monkeypatch):
    from src.fb_ads_rpa import ads_manager_flow as amf
    from src.fb_ads_rpa.draft import RpaPublishResult

    monkeypatch.setattr(amf, "pause_after_navigation", lambda: None)
    page = MagicMock()
    # ACL-1697: открытый кабинет читается из адреса страницы.
    page.url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=123"
    flow = amf.AdsManagerFlow(page)
    draft = MagicMock(ad_account_id="act_123")
    draft.name = "Campaign A"
    flow._goto_campaigns(draft, RpaPublishResult(campaign_name="Campaign A"))
    row = _phases(rows)["open_campaigns"]
    assert row["ok"] is True and row["campaign"] == "Campaign A"
    assert row["diag"] == {"attempts": 1}
