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
# Имя переменной целиком, без учёта регистра: только внутри вызовов os.environ / os.getenv.
_WIN_ENV_NAME = re.compile(r"LOCALAPPDATA|APPDATA|USERPROFILE|PROGRAMDATA|PROGRAMFILES|SYSTEMROOT|COMSPEC", re.I)
_PS1 = re.compile(r"\.ps1\b", re.I)
# Переменные окружения Windows (имя целиком, не часть слова).
# Регистр важен: «AppData» в строке пути — данные, «APPDATA» — переменная окружения.
_WIN_ENV = re.compile(r"(?<![A-Za-z])((?i:LOCALAPPDATA)|APPDATA|USERPROFILE|PROGRAMDATA|PROGRAMFILES|SYSTEMROOT|COMSPEC)"
                      r"(?![A-Za-z])")
# Windows-путь: диск («C:\»), переменная («%X%\»), UNC («\\server\»).
_DRIVE = re.compile(r"(?<![A-Za-z])[A-Za-z]:\\")
_ENV_PATH = re.compile(r"%\w+%\\")
_UNC = re.compile(r"^\\\\[\w.$-]+\\")
_REGEX_META = re.compile(r"[\^$*+?()\[\]{}|]")
# Сегмент пути без пробелов по краям: «A \ B» (разделитель групп) — не путь.
_SEGMENT = re.compile(r"[\w.%~-](?:[\w .%~-]*[\w.%~-])?")
# Программы, которых нет вне Windows: первый элемент списка-команды.
# Короткие слова (where, reg, net) считаются командой только с «.exe»: без
# суффикса это слишком часто просто данные.
_WINDOWS_COMMANDS = {"cmd", "taskkill", "tasklist", "robocopy", "icacls", "wmic", "schtasks",
                     "powershell", "pwsh", "where.exe", "reg.exe", "net.exe", "attrib.exe"}
# Строка-команда с shell=True: первое слово — Windows-программа или встроенная команда cmd.
_WINDOWS_SHELL_WORDS = _WINDOWS_COMMANDS | {"dir", "del", "copy", "xcopy", "ver", "start", "cls", "mklink",
                                            "findstr", "ipconfig", "systeminfo", "sc", "reg", "where"}
_WINDOWS_MODULES = {"msvcrt", "winreg", "_winreg", "_winapi", "winsound", "pywintypes", "pythoncom", "wmi",
                    "winerror", "servicemanager"}
_PLATFORM_CONSTS = {"win32", "windows", "cygwin", "win", "nt"}  # в нижнем регистре: сравнение без учёта регистра
_WINDLL = ("windll", "WinDLL", "oledll")
# Имена, которые существуют только на Windows (атрибут, имя из from-импорта или getattr).
_WINDOWS_ATTRS = {"ProactorEventLoop", "WindowsSelectorEventLoopPolicy", "WindowsProactorEventLoopPolicy",
                  "startfile", "CREATE_NO_WINDOW", "CREATE_NEW_CONSOLE", "CREATE_NEW_PROCESS_GROUP",
                  "DETACHED_PROCESS", "CTRL_BREAK_EVENT", "getwindowsversion", "STARTUPINFO",
                  "STARTF_USESHOWWINDOW", "SW_HIDE", "WinError", *_WINDLL}
_PLATFORM_HELPER = re.compile(r"^_*(?:is_?windows|on_?windows|is_?win|is_?win32|is_?nt)$", re.I)
_SUBPROCESS_CALLS = {"subprocess.run", "subprocess.call", "subprocess.Popen", "subprocess.check_call",
                     "subprocess.check_output", "os.system", "os.popen"}
_IMPORT_CALLS = {"pytest.importorskip", "importlib.import_module", "__import__"}


def _is_windows_module(name: str) -> bool:
    root = name.split(".")[0]
    return root in _WINDOWS_MODULES or root.startswith("win32")


def _windows_path_literal(value: str, *, loose: bool = True) -> bool:
    """Windows-путь в строке. ``loose`` — ещё и «a\\b» без метасимволов регулярок
    (так — только в утверждениях: вне их это слишком часто данные)."""
    if "\\" not in value:
        return False
    if _DRIVE.search(value) or _ENV_PATH.search(value) or _UNC.match(value):
        return True
    if _REGEX_META.search(value) or "\n" in value or re.match(r"\\[0-9.]", value):
        return False  # регулярка или обратная ссылка: «\.xlsx», «\1false»
    parts = value.split("\\")
    named = [p for p in parts if p]
    if not named or not all(_SEGMENT.fullmatch(p) for p in named):
        return False
    # «..\escape», «\config\», «a\b»: относительный путь с обратной косой — где угодно.
    # Одиночный короткий сегмент («\d», «\w») — скорее регулярка, чем путь.
    return len(named) >= 2 or (loose and len(parts) >= 2) or len(named[0]) >= 3 or ".." in named


def _aliases(tree) -> dict[str, str]:
    """Имя в файле → настоящее: ``import os as _o`` даёт ``_o -> os``, а
    ``from sys import platform`` даёт ``platform -> sys.platform``."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    found[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                found[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return found


def _dotted(node, aliases: dict[str, str] | None = None) -> str:
    """«os.name», «sys.platform», «platform.system()» для цепочки атрибутов и вызова."""
    aliases = aliases or {}
    if isinstance(node, ast.Call):
        inner = _dotted(node.func, aliases)
        return f"{inner}()" if inner else ""
    names = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Call):  # platform.system().lower
        base = _dotted(node, aliases)
        if not base:
            return ""
        names.append(base)
        return ".".join(reversed(names))
    if isinstance(node, ast.Name):
        names.append(aliases.get(node.id, node.id))
        return ".".join(reversed(names))
    return ""


def _parse(source: str):
    # Предупреждения вида «invalid escape sequence» в чужом тесте — не наше дело.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(source)


def _first_word(text: str) -> str:
    words = text.strip().replace("\\", "/").split()
    word = words[0].rsplit("/", 1)[-1].lower() if words else ""
    return word[:-4] if word.endswith(".exe") and word[:-4] in _WINDOWS_COMMANDS else word


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


def _leading_text(node) -> str | None:
    """Начало строки-команды: константа, f-строка, «'cmd ' + x», «'cmd %s' % x»."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        return _leading_text(node.values[0])
    if isinstance(node, ast.BinOp):
        return _leading_text(node.left)
    return None


def _shell_string_command(node, aliases) -> bool:
    """``subprocess.run('taskkill /F /PID 1', shell=True)`` и ``os.system('cmd /c x')``."""
    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = _dotted(node.func, aliases)
    if func not in _SUBPROCESS_CALLS:
        return False
    text = _leading_text(node.args[0])
    if text is None:
        return False
    if func.startswith("subprocess.") and not any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords):
        return False
    word = _first_word(text)
    return word in _WINDOWS_SHELL_WORDS or word.endswith((".bat", ".cmd", ".ps1"))


def _docstring_ids(tree) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _string_consts(node) -> set[str]:
    """Строки стороны сравнения, в том числе внутри ``("nt",)`` / ``["win32"]``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return {c for e in node.elts for c in _string_consts(e)}
    return set()


def _platform_compare(node, aliases=None) -> bool:
    """``os.name == "nt"``, ``os.name in ("nt",)``, ``"win" in sys.platform``,
    ``platform.system() == "Windows"``; ``from sys import platform`` и ``import os as _o`` тоже."""
    if not isinstance(node, ast.Compare):
        return False
    sides = [node.left, *node.comparators]
    names = {re.sub(r"\.(?:lower|upper|casefold)\(\)$", "", _dotted(s, aliases)) for s in sides}
    consts = {c.casefold() for s in sides for c in _string_consts(s)}
    return bool(("os.name" in names and "nt" in consts)
                or ({"sys.platform", "platform.system()", "platform.platform()"} & names
                    and consts & _PLATFORM_CONSTS))


def _platform_string(expr: str) -> bool:
    """Строковое условие ``skipif("sys.platform == 'win32'")``."""
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError:
        return False
    return any(_platform_compare(n, {}) or (isinstance(n, ast.Call) and
                                            _dotted(n.func).endswith("platform.startswith"))
               for n in ast.walk(tree))


def _condition_ids(tree) -> set[int]:
    """Узлы внутри условий (if, тернарное, assert, skipif): там помощник вроде IS_WINDOWS что-то решает."""
    ids = set()
    for node in ast.walk(tree):
        tests = []
        if isinstance(node, (ast.If, ast.IfExp, ast.While, ast.Assert)):
            tests = [node.test]
        elif isinstance(node, ast.Call) and _dotted(node.func).split(".")[-1] in ("skipif", "skipIf", "skipUnless"):
            tests = list(node.args[:1])
        for t in tests:
            ids.update(id(n) for n in ast.walk(t))
    return ids


def windows_signals(source: str) -> list[tuple[int, str, str]]:
    """(строка, правило, текст) каждого Windows-признака, кроме строк с waiver.

    Разбор по AST: строка в комментарии или в докстринге — не признак, а
    ``import msvcrt`` внутри теста — признак, даже если тест его «прячет»
    за ``if os.name == "nt"`` (это само по себе второй признак).

    Правила: модули Windows (в import, importorskip, import_module, __import__);
    платформенные ветки (os.name, sys.platform, platform.system(), в том числе
    через ``in``, from-импорт и псевдоним, и строкой в skipif); помощники
    IS_WINDOWS из импорта в условиях; Windows-команды списком или строкой с
    shell=True; переменные окружения Windows; API, которых нет вне Windows
    (windll, ProactorEventLoop, os.startfile, CREATE_NO_WINDOW, ...); Windows-пути
    с обратной косой и голая константа из одной обратной косой."""
    tree = _parse(source)
    lines = source.splitlines()
    aliases = _aliases(tree)
    imported = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    docstrings = _docstring_ids(tree)
    conditions = _condition_ids(tree)
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
                if _is_windows_module(alias.name):
                    hit(node, alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if _is_windows_module(module):
                hit(node, module)
            for a in node.names:
                if module == "ctypes" and a.name in _WINDLL:
                    hit(node, "windll")
                elif a.name in _WINDOWS_ATTRS and a.name not in _WINDLL:
                    hit(node, "windows-api")
        elif isinstance(node, ast.Attribute):
            if node.attr in _WINDLL:
                hit(node, "windll")
            elif node.attr in _WINDOWS_ATTRS and isinstance(node.ctx, ast.Load):
                hit(node, "windows-api")
            elif _PLATFORM_HELPER.match(node.attr) and id(node) in conditions:
                hit(node, "platform-helper")
        elif isinstance(node, ast.Name):
            if node.id == "windll":
                hit(node, "windll")
            elif _PLATFORM_HELPER.match(node.id) and id(node) in conditions and node.id in imported:
                hit(node, "platform-helper")
            elif node.id in _WINDOWS_ATTRS and node.id in imported and isinstance(node.ctx, ast.Load):
                hit(node, "windows-api")
        elif _windows_command(node):
            hit(node, "windows-command")
        elif isinstance(node, ast.Compare):
            if _platform_compare(node, aliases):
                hit(node, "platform-branch")
        elif isinstance(node, ast.Call):
            func = _dotted(node.func, aliases)
            first = node.args[0] if node.args else None
            text = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
            if func in ("os.getenv", "os.environ.get", "os.environ.pop", "os.environ.setdefault")                     and text is not None and _WIN_ENV_NAME.fullmatch(text):
                hit(node, text.upper())  # userprofile, LocalAppData: Windows сравнивает без учёта регистра
            if func.endswith("platform.startswith") and func.split(".")[0] in ("sys", "platform"):
                hit(node, "platform-branch")
            elif func.split(".")[-1] in ("skipif", "skipIf") and text is not None and _platform_string(text):
                hit(node, "platform-branch")
            elif func in _IMPORT_CALLS and text is not None and _is_windows_module(text):
                hit(node, text.split(".")[0])
            elif func == "getattr" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant) \
                    and node.args[1].value in _WINDOWS_ATTRS:
                hit(node, "windll" if node.args[1].value in _WINDLL else "windows-api")
            elif _shell_string_command(node, aliases):
                hit(node, "windows-command")
        elif isinstance(node, ast.Subscript) and _dotted(node.value, aliases) == "os.environ":
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and _WIN_ENV_NAME.fullmatch(key.value):
                hit(node, key.value.upper())
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            value = node.value
            env = _WIN_ENV.search(value)
            if _SHELLS.fullmatch(value.strip()) or re.match(r"(?:powershell|pwsh)(?:\.exe)?\s", value, re.I):
                hit(node, "powershell")
            elif _PS1.search(value):
                hit(node, ".ps1")
            elif env:
                hit(node, env.group(1).upper())
            elif value == "\\" or _windows_path_literal(value, loose=id(node) in asserted):
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
