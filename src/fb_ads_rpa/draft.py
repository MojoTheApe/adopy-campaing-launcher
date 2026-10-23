"""Campaign tree for RPA publish: Campaign → Ad Set(s) → Ad(s)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.creative_variant import file_variant as _file_variant
from src.fb_ads_rpa import failure_policy as _failure_policy

# Dimensions, not creative numbers: "1080x1080", "1080x1920", and the same
# with a Cyrillic "х" — which really occurs in the folder
# ("text5_img2_1080х1920.png"), so both letters have to be stripped.
_DIMENSIONS_RE = re.compile(r"\d{3,4}\s*[xх×]\s*\d{3,4}", re.I)
# Creative numbers observed across the real folder (4717 files) are always
# four digits: CAC-1253, cac-2285, 1_eslat_2243, 2175_ADN3, "1328 - 26s".
# The boundaries keep it from matching inside longer runs like
# "516594629_1264482105091024".
_CREATIVE_NUM_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def _creative_number(filename: str) -> str:
    """Pull the creative number out of a file name ("…_2243_square.mp4" ->
    "2243"). Returns "" when the name carries no number to speak of — a
    plain "coffee.mp4" or a pure-dimensions "1080x1080-17.png"."""
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    m = _CREATIVE_NUM_RE.search(_DIMENSIONS_RE.sub(" ", stem))
    return m.group(1) if m else ""


@dataclass
class AdDraft:
    creative_type: str = "Image"
    creative_id: str = ""
    creative_filename: str = ""
    creative_path: str | None = None
    copy_name: str = ""
    ad_title: str = ""
    ad_body: str = ""
    ad_link_description: str = ""
    # ACL-1344: необязательный второй вариант основного текста — запись
    # библиотеки текстов (как `copy_name`), из неё берётся только body.
    copy2_name: str = ""
    ad_body2: str = ""
    # ACL-1775: id существующей формы Страницы (только Instant Form, только API-бэкенд).
    lead_form_id: str = ""
    # ACL-1767: тип кнопки (call_to_action.type); пусто = LEARN_MORE как всегда (только API-бэкенд).
    cta: str = ""
    # ACL-1774 (только API-бэкенд): тексты 3-5 — названия записей библиотеки (как `copy2_name`)
    # и разобранные записи для слотов 2-5: [{"name", "body", "headline", "description"}].
    more_copy_names: list[str] = field(default_factory=list)
    text_variants: list[dict[str, str]] = field(default_factory=list)
    # ACL-1774: необязательный вертикальный (9:16) файл Stories/Reels рядом с основным.
    vertical_filename: str = ""
    vertical_path: str | None = None
    # ACL-1776: id шаблона формы из библиотеки (форму создаёт робот; только API-бэкенд).
    lead_form_template: str = ""

    def ad_name(self) -> str:
        """Name for the ad in Ads Manager: "<creative> [<variant>] - <text>".

        The stats sheet stores both halves in one Ad Title cell in exactly
        this shape ("2046 1 - Text6"), and the dashboard splits it back
        apart (`split_ad_title`). Publishing only the text half — which is
        what happened until 2026-08-05, ads landed named just "Text4" —
        meant the creative could never be attributed in the reports.

        The creative half is `creative_id` when the builder supplied one,
        otherwise the file's own name without its extension.
        """
        creative = (self.creative_id or "").strip()
        if not creative and self.creative_filename:
            creative = _creative_number(self.creative_filename)
        # ACL-1593: ведущее число файла — вариант («1_es_2403_post.mp4» →
        # «2403 1»). Только когда номер имени совпадает с креативом объявления:
        # свободный текст в creative_id число из файла не получает.
        if creative and self.creative_filename and creative == _creative_number(self.creative_filename):
            variant = _file_variant(self.creative_filename)
            if variant:
                creative = f"{creative} {variant}"
        text = (self.copy_name or "").strip()
        if creative and text:
            return f"{creative} - {text}"
        return text or creative

    def to_preview_dict(self) -> dict[str, Any]:
        out = {
            "creative_type": self.creative_type,
            "creative_id": self.creative_id,
            "creative_file": self.creative_filename or "—",
            "creative_path": self.creative_path or "—",
            "copy_name": self.copy_name or "—",
            "ad_title": self.ad_title or "—",
            "ad_body": (self.ad_body or "")[:80],
        }
        if self.copy2_name:
            # ACL-1344: второй текст — только у объявлений, где он выбран.
            out["copy2_name"] = self.copy2_name
            out["ad_body2"] = (self.ad_body2 or "")[:80]
        if self.lead_form_id:
            out["lead_form_id"] = self.lead_form_id
        if self.cta:
            out["cta"] = self.cta
        if self.more_copy_names:
            out["more_copy_names"] = list(self.more_copy_names)
        if self.vertical_filename:
            out["vertical_file"] = self.vertical_filename
        if self.lead_form_template:
            out["lead_form_template"] = self.lead_form_template
        return out


@dataclass
class AdSetDraft:
    name: str
    targeting_label: str = ""
    os: str = "All"
    age_min: int = 18
    age_max: int = 65
    daily_budget: float | None = None
    ads: list[AdDraft] = field(default_factory=list)
    # ACL-1775: "website" (как всегда) или "instant_form" (только API-бэкенд).
    destination: str = "website"
    # ACL-1767 (только API-бэкенд): пусто = как раньше, в черновике и плане ключей нет.
    gender: str = ""            # "men" | "women"
    languages: list[str] = field(default_factory=list)   # дополнительные языки
    device: str = ""            # "mobile" | "desktop" (пусто = все устройства)
    os_min: str = ""            # минимальная версия ОС, например "14"
    start_time: str = ""        # "YYYY-MM-DDTHH:MM", время часового пояса кабинета

    def to_preview_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "targeting": self.targeting_label or "—",
            "os": self.os,
            "age": f"{self.age_min}-{self.age_max}",
            "daily_budget": self.daily_budget,
            "ads": [a.to_preview_dict() for a in self.ads],
        }
        if self.destination != "website":
            out["destination"] = self.destination
        for key in ("gender", "languages", "device", "os_min", "start_time"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out


@dataclass
class CampaignDraft:
    """One campaign to create in Ads Manager UI.

    status values:
      "DRAFT"  — fill wizard fully, do NOT click Publish (default)
      "ACTIVE" — fill wizard, click Publish → live campaign
    """

    name: str
    country: str
    account_label: str
    objective: str = "Leads"
    status: str = "DRAFT"
    link: str = ""
    pixel_id: str = ""
    language: str = ""
    page_id: str | None = None
    ad_account_id: str | None = None
    ad_sets: list[AdSetDraft] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Empty form fields are explicit choices; older callers may omit them.
    link_provided: bool = False
    language_provided: bool = False
    # Builder Page selection is campaign-local; empty means automatic, never map fallback.
    page_provided: bool = False
    page_name: str = ""
    page_name_unique: bool = False

    @property
    def ad_count(self) -> int:
        return sum(len(ad_set.ads) for ad_set in self.ad_sets)

    def to_preview_dict(self) -> dict[str, Any]:
        return {
            "Campaign Name": self.name,
            "Countries": self.country,
            "Account": self.account_label,
            "Ad Account ID": self.ad_account_id or "—",
            "Page ID": self.page_id or "—",
            "Status": self.status,
            "Objective": self.objective,
            "Link": self.link or "—",
            "Language": self.language or "—",
            "ad_sets": [a.to_preview_dict() for a in self.ad_sets],
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class RpaPublishResult:
    campaign_name: str
    success: bool = False
    error: str | None = None
    steps_completed: list[str] = field(default_factory=list)
    screenshot_paths: list[str] = field(default_factory=list)
    campaign_id: str | None = None
    adset_id: str | None = None
    ad_ids: list[str] = field(default_factory=list)
    # Ad-level steps that failed while the run as a whole still reached the
    # end. `success` stays True because the draft really was created — but a
    # draft whose ad has no creative must not be published, and until now the
    # only trace of that was a telemetry line nobody reads before clicking
    # Publish (run 58c3cb9b53c6: ad3 had no media, result said success).
    warnings: list[str] = field(default_factory=list)
    # Steps whose failure makes the draft unsafe to publish, as opposed to
    # merely imperfect. Separate from `warnings` because the operator needs to
    # know which of the two they are looking at without reading the text.
    blocking_failures: list[str] = field(default_factory=list)
    # Telemetry run id of this campaign (ACL-1437): shown under "Technical
    # details" so a card can be matched to rpa_telemetry.jsonl.
    run_id: str | None = None

    @property
    def publishable(self) -> bool:
        """Did the run finish AND produce a draft a human may publish?

        `success` answers only the first half — the automation reached the end
        and a draft exists. Treating it as the second half is how a draft whose
        ad had no creative came back green. Keep the two apart: `success` is
        about execution, `publishable` is about the campaign.
        """
        return bool(self.success) and not self.blocking_failures

    def to_job_dict(self) -> dict[str, Any]:
        return {
            "campaign_name": self.campaign_name,
            "success": self.success,
            # Everything the operator needs in order NOT to publish a broken
            # draft. `warnings` existed on the object but was never serialized,
            # so the layer that shows results to a human never received it.
            "publishable": self.publishable,
            "warnings": self.warnings,
            "blocking_failures": self.blocking_failures,
            # ACL-1437: the same failures as a short readable list (title,
            # impact, action per step). `error` keeps the old plain text for
            # older readers and Sentry.
            "problems": _failure_policy.problems(
                list(self.blocking_failures) + list(self.warnings)),
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "adset_id": self.adset_id,
            "creative_id": None,
            "ad_id": self.ad_ids[0] if self.ad_ids else None,
            "ad_ids": self.ad_ids,
            "error": self.error,
            "steps_completed": self.steps_completed,
            "screenshots": self.screenshot_paths,
        }
