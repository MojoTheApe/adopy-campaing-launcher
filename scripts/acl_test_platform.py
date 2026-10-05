"""Which test files need Windows, checked rather than guessed (ACL-1582).

ACL runs only on buyers' Windows machines, but most of its tests are pure
logic, fixtures and bench pages: they behave the same on ubuntu-latest, where
CI minutes cost half. A test file that really needs Windows says so itself:

    pytestmark = pytest.mark.windows_only(reason="msvcrt byte-range locks")

The reason is required: it is the checked-in rationale for keeping the file on
the more expensive runner. The split is per file, because CI shards files.

``windows_signals`` is the guard (tests/test_acl_test_platform.py): a file
without the marker must not use Windows-only APIs or assumptions, or it would
fail on Linux or, worse, quietly skip its Windows-only part there. A line that
only *mentions* such a thing as data (a path string fed to a classifier, a
Linux-only shell contract) may carry a waiver comment with its own reason:

    "packaging/windows/build.ps1",  # linux-ok: a path for the classifier, never run

Selection:

    python scripts/acl_test_platform.py linux     # files for the Linux job
    python scripts/acl_test_platform.py windows   # marked files + Windows smoke
    python scripts/acl_test_platform.py report    # counts and rationale
"""
from __future__ import annotations

import ast
import re
import warnings
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

MARKER = "windows_only"
PLATFORMS = ("linux", "windows")
WAIVER = re.compile(r"#\s*linux-ok:\s*\S")

# Небольшой дымовой набор, который идёт на Windows и на Linux: импорт
# приложения и связка путей/машинной папки. Не помечен — он нейтральный, просто
# дублируется на Windows как проверка «приложение на Windows вообще живо»
# (вместе с шагом scripts/acl_smoke.py в задании Windows).
WINDOWS_SMOKE = (
    "tests/test_acl_test_platform.py",
    "tests/test_tests_do_not_touch_the_live_machine_folder.py",
)

_SHELLS = re.compile(r"(?:powershell|pwsh)(?:\.exe)?", re.I)
_PS1 = re.compile(r"\.ps1\b", re.I)
_LOCALAPPDATA = re.compile(r"LOCALAPPDATA", re.I)
# Windows-путь в утверждении: диск («C:\»), переменная («%X%\») или хотя бы два
# сегмента через одиночную обратную косую без метасимволов регулярок.
_DRIVE = re.compile(r"(?<![A-Za-z])[A-Za-z]:\\")
_ENV_PATH = re.compile(r"%\w+%\\")
_REGEX_META = re.compile(r"[\^$*+?()\[\]{}|]")
# Сегмент пути без пробелов по краям: «A \ B» (разделитель групп) — не путь.
_SEGMENT = re.compile(r"[\w.%~-](?:[\w .%~-]*[\w.%~-])?")
# Программы, которых нет вне Windows: первый элемент списка-команды.
# Короткие слова (where, reg, net) считаются командой только с «.exe»: без
# суффикса это слишком часто просто данные.
_WINDOWS_COMMANDS = {"cmd", "taskkill", "tasklist", "robocopy", "icacls", "wmic", "schtasks",
                     "powershell", "pwsh", "where.exe", "reg.exe", "net.exe", "attrib.exe"}


def _windows_path_literal(value: str) -> bool:
    if "\\" not in value:
        return False
    if _DRIVE.search(value) or _ENV_PATH.search(value):
        return True
    if _REGEX_META.search(value) or "\n" in value:
        return False
    parts = value.split("\\")
    return len(parts) >= 2 and all(_SEGMENT.fullmatch(p) for p in parts)


def _dotted(node) -> str:
    """«os.name», «sys.platform», «ctypes.windll» для цепочки атрибутов."""
    names = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
        return ".".join(reversed(names))
    return ""


def _parse(source: str):
    # Предупреждения вида «invalid escape sequence» в чужом тесте — не наше дело.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(source)


def _windows_command(node) -> bool:
    """``["taskkill", "/F", ...]``: список-команда, начинающийся с Windows-программы."""
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return False
    first = node.elts[0]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return False
    name = first.value.strip().lower().replace("\\", "/").rsplit("/", 1)[-1]
    if name in _WINDOWS_COMMANDS or name.endswith((".bat", ".cmd", ".ps1")):
        return True
    return name.endswith(".exe") and name[:-4] in _WINDOWS_COMMANDS


def _docstring_ids(tree) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _platform_compare(node) -> bool:
    """``os.name == "nt"``, ``sys.platform != "win32"`` и т.п."""
    if not isinstance(node, ast.Compare):
        return False
    sides = [node.left, *node.comparators]
    names = {_dotted(s) for s in sides}
    consts = {s.value for s in sides if isinstance(s, ast.Constant) and isinstance(s.value, str)}
    return bool(({"os.name"} & names and "nt" in consts)
                or ({"sys.platform", "platform.system()"} & names and consts & {"win32", "Windows", "cygwin"}))


def windows_signals(source: str) -> list[tuple[int, str, str]]:
    """(строка, правило, текст) каждого Windows-признака, кроме строк с waiver.

    Разбор по AST: строка в комментарии или в докстринге — не признак, а
    ``import msvcrt`` внутри теста — признак, даже если тест его «прячет»
    за ``if os.name == "nt"`` (это само по себе второй признак)."""
    tree = _parse(source)
    lines = source.splitlines()
    docstrings = _docstring_ids(tree)
    asserted = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            asserted.update(id(n) for n in ast.walk(node))
    hits: list[tuple[int, str, str]] = []

    def hit(node, rule):
        line = getattr(node, "lineno", 0)
        text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        end = getattr(node, "end_lineno", line) or line
        span = lines[line - 1:end] if line else []
        if any(WAIVER.search(s) for s in span):
            return
        hits.append((line, rule, text))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in ("msvcrt", "winreg", "_winapi", "winsound"):
                    hit(node, root)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in (
                "msvcrt", "winreg", "_winapi", "winsound"):
            hit(node, node.module.split(".")[0])
        elif isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            if dotted.startswith(("msvcrt.", "winreg.")) and isinstance(node.ctx, ast.Load):
                pass  # сам import уже признак; атрибуты не дублируем
            elif dotted.endswith(".windll") or dotted == "windll" or dotted.endswith(".WinDLL"):
                hit(node, "windll")
        elif _windows_command(node):
            hit(node, "windows-command")
        elif isinstance(node, ast.Compare) and _platform_compare(node):
            hit(node, "platform-branch")
        elif isinstance(node, ast.Call) and _dotted(node.func) == "sys.platform.startswith":
            hit(node, "platform-branch")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            value = node.value
            if _SHELLS.fullmatch(value.strip()) or re.match(r"(?:powershell|pwsh)(?:\.exe)?\s", value, re.I):
                hit(node, "powershell")
            elif _PS1.search(value):
                hit(node, ".ps1")
            elif _LOCALAPPDATA.search(value):
                hit(node, "LOCALAPPDATA")
            elif id(node) in asserted and _windows_path_literal(value):
                hit(node, "backslash-path")
    return sorted(set(hits))


def _marker_reason(node) -> str | None:
    """Причина из ``pytest.mark.windows_only(reason=...)``; "" без причины; None — не маркер."""
    target = node.func if isinstance(node, ast.Call) else node
    if _dotted(target) not in (f"pytest.mark.{MARKER}", f"mark.{MARKER}"):
        return None
    if not isinstance(node, ast.Call):
        return ""
    for kw in node.keywords:
        if kw.arg == "reason" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value.strip()
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value.strip()
    return ""


def marker_of(source: str) -> str | None:
    """Причина модульного ``pytestmark`` с windows_only; None — файл не помечен.

    Пустая строка — помечен без причины (guard такое не пропускает)."""
    tree = _parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
        for value in values:
            reason = _marker_reason(value)
            if reason is not None:
                return reason
    return None


def stray_markers(source: str) -> list[int]:
    """Строки, где windows_only стоит не на модуле: разбиение идёт по файлам,
    так что маркер на отдельном тесте ничего бы не перенёс на Windows."""
    tree = _parse(source)
    module_level = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark"
                                                for t in node.targets):
            module_level.update(id(n) for n in ast.walk(node))
    return sorted({n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Attribute) and n.attr == MARKER and id(n) not in module_level})


def test_files() -> list[Path]:
    return sorted(TESTS.glob("test_*.py"))


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def windows_files() -> list[Path]:
    return [p for p in test_files() if marker_of(_source(p)) is not None]


def select(platform: str) -> list[Path]:
    """Файлы задания ``platform``: Linux — всё непомеченное; Windows — помеченное и дымовой набор."""
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {PLATFORMS}")
    marked = set(windows_files())
    if platform == "linux":
        return [p for p in test_files() if p not in marked]
    smoke = {ROOT / name for name in WINDOWS_SMOKE}
    return sorted(marked | smoke)


def report() -> str:
    linux, marked = select("linux"), windows_files()
    lines = [f"{len(test_files())} test files: {len(linux)} on Linux, {len(marked)} marked windows_only "
             f"(+{len(WINDOWS_SMOKE)} smoke files also run on Windows)"]
    for path in marked:
        lines.append(f"  {path.name}: {marker_of(_source(path))}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in (*PLATFORMS, "report"):
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: acl_test_platform.py linux|windows|report", file=sys.stderr)
        return 2
    if argv[0] == "report":
        print(report())
        return 0
    for path in select(argv[0]):
        print(path.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
