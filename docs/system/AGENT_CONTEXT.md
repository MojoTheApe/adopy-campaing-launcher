# ACL engineering context

Read the whole task and affected code before implementation. Follow existing
configuration, navigation, error and persistence mechanisms. Avoid broad rewrites
and independent handlers for the same state. Preserve domain distinctions: campaign
creation vs final publishing, code merge vs package publication, local collection vs
external telemetry, confirmed success vs unknown external outcome.

- Dashboard changes must check affected routes, DOM IDs, event consumers, cart,
  builder expansion, account/country selection, old links and state restoration.
- RPA stays draft-only unless the owner explicitly authorizes the actual external
  action. Never publish ads, change budgets or modify production data as a test.
- Only one process may own a live Multilogin profile. In-process locks are not
  cross-process isolation. Read/probe uses a separate tab in the verified profile;
  do not restart it, change account or destroy drafts to make a check pass.
- Worktrees isolate code, not configuration, databases, ports, browsers or accounts.
  Tests use their own ACL_CONFIG_DIR, synthetic data and separate resources. No real
  credentials, account identifiers, proxy details or raw private captures in commits.
  Keep output local; review/sanitize minimal fixtures before storing them.
- Use config.paths and existing helpers. Preserve local workbooks, creatives, machine
  setup and credentials across source/package updates. Telemetry writes need their
  actual authorization; a stored OAuth client or token is not evidence of a send.
- Define behavior for duplicate requests, retries, concurrent actions, timeouts and
  partial success. Do not blindly retry an uncertain external write.
- Every robot (RPA) fix adds the live capture that motivated it to the offline bench
  (`scripts/capture_to_fixture.py`, `tests/bench/CATALOGUE.md`); review checks it.
- Test observable behavior and affected callers, not merely matching source strings.
  Useful regressions should fail on the original bug when practical. Do not weaken
  expectations to hide a failure. Text-only changes need no artificial product tests.
- Preserve unrelated work and file ownership. Stage only intended files. Source
  recovery and installer rollback are separate from a data/schema downgrade.

Required local commands: scanner, import smoke, relevant regressions, full tests
and packaging unittest suite; see repository/WORKFLOW.md and CI. Record OS/Python,
exact commit, passes/skips/failures and unavailable checks. A mock or macOS run does
not establish Windows installation or real Multilogin behavior. Use the existing
Windows workflows for those applicable checks; the manual pilot remains explicit.
