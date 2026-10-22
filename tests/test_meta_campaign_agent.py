import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.meta_campaign_agent.ad_account_map import AccountMapEntry, load_ad_account_map, resolve_account_entry
from src.meta_campaign_agent.campaign_builder import PublishJob, build_publish_job
from src.meta_campaign_agent.creative_finder import AmbiguousCreativeError, CreativeNotFoundError, find_creative_file
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


def test_find_creative_file_rejects_a_symlink_that_escapes_the_root():
    """ACL-1342 S02: `is_file()` идёт по ссылке молча — ссылка может лежать
    внутри корня и указывать на файл снаружи (например, системный файл)."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "creatives"
        outside = Path(d) / "outside"
        root.mkdir()
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("not a creative", encoding="utf-8")

        link = root / "escape.jpg"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            pytest.skip("нет прав на создание symlink в этом окружении")

        with pytest.raises(CreativeNotFoundError):
            find_creative_file(root, "escape.jpg")


def test_find_creative_file_still_finds_a_symlink_that_stays_inside_the_root():
    """Фикс не должен отказывать ссылкам, которые целят внутрь того же
    корня — только тем, что указывают наружу."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        real = root / "sub" / "real.jpg"
        real.parent.mkdir()
        real.write_bytes(b"x")

        link = root / "alias.jpg"
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            pytest.skip("нет прав на создание symlink в этом окружении")

        found = find_creative_file(root, "alias.jpg")
        assert found == link


def test_the_realpath_escape_check_itself_does_not_need_symlink_privilege(monkeypatch):
    """`os.symlink` требует привилегию, которой это окружение может не
    выдавать (уже видно по скипам выше) — сама проверка эскейпа устроена
    так, что её можно проверить и без реальной ссылки: достаточно
    подменить `os.path.realpath`, ровно то, что функция и вызывает для
    каждого совпадения."""
    from src.meta_campaign_agent import creative_finder as cf

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "escape.jpg").write_bytes(b"x")
        outside_real = str(Path(d).parent / "somewhere_outside" / "secret.jpg")

        real_realpath = os.path.realpath

        def fake_realpath(p):
            if os.fspath(p).endswith("escape.jpg"):
                return outside_real
            return real_realpath(p)

        monkeypatch.setattr(cf.os.path, "realpath", fake_realpath)

        with pytest.raises(CreativeNotFoundError):
            find_creative_file(root, "escape.jpg")


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


def test_meta_publisher_success_chain(tmp_path: Path):
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

    pub = MetaPublisher(client, uploader, log_path=str(tmp_path / "test_publish_log.jsonl"))
    result = pub.publish_job(job)
    assert result.success
    assert result.campaign_id == "camp_1"
    assert result.ad_id == "ad_1"
    uploader.upload_image.assert_called_once()


# --- ACL-1783: версия Graph/Marketing API по умолчанию ---

def test_acl1783_default_graph_version_is_v26(monkeypatch):
    from src.meta_campaign_agent.meta_client import (
        DEFAULT_GRAPH_VERSION, MetaClient, MetaClientConfig, meta_client_from_env)
    assert DEFAULT_GRAPH_VERSION == "v26.0"
    assert MetaClient(MetaClientConfig(access_token="t")).base_url == "https://graph.facebook.com/v26.0"
    assert MetaClient(MetaClientConfig(access_token="t", graph_version=" ")).base_url.endswith("/v26.0")
    monkeypatch.setenv("META_ACCESS_TOKEN", "t")
    monkeypatch.delenv("META_GRAPH_API_VERSION", raising=False)
    assert meta_client_from_env().base_url.endswith("/v26.0")
    monkeypatch.setenv("META_GRAPH_API_VERSION", "v25.0")  # older than meta.json: ignored
    assert meta_client_from_env().base_url.endswith("/v26.0")
    monkeypatch.setenv("META_GRAPH_API_VERSION", "v22.0")  # the stale pin: ignored
    assert meta_client_from_env().base_url.endswith("/v26.0")
    monkeypatch.setenv("META_GRAPH_API_VERSION", "v27.0")  # explicitly newer: wins
    assert meta_client_from_env().base_url.endswith("/v27.0")


def test_acl1783_payloads_have_no_v26_removed_fields():
    """Поля, убранные/изменённые в v24-v26 (placements, poll, advantage_audience), мы не шлём."""
    import inspect
    from src.meta_campaign_agent import meta_client
    src = inspect.getsource(meta_client)
    for bad in ("messenger_positions", "instagram_positions", "poll_spec", "applink_treatment"):
        assert bad not in src
    sent = []

    class C(meta_client.MetaClient):
        def post_form(self, path, data=None):
            sent.append((path, data)); return {"id": "1"}
    c = C(meta_client.MetaClientConfig(access_token="t"))
    c.create_campaign("act_1", name="n", objective="OUTCOME_LEADS", status="PAUSED")
    assert sent[0][1]["is_adset_budget_sharing_enabled"] == "false"
    assert sent[0][1]["special_ad_categories"] == "[]"
