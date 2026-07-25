"""POST /account-map-patch: insert-new and update-existing paths.

Uses a temp CSV (via get_ad_account_map_path patch) so tests never touch the
real config/ad_account_map.csv.
"""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path
from unittest.mock import patch

from src.web.app import account_map_patch


def _patch_request(body: dict) -> str:
    import json

    body_bytes = json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/account-map-patch",
        "headers": [(b"content-type", b"application/json")],
    }

    async def _receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def _run():
        from starlette.requests import Request

        request = Request(scope, receive=_receive)
        response = await account_map_patch(request)
        return response.body.decode()

    return asyncio.run(_run())


def _read_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_insert_new_account(tmp_path) -> None:
    csv_path = tmp_path / "ad_account_map.csv"
    with patch("src.web.app.get_ad_account_map_path", return_value=str(csv_path)):
        result = _patch_request({"account_name": "NewAcc123", "country": "kw"})

    assert '"ok": true' in result or '"ok":true' in result.replace(" ", "")
    rows = _read_rows(csv_path)
    assert len(rows) == 1
    assert rows[0]["account_name"] == "NewAcc123"
    assert rows[0]["country"] == "KW"  # uppercased


def test_update_existing_account_preserves_other_fields(tmp_path) -> None:
    csv_path = tmp_path / "ad_account_map.csv"
    fieldnames = [
        "account_name", "ad_account_id", "page_id", "multilogin_instance",
        "folder_id", "profile_id", "proxy_type", "proxy_host", "proxy_port",
        "proxy_user", "proxy_pass", "country",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "account_name": "ExistingAcc", "ad_account_id": "act_111",
            "page_id": "", "multilogin_instance": "A", "folder_id": "folder-xyz",
            "profile_id": "", "proxy_type": "", "proxy_host": "", "proxy_port": "",
            "proxy_user": "", "proxy_pass": "", "country": "MX",
        })

    with patch("src.web.app.get_ad_account_map_path", return_value=str(csv_path)):
        _patch_request({"account_name": "ExistingAcc", "profile_id": "new-profile-id"})

    rows = _read_rows(csv_path)
    assert len(rows) == 1  # updated in place, not duplicated
    row = rows[0]
    assert row["profile_id"] == "new-profile-id"
    assert row["ad_account_id"] == "act_111"  # untouched fields preserved
    assert row["folder_id"] == "folder-xyz"
    assert row["country"] == "MX"


def test_missing_account_name_is_rejected(tmp_path) -> None:
    csv_path = tmp_path / "ad_account_map.csv"
    with patch("src.web.app.get_ad_account_map_path", return_value=str(csv_path)):
        result = _patch_request({"profile_id": "abc"})

    assert "account_name is required" in result
    assert not csv_path.exists()
