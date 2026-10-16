"""Real CurrentUser DPAPI evidence, using only synthetic temporary secrets."""
import json
import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from config import paths, protected_secrets as vault

pytestmark = pytest.mark.windows_only(reason="Windows CurrentUser DPAPI encryption and integrity checks")


def test_current_user_roundtrip_owner_revision_and_corrupt_blob(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    secret = "synthetic-dpapi-credential-1872"
    reference = vault.put("synthetic-connection", 1, {"access_token": secret})
    source = tmp_path / "protected-secrets" / (reference + ".bin")
    ciphertext = source.read_bytes()
    assert secret.encode() not in ciphertext
    assert vault.get(reference, "synthetic-connection", 1) == {"access_token": secret}
    for owner, revision in (("other-connection", 1), ("synthetic-connection", 2)):
        with pytest.raises(vault.SecretUnavailable):
            vault.get(reference, owner, revision)
        with pytest.raises(vault.SecretUnavailable):
            vault.delete(reference, owner, revision)
    source.write_bytes(ciphertext[:10])
    with pytest.raises(vault.SecretUnavailable):
        vault.get(reference, "synthetic-connection", 1)
    source.write_bytes(ciphertext)
    vault.delete(reference, "synthetic-connection", 1)
    assert not source.exists()


def test_valid_dpapi_ciphertext_with_invalid_envelope_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    reference = vault.put("owner", 1, {"token": "synthetic-value"})
    source = tmp_path / "protected-secrets" / (reference + ".bin")
    paths._atomic_write(source, vault._protect(json.dumps({"schema": 999, "owner": "owner", "revision": 1, "values": {"token": "synthetic-value"}}).encode()))
    with pytest.raises(vault.SecretUnavailable):
        vault.get(reference, "owner", 1)


def test_real_public_saved_credential_survives_a_fresh_application_process(tmp_path, monkeypatch):
    import httpx
    from src.web import app as web
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    mapping = tmp_path / "ad_account_map.csv"
    paths.write_csv_atomic(mapping, ["account_name", "profile_id", "ad_account_id"], [{
        "account_name": "Synthetic", "profile_id": str(uuid.UUID(int=3, version=4)), "ad_account_id": "act_555555555"}])
    monkeypatch.setenv("META_AD_ACCOUNT_MAP_PATH", str(mapping))
    async def prepare():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://127.0.0.1") as client:
            response = await client.post("/meta-connections/begin", json={"account_name": "Synthetic"})
            state = response.json()["connection"]
            response = await client.post(f"/meta-connections/{state['connection_id']}/credential", json={
                "app_id": "300000003", "access_token": "synthetic-process-dpapi-secret", "expected_revision": state["revision"], "request_id": "real-dpapi-save-0001"})
            assert response.status_code == 200
            return response.json()["connection"]
    saved = asyncio.run(prepare())
    assert saved["has_credential"]
    code = '''
import asyncio, httpx, json, sys
from src.web.app import app
async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://127.0.0.1") as client:
        response=await client.get("/meta-connections/"+sys.argv[1])
        print(json.dumps(response.json()["connection"]))
asyncio.run(run())
'''
    result = subprocess.run([sys.executable, "-c", code, saved["connection_id"]], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == saved
    assert "synthetic-process-dpapi-secret" not in result.stdout
