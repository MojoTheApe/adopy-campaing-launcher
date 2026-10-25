"""Actual reset-guard tests accept a real reload and still detect broken undo."""
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("mode", [
    "legitimate_reload",
    "undo_leaks_capture", "undo_leaks_stall", "undo_leaks_tls",
    "baseline_leaks_capture", "baseline_leaks_stall",
    "baseline_spoofed_capture", "baseline_spoofed_stall",
])
def test_real_guard_across_collection_reload_and_patch_undo(tmp_path, mode):
    root = Path(__file__).resolve().parents[1]
    code = r'''
import importlib
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
import pytest
from tests import test_rpa_module_state_is_reset_between_tests as guard

# Model collection importing the guard BEFORE another test's legitimate reload.
importlib.reload(guard.md)
mode = sys.argv[2]
if mode.startswith('baseline_'):
    module, name = ((guard.page_capture, 'capture') if mode.endswith('capture')
                    else (guard.md, '_stall_evidence'))
    fake = lambda *args, **kwargs: None
    if 'spoofed' in mode:
        fake.__module__, fake.__name__ = module.__name__, name
    setattr(module, name, fake)
    with pytest.raises(AssertionError):
        guard._real_capture_and_stall_functions()
else:
    baseline = guard._real_capture_and_stall_functions()
    patch = pytest.MonkeyPatch()
    guard.test_3_patched_missing_thread_local_does_not_leak_other_mocks(patch, baseline)
    leaked_capture, leaked_stall = guard.page_capture.capture, guard.md._stall_evidence
    leaked_tls = guard.md._LAST_OPEN.outcome
    patch.undo()
    # Deliberately inject the effects of an undo that failed to restore one item.
    # The real guard's strict assertions must catch each item independently.
    if mode == 'undo_leaks_capture':
        guard.page_capture.capture = leaked_capture
    elif mode == 'undo_leaks_stall':
        guard.md._stall_evidence = leaked_stall
    elif mode == 'undo_leaks_tls':
        guard.md._LAST_OPEN.outcome = leaked_tls
    if mode == 'legitimate_reload':
        guard.test_4_the_next_test_has_the_real_capture_and_stall_functions(baseline)
    else:
        with pytest.raises(AssertionError):
            guard.test_4_the_next_test_has_the_real_capture_and_stall_functions(baseline)
print('guard scenario passed:', mode)
'''
    result = subprocess.run([sys.executable, "-I", "-c", code, str(root), mode],
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "guard scenario passed: " + mode in result.stdout
