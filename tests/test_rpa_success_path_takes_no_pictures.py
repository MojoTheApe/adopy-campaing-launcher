"""ACL-1313: удачный шаг не снимается — ни PNG, ни разметкой. Отказ — всегда.

Прогоны 25.09: PNG удачного шага стоил 5–7 с при трёх профилях, разметка —
1–6 с (advantage_plus_before/control, library_pick_selected, ad_media_ready,
targeting_and_groups — 15–20 с на кампанию). Вдобавок разметка удачных шагов
ела лимит `page_capture.MAX_PER_RUN`, и отказу в конце прогона снимка уже не
доставалось. FB_RPA_SHOT_ALL=1 возвращает всё.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom, page_capture, screenshots  # noqa: E402

META_DOM = ROOT / "src" / "fb_ads_rpa" / "meta_dom.py"

#: Шаги, которые снимались на удачном пути каждого прогона.
SUCCESS_PAGE_STEPS = {
    "library_pick_selected", "ad_media_ready", "targeting_and_groups",
    "advantage_plus_before", "advantage_plus_control", "account_controls_review",
}


class _Page:
    url = "https://x.example/p"

    def __init__(self):
        self.screenshots = 0

    def locator(self, selector):
        return self

    def inner_html(self, *, timeout):
        return "<body>ok</body>"

    def screenshot(self, **kwargs):
        self.screenshots += 1
        Path(kwargs["path"]).write_bytes(b"png")


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.delenv("FB_RPA_SHOT_ALL", raising=False)
    monkeypatch.setattr(page_capture, "get_runtime_workspace", lambda: tmp_path)
    page_capture._taken.clear()
    yield
    page_capture._taken.clear()


@pytest.mark.parametrize("label", [
    "after_create", "after_placements", "after_ad_copy", "after_ad3_creative_upload",
    "success_website_selected", "success_conversion_event",
    "before_audience_location", "skip_targeting", "skip_browser_error_recovered",
])
def test_no_png_for_a_step_that_did_not_fail(label):
    assert meta_dom._shot_worth_a_picture(label) is False


def test_shot_all_brings_every_picture_back(monkeypatch):
    monkeypatch.setenv("FB_RPA_SHOT_ALL", "1")
    assert meta_dom._shot_worth_a_picture("after_campaign_name") is True
    assert meta_dom._shot_worth_a_picture("before_audience_location") is True


def test_success_page_capture_is_opt_in(monkeypatch):
    page = _Page()
    assert page_capture.capture_on_success(page, step="ad_media_ready", run_id="r1") == ""
    assert page_capture._taken.get("r1", 0) == 0
    monkeypatch.setenv("FB_RPA_SHOT_ALL", "1")
    assert page_capture.capture_on_success(page, step="ad_media_ready", run_id="r1")


def test_success_captures_leave_the_limit_to_real_failures():
    page = _Page()
    for _ in range(page_capture.MAX_PER_RUN * 2):
        page_capture.capture_on_success(page, step="library_pick_selected", run_id="r2")
    assert page_capture.capture(page, step="ad_copy", run_id="r2")


def test_success_png_is_opt_in(tmp_path, monkeypatch):
    page = _Page()
    assert screenshots.capture_on_success(page, "after_x", base_dir=tmp_path) == ""
    assert page.screenshots == 0
    monkeypatch.setenv("FB_RPA_SHOT_ALL", "1")
    assert screenshots.capture_on_success(page, "after_x", base_dir=tmp_path)
    assert page.screenshots == 1


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node


def test_meta_dom_success_steps_use_the_opt_in_page_capture():
    tree = ast.parse(META_DOM.read_text(encoding="utf-8"))
    seen = {}
    for call in _calls(tree):
        for kw in call.keywords:
            if kw.arg == "step" and isinstance(kw.value, ast.Constant) and kw.value.value in SUCCESS_PAGE_STEPS:
                seen[kw.value.value] = call.func.attr
    assert set(seen) == SUCCESS_PAGE_STEPS
    assert set(seen.values()) == {"capture_on_success"}, seen


def test_meta_dom_takes_no_unconditional_debug_png():
    """Отладочные PNG посреди шагов (возраст, локации) — только опционально.

    Единственный безусловный PNG в meta_dom — в `_shot`, и он там сам
    спрашивает `_shot_worth_a_picture` для не-отказов. ACL-1320: второй —
    доказательство зависания `_stall_evidence`; это путь отказа (сторож
    сработал, окна цели нет), не удачного шага, и он ограничен числом на прогон.
    """
    tree = ast.parse(META_DOM.read_text(encoding="utf-8"))
    evidence = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_stall_evidence")
    in_evidence = {id(n) for n in ast.walk(evidence)}
    plain = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "src.fb_ads_rpa.screenshots"
        and any(a.name == "capture" for a in node.names) and id(node) not in in_evidence
    ]
    assert len(plain) == 1, plain
