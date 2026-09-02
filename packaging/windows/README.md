# ACL Windows installer workstream

This directory contains Windows installer work developed in parallel with the
campaign-manager pilot.

The frozen pilot baseline is recorded in `baseline.json`. Until that baseline
is approved, the installer branch may change only this directory and the
dedicated installer workflow. `verify_core_isolation.py` enforces the boundary
locally and in CI.

Run the guard and its tests from the repository root:

```powershell
python packaging/windows/verify_core_isolation.py
python -m unittest discover -s packaging/windows/tests -p "test_*.py" -v
```

Phase 1 does not build or distribute an installer. Its purpose is to establish
a separate, enforceable workstream before the packaging spike begins.
