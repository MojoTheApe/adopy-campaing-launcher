"""Post / Story pair of a creative: which file goes on which Crop card (ACL-1426).

Owner rules, 2026-10-01 (Issue #426):

* Creative files are named like `4_es_2403_post.mp4`, `2_ar_2221_story.mp4`,
  `1_eslat_2243_square.mp4`. The **creative number** (`2403`) identifies a pair;
  `_square` counts as `_post`.
* Files available for the number → cards of Meta's Crop step:

  =========================================  ===================  ======================
  available                                  Square card (post)   Vertical card (story)
  =========================================  ===================  ======================
  a post/square file and a story file        the post, Original   Replace → story, Original
  only a post/square file, or no marker      that file, Original  the same file, Original
  only a story file                          that file, Original  the same file, Original
  =========================================  ===================  ======================

* "Crop and expand (Meta AI)" is off on both cards, images and videos alike.

Decided here (documented in the Issue): the pair is looked up **by number only,
regardless of the prefix** (`4_es_` / `1_ar_`), among files of the same media
kind (video with video, image with image) in the same folder. When several
siblings qualify, the one with the same prefix wins, then the shortest name, then
alphabetical order — deterministic, and the Builder shows which one was taken.

Pure functions, no I/O except `plan_for_path`, which lists one folder.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".avi", ".webm", ".mkv", ".gif"})
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})

POST = "post"
STORY = "story"
NONE = ""

_MARKERS = {"post": POST, "square": POST, "story": STORY}
_SPLIT = re.compile(r"[_\-\s.]+")
_NUMBER = re.compile(r"^\d{3,}$")


@dataclass(frozen=True)
class CreativeName:
    """What a file name says: creative number, post/story marker, prefix, kind."""

    name: str
    number: str
    marker: str
    prefix: str
    kind: str          # "video" | "image" | ""


@dataclass(frozen=True)
class CropPlan:
    """Which file goes on which card of Meta's Crop step."""

    square_file: str           # file name on the Square card (the ad's media)
    vertical_file: str         # file name on the Vertical card
    replace_vertical: bool     # Vertical gets another file through Replace
    rule: str                  # "pair" | "post_only" | "no_marker" | "story_only" | "unknown"
    number: str = ""

    def builder_label(self) -> str:
        """The Builder line next to the ad."""
        if self.replace_vertical:
            return f"Story: {self.vertical_file}"
        return "Story: same file, Original"


def media_kind(name: str) -> str:
    ext = Path(name or "").suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return ""


def parse(name: str) -> CreativeName:
    """`4_es_2403_post.mp4` → number 2403, marker post, prefix `4_es`."""
    base = Path(name or "").name
    stem = Path(base).stem
    tokens = [t for t in _SPLIT.split(stem) if t]
    marker = NONE
    if tokens and tokens[-1].lower() in _MARKERS:
        marker = _MARKERS[tokens[-1].lower()]
    number, prefix = "", ""
    for i, tok in enumerate(tokens):
        if _NUMBER.match(tok):
            number = tok          # the last 3+-digit token is the creative number
            prefix = "_".join(tokens[:i]).lower()
    return CreativeName(base, number, marker, prefix, media_kind(base))


def _rank(chosen: CreativeName, other: CreativeName) -> tuple:
    return (other.prefix != chosen.prefix, len(other.name), other.name.lower())


def plan(chosen_name: str, available: Iterable[str]) -> CropPlan:
    """Crop plan for the Builder's file `chosen_name`, given the folder's files."""
    me = parse(chosen_name)
    if not me.number:
        return CropPlan(me.name, me.name, False, "unknown")
    siblings = [parse(n) for n in available]
    siblings = [s for s in siblings
                if s.number == me.number and s.kind == me.kind and s.name != me.name]
    posts = sorted((s for s in siblings if s.marker == POST), key=lambda s: _rank(me, s))
    stories = sorted((s for s in siblings if s.marker == STORY), key=lambda s: _rank(me, s))
    if me.marker == POST:
        if stories:
            return CropPlan(me.name, stories[0].name, True, "pair", me.number)
        return CropPlan(me.name, me.name, False, "post_only", me.number)
    if me.marker == STORY:
        if posts:
            # The table decides by the files available for the number: a pair
            # means the post on Square and the story on Vertical.
            return CropPlan(posts[0].name, me.name, True, "pair", me.number)
        return CropPlan(me.name, me.name, False, "story_only", me.number)
    return CropPlan(me.name, me.name, False, "no_marker", me.number)


def plan_for_path(creative_path: str) -> CropPlan:
    """`plan` for a file on disk: siblings are the files in its own folder."""
    p = Path(creative_path or "")
    try:
        names = [n for n in os.listdir(p.parent) if (p.parent / n).is_file()]
    except OSError:
        names = []
    return plan(p.name, names)
