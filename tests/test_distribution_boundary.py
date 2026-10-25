"""What ACL ships, kept true by a test rather than by a document.

The repository holds more than the product: alongside the dashboard and the RPA
there is a content machine, a knowledge base, a Jira agent and an older publish
path. All of it is copied onto every media buyer's machine, and every package
in requirements.txt is installed there — including ones only the parts they
never run need (audit finding F-19).

A manifest in prose goes stale the first time someone adds an import. These
tests hold the two properties that make the manifest true:

* nothing that ships imports a package we decided not to ship;
* every package that ships is actually imported by something that ships.

The second one is what catches the opposite mistake: psutil was missing from
requirements entirely while browser.py depended on it, so a clean install could
not detect a running profile.

"Ships" means: reachable from `serve.py` (the dashboard) or from the RPA. See
docs/DISTRIBUTION.md for the manifest itself.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# Packages that travel to a buyer's machine.
SHIPPING_TREES = [
    "config",
    "src/analysis_agent",
    "src/campaign_bridge",
    "src/creative_agent",
    "src/fb_ads_rpa",
    "src/knowledge_agent",
    "src/meta_campaign_agent",
    "src/upload_agent",
    "src/web",
]
#: Одиночные файлы продукта — те, что лежат не внутри перечисленных пакетов.
#: Такой файл легко не заметить: список выше перечисляет папки, и новый модуль
#: рядом с ними в обход не попадает. `src/acl_sentry.py` так и появился, и
#: проверка поймала это с другого конца — пожаловалась, что `sentry-sdk` в
#: requirements никому не нужен.
SHIPPING_FILES = ["serve.py", "src/acl_sentry.py"]

# Deliberately NOT part of ACL. Their dependencies must not be in the file a
# buyer's install reads.
# knowledge_agent looked like a separate project and is not: seven ACL modules
# import it (analyze_fb_stats, morning_report, adset_helpers, …) and it needs
# nothing but pandas. This test is what corrected that assumption.
NON_SHIPPING_TREES = [
    "src/content_machine",
    "src/jira_agent",
    "src/fb_publish_agent",
]

# import name -> distribution name in requirements.txt
_DIST = {
    "dotenv": "python-dotenv",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "telegram": "python-telegram-bot",
    "google": "google-api-python-client",
    "googleapiclient": "google-api-python-client",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "google_auth_httplib2": "google-auth-httplib2",
    "PIL": "pillow",
    "sentry_sdk": "sentry-sdk",
    "imageio_ffmpeg": "imageio-ffmpeg",
}

_LOCAL = {"src", "config", "tests", "scripts"}


def _requirements() -> set[str]:
    text = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
    out = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[=<>!\[]", line, maxsplit=1)[0].strip()
        if name:
            out.add(name.lower())
    return out


def _python_files(trees: list[str], files: list[str] | None = None) -> list[Path]:
    found: list[Path] = []
    for t in trees:
        found.extend(sorted((_ROOT / t).rglob("*.py")))
    for f in files or []:
        p = _ROOT / f
        if p.is_file():
            found.append(p)
    return found


def _imports(path: Path) -> tuple[set[str], set[str]]:
    """(hard, guarded) top-level package names imported by this file.

    "Guarded" means the import sits inside a try or a function — the module
    loads without the package, and the feature degrades instead of the app
    failing to start. That distinction is the whole point: a guarded import may
    legitimately be absent from requirements, a hard one may not.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return set(), set()

    guarded_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Try, ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                guarded_nodes.add(id(child))

    hard: set[str] = set()
    soft: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        for n in names:
            if n in sys.stdlib_module_names or n in _LOCAL:
                continue
            (soft if id(node) in guarded_nodes else hard).add(n)
    return hard, soft - hard


def _dist(name: str) -> str:
    return _DIST.get(name, name).lower()


def test_shipping_code_declares_every_hard_dependency():
    """A clean install must not die on an import nobody declared."""
    reqs = _requirements()
    missing: list[str] = []
    for f in _python_files(SHIPPING_TREES, SHIPPING_FILES):
        hard, _ = _imports(f)
        for name in sorted(hard):
            if _dist(name) not in reqs:
                missing.append(f"{f.relative_to(_ROOT)} импортирует {name} ({_dist(name)})")
    assert not missing, (
        "Код, который уезжает байеру, импортирует пакеты, не объявленные в "
        "requirements.txt. Свежая установка упадёт на старте:\n  "
        + "\n  ".join(sorted(set(missing)))
    )


def test_nothing_shipping_pulls_a_non_shipping_dependency():
    """The boundary, stated as code.

    A hard import of a content-machine dependency inside the dashboard would
    mean the boundary exists only in the document.
    """
    non_shipping_only = {"telegram", "pyperclip", "bs4"}
    crossings: list[str] = []
    for f in _python_files(SHIPPING_TREES, SHIPPING_FILES):
        hard, _ = _imports(f)
        for name in sorted(hard & non_shipping_only):
            crossings.append(f"{f.relative_to(_ROOT)} → {name}")
    assert not crossings, (
        "Код ACL зависит от пакетов, которые мы решили не раздавать:\n  "
        + "\n  ".join(crossings)
    )


def test_requirements_carry_nothing_only_the_rest_of_the_repo_needs():
    """Every buyer installs this file; it should not carry other projects' deps."""
    reqs = _requirements()
    ship_names: set[str] = set()
    for f in _python_files(SHIPPING_TREES, SHIPPING_FILES):
        hard, soft = _imports(f)
        ship_names |= {_dist(n) for n in hard | soft}
    # Server and template plumbing is pulled in by FastAPI rather than imported
    # by name anywhere in our code.
    ship_names |= {"uvicorn", "jinja2", "google-auth-httplib2", "python-multipart"}
    # ACL-1767: `zoneinfo` на Windows берёт базу часовых поясов из пакета tzdata (в импортах его нет).
    ship_names |= {"tzdata"}

    stray = sorted(r for r in reqs if r not in ship_names)
    assert not stray, (
        "В requirements.txt пакеты, которые нужны только не-ACL частям "
        "репозитория. Каждый байер ставит их зря, и они попадают в обзор "
        "зависимостей как часть продукта:\n  " + "\n  ".join(stray)
    )


@pytest.mark.parametrize("tree", NON_SHIPPING_TREES)
def test_non_shipping_trees_are_not_imported_by_shipping_code(tree):
    """The other way round: ACL must not reach into the parts that stay behind."""
    module = tree.replace("/", ".")
    offenders: list[str] = []
    for f in _python_files(SHIPPING_TREES, SHIPPING_FILES):
        text = f.read_text(encoding="utf-8", errors="ignore")
        if re.search(rf"^\s*(from|import)\s+{re.escape(module)}\b", text, re.M):
            offenders.append(str(f.relative_to(_ROOT)))
    assert not offenders, (
        f"ACL импортирует {module}, который мы не раздаём: " + ", ".join(offenders)
    )
