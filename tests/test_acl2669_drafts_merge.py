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
    # R15: и у legacy-id надгробие абсолютно
    out = bd.merge_keep(doc({"A": form(10, camp("legacy-A-0"))}, removed={"A#legacy-A-1": 35}),
                        doc({"A": form(36, camp("legacy-A-0"), camp("legacy-A-1"))}))
    assert ids(out) == ["legacy-A-0"]


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
    # id-less у победителя + id у старой копии
    out = bd.merge_keep(doc({"A": form(10, camp("a1"))}), doc({"A": form(30, camp())}))
    assert ids(out) == ["a1"]  # R13: id без id берёт у старой копии по порядку


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


def named(name, cid=None):
    c = {"fields": {"name": name}}
    if cid:
        c["id"] = cid
    return c


def test_idless_incoming_pairs_with_stored_ids_by_content_then_by_index():
    # R13: страница старой версии шлёт кампании без id — дублей быть не должно
    stored = doc({"F": form(10, named("A", "c1"), named("B", "c2"))})
    out = bd.merge_keep(stored, doc({"F": form(20, named("A"), named("B2"))}))
    camps = out["forms"]["F"]["campaigns"]
    assert [c["id"] for c in camps] == ["c1", "c2"]
    assert [c["fields"]["name"] for c in camps] == ["A", "B2"]


def test_idless_incoming_with_fewer_campaigns_never_shrinks_the_stored_copy():
    stored = doc({"F": form(10, named("A", "c1"), named("B", "c2"))})
    out = bd.merge_keep(stored, doc({"F": form(20, named("A"))}))
    assert ids(out, "F") == ["c1", "c2"]


def test_content_pairing_beats_index_and_each_id_is_used_once():
    stored = doc({"F": form(10, named("A", "c1"), named("B", "c2"))})
    out = bd.merge_keep(stored, doc({"F": form(20, named("B"), named("B"), named("Z"))}))
    assert ids(out, "F")[0] == "c2"
    assert len(set(ids(out, "F"))) == 3  # c2, c1 по порядку, и legacy для третьей


def test_bare_legacy_number_is_rewritten_with_the_form_key():
    out = bd.merge_keep(doc({}), doc({"F": form(5, camp("legacy-0"), camp("legacy-1"))}))
    assert ids(out, "F") == ["legacy-F-0", "legacy-F-1"]


def names(d, key="F"):
    return [c["fields"]["name"] for c in d["forms"][key]["campaigns"]]


def test_legacy_ids_and_tombstoned_ids_are_never_handed_to_an_idless_campaign():
    # R14, случай 1: у сохранённой копии id legacy-F-1, номер 0 снят человеком
    stored = doc({"F": form(20, named("A", "legacy-F-1"))}, removed={"F#legacy-F-0": 10})
    out = bd.merge_keep(stored, doc({"F": form(30, named("A"), named("B"))}))
    assert len(set(ids(out, "F"))) == len(ids(out, "F")) and sorted(set(names(out))) == ["A", "B"]
    # следом сохраняется новая страница, видевшая только A под legacy-F-1: B не пропадает
    out = bd.merge_keep(out, doc({"F": form(40, named("A", "legacy-F-1"))}))
    assert "B" in names(out)


def test_tombstoned_real_id_is_not_given_to_a_new_idless_campaign():
    # R14, случай 2: c2 снят, а Z без id не должен его получить и пропасть
    stored = doc({"F": form(10, named("A", "c1"), named("B", "c2"))}, removed={"F#c2": 15})
    out = bd.merge_keep(stored, doc({"F": form(20, named("Q"), named("Z"))}))
    assert sorted(names(out)) == ["Q", "Z"]
    assert "c2" not in ids(out, "F")


def test_r15_legacy_tombstone_is_absolute_and_adoption_does_not_duplicate():
    # три воспроизведения ревью: ни одной кампании дважды, ни одного убранного id
    out = bd.merge_keep(doc({"F": form(20, named("A", "legacy-F-0"), named("B", "legacy-F-1"))},
                            removed={"F#legacy-F-0": 10}),
                        doc({"F": form(30, named("A"), named("B"))}))
    assert sorted(names(out)) == ["A", "B"] and "legacy-F-0" not in ids(out, "F")
    out = bd.merge_keep(doc({"F": form(20, named("B", "legacy-F-1"))}, removed={"F#legacy-F-1": 5}),
                        doc({"F": form(20, named("B"))}))
    assert names(out) == ["B"] and "legacy-F-1" not in ids(out, "F")
    out = bd.merge_keep(doc({"F": form(20, named("A", "legacy-F-1"))}, removed={"F#legacy-F-1": 10}),
                        doc({"F": form(30, named("A"), named("B"))}))
    assert sorted(names(out)) == ["A", "B"] and "legacy-F-1" not in ids(out, "F")


def test_fallback_legacy_id_skips_tombstoned_numbers():
    out = bd.merge_keep(doc({"F": form(10, named("F", "legacy-F-2"))}, removed={"F#legacy-F-0": 15}),
                        doc({"F": form(10, named("E"))}))
    assert sorted(names(out)) == ["E", "F"]
    assert "legacy-F-0" not in ids(out, "F")


def test_bare_legacy_id_next_to_an_idless_campaign_does_not_collide():
    out = bd.merge_keep(doc({}), doc({"F": form(5, named("X"), named("Y", "legacy-0"))}))
    assert len(set(ids(out, "F"))) == 2 and sorted(names(out)) == ["X", "Y"]


def test_stored_idless_copy_is_numbered_by_index_and_the_incoming_one_pairs_with_it():
    # R16: стоящая на сервере копия без id — доверенная, номера по порядку; страница старой версии подбирает id
    out = bd.merge_keep(doc({"F": form(10, named("E"), named("B"))}), doc({"F": form(20, named("A"), named("B"))}))
    assert sorted(names(out)) == ["A", "B"] and len(set(ids(out, "F"))) == 2


def test_removal_from_a_page_that_restored_a_stored_idless_copy_keeps_the_campaign_removed():
    # R16, S1-S3: на сервере [X,Y,Z] без id, страница восстановила их как legacy-F-0..2 и первым постом шлёт снятие
    for gone, left in ((0, (("Y", 1), ("Z", 2))), (1, (("X", 0), ("Z", 2))), (2, (("X", 0), ("Y", 1)))):
        stored = doc({"F": form(10, named("X"), named("Y"), named("Z"))})
        incoming = doc({"F": form(20, *[named(n, f"legacy-F-{i}") for n, i in left])},
                       removed={f"F#legacy-F-{gone}": 15})
        out = bd.merge_keep(stored, incoming)
        assert sorted(names(out)) == sorted(n for n, _ in left), gone
        assert len(set(ids(out, "F"))) == 2


def test_tombstone_of_a_bare_legacy_id_applies_to_the_normalised_id():
    out = bd.merge_keep(doc({"F": form(10, named("C", "legacy-1"))}, removed={"F#legacy-1": 5}), doc({"F": form(20, named("C"))}))
    assert names(out) == ["C"] and "legacy-F-1" not in ids(out, "F")
