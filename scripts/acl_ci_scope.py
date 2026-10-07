"""Select expensive Windows checks conservatively; never skip unknown changes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

try:
    from scripts import acl_ci_reuse as reuse
except ImportError:  # запуск как python3 scripts/acl_ci_scope.py
    import acl_ci_reuse as reuse

ROOT_DOCS = {"README.md", "AGENTS.md", "CLAUDE.md", "AGENT_CONTEXT.md", "CHANGELOG.md"}
TRACKER_METADATA = {"tracker/config.json", "tracker/engine.json"}

# ACL-1581: PR проверяется на одной версии Python, полный набор — push-прогоны
# (stage/main/master) и ручной запуск, они же доказательство для ACL-1565 и релиза.
PR_PYTHON_VERSIONS = reuse.BASE_PYTHON_VERSIONS
# ACL-1649: «лёгкий» контур — PR в stage и push в stage: одна версия Python и Windows-установщик
# только при изменении его входов. 3.11 и установщик обязательны перед main: PR в main/master,
# push в main/master, релизные теги и ручной запуск. Неизвестная база — тоже полный контур.
LIGHT_BRANCH = "stage"
# Входы установщика, поставки и зависимостей: их изменение требует сборки и проверки установщика
# уже на PR в stage. Содержимое приложения (src/, config/, knowledge_base/) сюда не входит:
# собранный пакет проверяется всегда на пути в main (ACL-1649, spec 2).
INSTALLER_PREFIXES = ("packaging/", ".github/workflows/", "assets/")
INSTALLER_FILES = {
    "delivery.json", ".env.example", "pyproject.toml", "setup.py", "setup.cfg", ".python-version",
    "src/web/acl_update.py", "src/web/machine_paths.py", "src/web/release_signing.py",
    "src/web/release_updates.py", "scripts/acl_runtime_manager.py",   # скрытый импорт пакета (acl_windows.spec)
}
# Данные, которые acl_windows.spec кладёт в пакет. Правка содержимого установщик не ломает (путь в main
# всё равно проверяет его целиком), но добавление, удаление или переименование такого файла меняет
# состав пакета, поэтому на PR в stage оно запускает установщик. Список сверяет тест со spec.
BUNDLED_FILES = {
    ".env.example", "config/ad_account_map.example.csv", "config/ads_manager_selectors.yaml",
    "config/analysis_reading_rules.json", "config/benchmarks.json", "config/content_plan_week.example.json",
    "config/knowledge_base.json", "config/rpa_forbidden_zones.yaml",
}
BUNDLED_DIRS = ("config/rpa_intents/", "config/rpa_recipes/", "config/rpa_variations/", "knowledge_base/",
                "src/web/static/", "src/web/templates/")


def is_bundled(name):
    return name in BUNDLED_FILES or name.startswith(BUNDLED_DIRS)
INSTALLER_SCRIPT_PREFIXES = (
    "scripts/acl_publish_release", "scripts/acl_release_", "scripts/acl_sign_release", "scripts/acl_smoke",
    "scripts/acl_update_gate", "scripts/acl_scan_secrets", "scripts/acl_ci_",
)
# Входы зависимостей и бутстрапа: PR, задевший любой из них, гоняет обе версии.
DEPENDENCY_FILES = {
    "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "conftest.py", "tests/conftest.py",
    ".python-version",
    "tests/bootstrap_windows_harness.ps1",
    # Сама матрица: деление на шарды и решение о версиях.
    "scripts/acl_test_shard.py", "scripts/acl_test_platform.py", "scripts/acl_ci_scope.py",
    "scripts/acl_ci_reuse.py",
}
DEPENDENCY_PREFIXES = (".github/workflows/", "packaging/")


def touches_dependencies(name):
    """Путь из зависимостей/бутстрапа: requirements*, установщик, .bat-установка, CI."""
    if name in DEPENDENCY_FILES or name.startswith(DEPENDENCY_PREFIXES):
        return True
    return root_dependency_file(name)


def root_dependency_file(name):
    """Корень: requirements*.txt/.in, constraints*.txt и .bat-скрипты установки/запуска."""
    if "/" in name:
        return False
    return (name.startswith("requirements") and name.endswith((".txt", ".in"))
            or name.startswith("constraints") and name.endswith(".txt")
            or name.lower().endswith(".bat"))


def light_context(event_name, base_ref=None, branch=None):
    """Лёгкий контур: PR в stage или push в stage (ACL-1649). Всё остальное — путь в main."""
    if event_name == "pull_request":
        return base_ref == LIGHT_BRANCH
    return event_name == "push" and branch == LIGHT_BRANCH


def python_versions(paths, event_name, base_ref=None, branch=None):
    """Версии Python матрицы ci.yml; при любом сомнении — полный набор."""
    if not light_context(event_name, base_ref, branch) or not paths or any(touches_dependencies(p) for p in paths):
        return list(reuse.FULL_PYTHON_VERSIONS)
    return list(PR_PYTHON_VERSIONS)


def is_documentation(name):
    return name in ROOT_DOCS or name.startswith("docs/") and name.endswith(".md") or name in TRACKER_METADATA


def classify(paths, event_name, base_ref=None, branch=None):
    python = python_versions(paths, event_name, base_ref, branch)
    # Документы без кода пропускают тесты на PR (в любую ветку) и на push в stage.
    skippable = event_name == "pull_request" or light_context(event_name, None, branch)
    if not skippable or not paths:
        return {"windows": True, "linux": True, "reason": "full-base-or-manual-validation", "python": python}
    if not all(is_documentation(name) for name in paths):
        return {"windows": True, "linux": True, "reason": "product-build-or-unknown-change", "python": python}
    # Тесты не запускаются; список версий полный, чтобы матрица никогда не была пустой.
    return {"windows": False, "linux": False, "reason": "documentation-and-tracker-metadata-only",
            "python": list(reuse.FULL_PYTHON_VERSIONS)}


def touches_installer(name):
    """Путь, из-за которого нужна сборка и проверка установщика (поставка, упаковка, зависимости)."""
    if name in INSTALLER_FILES or name.startswith(INSTALLER_PREFIXES + INSTALLER_SCRIPT_PREFIXES):
        return True
    return root_dependency_file(name)


def classify_installer(paths, event_name, base_ref=None, branch=None, structural=()):
    """Нужен ли workflow «Windows installer». На пути в main — всегда; в лёгком контуре — по входам."""
    python = list(reuse.FULL_PYTHON_VERSIONS)
    if not light_context(event_name, base_ref, branch) or not paths:
        return {"windows": True, "linux": False, "reason": "main-release-or-manual-path", "python": python}
    if any(touches_installer(name) for name in paths):
        return {"windows": True, "linux": False, "reason": "installer-packaging-delivery-or-dependency-change",
                "python": python}
    if any(is_bundled(name) for name in structural):
        return {"windows": True, "linux": False, "reason": "bundled-file-added-or-deleted", "python": python}
    return {"windows": False, "linux": False, "reason": "no-installer-inputs-changed", "python": python}


def changed_paths(root, base, head):
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha or "") for sha in (base, head)):
        raise ValueError("Expected exact base/head commits")
    ancestor = subprocess.check_output(["git", "merge-base", base, head], cwd=root, text=True).strip()
    # Disable rename folding so moving/deleting product code cannot look docs-only.
    output = subprocess.check_output(["git", "diff", "--no-renames", "--name-only", "-z", ancestor, head], cwd=root)
    return [p.decode("utf-8", errors="surrogateescape") for p in output.split(b"\0") if p]


def branch_of(ref):
    """Имя ветки из GITHUB_REF; теги и прочее — пустая строка (полный контур)."""
    ref = str(ref or "")
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""


def added_or_deleted_paths(root, base, head):
    """Пути, добавленные или удалённые между базой и головой (rename = удаление + добавление)."""
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha or "") for sha in (base, head)):
        raise ValueError("Expected exact base/head commits")
    ancestor = subprocess.check_output(["git", "merge-base", base, head], cwd=root, text=True).strip()
    output = subprocess.check_output(["git", "diff", "--no-renames", "--name-status", "-z", ancestor, head],
                                     cwd=root).split(b"\0")
    return [output[i + 1].decode("utf-8", errors="surrogateescape")
            for i in range(0, len(output) - 1, 2) if output[i][:1] in (b"A", b"D")]


def event_structural(root, event_name, event):
    if event_name == "pull_request":
        return added_or_deleted_paths(root, event["pull_request"]["base"]["sha"], event["pull_request"]["head"]["sha"])
    if event_name == "push":
        return added_or_deleted_paths(root, event["before"], event["after"])
    return []


def event_paths(root, event_name, event):
    """Список изменённых путей: PR — база..голова; push в stage — before..after; иначе None (полный контур)."""
    if event_name == "pull_request":
        pr = event["pull_request"]
        return changed_paths(root, pr["base"]["sha"], pr["head"]["sha"])
    if event_name == "push":
        return changed_paths(root, event["before"], event["after"])
    return None


def select(root, event_name, event, *, ref="", workflow="ci.yml"):
    classifier = classify_installer if workflow == "windows-installer.yml" else classify
    base_ref = branch = None
    try:
        if event_name == "pull_request":
            base_ref = event["pull_request"]["base"]["ref"]
        branch = branch_of(ref)
        light = light_context(event_name, base_ref, branch)
        paths = event_paths(root, event_name, event) if light else []
        if classifier is classify_installer:
            structural = event_structural(root, event_name, event) if light else []
            return classifier(paths, event_name, base_ref, branch, structural)
        return classifier(paths, event_name, base_ref, branch)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return {"windows": True, "linux": workflow != "windows-installer.yml",
                "reason": "classification-unavailable-run-full", "python": list(reuse.FULL_PYTHON_VERSIONS)}


def wanted_jobs(workflow, result):
    """Задания, которые этот контекст требует (без итогового ``ACL verification``)."""
    if workflow == "ci.yml":
        return reuse.required_jobs(workflow, result["python"], windows=result["windows"], linux=result.get("linux", False))
    return set(reuse.REQUIRED_JOBS[workflow]) if result["windows"] else set()


def decide(root, event_name, event, *, ref="", workflow="ci.yml", repo="", api=None, run_id=None, fetch=None):
    """Классификация, затем (ACL-1565/1649) переиспользование зелёных прогонов того же дерева.

    Покрытие складывается из нескольких прогонов (stage: 3.13 + Windows, PR в main: 3.11).
    Теги релизов и ручной запуск всегда идут полностью; любая ошибка — тоже."""
    result = select(root, event_name, event, ref=ref, workflow=workflow)
    if not result["windows"] and not result.get("linux"):
        return result
    if event_name not in ("push", "pull_request") or str(ref).startswith("refs/tags/") or api is None:
        return result
    try:
        tree = reuse.tree_of(root, "HEAD")
        if not tree:
            return dict(result, reuse="no-proven-full-run")
        # ACL-1649: головы влитых PR всех коммитов с этим деревом (stage, main, merge-коммит PR).
        heads = reuse.collect_pr_heads(root, api, repo, tree, [reuse._git(root, "rev-parse", "HEAD")],
                                       **({"fetch": fetch} if fetch else {}))
        coverage = reuse.find_coverage(root, api, repo, workflow, tree, exclude_run_id=run_id, pr_heads=heads)
    except Exception:  # noqa: BLE001 — сомнение означает полный прогон
        return dict(result, reuse="lookup-failed-run-full")
    if not coverage["runs"]:
        return dict(result, reuse="no-proven-full-run")
    proof = coverage["runs"][0]
    # Ссылка уходит в GITHUB_OUTPUT и сводку: только точный URL запуска этого репозитория.
    if not all(reuse.valid_run_url(r.get("run_url"), repo, r.get("run_id")) for r in coverage["runs"]):
        return dict(result, reuse="invalid-run-url-run-full")
    wanted = wanted_jobs(workflow, result)
    missing = wanted - coverage["covered"]
    if missing == wanted:
        return dict(result, reuse="no-proven-full-run")   # ничего из нужного не покрыто
    shared = {"reused_from": proof["run_url"], "tree": proof["tree"], "commit": proof["commit"],
              "reused_runs": [r["run_url"] for r in coverage["runs"]]}
    if not missing:
        return dict(shared, windows=False, linux=False, reason="reused-green-full-run-of-same-tree",
                    python=list(reuse.FULL_PYTHON_VERSIONS))
    # Частичное покрытие: запускаем только недостающее (например, 3.11 на PR в main).
    pythons = [v for v in result["python"]
               if any(n in missing for n in reuse.required_jobs(workflow, (v,), windows=False))] or list(result["python"])
    windows = any(n.startswith("windows tests") for n in missing) if workflow == "ci.yml" else True
    return dict(shared, windows=windows, linux=any(n.startswith("linux tests") for n in missing),
                reason="partly-reused-green-runs-of-same-tree", python=pythons)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--event-file", type=Path, default=Path(os.environ.get("GITHUB_EVENT_PATH", "")))
    p.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    p.add_argument("--output", type=Path, default=Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None)
    p.add_argument("--summary", type=Path, default=Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None)
    p.add_argument("--workflow", default="ci.yml", choices=sorted(reuse.REQUIRED_JOBS))
    args = p.parse_args()
    try:
        event = json.loads(args.event_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        event = {}
    token = os.environ.get("GITHUB_TOKEN", "")
    api = reuse.github_api(token, os.environ.get("GITHUB_API_URL") or "https://api.github.com") if token else None
    result = decide(Path(__file__).resolve().parents[1], args.event_name, event,
                    ref=os.environ.get("GITHUB_REF", ""), workflow=args.workflow,
                    repo=os.environ.get("GITHUB_REPOSITORY", ""), api=api, run_id=os.environ.get("GITHUB_RUN_ID"))
    if args.event_name == "pull_request":
        # ACL-1649: аннотация задания о реально проверенном дереве (checkout merge-коммита PR):
        # по ней stage и main позже доказывают, что прогон PR проверял именно их дерево.
        tested = reuse.tree_of(Path(__file__).resolve().parents[1], "HEAD")
        if tested:
            print(reuse.marker_line(tested))
    if args.output:
        with args.output.open("a", encoding="utf-8") as f:
            f.write("windows=" + str(result["windows"]).lower() + "\n")
            f.write("linux=" + str(bool(result.get("linux"))).lower() + "\n")
            f.write("reused_from=" + result.get("reused_from", "") + "\n")
            # JSON-список для fromJSON в матрице ci.yml; только известные версии, иначе полный набор.
            versions = [v for v in result.get("python") or () if v in reuse.FULL_PYTHON_VERSIONS]
            f.write("python=" + json.dumps(versions or list(reuse.FULL_PYTHON_VERSIONS)) + "\n")
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            if result.get("reused_from") and not result["windows"] and not result.get("linux"):
                f.write(reuse.summary_line(result["reused_from"], result["tree"]) + "\n")
            else:
                f.write(f"Scope: windows={str(result['windows']).lower()} linux={str(bool(result.get('linux'))).lower()}"
                        f" ({result['reason']}" + (f", {result['reuse']}" if result.get("reuse") else "") + ")"
                        + (f", Python {', '.join(result['python'])}" if result.get("linux") else "") + "\n")
                for url in result.get("reused_runs") or ():
                    f.write(f"Reused passed jobs of the same tree from run {url}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
