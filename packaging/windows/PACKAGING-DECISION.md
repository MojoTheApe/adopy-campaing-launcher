# Windows runtime packaging decision

Status: integrated Windows preview; manual pilot deferred by the owner.
Current checks and fixes: [integration review](INTEGRATION-2026-09-08.md).

## Decision

Use a PyInstaller `onedir` application containing CPython 3.13, the pinned ACL
dependencies, Playwright's Python/driver runtime, and the reviewed ACL source
commit. Wrap that versioned directory in a per-user Inno Setup installer.

The application exposes two executables from the same frozen dependency set:

- `ACL.exe` is the normal no-console launcher;
- `ACLConsole.exe` is the diagnostic and CI entry point.

## Historical spike evidence (before production integration)

GitHub Actions run `33641061374` built the package on `windows-latest` from
installer source `7a0f1d8f9a470aea89aec835d90eff7302d716cc` and frozen pilot core
`efa636bfcaf3ce44c57a13ad94e1b6b7ecfc41e8`. The packaged executable:

- reported its embedded version and both source SHAs;
- imported the dashboard and RPA runtime from the bundle;
- started without a source checkout;
- answered `GET /health` with the expected ACL response.

The uploaded CI artifact was 101,750,408 bytes after artifact compression.

## Alternatives compared

### Private embedded Python plus offline wheels

This would also remove the system Python and Git prerequisites, but it needs a
custom environment bootstrapper, offline wheel installation/repair logic, and
more moving pieces at first launch. It preserves a more source-like directory,
which is useful for developer debugging, but offers no campaign-manager benefit
once the `onedir` build passes the real import and health checks.

### PyInstaller one-file executable

This gives one file before installation but extracts a large private runtime
at startup. ACL is installed through Setup anyway, so the extra extraction,
startup delay, temporary files, and antivirus surface provide no useful UX
benefit.

## Consequences

- The installed computer needs neither Python nor Git.
- Startup uses already-extracted versioned files.
- Version activation can later switch directories without mutating user data.
- The full directory must be signed/scanned as a unit before broad rollout.
- Automatic updates remain disabled for the installer pilot.
