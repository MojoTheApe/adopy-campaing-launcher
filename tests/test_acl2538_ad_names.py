"""ACL-2538: different ads always get different names; the plan is not refused for it."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent.analyze_fb_stats import split_ad_title  # noqa: E402
from src.fb_ads_rpa.draft import AdDraft  # noqa: E402
from src.fb_api import creator as cr  # noqa: E402
from tests.test_acl1761_api_create import _draft  # noqa: E402


def _ad(fname, text="Head", cid=""):
    return AdDraft(creative_id=cid, creative_filename=fname, copy_name=text)


def _unnumber(d, names):
    for ad, n in zip(d.ad_sets[0].ads, names):
        ad.creative_id = ""
        ad.creative_filename = n
        ad.copy_name = "Head"


def test_numberless_files_use_their_stem_as_creative_half():
    assert _ad("coffee beans.mp4").ad_name() == "coffee beans - Head"
    assert _ad(r"C:\folder\Sunset.PNG").ad_name() == "Sunset - Head"  # linux-ok: sample Windows path is only data for the name function
    assert _ad("a/b/dog.png").ad_name() != _ad("a/b/cat.png").ad_name()


def test_stem_is_length_limited_and_dash_flattened():
    name = _ad("x" * 200 + ".png").ad_name()
    assert name == "x" * 40 + " - Head"
    assert _ad("big - sale.png").ad_name() == "big sale - Head"


def test_cut_at_40_does_not_leave_a_readable_digit_run():
    # the 40-char cut lands inside "12345": the leftover "123" must not be read as a creative number
    stem = _ad("a" * 37 + "12345.png").ad_name()
    creative, txt = split_ad_title(stem)
    assert creative != "123" and "123" not in stem
    assert len(stem.split(" - ")[0]) <= 40


def test_numbered_files_and_explicit_creative_id_keep_todays_names():
    assert _ad("CAC-1253.mp4").ad_name() == "1253 - Head"
    assert _ad("1_es_2403_post.mp4").ad_name() == "2403 1 - Head"
    assert _ad("anything.mp4", cid="77").ad_name() == "77 - Head"
    assert _ad("1080x1080-17.png", text="").ad_name() == "17"


def test_no_file_no_creative_is_just_the_text():
    assert _ad("").ad_name() == "Head"


def test_dashboard_still_splits_stem_names():
    assert split_ad_title(_ad("coffee beans.mp4").ad_name()) == ("coffee beans", "Head")
    assert split_ad_title(_ad("big - sale.png").ad_name()) == ("big sale", "Head")


def test_different_numberless_files_pass_check_plan(tmp_path):
    d = _draft(tmp_path, ads=3)
    _unnumber(d, ["dog.png", "cat.png", "bird.png"])
    plan = cr.plan_from_draft(d)
    assert [a.name for a in plan.ad_sets[0].ads] == ["dog - Head", "cat - Head", "bird - Head"]
    assert not [w for w in plan.warnings if "named" in w]


def test_same_file_and_text_get_numbered_with_a_note(tmp_path):
    d = _draft(tmp_path, ads=3)
    _unnumber(d, ["dog.png", "dog.png", "dog.png"])
    plan = cr.plan_from_draft(d)
    assert [a.name for a in plan.ad_sets[0].ads] == ["dog - Head", "dog - Head 2", "dog - Head 3"]
    assert "Ad 2 named 'dog - Head 2' because Ad 1 has the same creative and text" in plan.warnings
    assert "Ad 3 named 'dog - Head 3' because Ad 1 has the same creative and text" in plan.warnings


def test_numbered_suffix_does_not_collide_with_an_existing_name(tmp_path):
    d = _draft(tmp_path, ads=3)
    _unnumber(d, ["dog.png", "dog.png", "dog.png"])
    d.ad_sets[0].ads[1].copy_name = "Head 2"       # ad 2 is already "dog - Head 2"
    d.ad_sets[0].ads[2].copy_name = "Head"
    plan = cr.plan_from_draft(d)
    names = [a.name for a in plan.ad_sets[0].ads]
    assert len(set(names)) == 3 and names[0] == "dog - Head" and names[1] == "dog - Head 2"


def test_ad_set_name_duplicates_are_still_refused(tmp_path):
    d = _draft(tmp_path, ads=1)
    import copy
    d.ad_sets.append(copy.deepcopy(d.ad_sets[0]))
    with pytest.raises(cr.PlanError, match="Two ad sets are named"):
        cr.plan_from_draft(d)


def test_stem_digits_are_not_read_as_a_creative_number():
    # IMG_123 / clip 120 / 1080x1080-17 must not turn into creative 123/120/1080 in stats
    for fname, text, want in [
        ("IMG_123.jpg", "Head", ("IMG_1 23", "Head")),
        ("clip 120.mp4", "Head", ("clip 1 20", "Head")),
        ("1080x1080-17.png", "Head", ("17", "Head")),
        ("promo_1080х1920.png", "Head", ("promo", "Head")),
    ]:
        creative, txt = split_ad_title(_ad(fname, text=text).ad_name())
        assert (creative, txt) == want, fname
    # a long digit run (not 3-4 digits) is untouched and not a creative number either
    assert _ad("516594629.png").ad_name() == "516594629 - Head"


def test_js_file_stem_mirrors_python():
    import json, re, shutil, subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    html = (ROOT / "src/web/templates/report.html").read_text(encoding="utf-8")
    m = re.search(r"  function fileStem\(file\) \{.*?\n  \}\n", html, re.S)
    assert m
    from src.fb_ads_rpa.draft import _file_stem_label
    names = ["coffee beans.mp4", "IMG_123.jpg", "clip 120.mp4", "1080x1080-17.png",
             "promo_1080х1920.png", "big - sale.png", "a/b/dog 1234 x.png", "516594629.png", "x" * 60 + ".png",
             "a" * 37 + "12345.png"]
    script = m.group(0) + "\nconsole.log(JSON.stringify(%s.map(fileStem)));" % json.dumps(names)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", check=True).stdout
    assert json.loads(out) == [_file_stem_label(n) for n in names]
