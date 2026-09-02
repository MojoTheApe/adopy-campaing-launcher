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

Build and test the Phase 2 packaging spike on Windows:

```powershell
python -m pip install -r requirements.txt PyInstaller==6.22.2
./packaging/windows/build.ps1 -Version "0.1.0-spike.local"
./packaging/windows/test_bundle.ps1
```

The result is an unpacked, self-contained application at
`packaging/windows/.artifacts/dist/ACL/ACL.exe`. It contains Python and all
runtime dependencies; Git and a system Python installation are not required
on the campaign manager's computer.

This is deliberately an auditable packaging spike, not yet the campaign
manager installer. CI proves that the frozen pilot baseline can be packaged,
that its dashboard and RPA modules import from the package, and that the
packaged executable serves a healthy local dashboard. The next phase wraps
this validated directory in a signed-style installer flow with shortcuts,
preflight checks, repair/uninstall, and an explicit update channel.
