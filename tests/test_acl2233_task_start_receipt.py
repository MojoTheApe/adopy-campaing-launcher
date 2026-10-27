"""Execute real start helpers/controller with only COM replaced by memory objects.

This regression creates no native task, user, service, child or product state.
The separate real two-user acceptances remain required native CI evidence.
"""
import pytest

from tests import test_acl1309_two_windows_people as people
from tests import test_acl2063_two_windows_people as machine
from tests.acl2063_task_control import run_task_control, TaskControlError

pytestmark = pytest.mark.windows_only(reason="executes powershell.exe against in-memory Scheduler doubles only")

_COM_STUB = r"""
$script:definition = [pscustomobject]@{
 Principal=[pscustomobject]@{UserId='';LogonType=1;RunLevel=0}
 Settings=[pscustomobject]@{Enabled=$true;AllowDemandStart=$true;DisallowStartIfOnBatteries=$false;StopIfGoingOnBatteries=$false;ExecutionTimeLimit='';MultipleInstances=2;RunOnlyIfIdle=$false;RunOnlyIfNetworkAvailable=$false}
 Actions=[pscustomobject]@{}
}
$script:action=[pscustomobject]@{Path='';Arguments='';WorkingDirectory=''}
$script:definition.Actions | Add-Member ScriptMethod Create {param($n) return $script:action}
$script:instance=[pscustomobject]@{InstanceGuid='550e8400-e29b-41d4-a716-446655440000'}
$global:stateObserved=$false
$script:instance | Add-Member ScriptProperty State {$global:stateObserved=$true; return 4}
$script:task=[pscustomobject]@{Definition=$script:definition;Path='synthetic-owned-task';Enabled=$true;LastRunTime='synthetic'}
$script:task | Add-Member ScriptMethod Run {param($unused) return $script:instance}
$script:folder=[pscustomobject]@{}
$script:folder | Add-Member ScriptMethod RegisterTaskDefinition {param($n,$d,$flags,$u,$pw,$logon,$sddl) return $script:task}
$script:folder | Add-Member ScriptMethod GetTask {param($n) return $script:task}
$script:service=[pscustomobject]@{}
$script:service | Add-Member ScriptMethod Connect {}
$script:service | Add-Member ScriptMethod GetFolder {param($p) return $script:folder}
$script:service | Add-Member ScriptMethod NewTask {param($n) return $script:definition}
function New-Object {param([string]$ComObject) if ($ComObject -ne 'Schedule.Service') {throw 'Unexpected synthetic object'}; return $script:service}
"""


@pytest.fixture(params=[people, machine], ids=["1309", "2063"])
def start(request, monkeypatch):
    native = request.param
    def invoke(override=""):
        # The actual helper, redacted controller and its 30s bound stay in use.
        # Only Schedule.Service creation resolves to synthetic memory objects.
        monkeypatch.setattr(native, "run_task_control", lambda script, payload:
                            run_task_control(_COM_STUB + override + script +
                                             "\nif ($global:stateObserved) { exit 21 }\n", payload))
        prefix = "acl1309-" if native is people else "acl2063-"
        return native.scheduled_tasks({"operation": "start", "folder": prefix + "0" * 32,
            "name": "synthetic", "user": "synthetic", "password": "synthetic",
            "executable": "synthetic", "arguments": "synthetic", "directory": "synthetic"})
    return invoke


def test_started_instance_reaches_readiness_without_unused_state_getter(start):
    observed = start()
    assert observed["completed"] is True
    assert observed["instance"] == "550e8400-e29b-41d4-a716-446655440000"
    assert observed["registered"]["logon_type"] == 1
    assert observed["registered"]["run_level"] == 0


@pytest.mark.parametrize(("override", "error"), [
    ("$script:instance=$null\n", TaskControlError),
    ("$script:instance.InstanceGuid='invalid'\n", ValueError),
    ("$script:instance.InstanceGuid='00000000-0000-0000-0000-000000000000'\n", AssertionError),
    ("$script:task | Add-Member ScriptMethod Run {throw 'synthetic refusal'} -Force\n", TaskControlError),
])
def test_start_still_requires_successful_nonnull_run_and_nonzero_instance(start, override, error):
    with pytest.raises(error):
        start(override)
