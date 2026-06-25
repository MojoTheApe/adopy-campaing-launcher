import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.meta_campaign_agent.ad_account_map import AccountMapEntry, load_ad_account_map, resolve_account_entry
from src.meta_campaign_agent.campaign_builder import PublishJob, build_publish_job
from src.meta_campaign_agent.creative_finder import AmbiguousCreativeError, find_creative_file
from src.meta_campaign_agent.excel_reader import UploadRow
from src.meta_campaign_agent.meta_publisher import MetaPublisher
from src.meta_campaign_agent.profile_proxy import ProfileProxy


def test_load_ad_account_map(tmp_path: Path):
    csv_path = tmp_path / "map.csv"
    csv_path.write_text(
        "account_name,ad_account_id,page_id\nFacebookUL574 EC Fav,1234567890,999\n",
        encoding="utf-8",
    )
    m = load_ad_account_map(csv_path)
    assert m["FacebookUL574 EC Fav"].ad_account_id == "act_1234567890"
    assert m["FacebookUL574 EC Fav"].page_id == "999"


def test_resolve_account_entry_partial():
    m = {"FacebookUL574 EC Fav": AccountMapEntry(ad_account_id="act_111", page_id="p1")}
    assert resolve_account_entry(m, "EC Fav").ad_account_id == "act_111"


def test_find_creative_file_nested():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        sub = root / "MX" / "2026"
        sub.mkdir(parents=True)
        f = sub / "test_creative.jpeg"
        f.write_bytes(b"x")
        found = find_creative_file(root, "test_creative.jpeg")
        assert found == f


def test_find_creative_ambiguous():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "a").mkdir()
        (root / "b").mkdir()
        (root / "a" / "dup.mp4").write_bytes(b"1")
        (root / "b" / "dup.mp4").write_bytes(b"2")
        with pytest.raises(AmbiguousCreativeError):
            find_creative_file(root, "dup.mp4")


def test_profile_proxy_to_requests():
    p = ProfileProxy.from_csv_fields(
        proxy_type="http",
        host="1.2.3.4",
        port="8080",
        username="u",
        password="p",
    )
    proxies = p.to_requests_proxies()
    assert proxies["http"] == "http://u:p@1.2.3.4:8080"
    assert proxies["https"] == "http://u:p@1.2.3.4:8080"


def test_meta_publisher_success_chain():
    client = MagicMock()
    client.create_campaign.return_value = "camp_1"
    client.create_ad_set.return_value = "adset_1"
    client.create_ad_creative.return_value = "cr_1"
    client.create_ad.return_value = "ad_1"

    uploader = MagicMock()
    uploader.upload_image.return_value = "hash_abc"

    row = UploadRow(
        sheet_account="Acc",
        row_number=2,
        cells={
            "Campaign Name": "EC_Fav_Test",
            "Creative": "Image",
            "Image File name": "x.jpeg",
            "Link": "https://example.com",
            "Pixel": "123",
            "Campaign Objective": "OUTCOME_LEADS",
            "Campaign Daily Budget": "50",
            "Countries": "EC",
            "Age Min": "35",
            "Age Max": "35",
        },
    )
    job = build_publish_job(
        row,
        ad_account_id="act_1",
        creative_path="/tmp/x.jpeg",
        page_id="page_1",
    )
    assert not job.errors

    pub = MetaPublisher(client, uploader, log_path="output/test_publish_log.jsonl")
    result = pub.publish_job(job)
    assert result.success
    assert result.campaign_id == "camp_1"
    assert result.ad_id == "ad_1"
    uploader.upload_image.assert_called_once()
