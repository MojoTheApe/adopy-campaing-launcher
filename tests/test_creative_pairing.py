# -*- coding: utf-8 -*-
"""Post / Story pairs by creative number (ACL-1426, owner rules 2026-10-01).

Every row of the Issue #426 table, `_square` as `_post`, pairing by number
regardless of the prefix, the same number in two media kinds, the Builder line.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import creative_pairing as cp  # noqa: E402


def test_names_are_read_by_number_and_marker():
    n = cp.parse("4_es_2403_post.mp4")
    assert (n.number, n.marker, n.prefix, n.kind) == ("2403", "post", "4_es", "video")
    assert cp.parse("2_ar_2221_story.mp4").marker == "story"
    assert cp.parse("1_eslat_2243_square.mp4").marker == "post"      # _square = _post
    assert cp.parse("4_factoryblack_1962_ar_facebook_1080x1080.jpg").number == "1962"
    assert cp.parse("4_factoryblack_1962_ar_facebook_1080x1080.jpg").marker == ""
    assert cp.parse("CCblk.mp4").number == ""


# ── the table ─────────────────────────────────────────────────────────────

def test_post_and_story_pair():
    plan = cp.plan("4_es_2403_post.mp4", ["4_es_2403_post.mp4", "4_es_2403_story.mp4"])
    assert (plan.square_file, plan.vertical_file, plan.replace_vertical, plan.rule) == (
        "4_es_2403_post.mp4", "4_es_2403_story.mp4", True, "pair")
    assert plan.builder_label() == "Story: 4_es_2403_story.mp4"


def test_square_counts_as_post():
    plan = cp.plan("1_eslat_2243_square.mp4", ["1_eslat_2243_square.mp4", "1_eslat_2243_story.mp4"])
    assert plan.replace_vertical and plan.vertical_file == "1_eslat_2243_story.mp4"


def test_story_chosen_in_the_builder_puts_the_post_on_square():
    plan = cp.plan("4_es_2403_story.mp4", ["4_es_2403_post.mp4", "4_es_2403_story.mp4"])
    assert (plan.square_file, plan.vertical_file, plan.replace_vertical) == (
        "4_es_2403_post.mp4", "4_es_2403_story.mp4", True)


def test_only_post_goes_on_both_cards_original():
    plan = cp.plan("4_es_2403_post.mp4", ["4_es_2403_post.mp4", "4_es_2404_story.mp4"])
    assert (plan.square_file, plan.vertical_file, plan.replace_vertical, plan.rule) == (
        "4_es_2403_post.mp4", "4_es_2403_post.mp4", False, "post_only")
    assert plan.builder_label() == "Story: same file, Original"


def test_no_marker_goes_on_both_cards():
    plan = cp.plan("2403.mp4", ["2403.mp4", "4_es_2403_story.mp4"])
    assert (plan.vertical_file, plan.replace_vertical, plan.rule) == ("2403.mp4", False, "no_marker")


def test_only_story_goes_on_both_cards():
    plan = cp.plan("2_ar_2221_story.mp4", ["2_ar_2221_story.mp4"])
    assert (plan.square_file, plan.vertical_file, plan.replace_vertical, plan.rule) == (
        "2_ar_2221_story.mp4", "2_ar_2221_story.mp4", False, "story_only")


# ── decided in ACL-1426 ───────────────────────────────────────────────────

def test_pair_by_number_regardless_of_prefix():
    plan = cp.plan("4_es_2403_post.mp4", ["1_ar_2403_story.mp4"])
    assert plan.vertical_file == "1_ar_2403_story.mp4"


def test_the_same_prefix_wins_among_several_stories():
    plan = cp.plan("4_es_2403_post.mp4",
                   ["1_ar_2403_story.mp4", "4_es_2403_story.mp4", "9_de_2403_story.mp4"])
    assert plan.vertical_file == "4_es_2403_story.mp4"


def test_an_image_never_pairs_with_a_video():
    plan = cp.plan("4_es_2403_post.jpg", ["4_es_2403_story.mp4"])
    assert not plan.replace_vertical


def test_another_number_is_not_a_pair():
    plan = cp.plan("4_es_2403_post.mp4", ["4_es_24030_story.mp4", "4_es_240_story.mp4"])
    assert not plan.replace_vertical


def test_plan_for_path_reads_the_files_own_folder(tmp_path):
    for name in ("4_es_2403_post.mp4", "4_es_2403_story.mp4", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    plan = cp.plan_for_path(str(tmp_path / "4_es_2403_post.mp4"))
    assert plan.vertical_file == "4_es_2403_story.mp4"
    assert cp.plan_for_path(str(tmp_path / "missing" / "4_es_2403_post.mp4")).rule == "post_only"
