"""Signed release manifests (ACL-1357, audit S06).

The installer hash comes from the same GitHub Release as the installer, so the
client must also check who published release.json: an Ed25519 signature from a
key pinned in the client. Private keys here are generated per test run; the
only real key material in the repository is the owner's PUBLIC key pinned in
src/web/release_signing.py (its private half lives only in the CI secret).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from src.web import release_signing as rs
from src.web import release_updates as ru

INSTALLER = b"MZ" + b"\x01" * 2048
VERSION = "2026-09-30"
TAG = f"acl-release-{VERSION}"
NAME = f"Adopy-Campaign-Launcher-Setup-{VERSION}.exe"


def _manifest(**installer) -> bytes:
    inst = {"name": NAME, "size": len(INSTALLER), "sha256": hashlib.sha256(INSTALLER).hexdigest(), "url": "unused"}
    inst.update(installer)
    return (json.dumps({"schema": 1, "version": VERSION, "tag": TAG, "installer": inst, "fixed": []},
                       indent=2) + "\n").encode()


@pytest.fixture
def publisher():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def pinned(monkeypatch, publisher):
    key_id, public = rs.pinned_entry(publisher)
    table = {key_id: public}
    monkeypatch.setattr(rs, "PINNED_PUBLIC_KEYS", table)
    monkeypatch.setattr(rs, "ENFORCE", False)
    monkeypatch.delenv(rs.ENFORCE_ENV, raising=False)
    monkeypatch.setattr(rs, "_reported", set())
    return table


# ── проверка подписи ─────────────────────────────────────────────────────

def test_valid_signature_from_the_pinned_key_is_accepted(pinned, publisher):
    manifest = _manifest()
    verdict = rs.verify_manifest(manifest, rs.sign_manifest(manifest, publisher))
    assert verdict.valid and verdict.key_id in pinned
    assert rs.decide(verdict, "enforce") == (True, "")


def test_changed_manifest_is_refused_in_every_mode(pinned, publisher):
    signature = rs.sign_manifest(_manifest(), publisher)
    tampered = _manifest(sha256="0" * 64)
    verdict = rs.verify_manifest(tampered, signature)
    assert verdict.status == rs.BAD_SIGNATURE
    assert rs.decide(verdict, "warn")[0] is False
    assert rs.decide(verdict, "enforce")[0] is False


def test_signature_by_an_unknown_key_is_refused_in_every_mode(pinned):
    manifest = _manifest()
    verdict = rs.verify_manifest(manifest, rs.sign_manifest(manifest, Ed25519PrivateKey.generate()))
    assert verdict.status == rs.UNKNOWN_KEY
    assert rs.decide(verdict, "warn")[0] is False


def test_other_key_claiming_the_pinned_key_id_is_refused(pinned):
    manifest = _manifest()
    forged = json.loads(rs.sign_manifest(manifest, Ed25519PrivateKey.generate()))
    forged["key_id"] = next(iter(pinned))
    verdict = rs.verify_manifest(manifest, json.dumps(forged).encode())
    assert verdict.status == rs.BAD_SIGNATURE


def test_pinned_entry_whose_id_does_not_match_the_key_is_not_trusted(monkeypatch, publisher):
    manifest = _manifest()
    signature = rs.sign_manifest(manifest, publisher)
    key_id = json.loads(signature)["key_id"]
    other = rs.pinned_entry(Ed25519PrivateKey.generate())[1]
    verdict = rs.verify_manifest(manifest, signature, pinned={key_id: other})
    assert verdict.status == rs.UNKNOWN_KEY


@pytest.mark.parametrize("garbage", [b"", b"not json", b"[]", b'{"schema": 1, "alg": "rsa"}',
                                     b'{"schema": 1, "alg": "ed25519", "key_id": "zz", "signature": ""}',
                                     b"x" * 10000])
def test_malformed_signature_is_refused_in_every_mode(pinned, garbage):
    verdict = rs.verify_manifest(_manifest(), garbage)
    assert verdict.status == rs.MALFORMED
    assert rs.decide(verdict, "warn")[0] is False


def test_unsigned_release_warns_now_and_is_refused_when_enforced(pinned):
    verdict = rs.verify_manifest(_manifest(), None)
    assert verdict.status == rs.UNSIGNED
    allowed, message = rs.decide(verdict, "warn")
    assert allowed and "no publisher signature" in message
    assert rs.decide(verdict, "enforce")[0] is False


def test_build_without_a_pinned_key_warns_now_and_refuses_when_enforced(publisher):
    manifest = _manifest()
    verdict = rs.verify_manifest(manifest, rs.sign_manifest(manifest, publisher), pinned={})
    assert verdict.status == rs.NO_PINNED_KEY
    assert rs.decide(verdict, "warn")[0] is True
    assert rs.decide(verdict, "enforce")[0] is False


def test_missing_crypto_library_is_never_a_pass_when_enforced(pinned, publisher, monkeypatch):
    manifest = _manifest()
    signature = rs.sign_manifest(manifest, publisher)

    def unavailable():
        raise ImportError("cryptography")

    monkeypatch.setattr(rs, "_ed25519", unavailable)
    verdict = rs.verify_manifest(manifest, signature)
    assert verdict.status == rs.UNAVAILABLE
    assert rs.decide(verdict, "enforce")[0] is False


def test_mode_can_only_be_made_stricter(monkeypatch):
    monkeypatch.setattr(rs, "ENFORCE", False)
    monkeypatch.delenv(rs.ENFORCE_ENV, raising=False)
    assert rs.mode() == "warn"
    assert rs.mode({"signatures": "enforce"}) == "enforce"
    monkeypatch.setenv(rs.ENFORCE_ENV, "enforce")
    assert rs.mode({"signatures": "warn"}) == "enforce"
    monkeypatch.setenv(rs.ENFORCE_ENV, "warn")
    monkeypatch.setattr(rs, "ENFORCE", True)
    assert rs.mode({"signatures": "warn"}) == "enforce"


def test_shipped_state_is_warn_with_only_public_key_material():
    # Этап выкатки на сегодня: ключ владельца закреплён, но отказ по «нет
    # подписи» включается отдельным решением (docs/RELEASE_TRUST.md, шаги 4–6).
    assert rs.ENFORCE is False
    for key_id, public in rs.PINNED_PUBLIC_KEYS.items():
        raw = base64.b64decode(public)
        assert len(raw) == 32 and rs.key_id_for(raw) == key_id


# Ключ владельца из Issue #357; любая правка таблицы должна быть осознанной.
OWNER_KEY_ID = "428a74e4f8e6e616"
OWNER_PUBLIC = "zNclue/L3oMZN3SnynUUOnk5AAVEzSwgCc8GzC0WWcM="


def _claiming(signature: bytes, key_id: str) -> bytes:
    doc = json.loads(signature)
    doc["key_id"] = key_id
    return json.dumps(doc).encode()


def test_owner_key_is_pinned_and_its_id_matches_the_key():
    assert rs.PINNED_PUBLIC_KEYS == {OWNER_KEY_ID: OWNER_PUBLIC}
    raw = base64.b64decode(OWNER_PUBLIC, validate=True)
    assert len(raw) == 32 and rs.key_id_for(raw) == OWNER_KEY_ID
    Ed25519PublicKey.from_public_bytes(raw)  # это настоящий ключ Ed25519


@pytest.mark.parametrize("mode", ["warn", "enforce"])
def test_manifest_signed_by_another_key_is_refused_by_the_shipped_table(mode):
    # Без monkeypatch: работает настоящая таблица PINNED_PUBLIC_KEYS.
    stranger = Ed25519PrivateKey.generate()
    manifest = _manifest()
    signature = rs.sign_manifest(manifest, stranger)

    unknown = rs.verify_manifest(manifest, signature)
    assert unknown.status == rs.UNKNOWN_KEY
    assert rs.decide(unknown, mode)[0] is False

    # Чужая подпись, выдающая себя за ключ владельца, не сходится.
    forged = rs.verify_manifest(manifest, _claiming(signature, OWNER_KEY_ID))
    assert forged.status == rs.BAD_SIGNATURE and forged.key_id == OWNER_KEY_ID
    assert rs.decide(forged, mode)[0] is False


def test_ci_with_a_key_other_than_the_pinned_owner_key_fails(tmp_path):
    from scripts import acl_sign_release as signer

    manifest = tmp_path / "release.json"
    manifest.write_bytes(_manifest())
    stranger = Ed25519PrivateKey.generate()
    env = {rs.SIGNING_KEY_ENV: stranger.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}
    assert signer.sign(manifest, require=False, env=env) == 1
    assert not (tmp_path / rs.SIGNATURE_ASSET).exists()


def test_private_key_loads_from_pem_or_raw_seed(publisher):
    pem = publisher.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode()
    seed = base64.b64encode(publisher.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                    serialization.NoEncryption())).decode()
    expected = rs.pinned_entry(publisher)
    assert rs.pinned_entry(rs.load_private_key(pem)) == expected
    assert rs.pinned_entry(rs.load_private_key(seed)) == expected
    for bad in ("", "not-a-key", base64.b64encode(b"short").decode()):
        with pytest.raises(rs.SigningError):
            rs.load_private_key(bad)


def test_a_refused_release_is_logged_once(pinned, caplog):
    verdict = rs.Verdict(rs.UNSIGNED)
    with caplog.at_level(logging.WARNING, logger=rs.__name__):
        rs.report(verdict, "warn", TAG)
        rs.report(verdict, "warn", TAG)
    assert [r.getMessage() for r in caplog.records].count(
        f"release signature unsigned (mode=warn, version={TAG}, key=-)") == 1


# ── клиент обновлений целиком ────────────────────────────────────────────

class FakeRelease:
    """GitHub as seen by release_updates._request: URL -> bytes."""

    def __init__(self, manifest: bytes, signature: bytes | None, installer: bytes = INSTALLER, tag: str = TAG):
        self.manifest, self.signature, self.installer, self.tag = manifest, signature, installer, tag
        self.requested: list[str] = []

    def request(self, url, ch, *, accept, timeout=30.0, destination=None, limit=2 * 1024 * 1024):
        self.requested.append(url)
        if url.endswith("/releases/latest"):
            assets = [{"name": "release.json", "url": "api://asset/manifest"},
                      {"name": NAME, "url": "api://asset/installer"}]
            if self.signature is not None:
                assets.append({"name": rs.SIGNATURE_ASSET, "url": "api://asset/signature"})
            return json.dumps({"tag_name": self.tag, "assets": assets}).encode()
        if url == "api://asset/manifest":
            return self.manifest
        if url == "api://asset/signature":
            return self.signature
        if url == "api://asset/installer":
            destination.write_bytes(self.installer[:limit + 1])
            return b""
        raise AssertionError(url)


@pytest.fixture
def client(tmp_path, monkeypatch, pinned):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ACL_RELEASES_REPO", "adopy/acl-releases")
    monkeypatch.setenv("ACL_UPDATE_TOKEN", "read-token")
    monkeypatch.setattr(ru, "installed_version", lambda: "2026-09-27")
    reports = []
    monkeypatch.setattr(rs, "report", lambda verdict, mode, version: reports.append((verdict.status, mode, version)))

    def use(release: FakeRelease) -> FakeRelease:
        monkeypatch.setattr(ru, "_request", release.request)
        return release

    use.reports = reports
    return use


def test_signed_release_is_downloaded_and_offered(client, publisher, monkeypatch):
    monkeypatch.setenv(rs.ENFORCE_ENV, "enforce")
    manifest = _manifest()
    client(FakeRelease(manifest, rs.sign_manifest(manifest, publisher)))

    setup, parsed = ru.download()
    assert setup.read_bytes() == INSTALLER and parsed["version"] == VERSION
    state = ru.check(force=True)
    assert state["available"] and state["signature"]["status"] == rs.VALID
    assert state["signature"]["mode"] == "enforce"
    assert {status for status, _, _ in client.reports} == {rs.VALID}


def test_manifest_changed_after_signing_is_refused_before_download(client, publisher):
    evil = hashlib.sha256(b"evil installer").hexdigest()
    fake = client(FakeRelease(_manifest(sha256=evil), rs.sign_manifest(_manifest(), publisher),
                              installer=b"evil installer"))

    with pytest.raises(ru.UntrustedRelease, match="signature does not match"):
        ru.download()
    assert "api://asset/installer" not in fake.requested
    assert client.reports and client.reports[-1][0] == rs.BAD_SIGNATURE


def test_release_signed_by_an_unknown_key_is_refused(client):
    manifest = _manifest()
    fake = client(FakeRelease(manifest, rs.sign_manifest(manifest, Ed25519PrivateKey.generate())))

    with pytest.raises(ru.UntrustedRelease, match="unknown publisher key"):
        ru.download()
    assert "api://asset/installer" not in fake.requested
    state = ru.check(force=True)
    assert not state["available"] and "unknown publisher key" in state["note"]


def test_changed_artifact_under_a_valid_signature_is_refused(client, publisher, monkeypatch):
    monkeypatch.setenv(rs.ENFORCE_ENV, "enforce")
    manifest = _manifest()
    client(FakeRelease(manifest, rs.sign_manifest(manifest, publisher), installer=INSTALLER[:-1] + b"\x02"))

    with pytest.raises(ru.UpdateError, match="SHA-256"):
        ru.download()
    assert not list((ru.updates_dir()).glob("*.exe"))


def test_signed_manifest_of_another_release_is_refused(client, publisher):
    manifest = _manifest()
    client(FakeRelease(manifest, rs.sign_manifest(manifest, publisher), tag="acl-release-2099-01-01"))

    with pytest.raises(ru.UntrustedRelease, match="different release"):
        ru.download()


def test_signed_manifest_without_a_tag_is_refused(client, publisher):
    # Подпись без привязки к выпуску позволила бы переложить release.json в
    # чужой релиз, поэтому у подписанного манифеста tag обязателен.
    doc = json.loads(_manifest())
    del doc["tag"]
    manifest = json.dumps(doc, indent=2).encode()
    fake = client(FakeRelease(manifest, rs.sign_manifest(manifest, publisher)))

    with pytest.raises(ru.UntrustedRelease, match="different release"):
        ru.download()
    assert "api://asset/installer" not in fake.requested


def test_unsigned_release_is_reported_but_still_installs_in_warn_mode(client):
    client(FakeRelease(_manifest(), None))

    setup, _ = ru.download()
    assert setup.read_bytes() == INSTALLER
    state = ru.check(force=True)
    assert state["available"] and state["signature"]["status"] == rs.UNSIGNED
    assert (rs.UNSIGNED, "warn", TAG) in client.reports


def test_unsigned_release_is_refused_when_enforced_and_not_left_on_offer(client, monkeypatch):
    client(FakeRelease(_manifest(), None))
    assert ru.check(force=True)["available"]  # вчера (warn) выпуск был предложен

    monkeypatch.setenv(rs.ENFORCE_ENV, "enforce")
    with pytest.raises(ru.UntrustedRelease, match="no publisher signature"):
        ru.download()
    state = ru.check(force=True)
    assert not state["available"] and "no publisher signature" in state["note"]


def test_build_can_enforce_through_its_channel_file(client, monkeypatch):
    client(FakeRelease(_manifest(), None))
    monkeypatch.setattr(ru, "_bundle_channel", lambda: {"signatures": "enforce"})

    with pytest.raises(ru.UntrustedRelease):
        ru.download()


# ── подпись в CI и публикация ────────────────────────────────────────────

def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def test_ci_signs_with_the_secret_and_the_client_accepts_it(tmp_path, publisher, capsys):
    from scripts import acl_sign_release as signer

    manifest = tmp_path / "release.json"
    manifest.write_bytes(_manifest())
    pinned = dict([rs.pinned_entry(publisher)])

    assert signer.sign(manifest, require=True, env={rs.SIGNING_KEY_ENV: _pem(publisher)}, pinned=pinned) == 0
    signature = (tmp_path / rs.SIGNATURE_ASSET).read_bytes()
    assert rs.verify_manifest(manifest.read_bytes(), signature, pinned=pinned).valid
    assert "PRIVATE KEY" not in capsys.readouterr().out


def test_ci_without_the_secret_publishes_unsigned_only_when_not_required(tmp_path):
    from scripts import acl_sign_release as signer

    manifest = tmp_path / "release.json"
    manifest.write_bytes(_manifest())
    assert signer.sign(manifest, require=False, env={}, pinned={}) == 0
    assert signer.sign(manifest, require=True, env={}, pinned={}) == 1
    assert not (tmp_path / rs.SIGNATURE_ASSET).exists()


def test_ci_without_the_secret_fails_once_a_key_is_pinned(tmp_path, publisher):
    from scripts import acl_sign_release as signer

    manifest = tmp_path / "release.json"
    manifest.write_bytes(_manifest())
    pinned = dict([rs.pinned_entry(publisher)])
    # Закреплённый ключ сам по себе требует подписи, даже без --require.
    assert signer.sign(manifest, require=False, env={}, pinned=pinned) == 1
    assert signer.sign(manifest, require=False, env={}) == 1  # настоящая таблица
    assert not (tmp_path / rs.SIGNATURE_ASSET).exists()


def test_ci_refuses_a_key_the_clients_do_not_pin(tmp_path, publisher):
    from scripts import acl_sign_release as signer

    manifest = tmp_path / "release.json"
    manifest.write_bytes(_manifest())
    pinned = dict([rs.pinned_entry(Ed25519PrivateKey.generate())])
    assert signer.sign(manifest, require=False, env={rs.SIGNING_KEY_ENV: _pem(publisher)}, pinned=pinned) == 1
    # Обязательная подпись без закреплённого ключа убила бы все обновления.
    assert signer.sign(manifest, require=True, env={rs.SIGNING_KEY_ENV: _pem(publisher)}, pinned={}) == 1
    assert not (tmp_path / rs.SIGNATURE_ASSET).exists()


def test_keygen_never_writes_inside_the_repository_or_prints_the_key(tmp_path, capsys, monkeypatch):
    from scripts import acl_sign_release as signer

    # Репозиторий подменён временной папкой: даже сломанная проверка не
    # оставит ключ в настоящем чекауте.
    repo = tmp_path / "repo"
    monkeypatch.setattr(signer, "_ROOT", repo.resolve())
    assert signer.keygen(repo / "config" / "release_key.pem") == 1
    assert not (repo / "config" / "release_key.pem").exists()
    out = tmp_path / "keys" / "acl_release.pem"
    assert signer.keygen(out) == 0
    printed = capsys.readouterr().out
    assert "PRIVATE KEY" not in printed and "PINNED_PUBLIC_KEYS" in printed
    key = rs.load_private_key(out.read_text())
    assert rs.pinned_entry(key)[0] in printed
    assert signer.keygen(out) == 1  # не перезаписывает


def test_publication_uploads_the_signature_next_to_release_json(tmp_path, monkeypatch):
    from scripts import acl_publish_release as pub

    (tmp_path / "release.json").write_text(json.dumps({"tag": TAG, "version": VERSION, "installer": {"name": NAME}}))
    for name in [NAME, NAME + ".sha256", "release-notes.md", rs.SIGNATURE_ASSET]:
        (tmp_path / name).write_text("test")
    calls = []
    monkeypatch.setattr(pub, "_gh", lambda *args: calls.append(args) or "[]")
    pub.publish(TAG, "test/releases", tmp_path)
    create = next(c for c in calls if c[:2] == ("release", "create"))
    assert str(tmp_path / rs.SIGNATURE_ASSET) in create
