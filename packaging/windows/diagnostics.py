"""Create a bounded, sanitized support bundle without configuration secrets."""
from __future__ import annotations

import csv
import json
import os
import platform
import re
import sys
import tempfile
import zipfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MAX_SOURCE_FILE_BYTES = 256 * 1024
MAX_JSON_SECRET_SOURCE_BYTES = 2 * 1024 * 1024
MAX_LOG_FILES = 12
GENERIC_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----.*?-----END (?:RSA |EC )?PRIVATE KEY-----", re.S),
        "<redacted-private-key>",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{12,}"), "Bearer <redacted>"),
    (
        re.compile(r"(?i)\b(token|secret|password|api[_-]?key)\b\s*[:=]\s*[^\s,;]+"),
        r"\1=<redacted>",
    ),
    (re.compile(r"(?i)([\"']?(?:access_token|refresh_token|client_secret|password|proxy_pass|api[_-]?key|token|secret)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"), r"\1<redacted>"),
    (re.compile(r"\bact_\d{8,}\b", re.I), "act_<redacted>"),
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
        "<redacted-uuid>",
    ),
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I), "<redacted-email>"),
    (
        re.compile(r"https://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]+", re.I),
        "https://docs.google.com/spreadsheets/d/<redacted>",
    ),
    (re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+"), r"C:\\Users\\<redacted-user>"),
)
PROHIBITED_AFTER_SANITIZE: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
    re.compile(r"\bact_\d{8,}\b", re.I),
    re.compile(r"(?i)\bBearer\s+(?!<redacted>)[A-Za-z0-9._~-]{12,}"),
    re.compile(r"(?i)\b(?:refresh_token|client_secret)\b\s*[:=]\s*(?!<redacted>)\S+"),
)


def _read_bounded(path: Path, limit: int = MAX_SOURCE_FILE_BYTES, *, local_values: Iterable[str] = ()) -> str:
    values = [value.encode("utf-8") for value in local_values if value]
    overlap = max((len(value) - 1 for value in values), default=0)
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - limit - overlap))
        raw = stream.read(limit + overlap)
    start = max(0, len(raw) - limit)
    crossing = []
    for value in values:
        at = raw.find(value, max(0, start - len(value) + 1), start + len(value) - 1)
        if 0 <= at < start < at + len(value):
            crossing.append((at, at + len(value)))
    # Keep byte offsets stable: shrinking a replacement could pull uninspected
    # preceding fragments into the tail. Mask crossings, then retain the old cap.
    for left, right in crossing:
        raw = raw[:left] + b"*" * (right - left) + raw[right:]
    return raw[start:].decode("utf-8", errors="replace")


def _env_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
            if match:
                entries[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    except Exception:
        pass
    return entries


def _json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


def local_values_to_redact(config_directory: str | Path) -> set[str]:
    """Read sensitive local sources only to build a denylist; never export them."""
    config_dir = Path(config_directory)
    values = {value for value in _env_entries(config_dir / ".env").values() if len(value) >= 4}
    # Tokens kept in protected storage: the stored form is ciphertext, so the
    # denylist must also hold the plain value (opened here, never exported).
    try:
        from config import token_vault

        for stored in list(values):
            if token_vault.is_protected_value(stored):
                try:
                    values.add(token_vault.decode_env_value(stored))
                except Exception:
                    pass
    except Exception:
        token_vault = None
    map_path = config_dir / "ad_account_map.csv"
    try:
        with map_path.open(newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                values.update(str(value).strip() for value in row.values() if value and len(str(value).strip()) >= 4)
    except Exception:
        pass
    for name in ("oauth_client.json", "token_accounts.json", "token_telemetry.json"):
        path = config_dir / name
        try:
            if path.stat().st_size > MAX_JSON_SECRET_SOURCE_BYTES:
                continue
            if token_vault is not None:
                text = token_vault.read_secret_text(path)
            else:
                text = path.read_text(encoding="utf-8")
            document = json.loads(text)
            values.update(value for value in _json_strings(document) if len(value) >= 8)
        except Exception:
            pass
    return values


def sanitize_text(text: str, local_values: Iterable[str] = ()) -> str:
    for value in sorted(set(local_values), key=len, reverse=True):
        text = text.replace(value, "<redacted-local-value>")
    for pattern, replacement in GENERIC_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def assert_sanitized(text: str) -> None:
    for pattern in PROHIBITED_AFTER_SANITIZE:
        if pattern.search(text):
            raise RuntimeError(f"diagnostic sanitizer left prohibited data matching {pattern.pattern!r}")


def _account_map_summary(path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"present": path.is_file(), "rows": 0, "complete_profiles": 0}
    try:
        with path.open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        summary["rows"] = len(rows)
        summary["complete_profiles"] = sum(
            bool((row.get("folder_id") or "").strip() and (row.get("profile_id") or "").strip())
            for row in rows
        )
    except Exception:
        summary["readable"] = False
    else:
        summary["readable"] = True
    return summary


def _safe_setup_state(path: Path) -> dict[str, Any]:
    allowed = {"schema", "status", "completed_at_utc", "installer_version", "automatic_updates"}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return {key: state[key] for key in allowed if key in state}
    except Exception:
        return {"present": path.is_file(), "readable": False}


def build_summary(config_directory: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    config_dir = Path(config_directory)
    env = _env_entries(config_dir / ".env")
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception:
        manifest = {"unavailable": True}
    return {
        "schema": 1,
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "application": {
            key: manifest.get(key)
            for key in ("product", "version", "core_baseline_sha", "installer_source_sha", "built_at_utc")
        },
        "runtime": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "frozen": bool(getattr(sys, "frozen", False)),
        },
        "configuration": {
            "env_present": (config_dir / ".env").is_file(),
            "oauth_client_present": (config_dir / "oauth_client.json").is_file(),
            "google_token_present": (config_dir / "token_accounts.json").is_file(),
            "multilogin_token_a_present": bool(env.get("MULTILOGIN_AUTOMATION_TOKEN_A")),
            "multilogin_token_b_present": bool(env.get("MULTILOGIN_AUTOMATION_TOKEN_B")),
            "stats_path_configured": bool(env.get("FB_STATS_EXCEL")),
            "upload_path_configured": bool(env.get("UPLOAD_FB_EXCEL")),
            "creatives_path_configured": bool(env.get("FB_CREATIVES_ROOT")),
            "safety_backend_ok": env.get("FB_PUBLISH_BACKEND", "rpa") == "rpa",
            "safety_engine_ok": env.get("FB_RPA_ENGINE") == "recorded_flow",
            "account_map": _account_map_summary(config_dir / "ad_account_map.csv"),
            "account_name_rules": _account_name_rules_summary(),
            "setup": _safe_setup_state(config_dir / "setup-state.json"),
        },
    }


def _account_name_rules_summary() -> dict[str, Any]:
    """ACL-2138: is the bundled account-name rules file present and valid? Never raises."""
    try:
        from src.analysis_agent import account_names

        status = account_names.rules_status()
        return {"ok": status["ok"], "rules": status["rules"], "error": status["error"]}
    except Exception as exc:
        return {"ok": False, "rules": 0, "error": f"{type(exc).__name__}: {exc}"}


def _sanitize_document(value: Any, local_values: Iterable[str]) -> Any:
    if isinstance(value, str):
        return sanitize_text(value, local_values)
    if isinstance(value, dict):
        return {key: _sanitize_document(item, local_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_document(item, local_values) for item in value]
    return value


def export_diagnostics(config_directory: str | Path, manifest_path: str | Path) -> Path:
    config_dir = Path(config_directory)
    output_dir = config_dir / "diagnostics"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = output_dir / f"ACL-Diagnostics-{timestamp}-{uuid.uuid4().hex[:8]}.zip"
    handle, temp_name = tempfile.mkstemp(prefix=".acl-diagnostics-", suffix=".zip", dir=output_dir)
    os.close(handle)
    temp = Path(temp_name)
    local_values = local_values_to_redact(config_dir)
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            summary_text = json.dumps(_sanitize_document(build_summary(config_dir, manifest_path), local_values), indent=2, sort_keys=True) + "\n"
            assert_sanitized(summary_text)
            archive.writestr("summary.json", summary_text)
            log_dir = config_dir / "logs"
            logs = sorted(
                (path for path in log_dir.glob("*.log") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:MAX_LOG_FILES]
            for index, path in enumerate(logs, start=1):
                sanitized = sanitize_text(_read_bounded(path, local_values=local_values), local_values)
                assert_sanitized(sanitized)
                archive.writestr(f"logs/{index:02d}.txt", sanitized)
            archive.writestr(
                "README.txt",
                "Sanitized ACL diagnostics. Configuration, account mappings, OAuth files, and tokens are excluded.\n",
            )
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    return output
