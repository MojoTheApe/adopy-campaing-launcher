# -*- coding: utf-8 -*-
"""ACL-1668: выбранный креатив удалён после выбора — публикация называет это прямо."""
from __future__ import annotations

from types import SimpleNamespace

from src.fb_ads_rpa import enrich_draft as en
from src.fb_ads_rpa.draft import AdDraft, AdSetDraft, CampaignDraft


def _enrich(monkeypatch, tmp_path, filename):
    monkeypatch.setattr(en, "load_ad_account_map", lambda p: {})
    monkeypatch.setattr(en, "resolve_account_entry",
                        lambda *a: SimpleNamespace(ad_account_id="act_test", page_id="page", profile_id="profile"))
    draft = CampaignDraft("SA_x", "SA", "Acc",
                          ad_sets=[AdSetDraft("SA_x", ads=[AdDraft(creative_filename=filename)])])
    en.enrich_campaign_drafts([draft], account_label="Acc",
                              upload_fb_path=str(tmp_path / "none.xlsx"),
                              ad_account_map_path=str(tmp_path / "map.csv"),
                              creatives_root=str(tmp_path), preview_mode=False)
    return draft


def test_deleted_chosen_file_gives_a_clear_publish_error(monkeypatch, tmp_path):
    creative = tmp_path / "1111_old.jpg"
    creative.write_bytes(b"x")
    ok = _enrich(monkeypatch, tmp_path, "1111_old.jpg")
    assert not [e for e in ok.errors if "Creative" in e], ok.errors
    creative.unlink()
    gone = _enrich(monkeypatch, tmp_path, "1111_old.jpg")
    msgs = [e for e in gone.errors if "Creative file missing" in e]
    assert msgs and "1111_old.jpg" in msgs[0] and "pick the file again" in msgs[0], gone.errors
