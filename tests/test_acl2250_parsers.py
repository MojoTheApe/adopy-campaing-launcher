"""ACL-2250: счётчик `copy <n>` в имени набора не ломает разбор статистики и сверку BI. Fakes only."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from src.analysis_agent import analyze_fb_stats as afs  # noqa: E402
from src.analysis_agent import creative_verdicts as cv  # noqa: E402
from src.analysis_agent.adset_name import strip_copy_counter  # noqa: E402
from src.analysis_agent.rules import evidence as ev  # noqa: E402
from tests.test_acl1815_rules_evidence import make_snapshot, m  # noqa: E402

DEVICES = {"All": ["all"], "iOS": ["ios"], "Android": ["android"]}


@pytest.mark.parametrize("name", [
    "MX_Leads_All_Image_GMBL|Trading_35_27.10",
    "MX_Leads_All_Image_GMBL|Trading_35",
    "MX_Leads_All_Image",
])
def test_23_both_parsers_read_a_copy_like_its_original(name):
    for n in (1, 7, 30):
        copy = f"{name} copy {n}"
        assert afs.parse_adset_name(copy, DEVICES, 18, 65, []) == afs.parse_adset_name(name, DEVICES, 18, 65, [])
        assert cv.parse_adset(copy) == cv.parse_adset(name)


def test_the_helper_strips_only_a_trailing_counter():
    assert strip_copy_counter("A_35_27.10 copy 12") == "A_35_27.10"
    assert strip_copy_counter("A copy 1 copy 2") == "A copy 1"
    assert strip_copy_counter("A copy 1x") == "A copy 1x"
    assert strip_copy_counter("A - Copy") == "A - Copy"
    assert strip_copy_counter("A") == "A" and strip_copy_counter(None) is None


def test_22_an_original_and_its_copy_each_keep_their_own_bi_match_and_verdict():
    snap = make_snapshot([(1, "X", [(11, "X")]), (2, "X copy 1", [(21, "X copy 1")])])
    book = {"campaigns": {"X": m(), "X copy 1": m(spend=50.0)}, "ads": {}}
    joined, _ = ev.exact_name_join(snap, book)
    assert joined["adset:11"]["match"] == "name" and joined["adset:21"]["match"] == "name"
    verdicts = ev.identity_verdicts(snap, joined)
    for key in ("adset:11", "adset:21"):
        assert verdicts[key]["verdict"] not in (ev.DUPLICATE_NAME, ev.COPY_SUFFIX)
