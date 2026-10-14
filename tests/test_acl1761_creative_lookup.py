"""ACL-1761: поиск своего креатива без `filtering` по имени (живой 5414, job 2fb409d1).

Meta ответил на `/act_X/adcreatives?filtering=[name CONTAIN …]` ошибкой #100.
Креатив ищется постраничным списком кабинета (не больше
`CREATIVE_LOOKUP_PAGES` страниц) по точному имени и нашей метке; повторный
запуск после такого стопа берёт готовые кампанию и группу и доделывает
креативы и объявления. Поддельная Meta здесь, как живая, отказывает фильтру.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import creator as cr  # noqa: E402
from tests.test_acl1761_api_create import FakeMeta, _draft, _run  # noqa: E402


# ── живой 5414 (job 2fb409d1): /adcreatives не принимает filtering по name ──

def test_creatives_are_never_looked_up_with_a_name_filter(tmp_path):
    meta = FakeMeta()
    out, _ = _run(meta, _draft(tmp_path, video=True, ads=3))
    assert out.error == "" and out.readback["ok"] is True
    creative_gets = [q for p, q in meta.gets if p.endswith("/adcreatives")]
    assert creative_gets and all("filtering" not in q for q in creative_gets)
    assert len(creative_gets) == 1                       # список кабинета — раз на кампанию


def test_rerun_after_the_live_stop_at_the_creative_reuses_campaign_and_ad_set(tmp_path):
    meta = FakeMeta()
    meta.fail_next["adcreatives"] = "unsent"             # как 2fb409d1: кампания, группа, картинка — и стоп
    first, _ = _run(meta, _draft(tmp_path, ads=2))
    assert first.stop_kind == "unknown"
    assert [p.rsplit("/", 1)[1] for p, _ in meta.posts] == ["campaigns", "adsets", "adimages", "adcreatives"]
    second, client = _run(meta, _draft(tmp_path, ads=2))
    assert second.error == "" and second.readback["ok"] is True, (second.error, second.mismatches)
    kinds = [o["_kind"] for o in meta.objects.values()]
    assert kinds.count("campaign") == 1 and kinds.count("adset") == 1
    assert kinds.count("creative") == 2 and kinds.count("ad") == 2
    assert second.reused.get("campaign") and second.reused.get("adsets")
    # картинка первого объявления не загружалась второй раз
    assert len([p for p, _ in meta.posts if p.endswith("/adimages")]) == 2


def test_a_creative_left_without_its_ad_is_reused_not_duplicated(tmp_path):
    meta = FakeMeta()
    meta.fail_next["ads"] = "unsent"
    first, _ = _run(meta, _draft(tmp_path, ads=1))
    assert first.stop_kind == "unknown"
    second, _ = _run(meta, _draft(tmp_path, ads=1))
    assert second.error == "" and second.readback["ok"] is True
    kinds = [o["_kind"] for o in meta.objects.values()]
    assert kinds.count("creative") == 1 and kinds.count("ad") == 1
    assert any("creative found from a previous run" in s for s in second.steps)


def test_creative_lookup_reads_at_most_the_page_cap(tmp_path):
    meta = FakeMeta()
    for i in range(500):
        meta._new("creative", name=f"someone else's {i}")
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == ""
    assert len([p for p, _ in meta.gets if p.endswith("/adcreatives")]) == cr.CREATIVE_LOOKUP_PAGES


def test_a_same_name_creative_without_our_tag_is_not_reused(tmp_path):
    meta = FakeMeta()
    d = _draft(tmp_path, ads=1)
    plan = cr.plan_from_draft(d)
    ad = plan.ad_sets[0].ads[0]
    foreign = meta._new("creative", name=f"{ad.name} #{ad.index} [{plan.label}]",
                        object_story_spec={"page_id": "1"})
    out, _ = _run(meta, d)
    assert out.error == "" and out.readback["ok"] is True
    assert out.ad_media[1]["creative_id"] != foreign
    assert len([o for o in meta.objects.values() if o["_kind"] == "creative"]) == 2


def test_an_unknown_creative_write_re_reads_the_list_not_the_cache(tmp_path):
    meta = FakeMeta()
    meta.fail_next["adcreatives"] = "lost"               # создан, ответ потерян
    out, _ = _run(meta, _draft(tmp_path, ads=1))
    assert out.error == "" and out.readback["ok"] is True
    gets = [p for p, _ in meta.gets if p.endswith("/adcreatives")]
    assert len(gets) == 2                                 # до записи и после неё — заново
    assert len([o for o in meta.objects.values() if o["_kind"] == "creative"]) == 1
    assert any("reconciled" in s for s in out.steps)
