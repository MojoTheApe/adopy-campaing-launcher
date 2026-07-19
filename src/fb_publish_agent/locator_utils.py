"""Resolve a step name to a Playwright Locator by trying a list of candidate
selectors in order — the "self-healing" bit described in
docs/agent10_fb_ads_playwright_upload.md §1.2: selectors are data (selectors.json),
not hardcoded in the flow code, so a UI change is fixed by editing the config,
not the script.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

DEFAULT_SELECTORS_PATH = Path(__file__).resolve().parent / "selectors.json"


class SelectorResolutionError(Exception):
    """Raised when none of the candidate selectors for a step could be found.

    Carries `step_name` and `attempted` so the caller can log exactly what
    was tried — this is what you paste into Playwright Inspector when
    re-calibrating a broken step.
    """

    def __init__(self, step_name: str, attempted: list[dict]):
        self.step_name = step_name
        self.attempted = attempted
        super().__init__(
            f"Could not resolve step '{step_name}' — tried {len(attempted)} candidate(s): {attempted}"
        )


def load_selectors(path: str | Path | None = None) -> dict[str, list[dict]]:
    """Load the selector map from JSON. Keys starting with '_' (e.g. '_readme') are metadata, not steps."""
    p = Path(path) if path else DEFAULT_SELECTORS_PATH
    with open(p, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _format_candidate(candidate: dict, **kwargs) -> dict:
    """Substitute {value}-style placeholders in string fields of a candidate dict."""
    out = copy.deepcopy(candidate)
    if not kwargs:
        return out
    for key, val in out.items():
        if isinstance(val, str) and "{" in val:
            try:
                out[key] = val.format(**kwargs)
            except (KeyError, IndexError):
                pass
    return out


def _build_locator(page, candidate: dict):
    by = candidate.get("by")
    if by == "role":
        kwargs = {}
        if "name" in candidate:
            kwargs["name"] = candidate["name"]
        if "exact" in candidate:
            kwargs["exact"] = candidate["exact"]
        return page.get_by_role(candidate["role"], **kwargs)
    if by == "label":
        return page.get_by_label(candidate["label"], exact=candidate.get("exact", False))
    if by == "placeholder":
        return page.get_by_placeholder(candidate["placeholder"], exact=candidate.get("exact", False))
    if by == "text":
        return page.get_by_text(candidate["text"], exact=candidate.get("exact", False))
    if by == "test_id":
        return page.get_by_test_id(candidate["test_id"])
    raise ValueError(f"Unknown selector type 'by={by}' in candidate: {candidate}")


def resolve(page, step_name: str, selectors_map: dict[str, list[dict]], timeout: int = 6000, **format_kwargs) -> Any:
    """Try each candidate selector for `step_name` in order; return the first visible Locator.

    `format_kwargs` fill in {value} placeholders for dynamic steps (e.g. interest_option, value="Trading").
    Raises SelectorResolutionError with every candidate tried if none resolve — the caller should
    screenshot the page and log this so the selector can be fixed in selectors.json.
    """
    candidates = selectors_map.get(step_name)
    if not candidates:
        raise SelectorResolutionError(step_name, [])
    per_candidate_timeout = max(1500, timeout // len(candidates))
    attempted = []
    for raw_candidate in candidates:
        candidate = _format_candidate(raw_candidate, **format_kwargs)
        attempted.append(candidate)
        try:
            locator = _build_locator(page, candidate).first
            locator.wait_for(state="visible", timeout=per_candidate_timeout)
            return locator
        except Exception:
            continue
    raise SelectorResolutionError(step_name, attempted)


def click_step(page, step_name: str, selectors_map: dict[str, list[dict]], timeout: int = 6000, **format_kwargs) -> None:
    resolve(page, step_name, selectors_map, timeout=timeout, **format_kwargs).click()


def fill_step(page, step_name: str, value: str, selectors_map: dict[str, list[dict]], timeout: int = 6000, **format_kwargs) -> None:
    resolve(page, step_name, selectors_map, timeout=timeout, **format_kwargs).fill(value)


def step_exists(page, step_name: str, selectors_map: dict[str, list[dict]], timeout: int = 3000, **format_kwargs) -> bool:
    """Non-raising check — used for optional fields and login verification."""
    try:
        resolve(page, step_name, selectors_map, timeout=timeout, **format_kwargs)
        return True
    except SelectorResolutionError:
        return False
