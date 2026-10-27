"""Bounded, credential-free diagnostics for owned native acceptance tasks."""
from contextlib import contextmanager
import json
import re
import subprocess

_OPERATIONS = frozenset({"create", "start", "status", "cleanup"})
_STAGES = ("service.create", "service.connect", "root.open", "folder.create",
           "task.folder", "task.definition", "register", "snapshot", "run", "receipt",
           "status.task", "status.instances", "status.events", "status.security",
           "cleanup.folder", "cleanup.list", "cleanup.stop", "cleanup.delete",
           "cleanup.folder_delete")
_STAGES += (
    'action.arguments',
    'action.create',
    'action.path',
    'action.workingdirectory',
    'cleanup.state',
    'definition.principal_logontype',
    'definition.principal_runlevel',
    'definition.principal_userid',
    'definition.settings_allowdemandstart',
    'definition.settings_disallowstartifonbatteries',
    'definition.settings_enabled',
    'definition.settings_executiontimelimit',
    'definition.settings_multipleinstances',
    'definition.settings_stopifgoingonbatteries',
    'instance.guid',
    'instance.state',
    'snapshot.demand_start',
    'snapshot.disallow_battery',
    'snapshot.enabled',
    'snapshot.last_run',
    'snapshot.logon_type',
    'snapshot.path',
    'snapshot.run_level',
    'snapshot.run_only_if_idle',
    'snapshot.run_only_if_network',
    'snapshot.settings',
    'status.result',
    'status.snapshot',
    'status.state',
)
_PHASES = {stage + suffix for stage in _STAGES for suffix in (".begin", ".end")}
_PHASES.update({"powershell.begin", "complete"})


class TaskControlError(RuntimeError):
    """Only fixed operation/phase and numeric system error, never payload/raw stderr."""


class _PrivatePayload(dict):
    def __repr__(self):
        operation = self.get("operation")
        operation = operation if operation in _OPERATIONS else "unknown"
        return f"<controlled task {operation}; credential payload omitted>"


def private_payload(payload):
    # pytest includes function arguments in failure traces. JSON serialization
    # still carries the actual credential over stdin, never in argv or repr.
    return _PrivatePayload(payload)


def _diagnostic(stderr):
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr or "")
    phase, hresult = "unknown", ""
    for line in text.splitlines():
        if line.startswith("ACL2063_TASK_PHASE:"):
            value = line.removeprefix("ACL2063_TASK_PHASE:")
            if value in _PHASES:
                phase = value
        if re.fullmatch(r"ACL2063_TASK_HRESULT:-?\d{1,11}", line):
            value = int(line.split(":", 1)[1])
            if -(2**31) <= value < 2**31:
                hresult = f"; HRESULT={value}"
    return f"phase={phase}{hresult}"


def run_task_control(script, payload):
    payload = private_payload(payload)
    operation = payload["operation"]
    if operation not in _OPERATIONS:
        raise ValueError("Unknown controlled task operation")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                input=json.dumps(payload), capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise TaskControlError(f"Controlled task {operation} timed out after 30 seconds; "
                               + _diagnostic(exc.stderr)) from None
    if result.returncode != 0:
        raise TaskControlError(f"Controlled task {operation} failed (exit={result.returncode}); "
                               + _diagnostic(result.stderr))
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise TaskControlError(f"Controlled task {operation} returned invalid JSON; "
                               + _diagnostic(result.stderr)) from None


@contextmanager
def preserve_primary_cleanup(primary):
    try:
        yield
    except BaseException as cleanup:
        if primary is None:
            raise
        # Unknown cleanup errors may contain paths/credentials. Keep their
        # type; only our curated diagnostic error has a safe textual context.
        detail = str(cleanup) if isinstance(cleanup, TaskControlError) else type(cleanup).__name__
        primary.add_note("Owned task cleanup also failed: " + detail)
