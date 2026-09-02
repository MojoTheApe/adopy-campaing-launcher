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

## Campaign-manager setup pack

The recommended first-run path uses a personal `.aclsetup` ZIP. Support starts
from `setup-pack.example.json`, replaces every example value, adds the approved
OAuth client, and runs:

```powershell
python packaging/windows/create_setup_pack.py `
  --setup-json path/to/setup.json `
  --oauth-client path/to/oauth_client.json `
  --output path/to/campaign-manager.aclsetup
```

The utility and first-run wizard reject unknown fields, archive path traversal,
placeholder values, duplicate account labels/IDs, missing Multilogin mappings,
and Google user tokens. Valid configuration is written transactionally under
`%LOCALAPPDATA%\ACL`; the wizard fixes the production safety mode and cannot
execute campaign actions.

## Support diagnostics

The Start Menu action `Create diagnostic bundle` writes a bounded ZIP under
`%LOCALAPPDATA%\ACL\diagnostics`. It contains version/platform/readiness facts
and sanitized recent launcher logs. It never includes `.env`, account-map
rows, OAuth clients, Google tokens, or Multilogin token values. A final denylist
scan aborts export if a prohibited token/account pattern survives sanitizing.
