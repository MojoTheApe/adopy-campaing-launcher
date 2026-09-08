"""First-run Windows Setup Wizard, intentionally separate from ACL operations."""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any

from machine_config import (
    AccountAssignment,
    MachineConfiguration,
    MachineConfigError,
    apply_machine_configuration,
    extract_sheet_id,
    validate_oauth_client,
)


STATS_COLUMNS = {
    "Country",
    "Account Alias",
    "Adset Name",
    "Ad Title",
    "# Leads",
    "# FTDs",
    "$ Spend",
    "CM",
    "Ad Asset",
    "$ Net",
}
UPLOAD_SHEETS = {"Accounts", "Ad copy", "Countries", "Interests", "Upload all"}


class WizardValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReadinessCheck:
    label: str
    ok: bool
    detail: str


def setup_state_path(config_directory: str | Path) -> Path:
    return Path(config_directory) / "setup-state.json"


def setup_is_complete(config_directory: str | Path) -> bool:
    config_dir = Path(config_directory)
    try:
        state = json.loads(setup_state_path(config_dir).read_text(encoding="utf-8"))
        return bool(
            state.get("schema") == 1
            and state.get("status") == "ready"
            and (config_dir / ".env").is_file()
            and (config_dir / "ad_account_map.csv").is_file()
        )
    except Exception:
        return False


def validate_stats_workbook(path: str) -> str:
    workbook_path = Path(path)
    if not workbook_path.is_file():
        raise WizardValidationError("Stats workbook does not exist.")
    if workbook_path.suffix.casefold() not in {".xlsx", ".xlsm"}:
        raise WizardValidationError("Stats workbook must be an .xlsx or .xlsm file.")
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(workbook_path, read_only=True, data_only=True)
        sheet = workbook[workbook.sheetnames[0]]
        headers = {str(cell.value).strip() for cell in next(sheet.iter_rows(max_row=1)) if cell.value}
        workbook.close()
    except Exception as exc:
        raise WizardValidationError(f"Stats workbook cannot be opened: {exc}") from exc
    missing = sorted(STATS_COLUMNS - headers)
    if missing:
        raise WizardValidationError("Stats workbook is missing columns: " + ", ".join(missing))
    return str(workbook_path.resolve())


def validate_upload_workbook(path: str) -> str:
    workbook_path = Path(path)
    if not workbook_path.is_file():
        raise WizardValidationError("Upload FB workbook does not exist.")
    if workbook_path.suffix.casefold() not in {".xlsx", ".xlsm"}:
        raise WizardValidationError("Upload FB workbook must be an .xlsx or .xlsm file.")
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(workbook_path, read_only=True, data_only=True)
        names = set(workbook.sheetnames)
        workbook.close()
    except Exception as exc:
        raise WizardValidationError(f"Upload FB workbook cannot be opened: {exc}") from exc
    missing = sorted(UPLOAD_SHEETS - names)
    if missing:
        raise WizardValidationError("Upload FB workbook is missing sheets: " + ", ".join(missing))
    return str(workbook_path.resolve())


def validate_creatives_directory(path: str) -> str:
    directory = Path(path)
    if not directory.is_dir():
        raise WizardValidationError("Creatives folder does not exist.")
    if not os.access(directory, os.R_OK):
        raise WizardValidationError("Creatives folder is not readable.")
    return str(directory.resolve())


def validate_campaign_manager_name(value: str) -> str:
    name = (value or "").strip()
    if len(name) < 2:
        raise WizardValidationError("Enter your name exactly as it appears in the Stats workbook.")
    if len(name) > 120:
        raise WizardValidationError("Campaign manager name is too long.")
    if any(marker in name.casefold() for marker in ("campaign manager name", "your name", "example")):
        raise WizardValidationError("Replace the example campaign manager name with your own name.")
    return name


def validate_google_access_file(path: str) -> tuple[str, bytes]:
    client_path = Path(path)
    if not client_path.is_file():
        raise WizardValidationError("Choose the company Google access file supplied by support.")
    try:
        raw = client_path.read_bytes()
        validate_oauth_client(raw)
    except MachineConfigError as exc:
        raise WizardValidationError(f"The company Google access file is invalid: {exc}") from exc
    return str(client_path.resolve()), raw


def _normalise_ad_account_id(raw: str) -> str:
    import re

    text = (raw or "").strip()
    if not text:
        return ""
    match = re.search(r"act[=_](\d{6,})", text, re.IGNORECASE) or re.search(
        r"(?<!\d)(\d{8,})(?!\d)", text
    )
    return f"act_{match.group(1)}" if match else ""


def build_manual_setup(
    *,
    campaign_manager_name: str,
    accounts_sheet: str,
    oauth_client_path: str,
    stats_workbook: str,
    upload_workbook: str,
) -> MachineConfiguration:
    """Build machine configuration from fields entered in the setup wizard.

    Account labels come from the Stats workbook and ad-account IDs are filled
    from the Upload FB workbook when available. Multilogin profile connections
    remain blank intentionally: ACL's Accounts screen lets the user connect an
    open, human-readable profile without ever typing a UUID.
    """
    from openpyxl import load_workbook

    name = validate_campaign_manager_name(campaign_manager_name)
    try:
        sheet_id = extract_sheet_id(accounts_sheet)
    except MachineConfigError as exc:
        raise WizardValidationError(str(exc)) from exc
    _oauth_path, oauth_raw = validate_google_access_file(oauth_client_path)

    workbook = load_workbook(stats_workbook, read_only=True, data_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        rows = sheet.iter_rows(values_only=True)
        headers = [str(value or "").strip() for value in next(rows)]
        by_name = {header.casefold(): index for index, header in enumerate(headers)}
        if "cm" not in by_name or "account alias" not in by_name:
            raise WizardValidationError("Stats workbook is missing CM or Account Alias columns.")
        cm_index = by_name["cm"]
        account_index = by_name["account alias"]
        aliases: dict[str, str] = {}
        for row in rows:
            manager = str(row[cm_index] or "").strip() if cm_index < len(row) else ""
            alias = str(row[account_index] or "").strip() if account_index < len(row) else ""
            if manager.casefold() == name.casefold() and alias:
                aliases.setdefault(alias.casefold(), alias)
    finally:
        workbook.close()

    if not aliases:
        raise WizardValidationError(
            "No accounts were found for this campaign manager in the Stats workbook. "
            "Check the name or ask support."
        )

    upload_book = load_workbook(upload_workbook, read_only=True, data_only=True)
    try:
        account_sheet = upload_book["Accounts"]
        upload_rows = account_sheet.iter_rows(values_only=True)
        upload_headers = [str(value or "").strip().casefold() for value in next(upload_rows)]
        if "account" not in upload_headers:
            raise WizardValidationError("Upload FB workbook Accounts sheet is missing the Account column.")
        account_col = upload_headers.index("account")
        ad_id_col = upload_headers.index("ad account id") if "ad account id" in upload_headers else None
        upload_ids: dict[str, str] = {}
        token_ids: dict[str, set[str]] = {}
        for row in upload_rows:
            label = str(row[account_col] or "").strip() if account_col < len(row) else ""
            raw_id = (
                str(row[ad_id_col] or "").strip()
                if ad_id_col is not None and ad_id_col < len(row)
                else ""
            )
            normalized_id = _normalise_ad_account_id(raw_id)
            if not label or not normalized_id:
                continue
            upload_ids[label.casefold()] = normalized_id
            token = label.split()[0].casefold()
            token_ids.setdefault(token, set()).add(normalized_id)
    finally:
        upload_book.close()

    assignments = []
    for alias in sorted(aliases.values(), key=str.casefold):
        ad_account_id = upload_ids.get(alias.casefold(), "")
        if not ad_account_id:
            matches = token_ids.get(alias.split()[0].casefold(), set())
            ad_account_id = next(iter(matches)) if len(matches) == 1 else ""
        assignments.append(
            AccountAssignment(
                account_name=alias,
                ad_account_id=ad_account_id,
                page_id="",
                multilogin_instance="A",
                folder_id="",
                profile_id="",
            )
        )
    return MachineConfiguration(name, sheet_id, tuple(assignments), oauth_raw)


def system_readiness(config_directory: str | Path, *, port: int = 9090) -> list[ReadinessCheck]:
    config_dir = Path(config_directory)
    checks: list[ReadinessCheck] = []
    windows_x64 = platform.system() == "Windows" and platform.machine().casefold() in {
        "amd64",
        "x86_64",
    }
    checks.append(ReadinessCheck("Windows x64", windows_x64, platform.platform()))

    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=".acl-write-test-", dir=config_dir)
        os.close(handle)
        Path(temp_name).unlink()
        writable = True
        writable_detail = str(config_dir)
    except Exception as exc:
        writable = False
        writable_detail = str(exc)
    checks.append(ReadinessCheck("Writable ACL data folder", writable, writable_detail))

    free_bytes = shutil.disk_usage(config_dir).free if config_dir.exists() else 0
    checks.append(
        ReadinessCheck(
            "Free disk space",
            free_bytes >= 500 * 1024 * 1024,
            f"{free_bytes // (1024 * 1024)} MB available",
        )
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        port_free = sock.connect_ex(("127.0.0.1", port)) != 0
    checks.append(
        ReadinessCheck(
            "Local dashboard port",
            port_free,
            f"port {port} is {'available' if port_free else 'already in use'}",
        )
    )
    return checks


@dataclass(frozen=True)
class GoogleReadCheck:
    message: str
    token: bytes


def test_google_read_access(configuration: MachineConfiguration, config_directory: str | Path) -> GoogleReadCheck:
    """Verify in a temporary workspace; cancellation/failure preserves old tokens."""
    config_dir = Path(config_directory)
    try:
        from src.analysis_agent.gsheet_accounts import _build_sheets_service_oauth
        with tempfile.TemporaryDirectory(prefix="acl-setup-oauth-") as raw:
            staging = Path(raw)
            client = staging / "oauth_client.json"
            token = staging / "token_accounts.json"
            client.write_bytes(configuration.oauth_client)
            incoming = json.loads(configuration.oauth_client)
            client_id = (incoming.get("installed") or incoming.get("web") or {}).get("client_id")
            previous_token = config_dir / "token_accounts.json"
            if previous_token.is_file():
                try:
                    saved = json.loads(previous_token.read_bytes())
                    if saved.get("client_id") == client_id:
                        token.write_bytes(previous_token.read_bytes())
                except (ValueError, OSError):
                    pass
            service = _build_sheets_service_oauth(str(client), str(token))
            if service is None:
                raise WizardValidationError("Google authorization did not complete.")
            service.spreadsheets().get(spreadsheetId=configuration.accounts_sheet_id, fields="spreadsheetId").execute()
            if not token.is_file():
                raise WizardValidationError("Google access was checked but the login token was not saved.")
            return GoogleReadCheck("Company Google Sheet is readable with read-only access.", token.read_bytes())
    except WizardValidationError:
        raise
    except Exception as exc:
        raise WizardValidationError(f"Google Sheet read failed: {exc}") from exc


def test_multilogin_access(tokens: dict[str, str]) -> str:
    """Validate at least one Multilogin automation token.

    Individual ad-account/profile connections are completed in ACL's Accounts
    screen after setup, where the user can choose an open profile by name.
    """
    import urllib.error
    import urllib.request

    provided = {instance: token.strip() for instance, token in tokens.items() if token.strip()}
    if not provided:
        raise WizardValidationError("Paste the Multilogin access code supplied by support.")
    verified = []
    for instance, token in sorted(provided.items()):
        try:
            request = urllib.request.Request(
                "https://api.multilogin.com/workspace/folders",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (OSError, urllib.error.URLError) as exc:
            raise WizardValidationError(
                "Cannot reach Multilogin Launcher. Make sure Multilogin is installed, running, and signed in."
            ) from exc
        if status == 401:
            raise WizardValidationError(f"Multilogin rejected token {instance}.")
        if status >= 400:
            raise WizardValidationError(f"Multilogin returned HTTP {status}.")
        verified.append(instance)
    return f"Multilogin connection verified ({', '.join(verified)})."


def write_ready_state(config_directory: str | Path, version: str) -> None:
    path = setup_state_path(config_directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "status": "ready",
        "completed_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "installer_version": version,
        "automatic_updates": False,
    }
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def sanitized_setup_summary(configuration: MachineConfiguration, version: str) -> str:
    """Return a support-safe completion summary with no identity or identifiers."""
    instances = sorted({assignment.multilogin_instance for assignment in configuration.accounts})
    return "\n".join(
        (
            "Adopy Campaign Launcher setup summary",
            f"Installer version: {version}",
            "Status: base setup ready; account connection continues in the dashboard",
            f"Assigned accounts: {len(configuration.accounts)} (labels and IDs omitted)",
            f"Multilogin instances: {', '.join(instances)}",
            "Google Sheet: read verified (ID omitted)",
            "Local workbooks and Creatives folder: validated (paths omitted)",
            "Safety mode: RPA / recorded_flow / attach-only",
            "Automatic updates: disabled",
            "Campaign actions during setup: none",
        )
    )


class SetupWizard:
    STEPS = (
        "Welcome",
        "System check",
        "Your workspace",
        "Local files",
        "Google access",
        "Multilogin",
        "Readiness",
        "Finish",
    )

    def __init__(self, root, config_directory: Path, version_text: str):
        import tkinter as tk

        self.tk = tk
        self.root = root
        self.config_directory = config_directory
        self.version_text = version_text
        self.step = 0
        self.configuration: MachineConfiguration | None = None
        self.google_ok = False
        self.google_token: bytes | None = None
        self._verified_google_configuration = None
        self._verified_tokens = None
        self.multilogin_ok = False
        self.completed = False
        self.busy = False
        self.campaign_manager_name = tk.StringVar(value=self._existing_env_value("MY_CM_NAME"))
        self.accounts_sheet = tk.StringVar(value=self._existing_env_value("GSHEET_ACCOUNTS_ID"))
        existing_oauth = self.config_directory / "oauth_client.json"
        self.oauth_client_path = tk.StringVar(value=str(existing_oauth) if existing_oauth.is_file() else "")
        self.stats_path = tk.StringVar(value=self._existing_env_value("FB_STATS_EXCEL"))
        self.upload_path = tk.StringVar(value=self._existing_env_value("UPLOAD_FB_EXCEL"))
        self.creatives_path = tk.StringVar(value=self._existing_env_value("FB_CREATIVES_ROOT"))
        self.token_a = tk.StringVar(value=self._existing_env_value("MULTILOGIN_AUTOMATION_TOKEN_A"))
        self.token_b = tk.StringVar(value=self._existing_env_value("MULTILOGIN_AUTOMATION_TOKEN_B"))
        self.status = tk.StringVar()

        root.title("Adopy Campaign Launcher Setup")
        root.geometry("780x590")
        root.minsize(720, 540)
        root.protocol("WM_DELETE_WINDOW", self._close)
        self.header = tk.Frame(root, bg="#172036", padx=24, pady=18)
        self.header.pack(fill="x")
        self.heading = tk.Label(
            self.header,
            fg="white",
            bg="#172036",
            font=("Segoe UI", 18, "bold"),
        )
        self.heading.pack(anchor="w")
        self.progress = tk.Label(self.header, fg="#a9bbdf", bg="#172036", font=("Segoe UI", 10))
        self.progress.pack(anchor="w", pady=(4, 0))
        self.body = tk.Frame(root, padx=28, pady=24)
        self.body.pack(fill="both", expand=True)
        self.footer = tk.Frame(root, padx=24, pady=16)
        self.footer.pack(fill="x")
        self.status_label = tk.Label(
            self.footer,
            textvariable=self.status,
            anchor="w",
            fg="#9b1c1c",
            wraplength=500,
            justify="left",
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        self.back_button = tk.Button(self.footer, text="Back", width=10, command=self._back)
        self.back_button.pack(side="right", padx=(8, 0))
        self.next_button = tk.Button(self.footer, text="Next", width=12, command=self._next)
        self.next_button.pack(side="right")
        for variable in (self.campaign_manager_name, self.accounts_sheet, self.oauth_client_path, self.stats_path, self.upload_path):
            variable.trace_add("write", self._invalidate_google)
        for variable in (self.token_a, self.token_b):
            variable.trace_add("write", self._invalidate_multilogin)
        self._render()

    def _invalidate_google(self, *_args) -> None:
        self.google_ok = False
        self.google_token = None

    def _invalidate_multilogin(self, *_args) -> None:
        self.multilogin_ok = False

    def _existing_env_value(self, key: str) -> str:
        path = self.config_directory / ".env"
        try:
            from dotenv import dotenv_values
            return str(dotenv_values(path, interpolate=False).get(key) or "")
        except Exception:
            return ""

    def _clear_body(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def _text(self, value: str, *, bold: bool = False, color: str = "#273248") -> None:
        self.tk.Label(
            self.body,
            text=value,
            anchor="w",
            justify="left",
            wraplength=700,
            fg=color,
            font=("Segoe UI", 11, "bold" if bold else "normal"),
        ).pack(fill="x", pady=(0, 12))

    def _render(self) -> None:
        self._clear_body()
        self.status.set("")
        self.heading.config(text=self.STEPS[self.step])
        self.progress.config(text=f"Step {self.step + 1} of {len(self.STEPS)}")
        self.back_button.config(state="normal" if 0 < self.step < 7 else "disabled")
        self.next_button.config(text="Close" if self.step == 7 else "Finish" if self.step == 6 else "Next")
        renderers: tuple[Callable[[], None], ...] = (
            self._welcome,
            self._system,
            self._identity,
            self._files,
            self._google,
            self._multilogin,
            self._readiness,
            self._finish,
        )
        renderers[self.step]()
        self.next_button.config(state="disabled" if self.busy else "normal")
        if self.busy:
            self.back_button.config(state="disabled")
            def disable(widget):
                for child in widget.winfo_children():
                    try:
                        child.configure(state="disabled")
                    except self.tk.TclError:
                        pass
                    disable(child)
            disable(self.body)

    def _welcome(self) -> None:
        self._text("Set up Adopy Campaign Launcher on this computer", bold=True)
        self._text(self.version_text)
        self._text(
            "This wizard only configures the local application. It cannot create, publish, or change a campaign."
        )
        self._text("Automatic software updates are disabled for this installer pilot.")

    def _system(self) -> None:
        self.system_checks = system_readiness(self.config_directory)
        self._text("The installer checks this computer automatically; Python and Git are not prerequisites.")
        for check in self.system_checks:
            self._text(
                f"{'✓' if check.ok else '✕'}  {check.label} — {check.detail}",
                color="#17663a" if check.ok else "#9b1c1c",
            )

    def _identity(self) -> None:
        from tkinter import filedialog

        self._text("Enter your own workspace details. The installer creates the technical settings for you.")
        self.tk.Label(self.body, text="Your name (as written in the Stats workbook)", anchor="w").pack(fill="x")
        self.tk.Entry(self.body, textvariable=self.campaign_manager_name).pack(fill="x", pady=(2, 12))
        self.tk.Label(self.body, text="Company Accounts Google Sheet link", anchor="w").pack(fill="x")
        self.tk.Entry(self.body, textvariable=self.accounts_sheet).pack(fill="x", pady=(2, 12))
        self.tk.Label(self.body, text="Company Google access file", anchor="w").pack(fill="x")
        row = self.tk.Frame(self.body)
        row.pack(fill="x", pady=(2, 12))
        self.tk.Entry(row, textvariable=self.oauth_client_path).pack(side="left", fill="x", expand=True)

        def choose() -> None:
            selected = filedialog.askopenfilename(filetypes=[("Google access file", "*.json")])
            if selected:
                self.oauth_client_path.set(selected)

        self.tk.Button(row, text="Browse…", command=choose).pack(side="left", padx=(8, 0))
        self._text("Choose the Google access file supplied by support. You do not need to open or edit it.")

    def _path_row(self, label: str, variable, directory: bool = False) -> None:
        from tkinter import filedialog

        self.tk.Label(self.body, text=label, anchor="w", font=("Segoe UI", 10, "bold")).pack(fill="x")
        row = self.tk.Frame(self.body)
        row.pack(fill="x", pady=(3, 12))
        self.tk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True)

        def choose() -> None:
            selected = (
                filedialog.askdirectory()
                if directory
                else filedialog.askopenfilename(filetypes=[("Excel workbook", "*.xlsx *.xlsm")])
            )
            if selected:
                variable.set(selected)

        self.tk.Button(row, text="Browse…", command=choose).pack(side="left", padx=(8, 0))

    def _files(self) -> None:
        self._text("Use the file pickers; no path needs to be typed or edited.")
        self._path_row("Stats workbook", self.stats_path)
        self._path_row("Upload FB workbook", self.upload_path)
        self._path_row("Creatives folder", self.creatives_path, directory=True)

    def _google(self) -> None:
        self._text("The launcher requests read-only Google Sheets access using the company access file.")
        self._text("Sign in only with the approved company Google account. The token remains on this computer.")
        if self.google_ok:
            self._text("✓ Google Sheet read verified", bold=True, color="#17663a")
        self.tk.Button(
            self.body,
            text="Connect Google and test Sheet",
            command=self._start_google_check,
            state="disabled" if self.busy else "normal",
        ).pack(anchor="w", pady=10)

    def _multilogin(self) -> None:
        self._text("Open Multilogin Desktop and sign in. Paste only the approved automation token; never a password.")
        for instance, variable in (("A", self.token_a), ("B", self.token_b)):
            suffix = "" if instance == "A" else " (optional)"
            self.tk.Label(self.body, text=f"Multilogin access code{suffix}", anchor="w").pack(fill="x")
            self.tk.Entry(self.body, textvariable=variable, show="•").pack(fill="x", pady=(2, 10))
        if self.multilogin_ok:
            self._text("✓ Multilogin connection verified", bold=True, color="#17663a")
        self.tk.Button(
            self.body,
            text="Test Multilogin connection",
            command=self._start_multilogin_check,
            state="disabled" if self.busy else "normal",
        ).pack(anchor="w", pady=10)

    def _readiness(self) -> None:
        checks = [
            ("Runtime", all(check.ok for check in getattr(self, "system_checks", []))),
            ("Your workspace", self.configuration is not None),
            ("Local files", bool(self.stats_path.get() and self.upload_path.get() and self.creatives_path.get())),
            ("Google", self.google_ok),
            ("Multilogin", self.multilogin_ok),
            ("Safety mode", True),
        ]
        self._text("The launcher will open only when every required group is ready.")
        for label, ok in checks:
            self._text(f"{'✓' if ok else '✕'}  {label}", color="#17663a" if ok else "#9b1c1c")
        self._text("Safety: RPA / recorded_flow / Multilogin attach-only", color="#17663a")
        if self.configuration:
            self._text(f"Found {len(self.configuration.accounts)} account(s) for this campaign manager.")
            self._text("After the launcher opens, connect each account to its Multilogin profile on the Accounts screen.")

    def _finish(self) -> None:
        self._text("Adopy Campaign Launcher is ready on this computer.", bold=True, color="#17663a")
        self._text("No campaign action was performed. The first campaign run must remain supervised.")
        self._text("Click Close to open the dashboard. Open Accounts to connect your Multilogin profiles.")
        self.tk.Button(
            self.body,
            text="Copy sanitized setup summary",
            command=self._copy_summary,
        ).pack(anchor="w", pady=10)

    def _copy_summary(self) -> None:
        if not self.configuration:
            return
        version = self.version_text.splitlines()[0].rsplit(" ", 1)[-1]
        self.root.clipboard_clear()
        self.root.clipboard_append(sanitized_setup_summary(self.configuration, version))
        self.status.set("Sanitized setup summary copied.")

    def _run_worker(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        if self.busy:
            return
        self.busy = True
        self._render()
        self.status.set("Working…")

        def target() -> None:
            try:
                result = work()
            except Exception as exc:
                self.root.after(0, lambda message=str(exc): self._worker_failed(message))
            else:
                self.root.after(0, lambda: self._worker_done(result, done))

        threading.Thread(target=target, daemon=True).start()

    def _worker_failed(self, message: str) -> None:
        self.busy = False
        self.status.set(message)
        self._render()
        self.status.set(message)

    def _worker_done(self, message: str, done: Callable[[str], None]) -> None:
        self.busy = False
        done(message)
        self._render()

    def _start_google_check(self) -> None:
        if not self.configuration:
            self.status.set("Complete your workspace and local files first.")
            return

        configuration = self.configuration
        self.google_ok = False
        self.google_token = None
        def done(result: GoogleReadCheck) -> None:
            self.google_ok = self.configuration == configuration
            self.google_token = result.token if self.google_ok else None
            self._verified_google_configuration = configuration
        self._run_worker(lambda: test_google_read_access(configuration, self.config_directory), done)

    def _tokens(self) -> dict[str, str]:
        return {"A": self.token_a.get().strip(), "B": self.token_b.get().strip()}

    def _start_multilogin_check(self) -> None:
        tokens = self._tokens()  # Read Tk variables on the UI thread.
        self.multilogin_ok = False
        def done(_message: str) -> None:
            self._verified_tokens = tokens
            self.multilogin_ok = self._tokens() == tokens
        self._run_worker(lambda: test_multilogin_access(tokens), done)

    def _validate_step(self) -> None:
        if self.busy:
            raise WizardValidationError("Wait for the current check to finish.")
        if self.step == 1:
            failed = [check.label for check in self.system_checks if not check.ok]
            if failed:
                raise WizardValidationError("Resolve failed system checks: " + ", ".join(failed))
        elif self.step == 2:
            self.campaign_manager_name.set(validate_campaign_manager_name(self.campaign_manager_name.get()))
            try:
                self.accounts_sheet.set(extract_sheet_id(self.accounts_sheet.get()))
            except MachineConfigError as exc:
                raise WizardValidationError(str(exc)) from exc
            oauth_path, _raw = validate_google_access_file(self.oauth_client_path.get())
            self.oauth_client_path.set(oauth_path)
        elif self.step == 3:
            self.stats_path.set(validate_stats_workbook(self.stats_path.get()))
            self.upload_path.set(validate_upload_workbook(self.upload_path.get()))
            self.creatives_path.set(validate_creatives_directory(self.creatives_path.get()))
            self.configuration = build_manual_setup(
                campaign_manager_name=self.campaign_manager_name.get(),
                accounts_sheet=self.accounts_sheet.get(),
                oauth_client_path=self.oauth_client_path.get(),
                stats_workbook=self.stats_path.get(),
                upload_workbook=self.upload_path.get(),
            )
        elif self.step == 4 and not self.google_ok:
            raise WizardValidationError("Connect Google and verify the company Sheet before continuing.")
        elif self.step == 5 and (not self.multilogin_ok or self._verified_tokens != self._tokens()):
            raise WizardValidationError("Test the Multilogin connection before continuing.")
        elif self.step == 6:
            if not self.configuration or self.configuration != self._verified_google_configuration or not self.google_ok or self.google_token is None:
                raise WizardValidationError("Verify Google access for these workspace settings before finishing.")
            if not self.multilogin_ok or self._verified_tokens != self._tokens():
                raise WizardValidationError("Verify the current Multilogin access codes before finishing.")
            apply_machine_configuration(
                self.configuration,
                self.config_directory,
                stats_workbook=self.stats_path.get(),
                upload_workbook=self.upload_path.get(),
                creatives_directory=self.creatives_path.get(),
                multilogin_tokens=self._tokens(),
                google_token=self.google_token,
            )
            version = self.version_text.splitlines()[0].rsplit(" ", 1)[-1]
            write_ready_state(self.config_directory, version)
            self.completed = True

    def _next(self) -> None:
        if self.step == 7:
            self._close()
            return
        try:
            self._validate_step()
        except (WizardValidationError, MachineConfigError, OSError) as exc:
            self.status.set(str(exc))
            return
        self.step += 1
        self._render()

    def _back(self) -> None:
        if self.step > 0 and not self.busy:
            self.step -= 1
            self._render()

    def _close(self) -> None:
        if self.busy:
            self.status.set("Wait for the current check to finish before closing.")
            return
        self.root.destroy()


def run_setup_wizard(config_directory: str | Path, version_text: str) -> bool:
    try:
        import tkinter as tk

        root = tk.Tk()
    except Exception as exc:
        raise RuntimeError(f"Windows Setup Wizard could not start: {exc}") from exc
    wizard = SetupWizard(root, Path(config_directory), version_text)
    root.mainloop()
    return wizard.completed
