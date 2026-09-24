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
)


from src.analysis_agent.stats_workbook import (
    STATS_COLUMNS, StatsWorkbookError, validate_stats_workbook as _validate_stats_workbook,
)


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
    try:
        return _validate_stats_workbook(path)
    except StatsWorkbookError as exc:
        raise WizardValidationError(str(exc)) from exc


def default_creatives_directory() -> Path:
    """Documents\\ACL\\Creatives — created by setup, changeable later in the dashboard."""
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / "Documents" / "ACL" / "Creatives"


def validate_creatives_directory(path: str, *, create: bool = False) -> str:
    directory = Path(path) if (path or "").strip() else default_creatives_directory()
    if create and not directory.exists():
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WizardValidationError(f"Creatives folder could not be created: {exc}") from exc
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


def build_manual_setup(*, campaign_manager_name: str, stats_workbook: str) -> MachineConfiguration:
    """Build machine configuration from fields entered in the setup wizard.

    Account labels come from the Stats workbook. Ad-account IDs and Multilogin
    profiles stay blank on purpose: on the Accounts screen the manager picks an
    open profile by name, and ACL reads the ad account from that profile.
    """
    from openpyxl import load_workbook

    name = validate_campaign_manager_name(campaign_manager_name)
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
    return MachineConfiguration(
        name,
        tuple(AccountAssignment(account_name=alias) for alias in sorted(aliases.values(), key=str.casefold)),
    )


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


#: Слоты Multilogin — это два разных аккаунта, а не «основной и запасной».
#: Подписи «Multilogin access code» и «… (optional)» не говорили об этом
#: ничего, и человек с двумя аккаунтами не знал, куда что класть (ACL-1303).
MULTILOGIN_SLOTS = (("A", "Main Multilogin account"), ("B", "Second Multilogin account"))


def sign_in_to_multilogin(email: str, password: str) -> tuple[str, str]:
    """Почта и пароль -> (токен автоматизации, что сказать человеку).

    Токены Multilogin никому не выдают: их получает сам, кто входит. Мастер
    просил вставить то, чего у человека нет, — а дешборд рядом умел войти и
    получить токен. Теперь это одно место на обоих (`src/web/multilogin_auth`).

    Пароль уходит в один запрос и нигде не сохраняется.
    """
    from src.web.multilogin_auth import sign_in

    result = sign_in(email, password)
    if not result.ok:
        raise WizardValidationError(result.error or "Multilogin sign-in failed.")
    if not result.automation_token:
        # Вошли, но токена не дали: право выдавать его есть только у владельца
        # рабочего пространства. Без него робот не откроет ни один профиль,
        # поэтому это отказ, а не мелкая подробность.
        raise WizardValidationError(
            (result.token_error or "Multilogin issued no automation token.")
            + " Ask the owner of your Multilogin workspace to sign in here,"
              " or paste an automation token below."
        )
    return result.automation_token, "Signed in; automation token saved for this computer."


def test_multilogin_access(tokens: dict[str, str]) -> str:
    """Validate at least one Multilogin automation token.

    Individual ad-account/profile connections are completed in ACL's Accounts
    screen after setup, where the user can choose an open profile by name.
    """
    import urllib.error
    import urllib.request

    provided = {instance: token.strip() for instance, token in tokens.items() if token.strip()}
    if not provided:
        raise WizardValidationError(
            "Sign in to Multilogin above, or paste an automation token if you were given one."
        )
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
            "Stats workbook and Creatives folder: validated (paths omitted)",
            "Safety mode: RPA / recorded_flow / attach-only",
            "Updates: offered in the dashboard, installed only when the user clicks Update",
            "Campaign actions during setup: none",
        )
    )


class SetupWizard:
    STEPS = (
        "Welcome",
        "System check",
        "Your name",
        "Local files",
        "Multilogin",
        "Readiness",
        "Finish",
    )
    LAST = len(STEPS) - 1

    def __init__(self, root, config_directory: Path, version_text: str):
        import tkinter as tk

        self.tk = tk
        self.root = root
        self.config_directory = config_directory
        self.version_text = version_text
        self.step = 0
        self.configuration: MachineConfiguration | None = None
        self._verified_tokens = None
        self.multilogin_ok = False
        self.completed = False
        self.busy = False
        self.campaign_manager_name = tk.StringVar(value=self._existing_env_value("MY_CM_NAME"))
        self.stats_path = tk.StringVar(value=self._existing_env_value("FB_STATS_EXCEL"))
        self.creatives_path = tk.StringVar(
            value=self._existing_env_value("FB_CREATIVES_ROOT") or str(default_creatives_directory()))
        self.token_a = tk.StringVar(value=self._existing_env_value("MULTILOGIN_AUTOMATION_TOKEN_A"))
        self.token_b = tk.StringVar(value=self._existing_env_value("MULTILOGIN_AUTOMATION_TOKEN_B"))
        # Пароль живёт только в этом поле и только до входа: ни в настройку,
        # ни в журнал он не попадает (ACL-1303).
        self.mlx_email = tk.StringVar(value="")
        self.mlx_password = tk.StringVar(value="")
        self.mlx_slot = tk.StringVar(value="A")
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
        for variable in (self.token_a, self.token_b):
            variable.trace_add("write", self._invalidate_multilogin)
        self._render()

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
        self.back_button.config(state="normal" if 0 < self.step < self.LAST else "disabled")
        self.next_button.config(text="Close" if self.step == self.LAST else "Finish" if self.step == self.LAST - 1 else "Next")
        renderers: tuple[Callable[[], None], ...] = (
            self._welcome,
            self._system,
            self._identity,
            self._files,
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
        self._text("New versions are offered in the dashboard and installed only when you click Update.")

    def _system(self) -> None:
        self.system_checks = system_readiness(self.config_directory)
        self._text("The installer checks this computer automatically; Python and Git are not prerequisites.")
        for check in self.system_checks:
            self._text(
                f"{'✓' if check.ok else '✕'}  {check.label} — {check.detail}",
                color="#17663a" if check.ok else "#9b1c1c",
            )

    def _identity(self) -> None:
        self._text("Enter your name exactly as it is written in the CM column of the Stats workbook.")
        self.tk.Label(self.body, text="Your name", anchor="w").pack(fill="x")
        self.tk.Entry(self.body, textvariable=self.campaign_manager_name).pack(fill="x", pady=(2, 12))
        self._text("ACL finds your ad accounts in the workbook by this name.")

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
        self._path_row("Stats workbook (Excel)", self.stats_path)
        self._path_row("Creatives folder", self.creatives_path, directory=True)
        self._text("Put your videos and images into the Creatives folder. ACL creates it if it does not exist yet. "
                   "Both paths can be changed later in the dashboard.")

    def _multilogin(self) -> None:
        self._text("Sign in with your Multilogin email and password. ACL gets the access code itself "
                   "and keeps only that code on this computer; the password is not saved anywhere.")
        self.tk.Label(self.body, text="Multilogin email", anchor="w").pack(fill="x")
        self.tk.Entry(self.body, textvariable=self.mlx_email).pack(fill="x", pady=(2, 8))
        self.tk.Label(self.body, text="Multilogin password", anchor="w").pack(fill="x")
        self.tk.Entry(self.body, textvariable=self.mlx_password, show="•").pack(fill="x", pady=(2, 8))
        self.tk.Label(self.body, text="Which account", anchor="w").pack(fill="x")
        for instance, title in MULTILOGIN_SLOTS:
            self.tk.Radiobutton(self.body, text=title, value=instance,
                                variable=self.mlx_slot).pack(anchor="w")
        self.tk.Button(
            self.body,
            text="Sign in to Multilogin",
            command=self._start_multilogin_signin,
            state="disabled" if self.busy else "normal",
        ).pack(anchor="w", pady=(6, 10))
        # Вставка токена остаётся — кому-то его выдали на руки, — но вторым
        # вариантом: первым стоит то, что у человека есть (ACL-1303).
        self._text("Already have an access code? Paste it instead:")
        for instance, title in MULTILOGIN_SLOTS:
            variable = self.token_a if instance == "A" else self.token_b
            self.tk.Label(self.body, text=f"{title} — access code", anchor="w").pack(fill="x")
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
            ("Your accounts", self.configuration is not None),
            ("Local files", bool(self.stats_path.get() and self.creatives_path.get())),
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

    def _tokens(self) -> dict[str, str]:
        return {"A": self.token_a.get().strip(), "B": self.token_b.get().strip()}

    def _start_multilogin_signin(self) -> None:
        """Войти и положить полученный токен в поле выбранного аккаунта."""
        email = self.mlx_email.get().strip()
        password = self.mlx_password.get()
        slot = (self.mlx_slot.get() or "A").strip().upper()

        def work() -> str:
            token, message = sign_in_to_multilogin(email, password)
            return f"{slot}\t{token}\t{message}"

        def done(answer: str) -> None:
            slot_name, token, message = answer.split("\t", 2)
            (self.token_a if slot_name == "A" else self.token_b).set(token)
            # Пароль больше не нужен: токен получен и сохранится сам.
            self.mlx_password.set("")
            self._verified_tokens = self._tokens()
            self.multilogin_ok = True
            self.status.set(message)

        self._run_worker(work, done)

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
        elif self.step == 3:
            self.stats_path.set(validate_stats_workbook(self.stats_path.get()))
            self.creatives_path.set(validate_creatives_directory(self.creatives_path.get(), create=True))
            self.configuration = build_manual_setup(
                campaign_manager_name=self.campaign_manager_name.get(),
                stats_workbook=self.stats_path.get(),
            )
        elif self.step == 4 and (not self.multilogin_ok or self._verified_tokens != self._tokens()):
            raise WizardValidationError("Test the Multilogin connection before continuing.")
        elif self.step == 5:
            if not self.configuration:
                raise WizardValidationError("Go back and confirm your name and the Stats workbook.")
            if not self.multilogin_ok or self._verified_tokens != self._tokens():
                raise WizardValidationError("Verify the current Multilogin access codes before finishing.")
            apply_machine_configuration(
                self.configuration,
                self.config_directory,
                stats_workbook=self.stats_path.get(),
                creatives_directory=self.creatives_path.get(),
                multilogin_tokens=self._tokens(),
            )
            version = self.version_text.splitlines()[0].rsplit(" ", 1)[-1]
            write_ready_state(self.config_directory, version)
            self.completed = True

    def _next(self) -> None:
        if self.step == self.LAST:
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
