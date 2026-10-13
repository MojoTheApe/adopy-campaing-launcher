"""Таблица процента сборки (ACL-1347): шаг → этап и пересчёт по телеметрии.

Процент у точки сборки считается страницей по таблице в шаблоне
(`cb-build-model`). Шаг, которого не знает ни одно правило, держит процент
на месте — это безопасно, но процент тогда «спит». Поэтому здесь: каждый
шаг, который робот пишет (увиденный в телеметрии и названный в его коде),
раскладывается по этапу; таблица цельная; скрипт пересчёта выводит
длительности из телеметрии и переписывает только свою часть таблицы.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import acl_build_progress_model as m  # noqa: E402

#: Все имена шагов кампаний в output/rpa_telemetry.jsonl живого дешборда на
#: 28.09 (19 прогонов, классический редактор и «в один экран», 1–2 объявления).
STEPS_SEEN_IN_TELEMETRY = frozenset("""
ad2_copy ad2_create ad2_creative_upload ad2_destination ad2_name ad_copy ad_copy_blocked
ad_creative_reuse ad_creative_upload ad_destination ad_level_summary ad_name adset_name
advantage_plus_audience advantage_plus_off age_suggestion_off audience_age audience_age_row_opened
audience_location before_audience_location browser_addon_repair browser_error browser_error_recovered
browser_window campaign_budget_strategy campaign_fields campaign_name campaign_open_budget
campaigns_list_ready click_refused click_target_covered continue conversion_event conversion_menu_open
conversion_website_pick create create_retry create_took creative_upload_stalled destination_sweep
destination_sweep_cleared destination_sweep_skipped drafts_pending fresh_tab further_limit_continue
further_limit_reach further_limit_row job_phase languages leads listbox_left_open loading_modal_reload
next_to_ad next_to_adset objective_leads objective_wait onboarding_modal overlays_cleared
pick_further_limit_radio placements simple_ad_section simple_adset_section simple_card simple_create
simple_create_draft simple_create_layout simple_create_more_ads simple_objective simple_reentry
stall_evidence stall_unstuck tab_frozen tab_woken targeting targeting_open text_generation_off
text_variations_off website_click website_selected window_bounds window_resize
window_unminimized_on_attach
""".split())

MODEL = m.read_model()


def _robot_step_names() -> set[str]:
    """Имена шагов, которые робот называет в коде: `record("…")` и списки
    шагов политики повторов."""
    names: set[str] = set()
    for path in (ROOT / "src" / "fb_ads_rpa").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        names.update(re.findall(r"""\brecord\(\s*["']([a-z][a-z0-9_]*)["']""", text))
    # ACL-1580: и шаги чтений спенда (`spend_profile_*`) — они в той же
    # телеметрии; там их пишут и через обёртку `_record("…")`.
    for path in (ROOT / "src" / "analysis_agent").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        names.update(re.findall(r"""\b_?record\(\s*["']([a-z][a-z0-9_]*)["']""", text))
    from src.fb_ads_rpa import retry_policy as rp

    names |= set(rp.CAMPAIGN_RETRY_STEPS) | set(rp.NON_RETRYABLE_FLOW_STEPS) | set(rp.BLOCKING_REVIEW_STEPS)
    return names


def test_every_step_seen_in_telemetry_maps_to_a_stage():
    unmapped = sorted(s for s in STEPS_SEEN_IN_TELEMETRY if m.stage_of(s, MODEL["rules"]) is None)
    assert not unmapped, f"add these to cb-build-model rules: {unmapped}"


def test_every_step_the_robot_names_maps_to_a_stage():
    names = _robot_step_names()
    assert len(names) > 60, "the robot's step names were not found"
    unmapped = sorted(s for s in names if m.stage_of(s, MODEL["rules"]) is None)
    assert not unmapped, f"add these to cb-build-model rules: {unmapped}"


@pytest.mark.parametrize("step,stage", [
    ("", None), ("job_phase", "profile"), ("campaigns_list_ready", "profile"),
    ("create", "campaign"), ("campaigns_list_ready", "profile"), ("simple_create", "campaign"),
    ("campaign_fields", "campaign"), ("adset_name", "adset"), ("placements", "adset"),
    ("simple_card", "adset"), ("ad_name", "ad"), ("ad3_creative_upload", "ad"),
    ("ad_creative_reuse", "ad"), ("simple_create_more_ads", "ad"), ("ad_level_summary", "final"),
    ("simple_create_draft", "final"), ("destination_sweep_skipped", "final"),
    ("window_bounds", "hold"), ("window_hidden_woken", "hold"), ("tab_frozen", "hold"),
    ("account_blocked", "hold"), ("status_shot", "hold"), ("destination_instant_form_off", "ad"),
    ("ad_copy:attempt2", "ad"),
])
def test_stage_of_known_steps(step, stage):
    assert m.stage_of(step, MODEL["rules"]) == stage


def test_ad_steps_share_one_key_and_carry_their_number():
    assert m.key_of("ad3_copy") == m.key_of("ad_copy") == "ad_copy"
    assert m.key_of("job_phase", "browser_attach") == "job_phase:browser_attach"
    assert [m.ad_index(s) for s in ("ad_name", "ad2_name", "ad12_copy", "ad_creative_reuse")] == [1, 2, 12, None]


def test_the_table_in_the_template_is_whole():
    assert [s for s, _ in MODEL["stages"]] == list(m.STAGES)
    assert all(isinstance(v, int) and v >= 1 for _, v in MODEL["stages"])
    assert MODEL["steps"], "the table has no step positions — run the script with --write"
    for key, (at, nxt) in MODEL["steps"].items():
        step = key.split(":", 1)[0]
        assert m.stage_of(step, MODEL["rules"]) in m.STAGES, key
        assert 0 <= at <= 1 and nxt >= 1, key
    # Креатив — самый долгий этап на объявление, финальная проверка — самый короткий.
    secs = dict(MODEL["stages"])
    assert secs["ad"] > secs["campaign"] > secs["final"]


# ── скрипт пересчёта ──────────────────────────────────────────────────────

T0 = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)


def _row(t, step, run="", campaign="CL_A", job="job-1", **kw):
    row = {"run_id": run, "ts": (T0 + timedelta(seconds=t)).isoformat(), "step": step, "ok": True,
           "campaign": campaign, "job": job}
    row.update(kw)
    return row


def _attempt(offset, run, campaign, ads=1, upload=40):
    """Одна сборка: профиль 10 с, кампания 30, группа 100, объявление 20+upload, финал 5."""
    t = offset
    rows = [_row(t, "job_phase", campaign=campaign, note="open_campaigns"),
            _row(t + 10, "campaigns_list_ready", campaign=campaign)]
    t += 10
    rows += [_row(t + 1, "window_bounds", run, campaign), _row(t + 5, "create", run, campaign),
             _row(t + 30, "next_to_adset", run, campaign)]
    t += 30
    rows += [_row(t + 50, "adset_name", run, campaign), _row(t + 100, "next_to_ad", run, campaign)]
    t += 100
    for n in range(1, ads + 1):
        prefix = "ad" if n == 1 else f"ad{n}"
        rows += [_row(t + 5, f"{prefix}_name", run, campaign),
                 _row(t + 10, "ad_creative_reuse", run, campaign),
                 _row(t + 10 + upload, f"{prefix}_creative_upload", run, campaign),
                 _row(t + 20 + upload, f"{prefix}_copy", run, campaign)]
        t += 20 + upload
    rows.append(_row(t + 5, "ad_level_summary", run, campaign))
    return rows


def _write(path: Path, rows) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + '{"broken\n', encoding="utf-8")
    return path


def test_the_script_derives_stage_times_and_step_positions(tmp_path):
    rows = (_attempt(0, "r1", "CL_A") + _attempt(1000, "r2", "CL_B", ads=3)
            + _attempt(3000, "r3", "CL_C", upload=400)
            + [_row(5, "job_phase", campaign="", note="library_pull")])   # без кампании — мимо
    got = m.derive(m.load_rows(_write(tmp_path / "t.jsonl", rows)), MODEL["rules"])
    assert got["attempts"] == 3 and not got["unknown"]
    assert dict(got["stages"]) == {"profile": 10, "campaign": 30, "adset": 100, "ad": 60, "final": 5}
    # adset_name — на половине группы; ad2_name и ad_name — один ключ.
    assert got["steps"]["adset_name"] == [0.5, 50]
    assert got["steps"]["ad_name"][0] == pytest.approx(5 / 60, abs=0.01)
    # Пауза после next_to_adset — до adset_name: служебные строки не в счёт.
    assert got["steps"]["next_to_adset"][1] == 50
    assert got["steps"]["campaigns_list_ready"][1] == 5, "window_bounds right after it is not the next step"
    assert got["steps"]["ad_creative_reuse"][1] == 40


def test_write_updates_only_the_derived_part(tmp_path):
    template = tmp_path / "report.html"
    shutil.copy(m.TEMPLATE, template)
    tele = _write(tmp_path / "t.jsonl", _attempt(0, "r1", "CL_A"))
    assert m.main([str(tele), "--template", str(template), "--write"]) == 0
    new = m.read_model(template)
    assert new["rules"] == MODEL["rules"]
    assert dict(new["stages"])["adset"] == 100
    assert new["derived"]["attempts"] == 1
    # Вне блока шаблон не тронут.
    before = m._BLOCK.sub("", m.TEMPLATE.read_text(encoding="utf-8"))
    after = m._BLOCK.sub("", template.read_text(encoding="utf-8"))
    assert before == after


def test_a_step_no_rule_knows_is_reported(tmp_path, capsys):
    tele = _write(tmp_path / "t.jsonl", _attempt(0, "r1", "CL_A") + [_row(400, "brand_new_step", "r1")])
    assert m.main([str(tele), "--template", str(m.TEMPLATE)]) == 1
    assert "brand_new_step" in capsys.readouterr().out
