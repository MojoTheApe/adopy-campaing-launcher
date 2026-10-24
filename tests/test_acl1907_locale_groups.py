"""ACL-1907: группа «Spanish (All)» (1002) и локали, на которые её раскрыла Meta ([7, 23]), - одно и то же.

Числа живого прогона Doc120 14.10: readback ждал [1002], Meta вернула [7, 23]; повтор того же
задания останавливался на «languages (builder: 1002, Meta: 7, 23)». Состав группы берётся из поиска adlocale.
"""
from __future__ import annotations

from copy import deepcopy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.test_acl1761_api_create import FakeMeta, _draft, _run  # noqa: E402


class LocaleMeta(FakeMeta):
    """Поиск adlocale как у Meta: группа 1002 и её члены 7 и 23; Meta хранит и отдаёт [7, 23]."""

    def __init__(self, rows=None, **kw):
        super().__init__(**kw)
        self.rows = rows if rows is not None else [
            {"key": 1002, "name": "Spanish (All)"}, {"key": 7, "name": "Spanish (Latin America)"},
            {"key": 23, "name": "Spanish (Spain)"}, {"key": 6, "name": "English (All)"}]

    def _get(self, path, params):
        if path == "/search" and params.get("type") == "adlocale":
            return {"data": self.rows}
        return super()._get(path, params)


def _expand(meta, locales=(7, 23)):
    meta.mutate_on_read[("adset", "targeting.locales")] = list(locales)


def test_group_equals_its_expansion_in_readback(tmp_path):
    meta = LocaleMeta()
    _expand(meta)
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "", out.error
    assert out.readback["ok"] is True, out.mismatches
    lang = next(c for c in out.readback["checks"] if c["field"] == "languages")
    assert lang["expected"] == [1002] and lang["actual"] == [7, 23] and lang["ok"]


def test_a_different_language_is_still_a_mismatch(tmp_path):
    meta = LocaleMeta()
    _expand(meta, (6,))
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert [(m["field"], m["expected"], m["actual"]) for m in out.mismatches] == [("languages", [1002], [6])]


def test_an_expansion_plus_a_foreign_locale_is_a_mismatch(tmp_path):
    meta = LocaleMeta()
    _expand(meta, (7, 23, 6))
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert [m["field"] for m in out.mismatches] == ["languages"]


def test_an_incomplete_known_expansion_is_a_readback_mismatch(tmp_path):
    meta = LocaleMeta()
    _expand(meta, (7,))
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert [(m["field"], m["expected"], m["actual"]) for m in out.mismatches] == [
        ("languages", [1002], [7])]
    lang = next(c for c in out.readback["checks"] if c["field"] == "languages")
    assert lang["ok"] is False and not lang.get("expanded")


def test_rerun_does_not_reuse_an_incomplete_known_expansion(tmp_path):
    meta = LocaleMeta()
    _run(meta, _draft(tmp_path, ads=1))
    next(o for o in meta.objects.values() if o["_kind"] == "adset")["targeting"]["locales"] = [7]
    original = deepcopy({k: o for k, o in meta.objects.items()
                         if o["_kind"] in ("campaign", "adset")})
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.error == "" and any("Published as" in w and "_2" in w for w in out.warnings)
    assert any("languages (builder: 1002, Meta: 7)" in w for w in out.warnings)
    assert {k: meta.objects[k] for k in original} == original
    assert sum(o["_kind"] == "campaign" for o in meta.objects.values()) == 2
    assert sum(o["_kind"] == "adset" for o in meta.objects.values()) == 2


def test_rerun_reuses_objects_when_meta_expanded_the_group(tmp_path):
    meta = LocaleMeta()
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "", out.error
    adset = next(o for o in meta.objects.values() if o["_kind"] == "adset")
    adset["targeting"]["locales"] = [7, 23]
    made = {k for k, o in meta.objects.items() if o["_kind"] in ("campaign", "adset")}
    out2, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out2.error == "", out2.error
    assert not any("Published as" in w for w in out2.warnings)
    assert {k for k, o in meta.objects.items() if o["_kind"] in ("campaign", "adset")} == made


def test_rerun_still_stops_on_a_genuinely_different_language(tmp_path):
    meta = LocaleMeta()
    _run(meta, _draft(tmp_path, ads=1))
    next(o for o in meta.objects.values() if o["_kind"] == "adset")["targeting"]["locales"] = [6]
    before = len(meta.posts)
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    # ACL-1902: a campaign that differs is left as is and the run publishes under a new name.
    assert out.error == "" and any("Published as" in w and "_2" in w for w in out.warnings)
    assert any("languages (builder: 1002, Meta: 6)" in w for w in out.warnings)
    assert len(meta.posts) > before


def test_unknown_members_keep_strict_compare_and_say_so(tmp_path):
    meta = LocaleMeta(rows=[{"key": 1002, "name": "Spanish (All)"}])
    _run(meta, _draft(tmp_path, ads=1))
    next(o for o in meta.objects.values() if o["_kind"] == "adset")["targeting"]["locales"] = [7, 23]
    out, _ = _run(meta, _draft(tmp_path, ads=2))
    assert out.error == "" and any("Published as" in w and "_2" in w for w in out.warnings)
    text = " ".join(out.warnings)
    assert "languages (builder: 1002, Meta: 7, 23)" in text
    assert "compared strictly" in text and "did not list the locales" in text
