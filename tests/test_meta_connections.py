"""Public local setup routes, persistence failures and identity boundaries.

The explicit reversible fake models only the OS encryption boundary. Public
application routes, atomic persistence, locking and owner checks remain real.
"""
from __future__ import annotations

import asyncio
import csv
import importlib
import json
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from config import paths, protected_secrets as vault
from src.web import meta_connections as connections
from src.web import app as web

PROFILE_A = str(uuid.UUID(int=1, version=4))
PROFILE_B = str(uuid.UUID(int=2, version=4))
TOKEN_A = "synthetic-official-credential-A"
TOKEN_B = "synthetic-official-credential-B"
SECRET = "synthetic-app-secret"


def fake_protect(data):
    return b"EXPLICIT-TEST-BOUNDARY:" + data[::-1]


def fake_unprotect(data):
    if not data.startswith(b"EXPLICIT-TEST-BOUNDARY:"):
        raise vault.SecretUnavailable()
    return data.split(b":", 1)[1][::-1]


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    mapping = tmp_path / "ad_account_map.csv"
    write_map(mapping)
    monkeypatch.setattr(web, "get_ad_account_map_path", lambda: str(mapping))
    monkeypatch.setattr(vault, "_protect", fake_protect)
    monkeypatch.setattr(vault, "_unprotect", fake_unprotect)
    return mapping


def write_map(mapping, rows=None):
    paths.write_csv_atomic(mapping, ["account_name", "profile_id", "ad_account_id"], rows or [
        {"account_name": "Alpha", "profile_id": PROFILE_A, "ad_account_id": "act_123456789"},
        {"account_name": "Beta", "profile_id": PROFILE_B, "ad_account_id": "act_987654321"},
    ])


def call(method, url, *, body=None, headers=None, host="127.0.0.1", content=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app, raise_app_exceptions=False),
                                     base_url="http://" + host) as client:
            return await client.request(method, url, json=body, content=content, headers=headers)
    return asyncio.run(run())


def begin(name="Alpha"):
    response = call("POST", "/meta-connections/begin", body={"account_name": name})
    assert response.status_code == 200, response.text
    return response.json()["connection"]


def save(state, token=TOKEN_A, app="300000001", request="save-request-0001"):
    return call("POST", f"/meta-connections/{state['connection_id']}/credential", body={
        "app_id": app, "access_token": token, "app_secret": SECRET,
        "expected_revision": state["revision"], "request_id": request})


def status(state):
    return call("GET", "/meta-connections/" + state["connection_id"]).json()["connection"]


def registry(mapping):
    return json.loads((mapping.parent / "meta-connections.json").read_bytes())


def test_save_resume_restart_replace_disconnect_and_permanent_app_ownership(setup):
    a, b = begin(), begin("Beta")
    saved = save(a).json()["connection"]
    assert saved["phase"] == "saved_unverified" and saved["api_access"] == "unverified"
    assert saved["has_credential"] and "not verified" in saved["next_action"]
    second = save(b, TOKEN_B, "300000002").json()["connection"]
    assert begin()["connection_id"] == a["connection_id"]
    importlib.reload(connections)
    assert status(a) == saved
    assert save(a).json()["connection"] == saved  # Same request after lost HTTP acknowledgement.
    replaced = save(saved, TOKEN_B, request="replace-request-0002").json()["connection"]
    assert replaced["revision"] == saved["revision"] + 1
    assert status(second) == second
    data = registry(setup)
    entry = data["connections"][a["connection_id"]]
    assert vault.get(entry["credential_ref"], a["connection_id"], entry["credential_revision"])["access_token"] == TOKEN_B
    response = call("POST", f"/meta-connections/{a['connection_id']}/disconnect", body={
        "expected_revision": replaced["revision"], "request_id": "disconnect-request-01"})
    disconnected = response.json()["connection"]
    importlib.reload(connections)
    assert status(a)["phase"] == "disconnected" and not status(a)["has_credential"]
    assert status(second) == second
    assert save(second, app="300000001", request="collision-request-01").status_code == 409
    assert TOKEN_A not in response.text and TOKEN_B not in response.text and SECRET not in response.text
    assert disconnected["api_access"] == "unverified"


@pytest.mark.parametrize("mutate", ["profile", "account", "remove"])
def test_mapping_away_and_back_without_status_never_revives_a_secret(setup, mutate):
    state = save(begin()).json()["connection"]
    if mutate == "remove":
        response = call("POST", "/account-map-remove", body={"account_name": "Alpha"})
        assert response.status_code == 200
        body = {"account_name": "Alpha", "profile_id": PROFILE_A, "ad_account_id": "123456789"}
    else:
        field = "profile_id" if mutate == "profile" else "ad_account_id"
        response = call("POST", "/account-map-patch", body={"account_name": "Alpha", field: PROFILE_B if mutate == "profile" else "444444444"})
        assert response.status_code == 200, response.text
        body = {"account_name": "Alpha", field: PROFILE_A if mutate == "profile" else "123456789"}
    assert call("POST", "/account-map-patch", body=body).status_code == 200
    assert status(state)["phase"] == "binding_changed"
    assert not status(state)["has_credential"] and not status(state)["can_save"]
    assert begin()["connection_id"] != state["connection_id"]
    assert save(state, request="old-binding-save-01").status_code == 409


def test_rename_preserves_identity_but_ambiguous_aliases_are_rejected(setup):
    state = save(begin()).json()["connection"]
    rows = list(csv.DictReader(setup.open()))
    rows[0]["account_name"] = "Renamed"
    write_map(setup, rows)
    assert begin("Renamed")["connection_id"] == state["connection_id"]
    assert call("POST", "/meta-connections/begin", body={"account_name": "Rename"}).status_code == 409
    rows.append(dict(rows[0], account_name="RENAMED"))
    write_map(setup, rows)
    assert call("POST", "/meta-connections/begin", body={"account_name": "Renamed"}).status_code == 409
    assert status(state)["phase"] == "binding_changed"


def test_external_map_change_is_checked_before_save_even_without_patch(setup):
    state = save(begin()).json()["connection"]
    rows = list(csv.DictReader(setup.open()))
    rows[0]["profile_id"] = PROFILE_B
    write_map(setup, rows)
    assert save(state, request="external-change-save").status_code == 409
    assert status(state)["phase"] == "binding_changed"


def test_stale_tabs_and_app_collision_serialize_without_lost_other_connection(setup):
    a, b = begin(), begin("Beta")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda state: save(state), [a, b]))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert len(registry(setup)["app_owners"]) == 1
    winner = next(r.json()["connection"] for r in results if r.status_code == 200)
    assert save(winner, request="fresh-replacement-01").status_code == 200
    assert save(winner, request="stale-replacement-02").status_code == 409


def test_encryption_and_metadata_failures_preserve_prior_credential_and_redact(setup, monkeypatch):
    old = save(begin()).json()["connection"]
    snapshot = (setup.parent / "meta-connections.json").read_bytes()
    real_protect = vault._protect
    monkeypatch.setattr(vault, "_protect", lambda data: (_ for _ in ()).throw(OSError(TOKEN_B + SECRET)))
    response = save(old, TOKEN_B, request="encryption-failure-01")
    assert response.status_code == 503 and TOKEN_B not in response.text and SECRET not in response.text
    assert (setup.parent / "meta-connections.json").read_bytes() == snapshot
    monkeypatch.setattr(vault, "_protect", real_protect)
    real_write = paths._atomic_write
    def fail_registry(path, data):
        if Path(path).name == "meta-connections.json":
            raise OSError(TOKEN_B)
        return real_write(path, data)
    monkeypatch.setattr(paths, "_atomic_write", fail_registry)
    response = save(old, TOKEN_B, request="metadata-failure-001")
    assert response.status_code == 503 and TOKEN_B not in response.text
    assert (setup.parent / "meta-connections.json").read_bytes() == snapshot
    assert status(old)["has_credential"]
    for path in setup.parent.rglob("*"):
        if path.is_file():
            assert TOKEN_A.encode() not in path.read_bytes() and TOKEN_B.encode() not in path.read_bytes()
            assert SECRET.encode() not in path.read_bytes()


def test_disconnect_cleanup_failure_is_durable_and_does_not_delete_another_owner(setup, monkeypatch):
    a = save(begin()).json()["connection"]
    b = save(begin("Beta"), TOKEN_B, "300000002").json()["connection"]
    real_delete = vault.delete
    monkeypatch.setattr(vault, "delete", lambda *args, **kwargs: (_ for _ in ()).throw(vault.SecretUnavailable()))
    response = call("POST", f"/meta-connections/{a['connection_id']}/disconnect", body={
        "expected_revision": a["revision"], "request_id": "cleanup-failure-0001"})
    assert response.json()["connection"]["phase"] == "cleanup_pending"
    importlib.reload(connections)
    assert not status(a)["has_credential"] and status(b)["has_credential"]
    resumed = begin()
    assert resumed["connection_id"] == a["connection_id"] and resumed["phase"] == "cleanup_pending"
    assert not resumed["can_save"]
    assert save(resumed, request="save-during-cleanup").status_code == 409
    assert len(registry(setup)["connections"]) == 2
    data = registry(setup)
    foreign = data["connections"][b["connection_id"]]["credential_ref"]
    with pytest.raises(vault.SecretUnavailable):
        real_delete(foreign, a["connection_id"], 1)
    monkeypatch.setattr(vault, "delete", real_delete)
    assert status(a)["phase"] == "disconnected" and status(b)["has_credential"]
    assert begin()["connection_id"] == a["connection_id"]


@pytest.mark.parametrize("damage", ["invalid", "missing", "null", "number", "list", "object", "active", "saved", "pending",
                                  "empty_cleanup", "active_credential", "invalid_binding", "changed_valid_binding"])
def test_corrupt_retirement_is_refused_before_cleanup_or_writes(setup, monkeypatch, damage):
    a = save(begin()).json()["connection"]
    save(begin("Beta"), TOKEN_B, "300000002")
    with monkeypatch.context() as unavailable:
        unavailable.setattr(vault, "delete", lambda *args, **kwargs: (_ for _ in ()).throw(vault.SecretUnavailable()))
        response = call("POST", f"/meta-connections/{a['connection_id']}/disconnect", body={
            "expected_revision": a["revision"], "request_id": "retirement-preserve-01"})
        assert response.status_code == 200
        assert response.json()["connection"]["phase"] == "cleanup_pending"
    source = setup.parent / "meta-connections.json"
    data = registry(setup)
    item = data["connections"][a["connection_id"]]
    assert item["retired_phase"] == "disconnected" and len(item["cleanup"]) == 1
    if damage == "missing":
        item.pop("retired_phase")
    elif damage == "empty_cleanup":
        item["cleanup"] = []
    elif damage == "active_credential":
        item["credential_ref"] = next(record["credential_ref"] for cid, record in data["connections"].items()
                                      if cid != a["connection_id"])
    elif damage == "invalid_binding":
        item["binding_valid"] = False
    elif damage == "changed_valid_binding":
        item["retired_phase"] = "binding_changed"
    else:
        item["retired_phase"] = {"invalid": "corrupt-invalid-phase", "null": None, "number": 1,
                                 "list": [], "object": {}, "active": "awaiting_app",
                                 "saved": "saved_unverified", "pending": "cleanup_pending"}[damage]
    source.write_text(json.dumps(data), encoding="utf-8")
    original_registry = source.read_bytes()
    original_map = setup.read_bytes()
    folder = setup.parent / "protected-secrets"
    original_blobs = {path.name: path.read_bytes() for path in folder.glob("*.bin")}
    deletions = []
    real_delete = vault.delete
    def observe_delete(*args, **kwargs):
        deletions.append(True)
        return real_delete(*args, **kwargs)
    monkeypatch.setattr(vault, "delete", observe_delete)
    response = call("GET", "/meta-connections/" + a["connection_id"])
    assert response.status_code == 503
    assert source.read_bytes() == original_registry
    assert setup.read_bytes() == original_map
    assert {path.name: path.read_bytes() for path in folder.glob("*.bin")} == original_blobs
    assert not deletions
    assert TOKEN_A not in response.text and TOKEN_B not in response.text and SECRET not in response.text


@pytest.mark.parametrize("retirement", ["disconnect", "binding_changed"])
def test_valid_pending_retirement_recovers_and_preserves_other_saved_connection(setup, monkeypatch, retirement):
    a = save(begin()).json()["connection"]
    b = save(begin("Beta"), TOKEN_B, "300000002").json()["connection"]
    with monkeypatch.context() as unavailable:
        unavailable.setattr(vault, "delete", lambda *args, **kwargs: (_ for _ in ()).throw(vault.SecretUnavailable()))
        if retirement == "disconnect":
            response = call("POST", f"/meta-connections/{a['connection_id']}/disconnect", body={
                "expected_revision": a["revision"], "request_id": "valid-retirement-01"})
        else:
            response = call("POST", "/account-map-patch", body={"account_name": "Alpha", "profile_id": PROFILE_B})
        assert response.status_code == 200
        assert status(a)["phase"] == "cleanup_pending"
    data = registry(setup)
    item = data["connections"][a["connection_id"]]
    pending_blob = setup.parent / "protected-secrets" / (item["cleanup"][0]["ref"] + ".bin")
    assert pending_blob.exists()
    recovered = status(a)
    assert recovered["phase"] == ("disconnected" if retirement == "disconnect" else "binding_changed")
    assert not recovered["has_credential"] and not pending_blob.exists()
    assert status(b) == b
    resumed = begin()
    assert (resumed["connection_id"] == a["connection_id"]) == (retirement == "disconnect")
    # Disconnected records retain valid retirement history when active again;
    # freshly created records do not require a historical retired_phase field.
    restored = save(resumed, TOKEN_A, "300000001" if retirement == "disconnect" else "300000003",
                    request="restored-after-retire").json()["connection"]
    assert restored["phase"] == "saved_unverified" and restored["has_credential"]
    assert status(restored) == restored and status(b) == b


def test_corrupt_registry_and_swapped_envelopes_fail_closed(setup):
    a = save(begin()).json()["connection"]
    b = save(begin("Beta"), TOKEN_B, "300000002").json()["connection"]
    data = registry(setup)
    ca, cb = data["connections"][a["connection_id"]], data["connections"][b["connection_id"]]
    ca["credential_ref"] = cb["credential_ref"]
    path = setup.parent / "meta-connections.json"
    path.write_text(json.dumps(data))
    assert not status(a)["has_credential"] and status(b)["has_credential"]
    # Owner checks reject corrupt/swapped references on reads and deletes.
    path.write_bytes(b'{"schema":1,"connections":')
    snapshot = path.read_bytes()
    assert save(a, request="corrupt-registry-01").status_code == 503
    assert call("POST", "/account-map-patch", body={"account_name": "Alpha", "profile_id": PROFILE_B}).status_code == 500
    assert path.read_bytes() == snapshot


@pytest.mark.parametrize("headers,host,status_code", [
    ({"Origin": "https://foreign.invalid"}, "127.0.0.1", 403),
    ({"Referer": "https://foreign.invalid/form"}, "127.0.0.1", 403),
    ({"Sec-Fetch-Site": "cross-site"}, "127.0.0.1", 403),
    ({}, "foreign.invalid", 400),
])
def test_public_boundary_blocks_foreign_mutations_before_storage(setup, headers, host, status_code):
    response = call("POST", "/meta-connections/begin", body={"account_name": "Alpha"}, headers=headers, host=host)
    assert response.status_code == status_code
    assert not (setup.parent / "meta-connections.json").exists()
    assert not (setup.parent / "protected-secrets").exists()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("body", [[], None, {"account_name": {}}, {"account_name": "Alpha", "credential_ref": "../foreign"}])
def test_invalid_bodies_and_client_references_are_not_echoed_or_stored(setup, body):
    response = call("POST", "/meta-connections/begin", body=body)
    assert response.status_code == 400
    assert "../foreign" not in response.text
    assert not (setup.parent / "meta-connections.json").exists()


def test_oversized_request_and_invalid_identifier_no_secret_echo(setup):
    response = call("POST", "/meta-connections/begin", content=b"x" * 32769)
    assert response.status_code == 413
    a = begin()
    response = save(a, app=TOKEN_A)
    assert response.status_code == 400 and TOKEN_A not in response.text
    response = call("POST", f"/meta-connections/{a['connection_id']}/credential", body={
        "app_id": "300000001", "access_token": TOKEN_A, "secret_ref": TOKEN_B,
        "expected_revision": a["revision"], "request_id": "invalid-ref-request"})
    assert response.status_code == 400 and TOKEN_A not in response.text and TOKEN_B not in response.text
    assert call("GET", "/meta-connections/not-a-uuid").status_code == 404


def test_sentry_and_support_scrub_new_secret_fields(setup):
    from src import acl_sentry
    from src.fb_ads_rpa.run_telemetry import scrub, scrub_diag
    value = {"app_secret": SECRET, "credential_ref": "opaque-private-reference", "access_token": TOKEN_A}
    for scrubber in (scrub_diag, lambda data: acl_sentry._before_send(data, {})):
        output = json.dumps(scrubber(value.copy()))
        assert SECRET not in output and TOKEN_A not in output and "opaque-private-reference" not in output
    assert SECRET not in scrub(json.dumps(value))
    import importlib.util
    source = Path(__file__).resolve().parents[1] / "packaging/windows/diagnostics.py"
    spec = importlib.util.spec_from_file_location("meta_setup_diagnostics", source)
    diagnostics = importlib.util.module_from_spec(spec); spec.loader.exec_module(diagnostics)
    assert SECRET not in diagnostics.sanitize_text(json.dumps(value))
    save(begin())
    manifest = setup.parent / "manifest.json"; manifest.write_text("{}")
    bundle = diagnostics.export_diagnostics(setup.parent, manifest)
    import zipfile
    with zipfile.ZipFile(bundle) as archive:
        assert not any("meta-connections" in n or "protected-secrets" in n for n in archive.namelist())
        assert all(TOKEN_A.encode() not in archive.read(n) and SECRET.encode() not in archive.read(n) for n in archive.namelist())


def test_unsupported_platform_has_no_plaintext_fallback(setup, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(vault, "os", SimpleNamespace(name="unsupported"))
    monkeypatch.setattr(vault, "_protect", lambda data: vault._dpapi(data, decrypt=False))
    response = save(begin())
    assert response.status_code == 503 and TOKEN_A not in response.text
    assert not (setup.parent / "protected-secrets").exists()


def test_two_real_processes_race_for_one_app_owner(setup):
    a, b = begin(), begin("Beta")
    code = '''
import json, sys
from config import protected_secrets as vault
from src.web import meta_connections as c
vault._protect=lambda data: b"EXPLICIT-TEST-BOUNDARY:"+data[::-1]
vault._unprotect=lambda data: data.split(b":",1)[1][::-1]
try:
    c.save(sys.argv[1],sys.argv[2],{"app_id":"300000099","access_token":"synthetic-process-token",
           "expected_revision":1,"request_id":"process-request-0001"})
    print("committed")
except c.ConnectionError as exc:
    print("conflict:"+str(exc.status))
'''
    workers = [subprocess.Popen([sys.executable, "-c", code, str(setup), state["connection_id"]],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for state in (a, b)]
    results = [worker.communicate(timeout=30) for worker in workers]
    assert all(worker.returncode == 0 for worker in workers), results
    assert sorted(out.strip() for out, err in results) == ["committed", "conflict:409"]
    assert len(registry(setup)["app_owners"]) == 1


def test_link_writer_cannot_restore_profile_from_an_old_snapshot(setup, monkeypatch):
    save(begin())
    entered, release = threading.Event(), threading.Event()
    real = web.write_csv_atomic
    def pause_link(path, fields, rows):
        if "tracking_link" in fields:
            entered.set()
            assert release.wait(10)
        return real(path, fields, rows)
    monkeypatch.setattr(web, "write_csv_atomic", pause_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        link = pool.submit(web._save_link_to_machine_map, "Alpha", "https://synthetic.example/link")
        assert entered.wait(10)
        patch = pool.submit(call, "POST", "/account-map-patch", body={"account_name": "Alpha", "profile_id": PROFILE_B})
        release.set()
        assert link.result() and patch.result().status_code == 200
    rows = list(csv.DictReader(setup.open()))
    assert rows[0]["profile_id"] == PROFILE_B and rows[0]["tracking_link"] == "https://synthetic.example/link"


def test_legacy_copy_retires_binding_before_mapping_can_return(setup, monkeypatch):
    state = save(begin()).json()["connection"]
    legacy = setup.parent / "legacy.csv"
    write_map(legacy, [{"account_name": "Alpha", "profile_id": PROFILE_B, "ad_account_id": "act_123456789"}])
    monkeypatch.setattr(paths, "AD_ACCOUNT_MAP_DEFAULT", str(legacy))
    setup.unlink()
    assert "copied" in paths.migrate_legacy_account_map()
    setup.unlink()
    write_map(legacy)
    assert "copied" in paths.migrate_legacy_account_map()
    assert status(state)["phase"] == "binding_changed"


def test_failed_map_replace_is_fail_closed_without_restoring_old_binding(setup, monkeypatch):
    state = save(begin()).json()["connection"]
    before = setup.read_bytes()
    monkeypatch.setattr(web, "write_csv_atomic", lambda *args: (_ for _ in ()).throw(PermissionError()))
    response = call("POST", "/account-map-patch", body={"account_name": "Alpha", "profile_id": PROFILE_B})
    assert response.status_code == 409 and setup.read_bytes() == before
    assert status(state)["phase"] == "binding_changed"


def test_ciphertext_atomic_replace_failure_preserves_old_revision(setup, monkeypatch):
    state = save(begin()).json()["connection"]
    old = registry(setup)
    real_replace = paths._replace_when_free
    def fail_blob(source, target):
        if Path(target).suffix == ".bin":
            # The staged file must already be ciphertext, including at failure.
            assert TOKEN_B.encode() not in Path(source).read_bytes()
            raise OSError(TOKEN_B)
        real_replace(source, target)
    monkeypatch.setattr(paths, "_replace_when_free", fail_blob)
    response = save(state, TOKEN_B, request="blob-replace-failure")
    assert response.status_code == 503 and TOKEN_B not in response.text
    assert registry(setup) == old and status(state)["has_credential"]
    assert not list((setup.parent / "protected-secrets").glob("*.tmp"))


def test_replace_and_disconnect_race_has_one_committed_outcome(setup):
    state = save(begin()).json()["connection"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        replace = pool.submit(save, state, TOKEN_B, request="race-replace-0001")
        remove = pool.submit(call, "POST", f"/meta-connections/{state['connection_id']}/disconnect", body={
            "expected_revision": state["revision"], "request_id": "race-disconnect-0001"})
        results = [replace.result(), remove.result()]
    assert sorted(r.status_code for r in results) == [200, 409]
    current = status(state)
    assert current["revision"] == state["revision"] + 1
    assert current["has_credential"] == (current["phase"] == "saved_unverified")


def test_installer_keeps_existing_binding_and_refuses_corrupt_metadata(setup, monkeypatch):
    import importlib.util
    source = Path(__file__).resolve().parents[1] / "packaging/windows/machine_config.py"
    spec = importlib.util.spec_from_file_location("meta_setup_machine_config", source)
    machine = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, machine)
    spec.loader.exec_module(machine)
    state = save(begin()).json()["connection"]
    configuration = machine.MachineConfiguration("Synthetic manager", (
        machine.AccountAssignment("Alpha", "act_123456789"),))
    machine.apply_machine_configuration(configuration, setup.parent,
                                        stats_workbook="synthetic.xlsx", creatives_directory="synthetic-creatives")
    assert status(state)["has_credential"] and status(state)["connection_id"] == state["connection_id"]
    (setup.parent / "meta-connections.json").write_bytes(b"corrupt")
    old_map, old_env = setup.read_bytes(), (setup.parent / ".env").read_bytes()
    with pytest.raises(connections.ConnectionError):
        machine.apply_machine_configuration(configuration, setup.parent,
                                            stats_workbook="changed.xlsx", creatives_directory="synthetic-creatives")
    assert setup.read_bytes() == old_map and (setup.parent / ".env").read_bytes() == old_env


@pytest.mark.parametrize("owner_damage", ["missing", "other_connection"])
def test_historical_save_receipt_requires_its_permanent_app_owner(setup, owner_damage):
    a, b = begin(), begin("Beta")
    first = save(a, app="300000001").json()["connection"]
    assert save(first, app="300000002", request="replace-app-request").status_code == 200
    data = registry(setup)
    receipt = data["connections"][a["connection_id"]]["requests"]["save-request-0001"]
    assert receipt["app_id"] == "300000001"
    # Deliberately inconsistent metadata: a retained successful save disagrees
    # with permanent ownership. This is a corruption test, not a normal-flow bypass.
    if owner_damage == "missing":
        del data["app_owners"]["300000001"]
    else:
        data["app_owners"]["300000001"] = b["connection_id"]
    store = setup.parent / "meta-connections.json"
    store.write_text(json.dumps(data), encoding="utf-8")
    old_store = store.read_bytes()
    old_blobs = {p.name: p.read_bytes() for p in (setup.parent / "protected-secrets").iterdir()}
    response = save(b, TOKEN_B, "300000001", request="reassign-corrupt-app")
    assert response.status_code == 503
    assert "unreadable" in response.json()["error"]
    assert store.read_bytes() == old_store
    assert {p.name: p.read_bytes() for p in (setup.parent / "protected-secrets").iterdir()} == old_blobs
    assert TOKEN_A not in response.text and TOKEN_B not in response.text


@pytest.mark.parametrize("route,body,fallback", [
    ("/account-map-patch", {"account_name": "Alpha", "country": "MX"}, False),
    ("/account-map-remove", {"account_name": "Alpha"}, False),
    ("/account-link-patch", {"account_name": "Alpha", "link": "https://synthetic.example/link"}, False),
    ("/account-link-patch", {"account_name": "Alpha", "link": "https://synthetic.example/link"}, True),
])
def test_contended_map_writes_allow_real_dashboard_control_dispatch(setup, monkeypatch, route, body, fallback):
    acquired, release, attempted, holder_done = (threading.Event() for _ in range(4))
    real_transaction = connections.transaction
    @contextmanager
    def observed_transaction(*args, **kwargs):
        attempted.set()
        with real_transaction(*args, **kwargs):
            yield
    monkeypatch.setattr(connections, "transaction", observed_transaction)
    if fallback:
        real_link = web._save_link_to_machine_map
        first_lookup = True
        def miss_then_find(*args):
            nonlocal first_lookup
            if first_lookup:
                first_lookup = False
                return False
            return real_link(*args)
        monkeypatch.setattr(web, "_save_link_to_machine_map", miss_then_find)
        monkeypatch.setattr(web, "_write_link_to_excel", lambda *args: None)
        workbook = setup.parent / "synthetic-upload.xlsx"
        workbook.touch()
        monkeypatch.setattr(web, "get_upload_fb_excel_path", lambda: str(workbook))
    def holder():
        with connections._lock:
            acquired.set()
            release.wait(5)  # Bounded escape also makes the old blocking route fail.
            holder_done.set()
    thread = threading.Thread(target=holder)
    thread.start()
    assert acquired.wait(5)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app, raise_app_exceptions=False),
                                     base_url="http://127.0.0.1") as client:
            mutation = asyncio.create_task(client.post(route, json=body))
            try:
                assert await asyncio.to_thread(attempted.wait, 5)
                control = await client.post("/publish-jobs/synthetic-nonexistent/cancel")
                assert control.status_code == 404  # No real job or browser action.
                assert not holder_done.is_set(), "Map lock wait blocked the dashboard control caller"
            finally:
                release.set()
                response = await mutation
            assert response.status_code == 200, response.text
    try:
        asyncio.run(run())
    finally:
        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
