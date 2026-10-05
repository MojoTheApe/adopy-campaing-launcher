"""ACL-1582: tests that need Windows say so, and the guard catches those that do not.

The Linux CI job runs every test file without the ``windows_only`` marker. A
file that uses Windows-only APIs or assumptions without the marker would fail
there, or quietly skip its Windows-only part and lose that coverage. The guard
below reads every test file (AST, not text search) and refuses such a file.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import acl_test_platform as tp  # noqa: E402


def _rules(source: str) -> set[str]:
    return {rule for _, rule, _ in tp.windows_signals(textwrap.dedent(source))}


# --- the guard over the real tree ---------------------------------------------------------------

def test_no_unmarked_test_file_uses_windows_only_apis():
    offenders = []
    for path in tp.select("linux"):
        for line, rule, text in tp.windows_signals(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}:{line} [{rule}] {text}")
    assert not offenders, (
        "These test files use Windows-only APIs or paths but run on Linux. Add\n"
        '    pytestmark = pytest.mark.windows_only(reason="...")\n'
        "or, for a line that only mentions such a thing as data, a `# linux-ok: <why>` comment:\n  "
        + "\n  ".join(offenders))


def test_every_marked_file_states_its_reason():
    missing = [p.name for p in tp.windows_files() if not tp.marker_of(p.read_text(encoding="utf-8"))]
    assert not missing, f"windows_only without a reason: {missing}"


def test_the_marker_is_only_used_on_whole_files():
    stray = {p.name: tp.stray_markers(p.read_text(encoding="utf-8")) for p in tp.test_files()}
    stray = {k: v for k, v in stray.items() if v}
    assert not stray, f"windows_only must be a module-level pytestmark (CI splits by file): {stray}"


def test_the_marker_is_registered_for_pytest():
    text = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert 'addinivalue_line(\n        "markers",\n        "windows_only(reason)' in text


# --- the selection --------------------------------------------------------------------------------

def test_linux_and_windows_cover_the_whole_suite_and_marked_files_never_reach_linux():
    linux, windows, marked = set(tp.select("linux")), set(tp.select("windows")), set(tp.windows_files())
    assert marked, "at least the launcher contract needs Windows"
    assert linux | marked == set(tp.test_files())
    assert not linux & marked
    smoke = {ROOT / name for name in tp.WINDOWS_SMOKE}
    assert windows == marked | smoke
    # Дымовой набор нейтрален: он идёт и на Linux, и на Windows.
    assert all(p.is_file() for p in smoke) and smoke <= linux


def test_unknown_platform_is_refused():
    with pytest.raises(ValueError):
        tp.select("macos")


def test_marking_a_file_moves_it_from_linux_to_windows(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_plain.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    (tests / "test_locks.py").write_text(
        "import pytest\n"
        "pytestmark = pytest.mark.windows_only(reason='msvcrt')\n"
        "def test_y():\n    assert 1\n", encoding="utf-8")
    (tests / "test_smoke.py").write_text("def test_z():\n    assert 1\n", encoding="utf-8")
    monkeypatch.setattr(tp, "ROOT", tmp_path)
    monkeypatch.setattr(tp, "TESTS", tests)
    monkeypatch.setattr(tp, "WINDOWS_SMOKE", ("tests/test_smoke.py",))
    assert [p.name for p in tp.select("linux")] == ["test_plain.py", "test_smoke.py"]
    assert [p.name for p in tp.select("windows")] == ["test_locks.py", "test_smoke.py"]
    assert "test_locks.py: msvcrt" in tp.report()


def test_the_command_lists_repo_relative_files(capsys):
    assert tp.main(["windows"]) == 0
    out = capsys.readouterr().out.split()
    assert out == [p.relative_to(ROOT).as_posix() for p in tp.select("windows")]
    assert tp.main(["solaris"]) == 2


# --- the guard's rules ----------------------------------------------------------------------------

# Каждая строка — самостоятельный образец, который guard обязан поймать.
CAUGHT = {
    "import msvcrt": "msvcrt",
    "from msvcrt import locking": "msvcrt",
    "import winreg": "winreg",
    "import ctypes\nctypes.windll.kernel32.GetShortPathNameW('x', None, 0)": "windll",
    "if os.name == 'nt':\n    pass": "platform-branch",
    "if os.name != \"nt\":\n    pass": "platform-branch",
    "@pytest.mark.skipif(sys.platform != 'win32', reason='x')\ndef test_a():\n    pass": "platform-branch",
    "WIN = sys.platform.startswith('win')": "platform-branch",
    "subprocess.run(['powershell.exe', '-NoProfile', '-File', 'x'])": "powershell",  # linux-ok: guard sample
    "subprocess.run(['pwsh', '-c', 'x'])": "powershell",  # linux-ok: guard sample
    "HARNESS = 'tests/bootstrap_windows_harness.ps1'": ".ps1",  # linux-ok: guard sample
    "base = os.environ['LOCALAPPDATA']": "LOCALAPPDATA",  # linux-ok: guard sample
    "assert path == 'C:\\\\Users\\\\buyer\\\\ACL'": "backslash-path",  # linux-ok: guard sample
    "assert 'scripts\\\\acl_smoke.py' in text": "backslash-path",  # linux-ok: guard sample
    "subprocess.run(['taskkill', '/F', '/T', '/PID', '1'])": "windows-command",
    "subprocess.run(['cmd.exe', '/c', 'x'])": "windows-command",
    "subprocess.run(['install_acl.bat'])": "windows-command",
    # ACL-1582 review: каждый из этих образцов раньше проходил молча.
    "platform.system() == 'Windows'": "platform-branch",
    "from platform import system\nIS = system() == 'Windows'": "platform-branch",
    "if os.name in ('nt',):\n    pass": "platform-branch",
    "WIN = 'win' in sys.platform": "platform-branch",
    "from sys import platform\nif platform == 'win32':\n    pass": "platform-branch",
    "from os import name\nif name == 'nt':\n    pass": "platform-branch",
    "import os as _o\nif _o.name == 'nt':\n    pass": "platform-branch",
    "import sys as _s\nif _s.platform.startswith('win'):\n    pass": "platform-branch",
    "@pytest.mark.skipif(\"sys.platform == 'win32'\", reason='x')\ndef test_a():\n    pass": "platform-branch",
    "@pytest.mark.skipif('os.name != \"nt\"', reason='x')\ndef test_a():\n    pass": "platform-branch",
    "pytest.importorskip('msvcrt')": "msvcrt",
    "importlib.import_module('msvcrt')": "msvcrt",
    "from importlib import import_module\nimport_module('winreg')": "winreg",
    "__import__('winreg')": "winreg",
    "import win32api": "win32api",
    "import win32con": "win32con",
    "import pywintypes": "pywintypes",
    "import wmi": "wmi",
    "from config.paths import IS_WINDOWS\n@pytest.mark.skipif(not IS_WINDOWS, reason='x')\ndef test_a():\n    pass": "platform-helper",
    "from helpers import is_windows\nif is_windows():\n    pass": "platform-helper",
    "from config import paths\nif paths.IS_WINDOWS:\n    pass": "platform-helper",
    "subprocess.run('taskkill /F /IM chrome.exe', shell=True)": "windows-command",
    "subprocess.check_output('cmd /c dir', shell=True)": "windows-command",
    "os.system('powershell -c x')": "windows-command",
    "subprocess.run(f'robocopy {a} {b}', shell=True)": "windows-command",
    "home = os.environ['USERPROFILE']": "USERPROFILE",  # linux-ok: guard sample
    "base = os.getenv('APPDATA')": "APPDATA",  # linux-ok: guard sample
    "base = os.environ.get('PROGRAMDATA')": "PROGRAMDATA",  # linux-ok: guard sample
    "loop = asyncio.ProactorEventLoop()": "windows-api",
    "asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())": "windows-api",
    "os.startfile('x')": "windows-api",
    "subprocess.Popen(['x'], creationflags=subprocess.CREATE_NO_WINDOW)": "windows-api",
    "from subprocess import CREATE_NO_WINDOW": "windows-api",
    "getattr(ctypes, 'windll')": "windll",
    "from ctypes import windll": "windll",
    "x = getattr(subprocess, 'CREATE_NO_WINDOW')": "windows-api",
    "PATH = 'C:\\\\Users\\\\x'": "backslash-path",  # linux-ok: guard sample
    "SHARE = '\\\\\\\\server\\\\share'": "backslash-path",  # linux-ok: guard sample
    "BS = '\\\\'": "backslash-path",  # linux-ok: guard sample
    "parts = name.split('\\\\')": "backslash-path",  # linux-ok: guard sample
    # ACL-1582 round 2
    "x = {'id': '..\\\\escape'}": "backslash-path",  # linux-ok: guard sample
    "root = p.rsplit('\\\\config\\\\', 1)[0]": "backslash-path",  # linux-ok: guard sample
    "name = 'a\\\\b'": "backslash-path",  # linux-ok: guard sample
    "if platform.system().lower() == 'windows':\n    pass": "platform-branch",
    "if os.name.lower() == 'nt':\n    pass": "platform-branch",
    "base = os.environ.get('userprofile')": "USERPROFILE",  # linux-ok: guard sample
    "base = os.getenv('LocalAppData')": "LOCALAPPDATA",  # linux-ok: guard sample
    "base = os.environ['ProgramData']": "PROGRAMDATA",  # linux-ok: guard sample
    "from helpers import on_windows\nif on_windows():\n    pass": "platform-helper",
    "from helpers import is_win\nif is_win:\n    pass": "platform-helper",
    "from helpers import is_nt\nif not is_nt:\n    pass": "platform-helper",
    "from helpers import is_win32\nif is_win32:\n    pass": "platform-helper",
}


@pytest.mark.parametrize("source,rule", sorted(CAUGHT.items()))
def test_the_guard_catches_an_unmarked_windows_only_test(source, rule):
    assert rule in _rules(source)


def test_the_guard_reports_line_and_text():
    hits = tp.windows_signals("x = 1\n\nimport msvcrt\n")
    assert hits == [(3, "msvcrt", "import msvcrt")]


@pytest.mark.parametrize("source", [
    '"""Docstring about msvcrt, %LOCALAPPDATA% and powershell.exe is prose."""\n',  # linux-ok: guard sample
    "# import msvcrt   (a comment)\nx = 1\n",
    "assert re.search(r'\\d+\\.\\d+', text)\n",                # регулярка, не путь
    "assert groups('A \\\\ B') == ['A', 'B']\n",               # разделитель, не путь
    "assert p.as_posix() == 'tests/test_x.py'\n",
    "monkeypatch.setattr(rt.sys, 'platform', 'win32')\n",     # подмена платформы — нейтральна
    "rows = ['where', 'what']\n",
    "subprocess.run('taskkill /F /PID 1')\n",                  # без shell=True строка-команда не идёт через cmd
    "x = os.environ.get('userprofile_note')\n",                # имя целиком, не часть слова
    "html = re.sub(r'a', r'\\1b', s)\n",                        # обратная ссылка регулярки
    "x = 'taskkill'\n",                                       # упоминание, не команда
    "p = 'C:/Users/x/AppData/Local/ACL'\n",                    # AppData в строке пути — данные
    "subprocess.run('echo hi', shell=True)\n",                 # shell-строка не с Windows-программы
])
def test_the_guard_ignores_platform_neutral_code(source):
    assert tp.windows_signals(source) == []


def test_a_waiver_needs_a_reason_and_covers_only_its_line():
    assert tp.windows_signals("H = 'x.ps1'  # linux-ok: data for a classifier\n") == []
    assert _rules("H = 'x.ps1'  # linux-ok:\n") == {".ps1"}
    assert _rules("# linux-ok: elsewhere\nH = 'x.ps1'\n") == {".ps1"}


# --- the marker ------------------------------------------------------------------------------------

@pytest.mark.parametrize("source,reason", [
    ("pytestmark = pytest.mark.windows_only(reason='msvcrt locks')", "msvcrt locks"),
    ("pytestmark = pytest.mark.windows_only('cmd interpreter')", "cmd interpreter"),
    ("pytestmark = [pytest.mark.slow, pytest.mark.windows_only(reason='bat')]", "bat"),
    ("pytestmark = pytest.mark.windows_only", ""),
    ("pytestmark = pytest.mark.windows_only(reason='  ')", ""),
    ("pytestmark = pytest.mark.slow", None),
    ("x = 1", None),
])
def test_marker_of_reads_the_module_marker_and_its_reason(source, reason):
    assert tp.marker_of(source) == reason


def test_a_test_level_marker_does_not_mark_the_file_and_is_reported():
    source = "import pytest\n\n@pytest.mark.windows_only(reason='x')\ndef test_a():\n    pass\n"
    assert tp.marker_of(source) is None
    assert tp.stray_markers(source) == [3]
    assert tp.stray_markers("pytestmark = pytest.mark.windows_only(reason='x')\n") == []
