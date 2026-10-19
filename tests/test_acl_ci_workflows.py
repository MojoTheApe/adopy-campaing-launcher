"""ACL-1581: static checks of the CI workflows (the Actions budget means they cannot run here).

- superseded PR runs are cancelled, stage/main/master pushes and manual runs never are;
- the test matrix takes its Python versions from the scope job, with a full fallback;
- the installer build/install job stops at 20 minutes and every step limit fits inside.

Concurrency expressions are evaluated by a small GitHub-expression evaluator below, so
the test checks what the YAML really says, not a copy of it.
"""
from pathlib import Path
import ast
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


ALL = ["ci.yml", "windows-installer.yml", "tracker-link.yml", "delivery-check.yml"]
PUSHED = ["ci.yml", "windows-installer.yml", "delivery-check.yml"]


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


@pytest.mark.parametrize("activity,expected", [
    ("edited", {"tracker-link.yml"}),
    ("synchronize", set(ALL)),
])
def test_pr_metadata_feedback_does_not_restart_candidate_verification(activity, expected):
    # GitHub defaults an absent types filter to opened/synchronize/reopened:
    # https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request
    defaults = {"opened", "synchronize", "reopened"}
    triggered = set()
    for name in ALL:
        event = workflow(name)[True]["pull_request"] or {}
        if activity in event.get("types", defaults):
            triggered.add(name)
    assert triggered == expected


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
# p95 и максимум (с) по шагу в 91 зелёном запуске windows-installer.yml
# 2026-10-03..05 (шаги из API заданий, запуски с переиспользованием исключены).
# Только шаги с лимитом; у остальных шагов (set up job, checkout, setup python,
# тест setup, evidence, архив, upload, post) нет своего лимита, их суммарные
# значения: p95 = 884 - 787 = 97 с, максимум = 1093 - 892 = 201 с.
STEP_P95 = {
    "Install runtime and packager": 90,
    "Build previous isolated test package from the candidate source": 232,
    "Build onedir bundle": 78, "Test the packaged executable": 12,
    "Build per-user Windows installer": 164,
    "Test install, launch, repair, and uninstall": 85,
    "Test real package update, restart, and recovery": 126,
}
STEP_MAX = {
    "Install runtime and packager": 97,
    "Build previous isolated test package from the candidate source": 252,
    "Build onedir bundle": 104, "Test the packaged executable": 16,
    "Build per-user Windows installer": 176,
    "Test install, launch, repair, and uninstall": 110,
    "Test real package update, restart, and recovery": 137,
}
OTHER_STEPS_P95, OTHER_STEPS_MAX = 97, 201
JOB_P95, JOB_MAX = 774, 810
SUM_STEP_P95 = sum(STEP_P95.values()) + OTHER_STEPS_P95  # 884


def installer_job():
    return workflow("windows-installer.yml")["jobs"][INSTALLER_JOB]


def test_installer_build_job_times_out_at_20_minutes():
    job = installer_job()
    assert job["name"] == "Build, install, repair, and uninstall"
    assert job["timeout-minutes"] == 20


def test_data_is_consistent():
    assert SUM_STEP_P95 == 884
    assert sum(STEP_MAX.values()) + OTHER_STEPS_MAX == 1093


def test_job_limit_has_headroom_over_measured_p95():
    limit = installer_job()["timeout-minutes"] * 60
    assert limit >= 1.3 * SUM_STEP_P95
    assert limit >= 1.5 * JOB_P95
    assert limit > JOB_MAX


def test_every_step_limit_has_headroom_and_fits_inside_the_job_limit():
    """Каждый лимит шага не меньше 1.5x его p95, и зависший шаг падает по своему
    лимиту раньше лимита задания (p95 всех шагов до него плюс его лимит)."""
    steps = installer_job()["steps"]
    assert {s["name"] for s in steps if "timeout-minutes" in s} == set(STEP_P95)
    limit = installer_job()["timeout-minutes"] * 60
    elapsed = OTHER_STEPS_P95
    for step in steps:
        minutes = step.get("timeout-minutes")
        if minutes is None:
            continue
        assert minutes * 60 >= 1.5 * STEP_P95[step["name"]], step["name"]
        assert minutes * 60 >= 1.25 * STEP_MAX[step["name"]], step["name"]
        assert elapsed + minutes * 60 <= limit, step["name"]
        elapsed += STEP_P95[step["name"]]
    for step in steps:
        if step.get("shell") == "pwsh" and step["name"] != "Create downloadable archive":  # linux-ok: reads YAML, runs nothing
            assert "timeout-minutes" in step, step["name"]


def wait_timeouts(source):
    """Все .wait(...)-таймауты файла: ключом или позицией; имена — модульные int-константы.

    Всё, что не литерал int и не разрешимая константа, — ошибка (AssertionError)."""
    tree = ast.parse(source)
    constants = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and type(node.value.value) is int):
            constants[node.targets[0].id] = node.value.value
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "wait"):
            continue
        keyword = [k.value for k in node.keywords if k.arg == "timeout"]
        value = keyword[0] if keyword else node.args[0] if node.args else None
        if value is None:
            continue  # wait() без таймаута не используется; ниже проверим, что таймауты найдены
        if isinstance(value, ast.Constant) and type(value.value) is int:
            found.append(value.value)
        elif isinstance(value, ast.Name) and value.id in constants:
            found.append(constants[value.id])
        else:
            raise AssertionError(f"wait timeout at line {node.lineno} is not an int literal or module constant")
    return found, constants


@pytest.mark.parametrize("snippet,expected", [
    ("helper.wait(timeout=900)", [900]),
    ("helper.wait(timeout = 900)", [900]),
    ("helper.wait(900)", [900]),
    ("X = 700\nhelper.wait(timeout=X)\nhelper.wait(X)", [700, 700]),
])
def test_wait_timeouts_reader_sees_every_spelling(snippet, expected):
    assert wait_timeouts(snippet)[0] == expected


@pytest.mark.parametrize("snippet", [
    "helper.wait(timeout=UNKNOWN)", "helper.wait(timeout=2 * 450)", "helper.wait(900.0)", "helper.wait(timeout=t)",
])
def test_wait_timeouts_reader_rejects_unreadable_values(snippet):
    with pytest.raises(AssertionError):
        wait_timeouts(snippet)


def test_update_step_outlives_the_update_cycle_script_wait():
    """test_update_cycle.py ждёт helper до N с и чистит в finally. Шаг должен вместить
    работу до ожидания (макс. зелёный шаг), stop-server (30 с), ожидание и очистку (30 с)."""
    source = (ROOT / "packaging/windows/test_update_cycle.py").read_text(encoding="utf-8")
    waits, constants = wait_timeouts(source)
    assert constants.get("HELPER_WAIT_SECONDS") in waits, "the long wait must use HELPER_WAIT_SECONDS"
    name = "Test real package update, restart, and recovery"
    step = next(s for s in installer_job()["steps"] if s["name"] == name)
    limit_s = step["timeout-minutes"] * 60
    assert limit_s >= STEP_MAX[name] + max(waits) + 60


# --- ACL-1649: no automatic whole-workflow retries ----------------------------------------------------

import re  # noqa: E402

RETRY = re.compile(r"gh\s+run\s+rerun|nick-fields/retry|retry-action|run_attempt\s*<|rerun", re.I)


@pytest.mark.parametrize("name", ["ci.yml", "windows-installer.yml", "release.yml", "tracker-link.yml", "delivery-check.yml"])
def test_no_workflow_reruns_or_retries_itself(name):
    text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    assert not RETRY.search(text), name
    assert "workflow_run" not in workflow(name)[True]          # verification never reruns itself; authorized delivery has its own guarded workflow


def test_the_scope_job_runs_the_chain_tests_too():
    run = workflow("ci.yml")["jobs"]["scope"]["steps"][-1]["run"]
    assert "tests/test_acl_ci_chain.py" in run and "tests/test_acl_ci_minutes.py" in run
