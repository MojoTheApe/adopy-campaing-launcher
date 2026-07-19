"""Orchestrates one publish run: loads pending plan rows, drives the Ads Manager
wizard for each, and writes status/screenshots/trace so failures are debuggable
and re-runs are idempotent (docs/agent10_fb_ads_playwright_upload.md §1.4-§1.6).

Safety default: `dry_run=True` fills the whole wizard and screenshots it right
before the Publish button, but does not click Publish. Use this to calibrate
selectors.json against a real account without spending budget or creating
campaigns, before ever passing dry_run=False.
"""
from __future__ import annotations

import datetime
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

_proj_root = Path(__file__).resolve().parents[2]
if str(_proj_root) not in sys.path:
    sys.path.insert(0, str(_proj_root))

from config.paths import get_fb_ads_auth_state_path, get_fb_ads_manager_url, get_fb_publish_output_dir

from . import locator_utils as loc
from . import plan_loader
from .locator_utils import SelectorResolutionError
from .pages import AdsManagerCreateFlow

OBJECTIVE_LABELS = {
    "OUTCOME_LEADS": "Leads",
    "OUTCOME_SALES": "Sales",
    "OUTCOME_TRAFFIC": "Traffic",
    "OUTCOME_ENGAGEMENT": "Engagement",
    "OUTCOME_AWARENESS": "Awareness",
    "OUTCOME_APP_PROMOTION": "App promotion",
}

BETWEEN_ROW_DELAY_RANGE = (3.0, 7.0)  # human-like pacing, §1.6


def build_campaign_spec_from_row(row: plan_loader.PlanRow, ref: dict | None) -> dict[str, str]:
    """Map an Agent 8 plan row (arbitrary/varying headers) to the fields the wizard needs."""
    objective_code = row.get("Campaign Objective", default="OUTCOME_LEADS")
    video_file = row.get("Video File name", "Video file name")
    image_file = row.get("Image File name", "Image file name")

    ad_text = {"title": "", "body": "", "link_description": ""}
    if ref is not None:
        copy_key = row.get("Copy")
        if copy_key:
            try:
                from src.upload_agent.build_upload_excel import lookup_ad_copy

                ad_text = lookup_ad_copy(ref, copy_key)
            except Exception:
                pass

    return {
        "campaign_name": row.get("Campaign Name", "Campaign name"),
        "ad_set_name": row.get("Ad Set Name", "Ad set name") or row.get("Campaign Name"),
        "objective_label": OBJECTIVE_LABELS.get(objective_code.upper(), "Leads"),
        "daily_budget": row.get("Campaign Daily Budget", "Daily Budget"),
        "countries": row.get("Countries"),
        "age_min": row.get("Age Min"),
        "age_max": row.get("Age Max"),
        "interest": row.get("Targeting"),
        "creative_filename": video_file or image_file,
        "website_url": row.get("Link", "Website URL"),
        "primary_text": ad_text.get("body") or "",
        "headline": ad_text.get("title") or "",
        "link_description": ad_text.get("link_description") or "",
    }


def _load_reference(reference_excel_path: str | None) -> dict | None:
    if not reference_excel_path:
        return None
    try:
        from src.upload_agent.build_upload_excel import load_reference_data

        return load_reference_data(reference_excel_path)
    except Exception as e:
        print(f"[WARN] Could not load reference Excel ({reference_excel_path}): {e} — ad text will be left blank.")
        return None


def run(
    plan_path: str,
    account: str | None = None,
    limit: int | None = None,
    dry_run: bool = True,
    headless: bool = False,
    slow_mo: int = 250,
    auth_state_path: str | None = None,
    ads_manager_url: str | None = None,
    reference_excel_path: str | None = None,
    output_dir: str | None = None,
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    auth_state_path = auth_state_path or get_fb_ads_auth_state_path()
    if not Path(auth_state_path).is_file():
        raise RuntimeError(
            f"No saved session at {auth_state_path}. Run: python run_fb_login_session.py"
        )
    ads_manager_url = ads_manager_url or get_fb_ads_manager_url()
    output_dir = output_dir or get_fb_publish_output_dir()

    selectors_map = loc.load_selectors()
    ref = _load_reference(reference_excel_path)

    rows = plan_loader.load_pending_rows(plan_path, account=account, limit=limit)
    if not rows:
        print("Nothing to publish (no pending rows, or all already done/dry_run_ok).")
        return {"total": 0, "success": 0, "dry_run": 0, "failed": 0}

    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(output_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    counts = {"total": len(rows), "success": 0, "dry_run": 0, "failed": 0}
    row_reports = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
        context = browser.new_context(storage_state=auth_state_path)
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()
        flow = AdsManagerCreateFlow(page, selectors_map)

        try:
            flow.open(ads_manager_url)
        except SelectorResolutionError as e:
            context.tracing.stop(path=str(run_dir / "trace.zip"))
            browser.close()
            raise RuntimeError(
                "Could not confirm login on Ads Manager — session may have expired "
                "(re-run run_fb_login_session.py) or the UI changed (recalibrate selectors.json). "
                f"Details: {e}"
            ) from e

        try:
            for row in rows:
                row_label = f"{row.sheet_name} row {row.row_index}"
                try:
                    flow.open(ads_manager_url)  # reset wizard state before each row
                    flow.start_create()
                    spec = build_campaign_spec_from_row(row, ref)

                    flow.set_campaign_name(spec["campaign_name"])
                    flow.select_objective(spec["objective_label"])
                    flow.next_step()

                    flow.set_ad_set_name(spec["ad_set_name"])
                    flow.set_daily_budget(spec["daily_budget"])
                    flow.set_countries(spec["countries"])
                    flow.set_age_range(spec["age_min"], spec["age_max"])
                    flow.set_targeting_interest(spec["interest"])
                    flow.next_step()

                    flow.set_creative_from_library(spec["creative_filename"])
                    flow.set_ad_text(
                        primary_text=spec["primary_text"],
                        headline=spec["headline"],
                        link_description=spec["link_description"],
                        website_url=spec["website_url"],
                    )

                    page.screenshot(path=str(run_dir / f"{row.sheet_name}_row{row.row_index}_before_publish.png"))

                    if dry_run:
                        plan_loader.update_row_status(plan_path, row.sheet_name, row.row_index, plan_loader.STATUS_DRY_RUN_OK)
                        counts["dry_run"] += 1
                        print(f"[DRY-RUN OK] {row_label}: {spec['campaign_name']}")
                        row_reports.append({"row": row_label, "status": "dry_run_ok", "campaign_name": spec["campaign_name"]})
                    else:
                        fb_campaign_id = flow.publish()
                        plan_loader.update_row_status(
                            plan_path, row.sheet_name, row.row_index, plan_loader.STATUS_DONE, fb_campaign_id=fb_campaign_id
                        )
                        counts["success"] += 1
                        print(f"[PUBLISHED] {row_label}: {spec['campaign_name']} -> fb_campaign_id={fb_campaign_id}")
                        row_reports.append({"row": row_label, "status": "done", "campaign_name": spec["campaign_name"], "fb_campaign_id": fb_campaign_id})

                except Exception as e:
                    try:
                        page.screenshot(path=str(run_dir / f"{row.sheet_name}_row{row.row_index}_error.png"))
                    except Exception:
                        pass
                    plan_loader.update_row_status(plan_path, row.sheet_name, row.row_index, plan_loader.STATUS_FAILED, error_message=str(e))
                    counts["failed"] += 1
                    print(f"[FAILED] {row_label}: {e}")
                    row_reports.append({"row": row_label, "status": "failed", "error": str(e)})
                finally:
                    time.sleep(random.uniform(*BETWEEN_ROW_DELAY_RANGE))
        finally:
            context.tracing.stop(path=str(run_dir / "trace.zip"))
            context.close()
            browser.close()

    summary = {**counts, "run_id": run_id, "dry_run_mode": dry_run, "rows": row_reports}
    with open(run_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\nRun {run_id}: {counts['success']} published, {counts['dry_run']} dry-run OK, {counts['failed']} failed.")
    print(f"Details, screenshots and trace: {run_dir}")
    return summary
