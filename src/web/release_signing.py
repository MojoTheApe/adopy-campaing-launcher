"""Signed release manifests for the packaged ACL.exe (ACL-1357, audit S06).

``release.json`` names the installer and its SHA-256, and the client already
refuses an installer whose hash differs. But the hash arrives from the same
GitHub Release as the installer: whoever can replace one can replace both. A
signature closes that gap only if the verifying key does NOT travel with the
release — so the publisher's Ed25519 public key is pinned here, in the client
code, and ``release.json.sig`` must verify against it.

Signature file (published next to ``release.json``)::

    {"schema": 1, "alg": "ed25519", "key_id": "<16 hex>", "signature": "<base64>"}

It signs the exact bytes of ``release.json`` as published. ``key_id`` is the
first 16 hex digits of SHA-256 over the raw 32-byte public key.

Rollout (see docs/RELEASE_TRUST.md):

* **warn** (today) — no key is pinned yet and no release is signed yet, so a
  missing signature or a missing pinned key is logged and reported, and the
  update continues with the SHA-256 check as before. A signature that IS
  present but fails — unknown signer, bad signature, garbage file — is refused
  even now: a legitimate release never produces one.
* **enforce** — anything but a valid signature from a pinned key is refused.
  Switched on by ``ENFORCE = True`` here, or per build by the release
  workflow (``"signatures": "enforce"`` in the bundled channel file), or per
  machine with ``ACL_RELEASE_SIGNATURES=enforce``. Nothing can switch an
  enforcing build back to warn.

The private key lives only in the CI secret ``ACL_RELEASE_SIGNING_KEY``
(``scripts/acl_sign_release.py`` reads it); nothing here or in the repository
holds a real private key.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

log = logging.getLogger(__name__)

SIGNATURE_ASSET = "release.json.sig"
SIGNATURE_SCHEMA = 1
ALGORITHM = "ed25519"
# Имя секрета CI с закрытым ключом и переключатель этапа на машине.
SIGNING_KEY_ENV = "ACL_RELEASE_SIGNING_KEY"
ENFORCE_ENV = "ACL_RELEASE_SIGNATURES"

# Закреплённые публичные ключи издателя: key_id -> base64 от 32 байт Ed25519.
# Ключ владельца (Дима) выпущен 2026-09-30, закрытая часть — только в секрете
# ACL_RELEASE_SIGNING_KEY репозитория (Issue #357). Здесь лишь ПУБЛИЧНЫЙ ключ:
# он не секрет, по нему можно только проверить подпись. key_id = sha256[:16]
# от сырых 32 байт (key_id_for), тест сверяет. Для смены ключа сначала
# выпускается сборка с обоими ключами, потом старый убирается.
PINNED_PUBLIC_KEYS: dict[str, str] = {"428a74e4f8e6e616": "zNclue/L3oMZN3SnynUUOnk5AAVEzSwgCc8GzC0WWcM="}

# Этап выкатки: False — проверить и предупредить, True — отказывать во всём,
# что не подписано закреплённым ключом.
ENFORCE = False

VALID = "valid"
UNSIGNED = "unsigned"
NO_PINNED_KEY = "no_pinned_key"
UNKNOWN_KEY = "unknown_key"
BAD_SIGNATURE = "bad_signature"
MALFORMED = "malformed"
UNAVAILABLE = "crypto_unavailable"

# Что пропускает режим warn: только «подписи нет» и «проверить нечем». Подпись,
# которая есть и не сходится, — признак подмены в любом режиме.
_WARN_PASSES = frozenset({UNSIGNED, NO_PINNED_KEY, UNAVAILABLE})

_KEY_ID = re.compile(r"[0-9a-f]{16}\Z")
_MAX_SIGNATURE_BYTES = 4096


class SigningError(RuntimeError):
    """The release cannot be signed (CI side)."""


@dataclass(frozen=True)
class Verdict:
    status: str
    key_id: str = ""

    @property
    def valid(self) -> bool:
        return self.status == VALID


def key_id_for(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def _ed25519():
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

    return Ed25519PrivateKey, Ed25519PublicKey, InvalidSignature


def _parse_signature(raw: bytes) -> tuple[str, bytes] | None:
    try:
        if len(raw) > _MAX_SIGNATURE_BYTES:
            return None
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            return None
        if type(doc.get("schema")) is not int or doc["schema"] != SIGNATURE_SCHEMA:
            return None
        if doc.get("alg") != ALGORITHM:
            return None
        key_id = doc.get("key_id")
        if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
            return None
        signature = base64.b64decode(str(doc.get("signature", "")), validate=True)
        if len(signature) != 64:
            return None
        return key_id, signature
    except (ValueError, TypeError, binascii.Error):
        return None


def verify_manifest(manifest: bytes, signature: bytes | None, *,
                    pinned: Mapping[str, str] | None = None) -> Verdict:
    """Check ``signature`` over the exact ``manifest`` bytes against pinned keys."""
    keys = PINNED_PUBLIC_KEYS if pinned is None else pinned
    if signature is None:
        return Verdict(UNSIGNED)
    parsed = _parse_signature(signature)
    if parsed is None:
        return Verdict(MALFORMED)
    key_id, sig = parsed
    if not keys:
        return Verdict(NO_PINNED_KEY, key_id)
    encoded = keys.get(key_id)
    if not encoded:
        return Verdict(UNKNOWN_KEY, key_id)
    try:
        _, public_cls, invalid = _ed25519()
    except ImportError:
        return Verdict(UNAVAILABLE, key_id)
    try:
        public_raw = base64.b64decode(encoded, validate=True)
        # Таблица ключей, где id не соответствует ключу, — ошибка сборки; такой
        # ключ не считается закреплённым.
        if key_id_for(public_raw) != key_id:
            return Verdict(UNKNOWN_KEY, key_id)
        public_cls.from_public_bytes(public_raw).verify(sig, manifest)
    except invalid:
        return Verdict(BAD_SIGNATURE, key_id)
    except (ValueError, TypeError, binascii.Error):
        return Verdict(UNKNOWN_KEY, key_id)
    return Verdict(VALID, key_id)


def mode(channel_config: Mapping[str, Any] | None = None) -> str:
    """"enforce" or "warn". Every source can only make it stricter."""
    if ENFORCE:
        return "enforce"
    if (os.environ.get(ENFORCE_ENV) or "").strip().lower() == "enforce":
        return "enforce"
    if channel_config and channel_config.get("signatures") == "enforce":
        return "enforce"
    return "warn"


_MESSAGES = {
    UNSIGNED: "The release has no publisher signature.",
    NO_PINNED_KEY: "This build has no pinned publisher key to check the release signature.",
    UNKNOWN_KEY: "The release is signed by an unknown publisher key.",
    BAD_SIGNATURE: "The release signature does not match its release.json.",
    MALFORMED: "The release signature file is not valid.",
    UNAVAILABLE: "This build cannot check release signatures.",
}


def decide(verdict: Verdict, current_mode: str) -> tuple[bool, str]:
    """(allowed, message). The message is for the manager and the log."""
    if verdict.valid:
        return True, ""
    message = _MESSAGES.get(verdict.status, "The release signature could not be checked.")
    if current_mode != "enforce" and verdict.status in _WARN_PASSES:
        return True, message
    return False, message + " Nothing was installed."


_reported: set[tuple[str, str, str]] = set()


def report(verdict: Verdict, current_mode: str, version: str) -> None:
    """Log and send a telemetry event for anything but a valid signature, once per process."""
    if verdict.valid:
        return
    key = (verdict.status, current_mode, version)
    if key in _reported:
        return
    _reported.add(key)
    log.warning("release signature %s (mode=%s, version=%s, key=%s)",
                verdict.status, current_mode, version, verdict.key_id or "-")
    try:
        from src import acl_sentry
        if not acl_sentry.init():
            return
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.fingerprint = ["acl-update-trust", verdict.status, current_mode]
            scope.set_tag("kind", "update_trust")
            scope.set_tag("signature", verdict.status)
            scope.set_tag("signature_mode", current_mode)
            scope.set_context("release", {"version": version, "key_id": verdict.key_id})
            sentry_sdk.capture_message(f"Release signature {verdict.status} ({current_mode})",
                                       level="warning")
    except Exception:  # noqa: BLE001 — телеметрия не должна мешать проверке
        log.debug("update trust telemetry failed", exc_info=True)


# ── подпись (сторона CI) ────────────────────────────────────────────────

def load_private_key(text: str):
    """Ed25519 private key from PEM (PKCS#8) or base64 of the 32-byte seed."""
    private_cls, _, _ = _ed25519()
    from cryptography.hazmat.primitives import serialization

    text = (text or "").strip()
    if not text:
        raise SigningError(f"{SIGNING_KEY_ENV} is empty")
    try:
        if text.startswith("-----BEGIN"):
            key = serialization.load_pem_private_key(text.encode("ascii"), password=None)
        else:
            key = private_cls.from_private_bytes(base64.b64decode(text, validate=True))
    except (ValueError, TypeError, binascii.Error) as exc:
        raise SigningError(f"{SIGNING_KEY_ENV} is not an Ed25519 private key") from exc
    if not isinstance(key, private_cls):
        raise SigningError(f"{SIGNING_KEY_ENV} is not an Ed25519 private key")
    return key


def public_key_raw(private_key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def pinned_entry(private_key) -> tuple[str, str]:
    """(key_id, base64 public key) — the line to add to PINNED_PUBLIC_KEYS."""
    raw = public_key_raw(private_key)
    return key_id_for(raw), base64.b64encode(raw).decode("ascii")


def sign_manifest(manifest: bytes, private_key) -> bytes:
    key_id, _ = pinned_entry(private_key)
    doc = {
        "schema": SIGNATURE_SCHEMA,
        "alg": ALGORITHM,
        "key_id": key_id,
        "signature": base64.b64encode(private_key.sign(manifest)).decode("ascii"),
    }
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")
