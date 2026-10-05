"""ACL-1581: static checks of the CI workflows (the Actions budget means they cannot run here).

- superseded PR runs are cancelled, stage/main/master pushes and manual runs never are;
- the test matrix takes its Python versions from the scope job, with a full fallback;
- the installer build/install job stops at 15 minutes and every step limit fits inside.

Concurrency expressions are evaluated by a small GitHub-expression evaluator below, so
the test checks what the YAML really says, not a copy of it.
"""
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


class _Null:
    """null GitHub: ложь, и любое его свойство — тоже null."""

    def __getattr__(self, key):
        return self

    def __bool__(self):
        return False

    def __eq__(self, other):
        return isinstance(other, _Null)

    __hash__ = object.__hash__

    def __str__(self):
        return ""


NULL = _Null()


class Ctx(dict):
    """Контекст выражения: отсутствующее свойство даёт null, как в GitHub."""

    def __getattr__(self, key):
        value = self.get(key)
        if value is None:
            return NULL
        return Ctx(value) if isinstance(value, dict) else value


def _format(template, *args):
    return re.sub(r"\{(\d+)\}", lambda m: str(args[int(m[1])]), template)


def evaluate(text, github):
    """Подмножество выражений GitHub: свойства github.*, ==, &&, ||, format(), строки."""
    if not isinstance(text, str):
        return text

    def one(expr):
        expr = expr.replace("&&", " and ").replace("||", " or ")
        assert re.fullmatch(r"[\w\s.'(){},=!-]*", expr), expr
        return eval(expr, {"__builtins__": {}}, {"github": Ctx(github), "format": _format})  # noqa: S307

    parts = re.split(r"\$\{\{(.*?)\}\}", text)
    if len(parts) == 3 and parts[0] == parts[2] == "":
        return one(parts[1])
    return "".join(p if i % 2 == 0 else str(one(p)) for i, p in enumerate(parts))


def pr(number, run_id):
    return {"event_name": "pull_request", "ref": f"refs/pull/{number}/merge", "run_id": run_id,
            "event": {"pull_request": {"number": number}}}


def push(branch, run_id):
    return {"event_name": "push", "ref": f"refs/heads/{branch}", "run_id": run_id}


def dispatch(branch, run_id):
    return {"event_name": "workflow_dispatch", "ref": f"refs/heads/{branch}", "run_id": run_id}


def concurrency(name, github):
    block = workflow(name)["concurrency"]
    return evaluate(block["group"], github), evaluate(block["cancel-in-progress"], github)


def cancels(name, earlier, later):
    """Отменит ли запуск ``later`` уже идущий ``earlier`` (или ожидающий в той же группе)."""
    # В одной группе GitHub всегда снимает ожидающий запуск, а при cancel-in-progress — и идущий,
    # поэтому «не отменяет» означает разные группы.
    return concurrency(name, earlier)[0] == concurrency(name, later)[0]


ALL = ["ci.yml", "windows-installer.yml", "tracker-link.yml"]
PUSHED = ["ci.yml", "windows-installer.yml"]


@pytest.mark.parametrize("name", ALL)
def test_a_new_push_to_the_same_pr_cancels_the_superseded_run(name):
    group_a, _ = concurrency(name, pr(581, 1))
    group_b, cancel = concurrency(name, pr(581, 2))
    assert group_a == group_b and cancel is True


@pytest.mark.parametrize("name", ALL)
def test_different_prs_never_cancel_each_other(name):
    assert not cancels(name, pr(581, 1), pr(582, 2))


@pytest.mark.parametrize("name", PUSHED)
@pytest.mark.parametrize("branch", ["stage", "main", "master"])
def test_quick_successive_pushes_never_cancel_each_other(name, branch):
    # Два быстрых сквош-мерджа в stage: проверка первой задачи должна дойти до конца.
    assert not cancels(name, push(branch, 100), push(branch, 101))
    assert not cancels(name, push(branch, 100), push(branch, 102))
    _, cancel = concurrency(name, push(branch, 101))
    assert cancel is False


@pytest.mark.parametrize("name", PUSHED)
def test_manual_runs_are_never_cancelled(name):
    assert not cancels(name, dispatch("stage", 7), dispatch("stage", 8))
    assert concurrency(name, dispatch("stage", 8))[1] is False


@pytest.mark.parametrize("name", PUSHED)
def test_a_pr_run_never_cancels_a_push_run(name):
    assert not cancels(name, push("stage", 100), pr(581, 101))
    assert not cancels(name, pr(581, 100), push("stage", 101))


@pytest.mark.parametrize("name", PUSHED)
def test_push_triggers_are_the_permanent_branches(name):
    assert set(workflow(name)[True]["push"]["branches"]) == {"master", "main", "stage"}


def test_tracker_link_runs_only_on_pull_requests():
    assert set(workflow("tracker-link.yml")[True]) == {"pull_request"}


# --- the Python matrix ------------------------------------------------------------------------------

def test_scope_publishes_the_python_list_and_the_matrix_uses_it():
    ci = workflow("ci.yml")
    assert ci["jobs"]["scope"]["outputs"]["python"] == "${{ steps.scope.outputs.python }}"
    # ACL-1582: список версий из scope получает Linux-задание; Windows — всегда 3.13.
    test = ci["jobs"]["test-linux"]
    assert test["needs"] == "scope"
    # Имена заданий заданы явно (ACL-1582): «linux tests (<py>, <shard>)».
    assert test["name"] == "linux tests (${{ matrix.python-version }}, ${{ matrix.shard }})"
    assert list(test["strategy"]["matrix"]) == ["python-version", "shard"]
    assert test["strategy"]["matrix"]["python-version"].startswith("${{ fromJSON(needs.scope.outputs.python")
    assert test["strategy"]["matrix"]["shard"] == [1, 2, 3]
    assert test["strategy"]["fail-fast"] is False
    assert ci["jobs"]["test-windows"]["strategy"]["matrix"]["python-version"] == ["3.13"]


def test_required_check_names_are_unchanged():
    assert workflow("ci.yml")["jobs"]["admission"]["name"] == "ACL verification"
    assert workflow("windows-installer.yml")["jobs"]["admission"]["name"] == "Installer verification"
    # Агрегат смотрит на итог всей матрицы каждого задания (ACL-1582: Linux и Windows).
    dumped = yaml.safe_dump(workflow("ci.yml")["jobs"]["admission"], width=1000)
    assert "LINUX_TESTS: ${{ needs.test-linux.result }}" in dumped
    assert "WINDOWS_TESTS: ${{ needs.test-windows.result }}" in dumped


def test_scope_job_runs_the_workflow_checks():
    run = workflow("ci.yml")["jobs"]["scope"]["steps"][-1]["run"]
    assert "tests/test_acl_ci_workflows.py" in run and "--noconftest" in run


# --- the installer build/install job --------------------------------------------------------------

INSTALLER_JOB = "phase-2-packaging-spike"
# Самые долгие длительности шагов (с) из шести зелёных запусков 2026-10-04
# (37214804931, 37216023748, 37221077133, 37221943752, 37222309525, 37223632051).
SLOWEST = {
    "Set up job": 1, "Check out full history": 11, "Set up Python 3.13": 7,
    "Install runtime and packager": 63, "Test setup with runtime dependencies": 5,
    "Build previous isolated test package from the candidate source": 213,
    "Build onedir bundle": 69, "Test the packaged executable": 11,
    "Build per-user Windows installer": 176, "Test install, launch, repair, and uninstall": 68,
    "Test real package update, restart, and recovery": 125,
    "Keep update and recovery evidence even after failure": 6, "Create downloadable archive": 17,
    "Upload Windows preview and test logs": 17, "Post steps": 5,
}


def installer_job():
    return workflow("windows-installer.yml")["jobs"][INSTALLER_JOB]


def test_installer_build_job_times_out_at_15_minutes():
    job = installer_job()
    assert job["name"] == "Build, install, repair, and uninstall"
    assert job["timeout-minutes"] == 15


def test_slowest_green_run_fits_with_headroom():
    assert set(SLOWEST) - {"Set up job", "Post steps"} == {s["name"] for s in installer_job()["steps"]}
    # Сумма максимумов по шагам (794 с) пессимистичнее самого долгого реального
    # запуска (744 с): даже такой «худший во всём» запуск оставляет ~13 % запаса.
    total = sum(SLOWEST.values())
    assert total <= 800
    assert total * 1.13 <= installer_job()["timeout-minutes"] * 60


def test_every_step_limit_fits_inside_the_job_limit():
    """Зависший шаг падает по своему лимиту раньше лимита задания.

    Для каждого шага с лимитом: самое долгое время всех шагов до него плюс его
    лимит не больше 15 минут; после последнего лимитированного шага ещё
    помещаются самые долгие хвостовые шаги. Лимит шага — не меньше 1.4x его
    самой долгой зелёной длительности, чтобы обычная медленная машина не падала."""
    steps = installer_job()["steps"]
    limit = installer_job()["timeout-minutes"] * 60
    elapsed = SLOWEST["Set up job"]
    timed = 0
    for step in steps:
        minutes = step.get("timeout-minutes")
        if minutes is not None:
            timed += 1
            assert minutes * 60 >= 1.4 * SLOWEST[step["name"]], step["name"]
            assert elapsed + minutes * 60 <= limit, step["name"]
        elapsed += SLOWEST[step["name"]]
    assert elapsed + SLOWEST["Post steps"] <= limit
    # Все шаги, которые ставят зависимости, собирают или запускают пакет, ограничены.
    assert timed == 7
    for step in steps:
        if step.get("shell") == "pwsh" and step["name"] != "Create downloadable archive":  # linux-ok: reads YAML, runs nothing
            assert "timeout-minutes" in step, step["name"]
