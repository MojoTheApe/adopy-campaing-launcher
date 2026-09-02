"""Validation and atomic import for campaign-manager ``.aclsetup`` packs."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_PACK_BYTES = 2 * 1024 * 1024
SETUP_DOCUMENT = "setup.json"
OAUTH_DOCUMENT = "oauth_client.json"
ALLOWED_ARCHIVE_FILES = {SETUP_DOCUMENT, OAUTH_DOCUMENT}
SHEET_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,200}\Z")
OBJECT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
AD_ACCOUNT_PATTERN = re.compile(r"act_\d{10,20}\Z")
PAGE_ID_PATTERN = re.compile(r"\d+\Z")
PLACEHOLDER_MARKERS = (
    "assigned account",
    "campaign manager name",
    "example",
    "placeholder",
    "your-name",
    "your_name",
    "your-profile",
    "your-folder",
    "replace_with",
    "1234567890",
)
SAFETY_ENV = {
    "FB_PUBLISH_BACKEND": "rpa",
    "FB_RPA_ENGINE": "recorded_flow",
    "FB_MLX_ATTACH_ONLY": "1",
}
ACCOUNT_COLUMNS = (
    "account_name",
    "ad_account_id",
    "page_id",
    "multilogin_instance",
    "folder_id",
    "profile_id",
    "proxy_type",
    "proxy_host",
    "proxy_port",
    "proxy_user",
    "proxy_pass",
)


class SetupPackError(ValueError):
    """A setup pack is malformed, unsafe, or unsupported."""


@dataclass(frozen=True)
class AccountAssignment:
    account_name: str
    ad_account_id: str
    page_id: str
    multilogin_instance: str
    folder_id: str
    profile_id: str


@dataclass(frozen=True)
class SetupPack:
    campaign_manager_name: str
    accounts_sheet_id: str
    accounts: tuple[AccountAssignment, ...]
    oauth_client: bytes | None


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SetupPackError(f"{label} must be a non-empty string")
    return value.strip()


def _reject_placeholder(value: str, label: str) -> None:
    lowered = value.casefold()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        raise SetupPackError(f"{label} still contains an example value")


def extract_sheet_id(value: str) -> str:
    value = value.strip()
    match = re.search(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    sheet_id = match.group(1) if match else value
    if not SHEET_ID_PATTERN.fullmatch(sheet_id):
        raise SetupPackError("campaign_manager.accounts_sheet is not a valid Google Sheet ID or URL")
    _reject_placeholder(sheet_id, "campaign_manager.accounts_sheet")
    return sheet_id


def _strict_object(value: Any, allowed: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SetupPackError(f"{label} must be an object")
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise SetupPackError(f"{label} contains unexpected fields: {', '.join(unexpected)}")
    return value


def _validate_oauth(raw: bytes) -> None:
    try:
        document = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise SetupPackError(f"oauth_client.json is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise SetupPackError("oauth_client.json must contain a JSON object")
    credential = document.get("installed") or document.get("web")
    if not isinstance(credential, dict):
        raise SetupPackError("oauth_client.json must contain installed or web credentials")
    for key in ("client_id", "auth_uri", "token_uri"):
        if not isinstance(credential.get(key), str) or not credential[key].strip():
            raise SetupPackError(f"oauth_client.json is missing {key}")
    if "token" in document or "refresh_token" in document:
        raise SetupPackError("oauth_client.json must not contain a Google user token")


def validate_setup_document(document: Any, oauth_client: bytes | None = None) -> SetupPack:
    root = _strict_object(
        document,
        {"schema", "campaign_manager", "accounts", "oauth_client_file"},
        "setup.json",
    )
    if root.get("schema") != 1:
        raise SetupPackError("setup.json schema must be 1")

    manager = _strict_object(
        root.get("campaign_manager"),
        {"name", "accounts_sheet"},
        "campaign_manager",
    )
    name = _text(manager.get("name"), "campaign_manager.name")
    _reject_placeholder(name, "campaign_manager.name")
    sheet_id = extract_sheet_id(_text(manager.get("accounts_sheet"), "campaign_manager.accounts_sheet"))

    raw_accounts = root.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise SetupPackError("accounts must contain at least one assignment")
    assignments: list[AccountAssignment] = []
    seen_names: set[str] = set()
    seen_ad_accounts: set[str] = set()
    allowed_account_fields = {
        "account_name",
        "ad_account_id",
        "page_id",
        "multilogin_instance",
        "folder_id",
        "profile_id",
    }
    for index, raw_account in enumerate(raw_accounts, start=1):
        account = _strict_object(raw_account, allowed_account_fields, f"accounts[{index}]")
        account_name = _text(account.get("account_name"), f"accounts[{index}].account_name")
        ad_account_id = _text(account.get("ad_account_id"), f"accounts[{index}].ad_account_id")
        page_id = str(account.get("page_id") or "").strip()
        instance = _text(account.get("multilogin_instance"), f"accounts[{index}].multilogin_instance").upper()
        folder_id = _text(account.get("folder_id"), f"accounts[{index}].folder_id")
        profile_id = _text(account.get("profile_id"), f"accounts[{index}].profile_id")

        for value, label in (
            (account_name, "account_name"),
            (ad_account_id, "ad_account_id"),
            (folder_id, "folder_id"),
            (profile_id, "profile_id"),
        ):
            _reject_placeholder(value, f"accounts[{index}].{label}")
        if not AD_ACCOUNT_PATTERN.fullmatch(ad_account_id):
            raise SetupPackError(f"accounts[{index}].ad_account_id must be act_ followed by digits")
        if page_id and not PAGE_ID_PATTERN.fullmatch(page_id):
            raise SetupPackError(f"accounts[{index}].page_id must contain digits only")
        if instance not in {"A", "B"}:
            raise SetupPackError(f"accounts[{index}].multilogin_instance must be A or B")
        if not OBJECT_ID_PATTERN.fullmatch(folder_id) or not OBJECT_ID_PATTERN.fullmatch(profile_id):
            raise SetupPackError(f"accounts[{index}] has an invalid Multilogin folder/profile ID")

        normalized_name = account_name.casefold()
        if normalized_name in seen_names:
            raise SetupPackError(f"duplicate account label: {account_name}")
        if ad_account_id in seen_ad_accounts:
            raise SetupPackError(f"duplicate ad account ID: {ad_account_id}")
        seen_names.add(normalized_name)
        seen_ad_accounts.add(ad_account_id)
        assignments.append(
            AccountAssignment(
                account_name=account_name,
                ad_account_id=ad_account_id,
                page_id=page_id,
                multilogin_instance=instance,
                folder_id=folder_id,
                profile_id=profile_id,
            )
        )

    oauth_filename = root.get("oauth_client_file")
    if oauth_filename not in (None, OAUTH_DOCUMENT):
        raise SetupPackError(f"oauth_client_file must be {OAUTH_DOCUMENT!r} when present")
    if oauth_filename and oauth_client is None:
        raise SetupPackError(f"{OAUTH_DOCUMENT} is declared but missing from the archive")
    if not oauth_filename and oauth_client is not None:
        raise SetupPackError(f"{OAUTH_DOCUMENT} is present but not declared in setup.json")
    if oauth_client is not None:
        _validate_oauth(oauth_client)

    return SetupPack(name, sheet_id, tuple(assignments), oauth_client)


def load_setup_pack(path: str | Path) -> SetupPack:
    pack_path = Path(path)
    if pack_path.suffix.casefold() != ".aclsetup":
        raise SetupPackError("setup pack must use the .aclsetup extension")
    if not pack_path.is_file():
        raise SetupPackError(f"setup pack not found: {pack_path}")
    if pack_path.stat().st_size > MAX_PACK_BYTES:
        raise SetupPackError("setup pack is larger than the 2 MB safety limit")

    try:
        with zipfile.ZipFile(pack_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise SetupPackError("setup pack contains duplicate archive entries")
            if any(Path(name).name != name for name in names):
                raise SetupPackError("setup pack contains a directory or unsafe path")
            unexpected = sorted(set(names) - ALLOWED_ARCHIVE_FILES)
            if unexpected:
                raise SetupPackError(f"setup pack contains unexpected files: {', '.join(unexpected)}")
            if SETUP_DOCUMENT not in names:
                raise SetupPackError(f"setup pack is missing {SETUP_DOCUMENT}")
            if sum(item.file_size for item in archive.infolist()) > MAX_PACK_BYTES:
                raise SetupPackError("expanded setup pack is larger than the 2 MB safety limit")
            setup_raw = archive.read(SETUP_DOCUMENT)
            oauth_raw = archive.read(OAUTH_DOCUMENT) if OAUTH_DOCUMENT in names else None
    except SetupPackError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise SetupPackError(f"setup pack is not a readable ZIP archive: {exc}") from exc

    try:
        document = json.loads(setup_raw.decode("utf-8"))
    except Exception as exc:
        raise SetupPackError(f"setup.json is not valid UTF-8 JSON: {exc}") from exc
    return validate_setup_document(document, oauth_raw)


def _dotenv_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def merge_env(existing: str, values: dict[str, str]) -> str:
    pending = dict(values)
    output: list[str] = []
    for line in existing.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match and match.group(1) in pending:
            key = match.group(1)
            output.append(f"{key}={_dotenv_value(pending.pop(key))}")
        else:
            output.append(line)
    if pending and output and output[-1].strip():
        output.append("")
    output.extend(f"{key}={_dotenv_value(value)}" for key, value in pending.items())
    return "\n".join(output).rstrip() + "\n"


def _account_map_bytes(pack: SetupPack) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=ACCOUNT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for assignment in pack.accounts:
        writer.writerow(
            {
                "account_name": assignment.account_name,
                "ad_account_id": assignment.ad_account_id,
                "page_id": assignment.page_id,
                "multilogin_instance": assignment.multilogin_instance,
                "folder_id": assignment.folder_id,
                "profile_id": assignment.profile_id,
            }
        )
    return stream.getvalue().encode("utf-8")


def _replace_transaction(outputs: dict[Path, bytes]) -> None:
    """Replace several machine files with rollback if any commit step fails."""
    previous = {path: path.read_bytes() if path.exists() else None for path in outputs}
    staged: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for target, content in outputs.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            temp = Path(temp_name)
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            staged[target] = temp
        for target, temp in staged.items():
            os.replace(temp, target)
            committed.append(target)
    except Exception:
        for target in reversed(committed):
            old = previous[target]
            if old is None:
                target.unlink(missing_ok=True)
            else:
                handle, restore_name = tempfile.mkstemp(
                    prefix=f".{target.name}.restore.", suffix=".tmp", dir=target.parent
                )
                with os.fdopen(handle, "wb") as stream:
                    stream.write(old)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(restore_name, target)
        raise
    finally:
        for temp in staged.values():
            temp.unlink(missing_ok=True)


def apply_setup_pack(
    pack: SetupPack,
    config_directory: str | Path,
    *,
    stats_workbook: str,
    upload_workbook: str,
    creatives_directory: str,
    multilogin_tokens: dict[str, str] | None = None,
) -> None:
    config_dir = Path(config_directory)
    env_path = config_dir / ".env"
    existing_env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    env_values = {
        "MY_CM_NAME": pack.campaign_manager_name,
        "GSHEET_ACCOUNTS_ID": pack.accounts_sheet_id,
        "GSHEET_ACCOUNTS_SHEET": "auto_last",
        "FB_STATS_EXCEL": str(Path(stats_workbook)),
        "UPLOAD_FB_EXCEL": str(Path(upload_workbook)),
        "FB_CREATIVES_ROOT": str(Path(creatives_directory)),
        **SAFETY_ENV,
    }
    for instance, token in (multilogin_tokens or {}).items():
        normalized = instance.strip().upper()
        if normalized not in {"A", "B"}:
            raise SetupPackError(f"unsupported Multilogin instance: {instance}")
        if token.strip():
            env_values[f"MULTILOGIN_AUTOMATION_TOKEN_{normalized}"] = token.strip()
    outputs = {
        env_path: merge_env(existing_env, env_values).encode("utf-8"),
        config_dir / "ad_account_map.csv": _account_map_bytes(pack),
    }
    if pack.oauth_client is not None:
        outputs[config_dir / OAUTH_DOCUMENT] = pack.oauth_client
    _replace_transaction(outputs)
