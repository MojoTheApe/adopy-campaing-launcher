"""ACL-2073: отказ Crop одного теста не достаётся следующему.

21.10 `test_bench_b97_picker_and_crop_1007.py` оставлял `_tls.failure` в
главном потоке воркера, и `test_media_attachment_identity.py`, попав с ним в
один шард CI, получал `dirty` вместо `attached`. Тесты ниже идут по порядку в
одном файле (один воркер при `--dist loadfile`): первый оставляет состояние,
как оставлял тот тест, второй проверяет, что фикстура `tests/conftest.py`
его сбросила.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.fb_ads_rpa import crop_step
from src.fb_ads_rpa import meta_dom as md


def test_a_test_leaves_a_crop_failure_and_a_media_refusal_behind(monkeypatch):
    empty = {"found": True, "shared": [], "cards": {n: None for n in crop_step.CARDS}}
    monkeypatch.setattr(crop_step, "read", lambda page, mark=None: empty)
    monkeypatch.setattr(crop_step, "CARDS_RENDER_WAIT_S", 0.05)
    monkeypatch.setattr(crop_step.time, "sleep", lambda s: None)
    assert crop_step.apply(object(), None) == "crop_square_card_missing"
    md._note_media_refused("other_file_attached")
    md._reuse_state["counter_missing"] = True
    assert crop_step.failure_pending()


def test_the_next_test_starts_clean():
    assert not crop_step.failure_pending()
    assert crop_step.outcome() == ""
    assert crop_step.current_plan() is None
    assert md.media_refused_on_ad() is None
    assert md._reuse_state["counter_missing"] is False
