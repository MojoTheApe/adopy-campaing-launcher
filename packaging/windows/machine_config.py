"""Validated, atomic machine-local configuration for the Windows installer."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SHEET_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,200}\Z")
SAFETY_ENV = {
    "FB_PUBLISH_BACKEND": "rpa",
    "FB_RPA_ENGINE": "recorded_flow",
    "FB_BROWSER_PROVIDER": "multilogin_attach",
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


class MachineConfigError(ValueError):
    """Machine configuration is incomplete, invalid, or unsafe."""


@dataclass(frozen=True)
class AccountAssignment:
    account_name: str
    ad_account_id: str = ""
    page_id: str = ""
    multilogin_instance: str = "A"
    folder_id: str = ""
    profile_id: str = ""


@dataclass(frozen=True)
class MachineConfiguration:
    campaign_manager_name: str
    accounts_sheet_id: str
    accounts: tuple[AccountAssignment, ...]
    oauth_client: bytes


def extract_sheet_id(value: str) -> str:
    value = (value or "").strip()
    match = re.search(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    sheet_id = match.group(1) if match else value
    if not SHEET_ID_PATTERN.fullmatch(sheet_id):
        raise MachineConfigError("Enter a valid company Google Sheet link.")
    if any(marker in sheet_id.casefold() for marker in ("example", "replace_with")):
        raise MachineConfigError("Replace the example Google Sheet link with the company link.")
    return sheet_id


def validate_oauth_client(raw: bytes) -> None:
    try:
        document = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise MachineConfigError(f"Google access file is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise MachineConfigError("Google access file must contain a JSON object")
    credential = document.get("installed") or document.get("web")
    if not isinstance(credential, dict):
        raise MachineConfigError("Google access file must contain installed or web credentials")
    for key in ("client_id", "auth_uri", "token_uri"):
        if not isinstance(credential.get(key), str) or not credential[key].strip():
            raise MachineConfigError(f"Google access file is missing {key}")
    if "token" in document or "refresh_token" in document:
        raise MachineConfigError("Google access file must not contain a user token")


def _dotenv_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def merge_env(existing: str, values: dict[str, str]) -> str:
    pending = dict(values)
    output: list[str] = []
    for line in existing.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match.group(1) if match else None
        if key in values:
            if key in pending:
                output.append(f"{key}={_dotenv_value(pending.pop(key))}")
            # Drop every duplicate of an updated key, including export/spacing.
        else:
            output.append(line)
    if pending and output and output[-1].strip():
        output.append("")
    output.extend(f"{key}={_dotenv_value(value)}" for key, value in pending.items())
    return "\n".join(output).rstrip() + "\n"


def _account_map_bytes(configuration: MachineConfiguration, existing: bytes = b"") -> bytes:
    rows = []
    columns = list(ACCOUNT_COLUMNS)
    if existing:
        reader = csv.DictReader(io.StringIO(existing.decode("utf-8-sig")))
        if not reader.fieldnames or "account_name" not in reader.fieldnames:
            raise MachineConfigError("Existing account map is invalid; restore it before setup.")
        columns += [k for k in reader.fieldnames if k not in columns]
        rows = list(reader)
        if any(None in row or any(v is None for v in row.values()) for row in rows):
            raise MachineConfigError("Existing account map has malformed rows; it was not changed.")
    by_name = {row["account_name"].strip().casefold(): row for row in rows}
    if len(by_name) != len(rows):
        raise MachineConfigError("Existing account map has duplicate names; resolve them before setup.")
    for assignment in configuration.accounts:
        key = assignment.account_name.strip().casefold()
        old = by_name.get(key)
        if old is not None:
            previous_id = old.get("ad_account_id", "").strip()
            if previous_id and assignment.ad_account_id and previous_id != assignment.ad_account_id:
                raise MachineConfigError(f"Account ID changed for {assignment.account_name}. Check the mapping in ACL before setup.")
            if not previous_id:
                old["ad_account_id"] = assignment.ad_account_id
            # Existing profile, instance, page, proxy and additional columns survive.
            continue
        row = {"account_name": assignment.account_name, "ad_account_id": assignment.ad_account_id,
               "page_id": assignment.page_id, "multilogin_instance": assignment.multilogin_instance,
               "folder_id": assignment.folder_id, "profile_id": assignment.profile_id}
        rows.append(row)
        by_name[key] = row
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _replace_transaction(outputs: dict[Path, bytes]) -> None:
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


def apply_machine_configuration(
    configuration: MachineConfiguration,
    config_directory: str | Path,
    *,
    stats_workbook: str,
    upload_workbook: str,
    creatives_directory: str,
    multilogin_tokens: dict[str, str] | None = None,
    google_token: bytes | None = None,
) -> None:
    validate_oauth_client(configuration.oauth_client)
    extract_sheet_id(configuration.accounts_sheet_id)
    config_dir = Path(config_directory)
    client_path = config_dir / "oauth_client.json"
    if client_path.exists() and (config_dir / "token_telemetry.json").exists():
        old = json.loads(client_path.read_text(encoding="utf-8"))
        new = json.loads(configuration.oauth_client)
        old_id = (old.get("installed") or old.get("web") or {}).get("client_id")
        new_id = (new.get("installed") or new.get("web") or {}).get("client_id")
        if old_id != new_id:
            raise MachineConfigError("Use the existing Google access client while shared-write access is configured. Contact support to migrate credentials.")
    map_path = config_dir / "ad_account_map.csv"
    existing_map = map_path.read_bytes() if map_path.exists() else b""
    env_path = config_dir / ".env"
    existing_env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    env_values = {
        "MY_CM_NAME": configuration.campaign_manager_name,
        "GSHEET_ACCOUNTS_ID": configuration.accounts_sheet_id,
        "GSHEET_ACCOUNTS_SHEET": "auto_last",
        "FB_STATS_EXCEL": str(Path(stats_workbook)),
        "UPLOAD_FB_EXCEL": str(Path(upload_workbook)),
        "FB_CREATIVES_ROOT": str(Path(creatives_directory)),
        **SAFETY_ENV,
    }
    for instance, token in (multilogin_tokens or {}).items():
        normalized = instance.strip().upper()
        if normalized not in {"A", "B"}:
            raise MachineConfigError(f"unsupported Multilogin instance: {instance}")
        if token.strip():
            env_values[f"MULTILOGIN_AUTOMATION_TOKEN_{normalized}"] = token.strip()
    outputs = {
        env_path: merge_env(existing_env, env_values).encode("utf-8"),
        map_path: _account_map_bytes(configuration, existing_map),
        client_path: configuration.oauth_client,
    }
    if google_token is not None:
        outputs[config_dir / "token_accounts.json"] = google_token
    _replace_transaction(outputs)
