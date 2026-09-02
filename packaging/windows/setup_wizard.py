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
from typing import Callable

from setup_pack import SetupPack, SetupPackError, apply_setup_pack, load_setup_pack


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


def test_google_read_access(pack: SetupPack, config_directory: str | Path) -> str:
    """Run read-only OAuth and verify that the assigned spreadsheet is readable."""
    config_dir = Path(config_directory)
    config_dir.mkdir(parents=True, exist_ok=True)
    existing_client = config_dir / "oauth_client.json"
    temporary_client: Path | None = None
    if pack.oauth_client is not None:
        handle, temp_name = tempfile.mkstemp(prefix="acl-oauth-client-", suffix=".json")
        with os.fdopen(handle, "wb") as stream:
            stream.write(pack.oauth_client)
        client_path = Path(temp_name)
        temporary_client = client_path
    elif existing_client.is_file():
        client_path = existing_client
    else:
        raise WizardValidationError("The setup pack does not contain a Google OAuth client.")

    try:
        from src.analysis_agent.gsheet_accounts import _build_sheets_service_oauth

        service = _build_sheets_service_oauth(
            str(client_path),
            str(config_dir / "token_accounts.json"),
        )
        if service is None:
            raise WizardValidationError("Google authorization did not complete.")
        service.spreadsheets().get(
            spreadsheetId=pack.accounts_sheet_id,
            fields="spreadsheetId",
        ).execute()
        return "Company Google Sheet is readable with read-only access."
    except WizardValidationError:
        raise
    except Exception as exc:
        raise WizardValidationError(f"Google Sheet read failed: {exc}") from exc
    finally:
        if temporary_client:
            temporary_client.unlink(missing_ok=True)


def test_multilogin_access(pack: SetupPack, tokens: dict[str, str]) -> str:
    """Validate tokens and confirm every assigned folder/profile is returned."""
    import requests

    matched = 0
    for instance in sorted({assignment.multilogin_instance for assignment in pack.accounts}):
        token = tokens.get(instance, "").strip()
        if not token:
            raise WizardValidationError(f"Multilogin token {instance} is required.")
        try:
            response = requests.get(
                "https://launcher.mlx.yt:45001/api/v2/profile",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise WizardValidationError(
                "Cannot reach Multilogin Launcher. Make sure Multilogin is installed, running, and signed in."
            ) from exc
        if response.status_code == 401:
            raise WizardValidationError(f"Multilogin rejected token {instance}.")
        if response.status_code >= 400:
            raise WizardValidationError(f"Multilogin returned HTTP {response.status_code}.")
        body = response.json()
        profiles = body if isinstance(body, list) else body.get("data") or body.get("profiles") or []
        returned = {
            (
                str(profile.get("folder_id") or profile.get("folderId") or ""),
                str(profile.get("profile_id") or profile.get("uuid") or profile.get("id") or ""),
            )
            for profile in profiles
            if isinstance(profile, dict)
        }
        expected = {
            (assignment.folder_id, assignment.profile_id)
            for assignment in pack.accounts
            if assignment.multilogin_instance == instance
        }
        missing = expected - returned
        if missing:
            raise WizardValidationError(
                f"Multilogin instance {instance} is missing {len(missing)} assigned profile(s)."
            )
        matched += len(expected)
    return f"Matched all {matched} assigned Multilogin profile(s)."


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


class SetupWizard:
    STEPS = (
        "Welcome",
        "System check",
        "Setup pack",
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
        self.pack: SetupPack | None = None
        self.google_ok = False
        self.multilogin_ok = False
        self.completed = False
        self.busy = False
        self.pack_path = tk.StringVar()
        self.stats_path = tk.StringVar()
        self.upload_path = tk.StringVar()
        self.creatives_path = tk.StringVar()
        self.token_a = tk.StringVar(value=self._existing_env_value("MULTILOGIN_AUTOMATION_TOKEN_A"))
        self.token_b = tk.StringVar(value=self._existing_env_value("MULTILOGIN_AUTOMATION_TOKEN_B"))
        self.status = tk.StringVar()

        root.title("ACL Setup")
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
        self._render()

    def _existing_env_value(self, key: str) -> str:
        path = self.config_directory / ".env"
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
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
            self._pack,
            self._files,
            self._google,
            self._multilogin,
            self._readiness,
            self._finish,
        )
        renderers[self.step]()

    def _welcome(self) -> None:
        self._text("Set up ACL on this computer", bold=True)
        self._text(self.version_text)
        self._text(
            "This wizard only configures the local application. It cannot create, publish, or change a campaign."
        )
        self._text("Automatic software updates are disabled for this installer pilot.")

    def _system(self) -> None:
        self.system_checks = system_readiness(self.config_directory)
        self._text("ACL checks this computer automatically; Python and Git are not prerequisites.")
        for check in self.system_checks:
            self._text(
                f"{'✓' if check.ok else '✕'}  {check.label} — {check.detail}",
                color="#17663a" if check.ok else "#9b1c1c",
            )

    def _pack(self) -> None:
        from tkinter import filedialog

        self._text("Choose the personal .aclsetup file supplied through the approved company channel.")
        row = self.tk.Frame(self.body)
        row.pack(fill="x", pady=(8, 16))
        self.tk.Entry(row, textvariable=self.pack_path).pack(side="left", fill="x", expand=True)

        def choose() -> None:
            selected = filedialog.askopenfilename(filetypes=[("ACL setup pack", "*.aclsetup")])
            if not selected:
                return
            self.pack_path.set(selected)
            try:
                self.pack = load_setup_pack(selected)
                self.status.set("")
                self._render()
            except SetupPackError as exc:
                self.pack = None
                self.status.set(str(exc))

        self.tk.Button(row, text="Browse…", command=choose).pack(side="left", padx=(8, 0))
        if self.pack:
            self._text(f"Campaign manager: {self.pack.campaign_manager_name}", bold=True, color="#17663a")
            self._text(f"Assigned accounts: {len(self.pack.accounts)}")
            self._text("No Google user token, Multilogin password, or Meta credential is stored in this pack.")

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
        self._text("ACL requests read-only Google Sheets access using the company OAuth client.")
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
            if self.pack and not any(a.multilogin_instance == instance for a in self.pack.accounts):
                continue
            self.tk.Label(self.body, text=f"Multilogin token {instance}", anchor="w").pack(fill="x")
            self.tk.Entry(self.body, textvariable=variable, show="•").pack(fill="x", pady=(2, 10))
        if self.multilogin_ok:
            self._text("✓ Every assigned Multilogin profile matched", bold=True, color="#17663a")
        self.tk.Button(
            self.body,
            text="Test Multilogin profiles",
            command=self._start_multilogin_check,
            state="disabled" if self.busy else "normal",
        ).pack(anchor="w", pady=10)

    def _readiness(self) -> None:
        checks = [
            ("Runtime", all(check.ok for check in getattr(self, "system_checks", []))),
            ("Identity and accounts", self.pack is not None),
            ("Local files", bool(self.stats_path.get() and self.upload_path.get() and self.creatives_path.get())),
            ("Google", self.google_ok),
            ("Multilogin", self.multilogin_ok),
            ("Safety mode", True),
        ]
        self._text("ACL will be enabled only when every required group is ready.")
        for label, ok in checks:
            self._text(f"{'✓' if ok else '✕'}  {label}", color="#17663a" if ok else "#9b1c1c")
        self._text("Safety: RPA / recorded_flow / Multilogin attach-only", color="#17663a")

    def _finish(self) -> None:
        self._text("ACL setup is complete on this computer.", bold=True, color="#17663a")
        self._text("No campaign action was performed. The first campaign run must remain supervised.")
        self._text("Click Close; ACL will open the local dashboard.")

    def _run_worker(self, work: Callable[[], str], done: Callable[[str], None]) -> None:
        if self.busy:
            return
        self.busy = True
        self.status.set("Working…")

        def target() -> None:
            try:
                result = work()
            except Exception as exc:
                self.root.after(0, lambda: self._worker_failed(str(exc)))
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
        if not self.pack:
            self.status.set("Choose and validate the setup pack first.")
            return

        def done(_message: str) -> None:
            self.google_ok = True

        self._run_worker(lambda: test_google_read_access(self.pack, self.config_directory), done)

    def _tokens(self) -> dict[str, str]:
        return {"A": self.token_a.get().strip(), "B": self.token_b.get().strip()}

    def _start_multilogin_check(self) -> None:
        if not self.pack:
            self.status.set("Choose and validate the setup pack first.")
            return

        def done(_message: str) -> None:
            self.multilogin_ok = True

        self._run_worker(lambda: test_multilogin_access(self.pack, self._tokens()), done)

    def _validate_step(self) -> None:
        if self.step == 1:
            failed = [check.label for check in self.system_checks if not check.ok]
            if failed:
                raise WizardValidationError("Resolve failed system checks: " + ", ".join(failed))
        elif self.step == 2 and not self.pack:
            raise WizardValidationError("Choose a valid personal .aclsetup file.")
        elif self.step == 3:
            self.stats_path.set(validate_stats_workbook(self.stats_path.get()))
            self.upload_path.set(validate_upload_workbook(self.upload_path.get()))
            self.creatives_path.set(validate_creatives_directory(self.creatives_path.get()))
        elif self.step == 4 and not self.google_ok:
            raise WizardValidationError("Connect Google and verify the company Sheet before continuing.")
        elif self.step == 5 and not self.multilogin_ok:
            raise WizardValidationError("Test every assigned Multilogin profile before continuing.")
        elif self.step == 6:
            if not self.pack:
                raise WizardValidationError("Setup pack is missing.")
            apply_setup_pack(
                self.pack,
                self.config_directory,
                stats_workbook=self.stats_path.get(),
                upload_workbook=self.upload_path.get(),
                creatives_directory=self.creatives_path.get(),
                multilogin_tokens=self._tokens(),
            )
            version = self.version_text.splitlines()[0].removeprefix("ACL ")
            write_ready_state(self.config_directory, version)
            self.completed = True

    def _next(self) -> None:
        if self.step == 7:
            self._close()
            return
        try:
            self._validate_step()
        except (WizardValidationError, SetupPackError, OSError) as exc:
            self.status.set(str(exc))
            return
        self.step += 1
        self._render()

    def _back(self) -> None:
        if self.step > 0:
            self.step -= 1
            self._render()

    def _close(self) -> None:
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
