# -*- coding: utf-8 -*-
"""Ad Destination layouts met live 30.09–01.10, read by the robot's `destination_state`.

Bench (ACL-1433). Each fragment of `ad_destination_layouts_2026_10_01.html` is
the `#adgroupAdDestinationGeneric` section of a failing `ad_destination` /
`destination_sweep` capture. The expected values are the robot's own live
report (`context.json` → `diag`), so the test says: on this markup the reader
sees what it saw live, and the step names the same reason.

- `event_type` (cc8fcfa17ae9/07, 01.10, classic editor): destination type
  **Facebook event** selected — a type not met before (ACL-1371 saw Instant
  Experience). No Website URL field.
- `instant_form_terms` (5465ca61895f/01, 01.10): Website + Browser add-on
  "Instant form" with the Lead Gen error and the Lead Ads Terms card.
- `whatsapp_addon` (cb854b07857b/03, 30.09, single-screen editor): add-on
  **WhatsApp** selected ("To use WhatsApp add-on, connect your WhatsApp profile").
- `simple_create_instant_form` (d66a22d0fa30/01, 30.09): the single-screen
  editor's section with "Instant form" and the Lead Gen error; live the
  section was off screen (`section_shown: false`).
- `sweep_addon_none` (3100d06ad51e/04, 30.09): add-on None already set, the
  sweep stopped on `url_field_not_focused`.

Visibility (`section_shown`, which URL inputs count) depends on Meta's CSS,
which the bench does not have; those keys are not compared.
"""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import meta_dom as md
from tests.bench_support import FIXTURES, browser, fragment, offline_page  # noqa: F401

FIXTURE = FIXTURES / "ad_destination_layouts_2026_10_01.html"

#: fragment -> (live diag subset, raw why passed to `_destination_failed`, why named)
#:
#: ACL-1543: where Instant form is still on (add-on «Instant form»), the step
#: names it — `instant_form_not_switched` — instead of the field-level reason
#: it reported live (kept in diag as `field_why`).
CASES = {
    "event_type": (
        {"section": True, "dest_type": "event", "addon": "", "lead_gen_error": False, "lead_terms": False},
        "url_field_missing", "destination_not_website"),
    "instant_form_terms": (
        {"section": True, "dest_type": "external", "addon": "instant_form", "lead_gen_error": True,
         "lead_terms": True},
        "addon_none_not_set", "instant_form_not_switched"),
    "whatsapp_addon": (
        {"section": True, "dest_type": "external", "addon": "whatsapp", "lead_gen_error": False,
         "lead_terms": False},
        "addon_none_not_set", "addon_none_not_set"),
    "simple_create_instant_form": (
        {"section": True, "dest_type": "external", "addon": "instant_form", "lead_gen_error": True,
         "lead_terms": False},
        "url_field_not_focused", "instant_form_not_switched"),
    "sweep_addon_none": (
        {"section": True, "dest_type": "external", "addon": "none", "lead_gen_error": False,
         "lead_terms": False},
        "url_field_not_focused", "url_field_not_focused"),
}


#: The Lead Gen error is not inside the section: Meta shows it in the ad's
#: error block (`ad_video_error_section`), cut from the same capture.
ERRORS = {
    "instant_form_terms": "errors_lead_gen_and_auth",
    "simple_create_instant_form": "errors_lead_gen_simple_create",
}


def _load(page, name: str) -> None:
    html = fragment(FIXTURE, name)
    if name in ERRORS:
        html += fragment(FIXTURE, ERRORS[name])
    page.set_content(html)


@pytest.mark.parametrize("name", sorted(CASES))
def test_destination_state_reads_what_the_robot_saw_live(offline_page, name):
    want, *_ = CASES[name]
    _load(offline_page, name)
    got = md.destination_state(offline_page, "", full=True)
    assert {k: got.get(k) for k in want} == want


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_step_names_the_live_reason(offline_page, name, monkeypatch):
    _, raw, live = CASES[name]
    monkeypatch.setattr(md, "_tab_gone", lambda page: False)
    _load(offline_page, name)
    assert md._destination_failed(offline_page, raw) is False
    assert md.last_destination_why() == live
    if live == "instant_form_not_switched":
        assert md.last_destination_diag()["field_why"] == raw


def test_without_the_section_the_reason_is_section_missing(offline_page, monkeypatch):
    """Simple-create ads 2–3 on 01.10 (0bd520418dbe, d7b470f4c983, 04c8c214d2ba,
    4121938e6dc3; d1755a59e139 on 30.09): the ad form was not in the DOM at all —
    only the right-hand preview with its Ad / Destination tabs. Same reason as live."""
    monkeypatch.setattr(md, "_tab_gone", lambda page: False)
    offline_page.set_content('<span data-surface="/am/editor:ad"></span>'
                             '<div role="tablist"><div role="tab" aria-label="Destination">Destination</div></div>')
    assert md.destination_state(offline_page) == {"section": False}
    md._destination_failed(offline_page, "url_field_missing")
    assert md.last_destination_why() == "destination_section_missing"


def test_the_account_authentication_block_is_recognised(offline_page):
    """5465ca61895f (01.10): next to the Lead Gen error the ad showed Meta's
    "We think someone may have tried to access your account without
    permission … (#3858385)" with "Start authentication" — the account could
    not create or modify ads. The robot's blocked-account reader names it."""
    offline_page.set_content(fragment(FIXTURE, "errors_lead_gen_and_auth"))
    text = md._account_blocked_text(offline_page)
    assert text and md._account_blocked_match(text) == "authentication_error"
    assert md._review_errors_visible(offline_page) is True


def test_a_lead_gen_error_alone_is_not_an_account_block(offline_page):
    offline_page.set_content(fragment(FIXTURE, "errors_lead_gen_simple_create"))
    assert md._account_blocked_text(offline_page) is None
    assert md._review_errors_visible(offline_page) is True
