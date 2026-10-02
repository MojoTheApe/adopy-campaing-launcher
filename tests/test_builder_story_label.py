# -*- coding: utf-8 -*-
"""The Builder shows what goes on the Vertical (story) card of each ad (ACL-1426).

`/api/creatives` carries `story_label` computed by the robot's own pairing
(`creative_pairing.plan`, siblings of the same folder); the preview card prints it.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TPL = ROOT / "src" / "web" / "templates" / "report.html"


def _files(tmp_path, monkeypatch) -> dict:
    from src.web import app as webapp

    monkeypatch.setattr(webapp, "get_creatives_root", lambda: str(tmp_path))
    body = asyncio.run(webapp.api_creatives()).body
    return {f["rel_path"]: f for f in json.loads(body)["files"]}


def test_story_labels_follow_the_owner_table(tmp_path, monkeypatch):
    (tmp_path / "es").mkdir()
    for name in ("es/4_es_2403_post.mp4", "es/4_es_2403_story.mp4", "es/2_es_2221_post.mp4",
                 "2_ar_2403_story.mp4", "1962.jpg"):
        (tmp_path / name).write_bytes(b"x")
    files = _files(tmp_path, monkeypatch)
    assert files["es/4_es_2403_post.mp4"]["story_label"] == "Story: 4_es_2403_story.mp4"
    assert files["es/2_es_2221_post.mp4"]["story_label"] == "Story: same file, Original"
    # Another folder is not a sibling: the story alone goes on both cards.
    assert files["2_ar_2403_story.mp4"]["story_label"] == "Story: same file, Original"
    # A story chosen in the Builder: the post of the pair goes on Square.
    assert files["es/4_es_2403_story.mp4"]["square_file"] == "4_es_2403_post.mp4"
    assert files["1962.jpg"]["story_label"] == "Story: same file, Original"


def test_the_preview_card_prints_the_label():
    text = TPL.read_text(encoding="utf-8")
    assert "cr.story_label" in text and "data-cbp-story" in text
