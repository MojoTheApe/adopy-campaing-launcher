# -*- coding: utf-8 -*-
"""ACL-2669: серверное слияние `keep` — черновики билдера сами не сжимаются."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.web import builder_drafts as bd  # noqa: E402


@pytest.fixture(autouse=True)
def _local_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    return tmp_path


def camp(cid=None, **kw):
    c = {"fields": {"name": cid or "legacy"}, **kw}
    if cid:
        c["id"] = cid
    return c


def form(touched, *campaigns, tracking="t"):
    return {"touched": touched, "tracking": tracking, "campaigns": list(campaigns)}


def doc(forms, removed=None, v=4):
    d = {"v": v, "forms": forms}
    if removed is not None:
        d["removed"] = removed
    return d


def ids(d, key="A"):
    return [c.get("id") for c in d["forms"][key]["campaigns"]]


def test_incoming_without_a_form_key_keeps_the_stored_forms():
    cur = doc({"A": form(2, camp("a1")), "B": form(3, camp("b1"))})
    out = bd.merge_keep(cur, doc({"A": form(2, camp("a1"))}))
    assert set(out["forms"]) == {"A", "B"}


def test_incoming_without_a_campaign_keeps_it_from_the_stored_copy():
    cur = doc({"A": form(5, camp("a1"), camp("a2"))})
    out = bd.merge_keep(cur, doc({"A": form(9, camp("a1"))}))
    assert ids(out) == ["a1", "a2"]
    assert out["forms"]["A"]["touched"] == 9


def test_newer_touched_wins_per_key():
    cur = doc({"A": form(10, camp("a1", ads=1), tracking="old"), "B": form(50, camp("b1", ads=1))})
    inc = doc({"A": form(20, camp("a1", ads=2), tracking="new"), "B": form(5, camp("b1", ads=9))})
    out = bd.merge_keep(cur, inc)
    assert out["forms"]["A"]["campaigns"][0]["ads"] == 2 and out["forms"]["A"]["tracking"] == "new"
    assert out["forms"]["B"]["campaigns"][0]["ads"] == 1


def test_older_incoming_with_a_unique_campaign_id_is_merged_in():
    cur = doc({"A": form(20, camp("a1"))})
    out = bd.merge_keep(cur, doc({"A": form(10, camp("a1"), camp("a9"))}))
    assert ids(out) == ["a1", "a9"] and out["forms"]["A"]["touched"] == 20


def test_person_removal_deletes_and_older_copy_cannot_resurrect():
    cur = doc({"A": form(20, camp("a1"))}, removed={"A": 30})
    assert "A" not in bd.merge_keep(cur, doc({}))["forms"]
    later = bd.merge_keep(cur, doc({"A": form(25, camp("a1"))}))
    assert "A" not in later["forms"] and later["removed"] == {"A": 30}


def test_a_newer_edit_after_removal_brings_the_key_back():
    cur = doc({}, removed={"A": 30})
    out = bd.merge_keep(cur, doc({"A": form(40, camp("a1"))}))
    assert ids(out) == ["a1"]


def test_tombstoned_campaign_in_an_older_copy_does_not_resurrect():
    cur = doc({"A": form(40, camp("a1"))}, removed={"A#a2": 35})
    out = bd.merge_keep(cur, doc({"A": form(30, camp("a1"), camp("a2"))}))
    assert ids(out) == ["a1"]
    # R12: надгробие на настоящем id абсолютно — и правка формы после него кампанию не возвращает
    out = bd.merge_keep(doc({"A": form(10, camp("a1"))}, removed={"A#a2": 35}),
                        doc({"A": form(36, camp("a1"), camp("a2"))}))
    assert ids(out) == ["a1"]
    # а у legacy-id надгробие сравнивается с правкой формы
    out = bd.merge_keep(doc({"A": form(10, camp("legacy-A-0"))}, removed={"A#legacy-A-1": 35}),
                        doc({"A": form(36, camp("legacy-A-0"), camp("legacy-A-1"))}))
    assert ids(out) == ["legacy-A-0", "legacy-A-1"]


def test_tombstone_on_a_real_id_is_absolute_for_a_later_stale_edit():
    # R12: устаревшая вкладка правит другую кампанию позже надгробия — X не возвращается
    out = bd.merge_keep(doc({}, removed={"A#cx": 10}), doc({"A": form(20, camp("cx"), camp("cy"))}))
    assert ids(out) == ["cy"]


def test_form_tombstone_is_not_needed_and_never_deletes_a_campaign_the_remover_did_not_see():
    # R11: другая вкладка добавила cY; удалившая видела только cX
    stored = doc({"F": form(5, camp("cy"))})
    out = bd.merge_keep(stored, doc({}, removed={"F#cx": 10}))
    assert ids(out, "F") == ["cy"]


def test_campaign_removed_after_the_newer_edit_is_dropped_from_it():
    out = bd.merge_keep(doc({"A": form(20, camp("a1"), camp("a2"))}, removed={"A#a2": 25}), doc({}))
    assert ids(out) == ["a1"]


def test_form_whose_last_campaign_is_gone_is_dropped():
    out = bd.merge_keep(doc({"A": form(20, camp("a1"))}, removed={"A#a1": 25}), doc({}))
    assert "A" not in out["forms"]


def test_legacy_campaigns_without_id_are_named_legacy_index_and_merged():
    # R9: кампании без id получают legacy-<номер> и идут как обычные
    cur = doc({"A": form(20, camp(), camp())})
    out = bd.merge_keep(cur, doc({"A": form(30, camp())}))
    assert ids(out) == ["legacy-A-0", "legacy-A-1"]  # победитель legacy-A-0, у старой копии добавилась legacy-A-1
    out = bd.merge_keep(cur, doc({"A": form(10, camp(), camp(), camp())}))
    assert ids(out) == ["legacy-A-0", "legacy-A-1", "legacy-A-2"]
    # legacy у победителя + id у старой копии
    out = bd.merge_keep(doc({"A": form(10, camp("a1"))}), doc({"A": form(30, camp())}))
    assert ids(out) == ["legacy-A-0", "a1"]


def test_stored_idless_copy_and_incoming_with_legacy_ids_give_no_duplicates():
    stored = doc({"A": form(10, camp(), camp())})
    incoming = doc({"A": form(20, camp("legacy-A-0"), camp("legacy-A-1"))})
    assert ids(bd.merge_keep(stored, incoming)) == ["legacy-A-0", "legacy-A-1"]
    # и наоборот: старая копия с legacy-id, свежая без id
    assert ids(bd.merge_keep(incoming, doc({"A": form(30, camp(), camp())}))) == ["legacy-A-0", "legacy-A-1"]


def test_v_is_max_and_removed_is_a_union_with_max_per_key():
    out = bd.merge_keep(doc({}, removed={"A": 1, "B": 9}, v=3), doc({}, removed={"A": 5, "C": 2}, v=4))
    assert out["v"] == 4 and out["removed"] == {"A": 5, "B": 9, "C": 2}


def test_save_keep_merges_on_disk_and_never_loses_a_campaign():
    bd.save("keep", 1000, doc({"A": form(10, camp("a1")), "B": form(10, camp("b1"))}))
    ok, out = bd.save("keep", 2000, doc({"A": form(20, camp("a1"), camp("a2"))}), base=1000)
    assert ok is False  # слитое != входящего: клиент применит слитый документ
    stored = bd.load()["keep"]
    assert stored == out and stored["saved_at"] == 2000
    assert set(stored["data"]["forms"]) == {"A", "B"} and ids(stored["data"]) == ["a1", "a2"]


def test_accepted_flag_is_true_only_when_merged_equals_incoming():
    ok, _ = bd.save("keep", 1000, doc({"A": form(10, camp("a1"))}))
    assert ok is True
    ok, out = bd.save("keep", 2000, doc({"A": form(20, camp("a1"))}), base=1000)
    assert ok is True and out["saved_at"] == 2000
    # старый saved_at, но то же содержимое: серверная метка новее — не «принят»
    ok, out = bd.save("keep", 1500, doc({"A": form(20, camp("a1"))}), base=2000)
    assert ok is False and out["saved_at"] == 2000


def test_older_incoming_is_merged_and_saved_at_is_the_max():
    bd.save("keep", 2000, doc({"A": form(20, camp("a1"))}))
    ok, out = bd.save("keep", 1000, doc({"A": form(10, camp("a1"), camp("a5"))}))
    assert ok is False and out["saved_at"] == 2000 and ids(out["data"]) == ["a1", "a5"]
    assert bd.load_older("keep")["saved_at"] == 1000  # проигравшая копия сохранена


def test_stored_copy_goes_to_older_only_when_the_merge_shrinks_it():
    bd.save("keep", 1000, doc({"A": form(10, camp("a1"), camp("a2"))}))
    bd.save("keep", 2000, doc({"A": form(20, camp("a1"), camp("a2"))}, removed={}), base=1000)
    assert bd.load_older("keep") is None
    bd.save("keep", 3000, doc({"A": form(30, camp("a1"))}, removed={"A#a2": 30}), base=2000)
    older = bd.load_older("keep")
    assert older and ids(older["data"]) == ["a1", "a2"]
    assert ids(bd.load()["keep"]["data"]) == ["a1"]


def test_jobs_and_ui_keep_replace_semantics():
    bd.save("jobs", 2000, {"j": 2})
    ok, out = bd.save("jobs", 1000, {"j": 1})
    assert ok is False and out["data"] == {"j": 2}
    ok, out = bd.save("ui", 5, {"open": True})
    assert ok is True
