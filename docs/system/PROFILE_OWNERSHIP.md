# Profile ownership between ACM processes — ACL-1853

Browser operations that enter through `src/fb_ads_rpa/operations.py` take an
exclusive physical-profile lease for the complete operation. Publishing,
reading and closing the same profile wait on the same lease. Different
profiles can run concurrently. Nested operations on the same thread reenter;
a nested browser call that resolves to another profile refuses to proceed.

The lease combines a thread RLock with the existing `config.paths.machine_file_lock`
OS primitive. The outer acquisition holds the file descriptor until its matching
release. An exited or terminated process loses the OS lock automatically. Lock
files remain in place: deleting or replacing one can split ownership on POSIX.
There is no PID-based stale-lock recovery or timer that grants a second owner.

Coordination files are stored under the OS user's home directory,
`.acl-profile-leases/<sha256-of-canonical-identity>.lock`. Their location does not
depend on `ACL_CONFIG_DIR`, dashboard port or Multilogin API endpoint. Multilogin,
its attach/direct providers and the Playwright profile fallback use the same
normalized physical UUID. Account labels, folder IDs and instance aliases do not
split that identity. Dolphin retains its numeric profile IDs in a separate
`dolphin:<canonical-number>` namespace.

Missing, malformed and unresolved profile IDs refuse browser entry. The actual
entry is checked against current ownership immediately before reader attachment
and RPA opening/closing, including when the account map changes after queueing.
Publish queue failures become a finished error; cancellation while waiting never
starts the browser. A nonblocking close returns busy when another process owns
the profile. Filesystem lock failures return an error instead of proceeding.
Waiting checks cancellation every 50 ms between OS attempts and is bounded to
120 seconds (OS/filesystem calls themselves remain subject to the OS).

## Exact boundary and follow-up integration

The verified guarantee covers independently started Windows ACM processes
**of the same OS user on one machine**, with the same home directory and lease
protocol (including Python spawn). Inheriting an active lease through POSIX
`fork` is outside this contract; start a fresh process instead. It does not lock out
different Windows users, remote computers, old ACM versions without this protocol,
external automation or manual browser use. Run all cooperating instances on the
updated code; an older installed build cannot be made to honor a new lock.

The Read Spend/country/RPA paths, the Publish job (RPA and the Graph API backend, ACL-1761),
the plan check and the forms read (`serialized_browser`), and the campaign commands
session (ACL-1800, `campaign_commands.open_session`: reading gate + lease around the
whole session, so a warm checkout nests inside it and reenters) retain the lease for
their existing operation scope. `account_spend_api.read_country_split_via_api` is
decorated too. Leaving a review window open does not retain ownership forever; the
next operation must acquire the lease before driving or closing it. Pending reader
auto-close reacquires the same physical lease before closing. `WarmRegistry._adopt`
(ACL-1812) holds the lease while it takes over a running profile; a refusal there is
a normal `profile_lease` result, so one bad map entry does not abort `open_many`.

Merged paths that do **not** yet hold the lease across a whole session (ACL-1760,
ACL-1761 and ACL-1812; follow-up issue #862):

- `account_spend_api.read_via_api`, `campaign_inventory.read_via_api`: the Graph API
  reads take no lease at all (`_session` only computes the key for the cooldown).
- `account_spend_api._session`, `_open_gated`, `_warm_session`: start/attach/close
  the profile and check out the warm tab without verifying that a lease is held.
- `fb_api.warm.WarmRegistry.open_one` / `open_many` (profile start), `close_one`
  (`warm_api` stop via `stop_profile_on_request`), `checkout` (own per-session lock
  only): no lease around start, attach or stop.
- `web.warm_api._run_open` / `close_profiles`, `fb_api.profile.open_profile` and its
  other callers (`scripts/fb_api_pilot.py`), `publish_api.ApiPublisher` open/stop:
  inside the Publish job they run under the job lease, but nothing verifies it.

The downstream integration must acquire ownership before profile start/attach and
hold it through the last browser action, including recovery and closing. Decorating
only `open_profile()` and releasing when it returns a connection descriptor would
not protect that session.

Offline tests use synthetic identities, fake providers and isolated coordination
directories. They exercise independent spawned processes and process termination;
they neither drive live profiles nor complete the separate live Windows pilot
ACL-1187. Source review, combined batch/stage verification and delivered releases
remain separate workflow steps.
