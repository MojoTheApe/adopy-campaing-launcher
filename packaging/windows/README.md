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
./packaging/windows/build_installer.ps1 -Version "0.1.0-spike.local"
./packaging/windows/test_installer.ps1 -Version "0.1.0-spike.local"
```

The result is an unpacked, self-contained application at
`packaging/windows/.artifacts/dist/ACL/ACL.exe`. It contains Python and all
runtime dependencies; Git and a system Python installation are not required
on the campaign manager's computer.

The Inno Setup build creates a non-admin per-user installer under
`packaging/windows/.artifacts/installer/`. It installs versioned program files
under `%LOCALAPPDATA%\Programs\ACL`, creates Start Menu and optional desktop
shortcuts, and keeps all user configuration under `%LOCALAPPDATA%\ACL`.

CI proves that the frozen pilot baseline can be packaged, installed without a
source checkout, started twice, repaired, and uninstalled while preserving an
existing configuration sentinel. Automatic updates remain disabled for the
installer pilot.

## Universal first-run setup

The same installer is distributed to every campaign manager. No personal
configuration package is required. On first launch, the setup wizard asks the
user to:

1. enter their name as it appears in the Stats workbook;
2. paste the company Accounts Google Sheet link;
3. choose the company Google access JSON supplied by support;
4. choose the Stats workbook, Upload FB workbook, and Creatives folder;
5. complete read-only Google authorization;
6. paste and test their Multilogin automation token.

The wizard derives the manager's account labels from the Stats workbook and
fills available ad-account IDs from the Upload FB workbook. It writes the
machine-local configuration transactionally under `%LOCALAPPDATA%\ACL` and
fixes the production safety mode. After the dashboard opens, the user connects
each account to an open Multilogin profile through the existing Accounts UI;
they never edit `.env`, CSV, JSON, or profile UUIDs.

## Support diagnostics

The Start Menu action `Create diagnostic bundle` writes a bounded ZIP under
`%LOCALAPPDATA%\ACL\diagnostics`. It contains version/platform/readiness facts
and sanitized recent launcher logs. It never includes `.env`, account-map
rows, OAuth clients, Google tokens, or Multilogin token values. A final denylist
scan aborts export if a prohibited token/account pattern survives sanitizing.
