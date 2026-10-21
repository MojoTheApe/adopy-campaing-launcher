"""Paths and durable per-machine configuration for ACL.

Code is updated/re-cloned; account maps, tokens and operator settings are not.
All writers therefore target the OS-local ACL directory and use atomic file
replacement.  Checkout-local files are read only as a one-time legacy source.
"""
import contextlib
import csv
import errno
import io
import json
import os
import random
import re
import shutil
import time
import uuid
from pathlib import Path

# Единственный док с отчётом (фиксируем): cursor example.xlsx на рабочем столе.
#
# Defaults are built from the CURRENT user's Desktop, not a literal
# C:\Users\EvgeniSedin\... — on this machine they resolve to exactly the same
# paths as before, but on a media buyer's machine running ACL they point at
# that buyer's own Desktop instead of at a stranger's, which cannot exist. A
# missing file is then an honest "your file isn't there" rather than a
# confusing error naming someone else's account.
_DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")

EXCEL_PATH_DEFAULT = os.path.join(_DESKTOP, "cursor example.xlsx")
REPORT_EXCEL_DEFAULT = EXCEL_PATH_DEFAULT  # тот же файл — док с отчётом
STATS_FOLDER_DEFAULT = os.path.dirname(EXCEL_PATH_DEFAULT)
STATS_FILENAME_DEFAULT = os.path.basename(EXCEL_PATH_DEFAULT)

UPLOAD_FB_EXCEL_DEFAULT = os.path.join(_DESKTOP, "Upluad FB.xlsx")
CREATIVES_ROOT_DEFAULT = os.path.join(_DESKTOP, "Creatives")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
AD_ACCOUNT_MAP_DEFAULT = str(PROJECT_ROOT / "config" / "ad_account_map.csv")
AD_ACCOUNT_MAP_EXAMPLE = str(PROJECT_ROOT / "config" / "ad_account_map.example.csv")
ENV_EXAMPLE = str(PROJECT_ROOT / ".env.example")


def get_excel_path() -> str:
    """Путь к доку с отчётом (cursor example.xlsx). Переменная FB_STATS_EXCEL переопределяет путь."""
    return os.environ.get("FB_STATS_EXCEL", "").strip() or EXCEL_PATH_DEFAULT


def get_stats_folder() -> str:
    """Папка, куда заливаются данные (cursor example). Для подстановки имени файла."""
    return os.environ.get("FB_STATS_FOLDER", "").strip() or STATS_FOLDER_DEFAULT


def get_upload_fb_excel_path() -> str:
    """Path to Upload FB.xlsx (Accounts, Main, offers) — for agent 5 (Jira tasks)."""
    return os.environ.get("UPLOAD_FB_EXCEL", "").strip() or UPLOAD_FB_EXCEL_DEFAULT


def get_creatives_root() -> str:
    """Root folder for creative files (.mp4 / .jpeg); searched recursively by filename."""
    return os.environ.get("FB_CREATIVES_ROOT", "").strip() or CREATIVES_ROOT_DEFAULT


def get_local_config_dir() -> str:
    """Per-machine config that must NEVER travel with the code.

    ACL is moving from "one repo on Evgeni's machine" to a launcher installed
    on every media buyer's computer, updated from the shared work repo. That
    only works if the repo carries code and the machine carries its own
    accounts: ten buyers have ten different account maps, and a file living in
    git would both overwrite theirs on update and push theirs to a repository
    outside developers can read.

    Override with ACL_CONFIG_DIR. Default is the OS's own per-user data
    location, deliberately OUTSIDE the checkout so that deleting or re-cloning
    the repo cannot take a buyer's configuration with it.
    """
    explicit = os.environ.get("ACL_CONFIG_DIR", "").strip()
    if explicit:
        return unvirtualize_path(explicit)
    base = get_real_local_appdata()
    if base:  # Windows — every buyer machine today
        return os.path.join(base, "ACL")
    return os.path.join(os.path.expanduser("~"), ".config", "acl")


# --- One settings folder for every launch path (ACL-1680) -------------------
# A process started from Claude Desktop runs inside its MSIX package, where
# Windows hands it a *virtualised* %LOCALAPPDATA%:
#   C:\Users\X\AppData\Local\Packages\<Package>\LocalCache\Local
# ``ACL`` under that root is a private copy, not the folder Explorer, the
# installer and the buyer's own shortcut use. Every launcher must therefore
# resolve the REAL folder the same way; this is that one place.
_VIRTUAL_APPDATA = re.compile(
    r"^(?P<real>.+?)[\\/]Packages[\\/][^\\/]+[\\/]LocalCache[\\/]Local(?P<rest>(?:[\\/].*)?)$",
    re.IGNORECASE,
)


def unvirtualize_path(path: str) -> str:
    r"""Map a path inside an MSIX package's virtual AppData\Local to the real one.

    Anything that is not such a path is returned unchanged, so an explicit
    ACL_CONFIG_DIR that points elsewhere (tests, portable installs) is honoured.
    """
    match = _VIRTUAL_APPDATA.match(path or "")
    if not match:
        return path
    return match.group("real") + match.group("rest").replace("/", "\\")


def get_real_local_appdata() -> str:
    r"""The real per-user ``AppData\Local``, or "" off Windows / when unknown.

    Starts from %LOCALAPPDATA%; when that is the package-virtualised root the
    real one is derived from it (``USERPROFILE`` is not virtualised and is the
    fallback when %LOCALAPPDATA% is missing).
    """
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if base:
        return unvirtualize_path(base)
    profile = os.environ.get("USERPROFILE", "").strip()
    if profile and os.name == "nt":
        return os.path.join(profile, "AppData", "Local")
    return ""


def is_virtualized_environment() -> bool:
    """True when this process sees the package-virtualised %LOCALAPPDATA%."""
    base = os.environ.get("LOCALAPPDATA", "").strip()
    return bool(base) and unvirtualize_path(base) != base


def get_shadow_config_dirs() -> list[str]:
    r"""Other ``ACL`` folders that may hold a divergent copy of the settings.

    These are the per-package virtual copies
    (``Packages\*\LocalCache\Local\ACL``) under the real AppData, excluding
    the folder in use now.
    """
    real = get_real_local_appdata()
    if not real:
        return []
    current = os.path.normcase(os.path.normpath(get_local_config_dir()))
    found: list[str] = []
    packages = os.path.join(real, "Packages")
    try:
        names = sorted(os.listdir(packages))
    except OSError:
        return []
    for name in names:
        candidate = os.path.join(packages, name, "LocalCache", "Local", "ACL")
        if os.path.isdir(candidate) and os.path.normcase(os.path.normpath(candidate)) != current:
            found.append(candidate)
    return found


def pin_config_dir_env() -> str:
    """Make ACL_CONFIG_DIR explicit for this process and every child it starts.

    Called first by every launcher (exe, serve.py behind start_dashboard.bat,
    installer scripts) so they all agree on one folder.
    """
    resolved = get_local_config_dir()
    os.environ["ACL_CONFIG_DIR"] = resolved
    return resolved


def get_obsidian_dashboard_dir() -> Path | None:
    """The team's Obsidian "дешборд" folder, or None when this machine has none.

    ACL_OBSIDIAN_DIR wins when it points at an existing folder. Otherwise the
    known locations are tried in order: the owner's machine keeps the vault at
    ~/Desktop/Obsidian/дешборд, other machines still use the older
    ~/Desktop/Obsidian/Kuwait Forms/дешборд. Callers must handle None (warn
    and carry on) instead of writing into a folder that does not exist.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("ACL_OBSIDIAN_DIR", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    obsidian = Path.home() / "Desktop" / "Obsidian"
    candidates.append(obsidian / "дешборд")
    candidates.append(obsidian / "Kuwait Forms" / "дешборд")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


OBSIDIAN_MISSING_WARNING = "Obsidian vault not found; set ACL_OBSIDIAN_DIR"


def get_runtime_workspace() -> Path:
    """Writable runtime data survives replacement of a frozen installation."""
    import sys
    if getattr(sys, "frozen", False):
        return Path(get_local_config_dir()) / "workspace"
    return PROJECT_ROOT


def get_machine_env_path() -> str:
    return os.path.join(get_local_config_dir(), ".env")


def get_repo_env_path() -> str:
    return str(PROJECT_ROOT / ".env")


def get_draft_status_path() -> str:
    """Persistent operator verdicts survive an update or re-clone."""
    return os.path.join(get_local_config_dir(), "draft_status.json")


def get_google_oauth_client_path() -> str:
    """OAuth client JSON used by the dashboard and telemetry consent flows.

    New installs keep it with machine configuration. Existing installs may
    still have the ignored legacy file inside ``config/``; it remains a
    read-only fallback until the migration copies it out of the checkout.
    """
    explicit = os.environ.get("GSHEET_OAUTH_CLIENT", "").strip()
    if explicit:
        return explicit if os.path.isabs(explicit) else str(PROJECT_ROOT / explicit)
    local = os.path.join(get_local_config_dir(), "oauth_client.json")
    if os.path.isfile(local):
        return local
    legacy = str(PROJECT_ROOT / "config" / "oauth_client.json")
    return legacy if os.path.isfile(legacy) else local


def get_google_oauth_token_path() -> str:
    """Read-only Sheets token, always machine-local unless explicitly set."""
    explicit = os.environ.get("GSHEET_OAUTH_TOKEN", "").strip()
    if explicit:
        return explicit if os.path.isabs(explicit) else str(PROJECT_ROOT / explicit)
    return os.path.join(get_local_config_dir(), "token_accounts.json")


#: Сколько ждать, пока чужой открытый дескриптор освободит цель.
#:
#: На Windows `os.replace` падает с `PermissionError [WinError 5]`, если файл
#: в этот момент кем-то открыт — не заблокирован намеренно, а просто открыт:
#: вторая машина читает тот же `hidden_accounts.json`, антивирус смотрит
#: свежесозданный файл, дешборд перечитывает настройки. Окно — миллисекунды,
#: но пишем мы в эти файлы из нескольких процессов, и промах означает
#: потерянную правку оператора.
#:
#: POSIX такого не знает: там `rename` поверх открытого файла разрешён. Отсюда
#: и форма починки — не «сделать запись атомарной» (она атомарна), а
#: переждать. Полсекунды с запасом покрывают наблюдаемое окно; дольше ждать
#: незачем — если цель держат всерьёз, лучше честно упасть, чем зависнуть.
_REPLACE_ATTEMPTS = 10
_REPLACE_PAUSE = 0.05


def _replace_when_free(temp: Path, target: Path) -> None:
    """`os.replace` с ожиданием: последняя попытка бросает как обычно."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(temp, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_PAUSE)


#: ACL-1495: межпроцессный замок для машинных файлов (спрятанные аккаунты,
#: реестр таргетов).
#:
#: Прежний шаблон падал под нагрузкой двумя способами:
#:
#: * `msvcrt.locking(LK_LOCK)` — это не «ждать сколько нужно», а десять
#:   попыток раз в секунду, после чего EDEADLK («Resource deadlock
#:   avoided»). Взаимной блокировки нет — просто очередь длиннее десяти
#:   секунд. Здесь ждём сами: неблокирующая попытка в цикле, EDEADLK/EACCES
#:   — повод попробовать снова, предел — `_LOCK_TIMEOUT`.
#: * Первое использование: файл открывали в `a+b` и, увидев пустой, писали
#:   туда `b"0"`. Если байт 0 уже держит соседний процесс, запись в
#:   заблокированную область падает PermissionError на flush. Содержимое
#:   замку не нужно: Windows разрешает блокировать байт за концом файла,
#:   `flock` байтов не знает вовсе. Поэтому файл создаётся одним атомарным
#:   `os.open(O_CREAT)` и не пишется никогда.
_LOCK_TIMEOUT = 120.0
_LOCK_PAUSE_MIN = 0.005
_LOCK_PAUSE_MAX = 0.1
_LOCK_RETRY_ERRNOS = frozenset(
    e for e in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, getattr(errno, "EWOULDBLOCK", None),
                errno.EINTR)
    if e is not None
)


def _try_lock(fd: int) -> bool:
    """Одна неблокирующая попытка. False — замок занят, пробовать ещё."""
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in _LOCK_RETRY_ERRNOS:
            return False
        raise


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def machine_file_lock(lock_path: str | Path, timeout: float | None = None):
    """Эксклюзивный замок между процессами этой машины на время блока.

    Ждёт с растущей паузой (5 мс → 100 мс, с разбросом, чтобы очередь не
    шагала в ногу) не дольше `timeout` (по умолчанию `_LOCK_TIMEOUT`), затем
    бросает TimeoutError. Внутрипроцессную очередь потоков вызывающий держит
    сам (RLock модуля) — этот замок про соседние процессы.
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Создать-или-открыть — одна атомарная операция ОС; в файл не пишем.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o666)
    try:
        limit = _LOCK_TIMEOUT if timeout is None else timeout
        deadline = time.monotonic() + limit
        pause = _LOCK_PAUSE_MIN
        while not _try_lock(fd):
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(
                    f"Another ACL process kept {path.name} busy for {limit:g} s; try again."
                )
            time.sleep(min(left, pause * random.uniform(0.5, 1.5)))
            pause = min(pause * 2, _LOCK_PAUSE_MAX)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _atomic_write(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_when_free(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass


def _atomic_copy(source: str | Path, destination: str | Path) -> None:
    _atomic_write(destination, Path(source).read_bytes())


def write_csv_atomic(
    path: str | Path,
    fieldnames: list[str],
    rows: list[dict[str, object]],
) -> None:
    """Replace a machine CSV without leaving a partial file after a crash."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write(path, stream.getvalue().encode("utf-8"))


def inspect_account_map(path: str | Path) -> dict[str, object]:
    """Validate schema and placeholders without exposing any secret values."""
    result: dict[str, object] = {
        "valid": False,
        "rows": 0,
        "placeholder_rows": 0,
        "only_placeholders": False,
        "missing_profile_rows": 0,
        "invalid_ad_account_rows": 0,
        "errors": [],
    }
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = set(reader.fieldnames or [])
            missing_headers = sorted({"account_name", "ad_account_id"} - headers)
            if missing_headers:
                result["errors"] = ["missing columns: " + ", ".join(missing_headers)]
                return result
            rows = [row for row in reader if (row.get("account_name") or "").strip()]
    except Exception as exc:
        result["errors"] = [f"unreadable CSV: {type(exc).__name__}: {exc}"]
        return result

    placeholders = 0
    missing_profiles = 0
    invalid_ad_accounts = 0
    for row in rows:
        joined = " ".join(str(value or "").strip().lower() for value in row.values())
        if any(
            marker in joined
            for marker in (
                "act_1234567890",
                "your-profile-uuid",
                "your-folder-uuid",
                "proxy.example.com",
            )
        ):
            placeholders += 1
        if not (row.get("profile_id") or "").strip():
            missing_profiles += 1
        ad_account_id = (row.get("ad_account_id") or "").strip()
        if not re.fullmatch(r"act_\d+", ad_account_id):
            invalid_ad_accounts += 1

    errors: list[str] = []
    if not rows:
        errors.append("no account rows")
    if placeholders:
        errors.append(f"{placeholders} placeholder row(s)")
    if invalid_ad_accounts:
        errors.append(f"{invalid_ad_accounts} invalid ad_account_id row(s)")
    result.update(
        {
            "valid": bool(rows) and placeholders == 0 and invalid_ad_accounts == 0,
            "rows": len(rows),
            "placeholder_rows": placeholders,
            "only_placeholders": bool(rows) and placeholders == len(rows),
            "missing_profile_rows": missing_profiles,
            "invalid_ad_account_rows": invalid_ad_accounts,
            "errors": errors,
        }
    )
    return result


def get_ad_account_map_path() -> str:
    """CSV mapping Excel sheet name (account) -> act_* ad account id.

    Resolution order, first hit wins:
      1. META_AD_ACCOUNT_MAP_PATH — explicit override, unchanged behaviour.
      2. <local config dir>/ad_account_map.csv — where a buyer's own map lives.
      3. config/ad_account_map.csv in the checkout — how it has always worked;
         kept so existing installs keep running untouched after this change.

    When none exists, returns the local-config path rather than the repo one:
    the file is meant to be created there, so callers' "not found" errors point
    a new install at the right place instead of back into git.
    """
    explicit = os.environ.get("META_AD_ACCOUNT_MAP_PATH", "").strip()
    if explicit:
        return explicit
    local = os.path.join(get_local_config_dir(), "ad_account_map.csv")
    if os.path.exists(local):
        return local
    if os.path.exists(AD_ACCOUNT_MAP_DEFAULT):
        return AD_ACCOUNT_MAP_DEFAULT
    return local


def migrate_legacy_account_map() -> str:
    """Move an older install's account map out of the checkout, once.

    Machines that ran ACL before 2026-08-25 keep their map inside the checkout.
    They still work — the resolver falls back to it — but the file is one
    `git clean` or re-clone away from being lost, and it is the last thing
    holding a buyer's account ids inside a repository outside developers read.

    Deliberately copies ONLY when the machine-local file is absent, and never
    merges the two. The dashboard lets a buyer remove an account; merging would
    resurrect exactly the rows someone chose to delete, on every start, with no
    way to make the deletion stick.

    The checkout copy is left in place rather than deleted: it costs nothing,
    and an automatic delete of the only file holding real ad account ids is not
    something a start-up path should do behind a person's back. Returns a
    human-readable note, or "" when there was nothing to do.
    """
    local = os.path.join(get_local_config_dir(), "ad_account_map.csv")
    legacy = AD_ACCOUNT_MAP_DEFAULT
    local_info = inspect_account_map(local) if os.path.exists(local) else None
    legacy_info = inspect_account_map(legacy) if os.path.exists(legacy) else None

    if local_info and local_info.get("valid"):
        return ""
    if not legacy_info or not legacy_info.get("valid"):
        if local_info and not local_info.get("valid"):
            return "machine account map needs attention; no valid legacy map was copied"
        return ""

    # A fresh installer used to create the placeholder destination before this
    # migration ran.  That exact state must not mask a real legacy map.
    if local_info and not local_info.get("only_placeholders"):
        return "machine account map is invalid; left untouched for manual recovery"
    try:
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        if os.path.exists(local):
            backup = f"{local}.pre-migration.bak"
            if not os.path.exists(backup):
                shutil.copy2(local, backup)
        _atomic_copy(legacy, local)
    except Exception as exc:  # never block start-up over a convenience move
        return f"could not copy the account map to {local}: {exc}"
    return f"validated account map copied out of the checkout to {local}"


_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _dotenv_line(key: str, value: str) -> str:
    # JSON double-quoted strings are accepted by python-dotenv and keep tokens
    # containing spaces, #, quotes or backslashes unambiguous.
    import json

    return f"{key}={json.dumps(value, ensure_ascii=False)}"


def update_machine_env(values: dict[str, str]) -> str:
    """Atomically update machine-local `.env`; never write secrets to checkout."""
    for key in values:
        if not _ENV_KEY.fullmatch(key):
            raise ValueError(f"invalid environment key: {key!r}")

    target = Path(get_machine_env_path())
    if not target.exists():
        migrate_legacy_env(create_from_example=True)
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    pending = dict(values)
    out: list[str] = []
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
        key = match.group(1) if match else None
        if key in pending:
            out.append(_dotenv_line(key, pending.pop(key)))
        else:
            out.append(line)
    if pending and out and out[-1].strip():
        out.append("")
    out.extend(_dotenv_line(key, value) for key, value in pending.items())
    _atomic_write(target, ("\n".join(out).rstrip() + "\n").encode("utf-8"))
    for key, value in values.items():
        os.environ[key] = value
    return str(target)


def _read_env_value(path: str | Path, key: str) -> str | None:
    try:
        from dotenv import dotenv_values

        value = dotenv_values(path).get(key)
        return None if value is None else str(value)
    except Exception:
        return None


def migrate_legacy_env(*, create_from_example: bool = False) -> str:
    """Copy checkout `.env` to the machine once, then normalize old defaults."""
    local = Path(get_machine_env_path())
    source: Path | None = None
    note = ""
    if not local.exists():
        repo = Path(get_repo_env_path())
        example = Path(ENV_EXAMPLE)
        if repo.is_file():
            source = repo
            note = f"settings copied out of the checkout to {local}"
        elif create_from_example and example.is_file():
            source = example
            note = f"machine settings created from the safe example at {local}"
        if source is not None:
            _atomic_copy(source, local)

    if local.exists():
        engine = (_read_env_value(local, "FB_RPA_ENGINE") or "").strip().lower()
        if not engine:
            update_machine_env({"FB_RPA_ENGINE": "recorded_flow"})
            suffix = "legacy blank RPA engine normalized to recorded_flow"
            note = f"{note}; {suffix}" if note else suffix
    return note


def migrate_legacy_draft_status() -> str:
    """Copy the last dashboard verdict out of the checkout, once.

    The file contains no credentials, but it is operator state: an update or
    re-clone must not silently erase a blocked verdict and make yesterday's
    campaign look unreviewed.  Invalid JSON is deliberately ignored rather
    than copied into the machine configuration.
    """
    local = Path(get_draft_status_path())
    legacy = PROJECT_ROOT / "output" / "draft_status.json"
    if local.exists() or not legacy.is_file():
        return ""
    try:
        payload = json.loads(legacy.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return "legacy draft status is invalid; left in the checkout"
        _atomic_copy(legacy, local)
    except Exception as exc:
        return f"could not copy draft status to {local}: {exc}"
    return f"draft status copied out of the checkout to {local}"


def migrate_legacy_google_oauth() -> list[str]:
    """Copy ignored OAuth files out of the checkout without deleting sources."""
    notes: list[str] = []
    pairs = (
        (
            PROJECT_ROOT / "config" / "oauth_client.json",
            Path(get_local_config_dir()) / "oauth_client.json",
            "OAuth client",
        ),
        (
            PROJECT_ROOT / "config" / "token_accounts.json",
            Path(get_google_oauth_token_path()),
            "Google read token",
        ),
    )
    for source, destination, label in pairs:
        if destination.exists() or not source.is_file() or source.resolve() == destination.resolve():
            continue
        try:
            _atomic_copy(source, destination)
            notes.append(f"{label} copied out of the checkout to {destination}")
        except Exception as exc:
            notes.append(f"could not migrate {label} to {destination}: {exc}")
    return notes


def ensure_machine_config() -> list[str]:
    """Idempotent install/upgrade provisioning, migration before examples."""
    Path(get_local_config_dir()).mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    env_note = migrate_legacy_env(create_from_example=True)
    if env_note:
        notes.append(env_note)
    map_note = migrate_legacy_account_map()
    if map_note:
        notes.append(map_note)
    status_note = migrate_legacy_draft_status()
    if status_note:
        notes.append(status_note)
    notes.extend(migrate_legacy_google_oauth())
    local_map = Path(get_local_config_dir()) / "ad_account_map.csv"
    if not local_map.exists() and Path(AD_ACCOUNT_MAP_EXAMPLE).is_file():
        _atomic_copy(AD_ACCOUNT_MAP_EXAMPLE, local_map)
        notes.append(f"account map example created at {local_map}; replace placeholders")
    return notes


def get_env_files() -> list[str]:
    """`.env` files to load, in the order they should be applied.

    Once the machine-local file exists it is the only file loaded. Checkout
    `.env` is a legacy fallback only until migration; layering both lets a stale
    checkout silently fill keys that an operator deliberately removed locally.
    """
    local = get_machine_env_path()
    if os.path.exists(local):
        return [local]
    files = []
    repo = get_repo_env_path()
    if os.path.exists(repo):
        files.append(repo)
    return files


# The telemetry spreadsheet is the same one for every campaign manager, exactly
# like the accounts sheet. Leaving it as a setting meant every fresh install
# arrived with telemetry off and nobody knowing it existed: on 2026-09-04 the
# fleet was two machines and one of them had never sent a row, so a stalled run
# on the other could only be investigated through screenshots.
#
# A spreadsheet id is not a secret — access is controlled by who the sheet is
# shared with — and DEFAULT_ACCOUNTS_SHEET_ID already sets this precedent.
DEFAULT_TELEMETRY_SHEET_ID = "1M0FYL0EnxWFtTsCmKvFwJwuV0l2jWyvHmxfQdwcH6bs"


def load_env() -> list[str]:
    """Load the machine's settings. Returns the files actually loaded."""
    try:
        from dotenv import load_dotenv
    except Exception:
        return []
    loaded = []
    for f in get_env_files():
        try:
            load_dotenv(f)
            loaded.append(f)
        except Exception:
            continue
    # Fill in what every machine shares, after the machine's own files so a
    # deliberate local value always wins. Set on the environment rather than
    # read at each call site: telemetry belongs to the RPA track, and this way
    # it needs no change there to start reporting.
    if not (os.environ.get("GSHEET_TELEMETRY_ID") or "").strip():
        os.environ["GSHEET_TELEMETRY_ID"] = DEFAULT_TELEMETRY_SHEET_ID
    return loaded


# ── Agent 10: Facebook Ads Manager publish (Playwright) ──────────────────────

FB_ADS_AUTH_STATE_DEFAULT = ".auth/fb_state.json"
FB_ADS_MANAGER_URL_DEFAULT = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"
FB_PUBLISH_OUTPUT_DIR_DEFAULT = "output/fb_publish_runs"


def get_fb_ads_auth_state_path() -> str:
    """Путь к сохранённой сессии браузера (storage_state), созданной run_fb_login_session.py.
    Переменная FB_ADS_AUTH_STATE переопределяет путь. Файл в git не попадает (см. .gitignore)."""
    return os.environ.get("FB_ADS_AUTH_STATE", "").strip() or FB_ADS_AUTH_STATE_DEFAULT


def get_fb_ads_manager_url() -> str:
    """URL Ads Manager, с которого стартует флоу создания кампании."""
    return os.environ.get("FB_ADS_MANAGER_URL", "").strip() or FB_ADS_MANAGER_URL_DEFAULT


def get_fb_publish_output_dir() -> str:
    """Папка для скриншотов, trace-файлов и отчётов по прогонам агента 10."""
    return os.environ.get("FB_PUBLISH_OUTPUT_DIR", "").strip() or FB_PUBLISH_OUTPUT_DIR_DEFAULT


def get_hidden_accounts_path() -> str:
    """Аккаунты, убранные байером с глаз на ЭТОЙ машине.

    Список из общей таблицы одинаков у всех, а работает человек с двумя
    аккаунтами из двенадцати. Остальные он не может ни настроить, ни удалить:
    удалять нечего — записи на машине нет, а вычеркнуть строку из общей
    таблицы дешборд не вправе.

    Поэтому «спрятать» — настройка вида, и живёт она рядом с картой
    аккаунтов, в машинной папке: у десяти байеров десять разных наборов, и
    файл в гите означал бы, что вид одного человека становится видом всех.
    """
    return os.path.join(get_local_config_dir(), "hidden_accounts.json")


def get_account_notes_path() -> str:
    """Заметки менеджера к аккаунтам (ACL-1620) — только на ЭТОЙ машине.

    Текст пишет человек про свои аккаунты; в гит он попасть не должен,
    поэтому файл лежит в машинной папке, а не в репозитории.
    """
    return os.path.join(get_local_config_dir(), "account_notes.json")
