# Adopy Campaign Manager (ACL) Windows installer workstream

The installer is now integrated with production `main`. A bundle includes the
current reviewed source commit, identified in its embedded manifest, dashboard
header and RPA telemetry. `baseline.json` records the integration ancestor and
selects `runtime_policy: integrated`; it no longer claims that today's runtime
matches the obsolete September 2 pilot. Frozen mode remains available for an
explicitly isolated future experiment.

Build commands and CI require clean committed inputs. The guard detects runtime
deletions and moves as well as edits, and verifies ancestry. Normal production
CI reviews/tests later runtime changes; updating production does not require
rewriting a permanent frozen-runtime allowlist.

Run the guard and its tests from the repository root:

```powershell
python packaging/windows/verify_core_isolation.py --require-clean
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
under `%LOCALAPPDATA%\Programs\ACL`, creates Start Menu and (by default) desktop
shortcuts, and keeps all user configuration under `%LOCALAPPDATA%\ACL`.

CI builds the actual combined runtime, exercises packaged identity and local
targeting, starts the backend twice, and tests installation, repair and removal
with configuration-preservation sentinels. Nothing updates on its own: the
dashboard offers a new release and installs it only when the user clicks
Update (see *Updates* below).
No release tag or public installer release is created by a code merge. The owner
has deferred the manual Windows/Multilogin pilot until after integration; CI is
not evidence of a successful live campaign run. See [integration review](INTEGRATION-2026-09-08.md).

Before rerunning Setup or installing a new version, stop the backend using
**Start Menu → Adopy Campaign Launcher → Stop Campaign Launcher**. Closing a
browser tab leaves the backend running. Setup refuses to change an active
backend's settings. Repeat setup preserves existing account/profile/proxy rows;
a conflicting account identity requires resolving the mapping in ACL first.

## Universal first-run setup

The same installer is distributed to every campaign manager. No personal
configuration package is required. On first launch, the setup wizard asks the
user to:

1. enter their name as it appears in the CM column of the Stats workbook;
2. choose the Stats workbook and the Creatives folder (default
   `Documents\ACL\Creatives`, created if missing);
3. check the readiness summary. Multilogin is connected later, in the
   dashboard (Accounts → Connect Multilogin), not in the wizard (ACL-1303).

No Google Sheet link, Google access file or Upload FB workbook is asked for
(ACL-1239): targets and ad texts come from the team library (ACL-1241), the
account list from the Stats workbook, and ad-account IDs from each Multilogin
profile when the user connects it on the Accounts screen. The wizard writes the
machine-local configuration transactionally under `%LOCALAPPDATA%\ACL` and fixes
the production safety mode; the user never edits `.env`, CSV, JSON, or profile
UUIDs. The Stats workbook and Creatives folder can be changed later in the
dashboard (Accounts → This computer).

### Setup guide (ACL-1175)

`docs/SETUP-NEW-PC.md` is the page a campaign manager follows alone. `build.ps1`
renders it with `setup_guide.py` into `setup-guide.html` inside the bundle; the
installer adds **Start → Adopy Campaign Launcher → Setup guide**, offers to open
it on its last screen, and the wizard has a **Setup guide** button on every
step. Every update therefore ships the guide of that version.

Change the guide in the same PR as the setup it describes.
`tests/test_setup_guide.py` fails when a wizard step, system check or setup
refusal is missing from the page.

A promoted `acl-release-*` tag is published as a GitHub Release by
`.github/workflows/release.yml`: the installer, its `.sha256` and `release.json`
(`scripts/acl_release_manifest.py`).

## Updates (ACL-1240)

The installed dashboard reads `release.json` of the latest GitHub Release at most
once a day (`src/web/release_updates.py`). The releases repository and a
read-only token are baked into the build as `acl-update-channel.json` from the
release workflow's `ACL_RELEASES_REPO` / `ACL_UPDATE_TOKEN`; builds without them
simply offer no updates. The token never appears in the UI, logs or diagnostics.

When the user clicks **Update to X**, the dashboard downloads the installer,
keeps it only if size and SHA-256 match `release.json`, starts
`ACL.exe --apply-update` (`packaging/windows/updater.py`) and exits. The helper
runs from the old version's folder, installs silently into
`versions\<new version>`, starts it and waits for `/health` to report the new
version. If the installer fails or the new version does not answer, it starts
the previous version again and restores the shortcuts and `current.json`. The
outcome goes to `%LOCALAPPDATA%\ACL\updates\last_update.json`, and the dashboard
shows a failed attempt once. An unsuccessful recovery is reported separately;
an installer that cannot be stopped requires support before ACL is restarted.
Updates refuse active/queued publishing and active browser reads, reserve the
idle runtime before handoff, and reject a second concurrent Update request.

Release publication verifies the annotated tag, current main SHA and the
referenced successful full main CI run through GitHub. Assets are uploaded
to a draft before it becomes visible; an existing release tag is never replaced.
Shared-library access and company Google sign-in remain separate setup work.

## Support diagnostics

The Start Menu action `Create diagnostic bundle` writes a bounded ZIP under
`%LOCALAPPDATA%\ACL\diagnostics`. It contains version/platform/readiness facts
and sanitized recent launcher logs. It never includes `.env`, account-map
rows, OAuth clients, Google tokens, or Multilogin token values. A final denylist
scan aborts export if a prohibited token/account pattern survives sanitizing.
