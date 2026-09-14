from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src import acl_log
from src.meta_campaign_agent.campaign_builder import PublishJob
from src.meta_campaign_agent.creative_uploader import CreativeUploader
from src.meta_campaign_agent.meta_client import MetaClient, MetaGraphError


@dataclass
class PublishResult:
    job: PublishJob
    success: bool = False
    campaign_id: str | None = None
    adset_id: str | None = None
    creative_id: str | None = None
    ad_id: str | None = None
    image_hash: str | None = None
    video_id: str | None = None
    error: str | None = None


@dataclass
class LivePublishReport:
    results: list[PublishResult] = field(default_factory=list)
    log_path: str | None = None

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failure_count(self) -> int:
        return sum(1 for r in self.results if not r.success)


def _budget_cents(amount: float) -> int:
    return max(1, int(round(amount * 100)))


class MetaPublisher:
    def __init__(self, client: MetaClient, uploader: CreativeUploader, log_path: str | Path = "output/publish_log.jsonl"):
        self.client = client
        self.uploader = uploader
        self.log_path = Path(log_path)

    def _log(self, entry: dict) -> None:
        # Через `acl_log`: две публикации, идущие одновременно, при простом
        # дописывании на Windows затирают записи друг друга — замер 15.09.
        entry = {**entry, "ts": datetime.now(timezone.utc).isoformat()}
        acl_log.append_line(self.log_path, json.dumps(entry, ensure_ascii=False))

    def publish_job(self, job: PublishJob) -> PublishResult:
        result = PublishResult(job=job)
        if job.errors:
            result.error = "; ".join(job.errors)
            self._log(
                {
                    "event": "skipped",
                    "account": job.account_name,
                    "row": job.row_number,
                    "campaign_name": job.campaign_name,
                    "error": result.error,
                }
            )
            return result

        assert job.ad_account_id
        assert job.creative_path
        assert job.page_id
        assert job.link
        assert job.daily_budget is not None

        try:
            is_video = (job.creative_type or "").lower() == "video"
            image_hash: str | None = None
            video_id: str | None = None
            if is_video:
                video_id = self.uploader.upload_video(job.ad_account_id, job.creative_path)
                result.video_id = video_id
            else:
                image_hash = self.uploader.upload_image(job.ad_account_id, job.creative_path)
                result.image_hash = image_hash

            campaign_id = self.client.create_campaign(
                job.ad_account_id,
                name=job.campaign_name,
                objective=job.objective,
                status=job.status,
            )
            result.campaign_id = campaign_id

            targeting = job.targeting_spec or {
                "geo_locations": {"countries": [job.country_code] if job.country_code else []},
                "age_min": job.age_min,
                "age_max": job.age_max,
            }
            promoted_object: dict = {"pixel_id": job.pixel_id, "custom_event_type": "LEAD"}
            if not job.pixel_id:
                promoted_object = {}

            adset_id = self.client.create_ad_set(
                job.ad_account_id,
                name=job.campaign_name,
                campaign_id=campaign_id,
                daily_budget_cents=_budget_cents(job.daily_budget),
                optimization_goal=job.optimization_goal,
                billing_event=job.billing_event,
                bid_strategy=job.bid_strategy,
                targeting=targeting,
                promoted_object=promoted_object,
                status=job.status,
                destination_type=job.destination_type,
            )
            result.adset_id = adset_id

            creative_id = self.client.create_ad_creative(
                job.ad_account_id,
                name=f"{job.campaign_name} creative",
                page_id=job.page_id,
                link=job.link,
                title=job.ad_title,
                body=job.ad_body,
                link_description=job.ad_link_description,
                image_hash=image_hash,
                video_id=video_id,
            )
            result.creative_id = creative_id

            ad_id = self.client.create_ad(
                job.ad_account_id,
                name=job.campaign_name,
                adset_id=adset_id,
                creative_id=creative_id,
                status=job.status,
            )
            result.ad_id = ad_id
            result.success = True

            self._log(
                {
                    "event": "published",
                    "account": job.account_name,
                    "ad_account_id": job.ad_account_id,
                    "row": job.row_number,
                    "campaign_name": job.campaign_name,
                    "status": job.status,
                    "campaign_id": campaign_id,
                    "adset_id": adset_id,
                    "creative_id": creative_id,
                    "ad_id": ad_id,
                }
            )
        except (MetaGraphError, OSError, FileNotFoundError) as e:
            result.error = str(e)
            self._log(
                {
                    "event": "failed",
                    "account": job.account_name,
                    "row": job.row_number,
                    "campaign_name": job.campaign_name,
                    "error": result.error,
                    "partial": {
                        "campaign_id": result.campaign_id,
                        "adset_id": result.adset_id,
                        "creative_id": result.creative_id,
                        "ad_id": result.ad_id,
                    },
                }
            )
        return result

    def publish_jobs(self, jobs: list[PublishJob]) -> LivePublishReport:
        report = LivePublishReport(log_path=str(self.log_path))
        for job in jobs:
            report.results.append(self.publish_job(job))
        return report
