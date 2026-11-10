# Candidate review: TEST9095zzzc (19e999e50 = main c0cb1a619 + PR 1507, 1505, 1503, 1490)

Reviewer: independent (did not write this code). Read-only review of `git diff c0cb1a619 19e999e50`.

## Verdicts

| PR | Issue | Verdict |
|---|---|---|
| 1507 (ACL-2506) | #1506 | APPROVE |
| 1505 (ACL-2504) | #1504 | APPROVE |
| 1503 (ACL-2502) | #1502 | APPROVE, with test-coverage gaps (F2, F3) recorded, not blocking |
| 1490 (ACL-2369) | #1369 | APPROVE, but owner must confirm the scope reading (F1) |
| Combination | | APPROVE: no blocking interaction defect found |

## Tests run

`tests/test_acl1448_israel_time.py tests/test_acl1799_workbench_browser.py tests/test_acl1812_warm_sessions.py tests/test_acl1862_lease_checkout_leak.py tests/test_acl1862_lease_windows.py tests/test_acl2255_memory_share_gate.py tests/test_acl2278_queue_not_fail.py tests/test_acl2400_memory_92.py tests/test_acl2400_round3.py` plus the directly affected `test_acl1862_lease_close_open.py test_profile_process_lease.py test_acl1853_read_wait.py test_a_locked_screen_is_named_as_the_cause.py test_acl1761_plan_check.py test_acl1642_open_profile_needs_no_memory.py test_publish_profile_slots.py test_browser_boundary_contract.py`: **460 passed**, 0 failed (Windows, so the windows-only subprocess tests ran).

CI run 42811782657 when this was written: tooling, Windows tests and process instructions passed; the six Linux shards were still running.

## Interaction checks between 2502 and 2369 (warm.py / leases)

These were verified and are clean:
- **Checkout callers.** There are only two: `account_spend_api._warm_session_held` (now wrapped by `_warm_session`, which takes the lease re-entrantly) and `publish_api._open_warm`. The second runs in the job thread inside `_run_job_inner`'s `_publish_serial(profile_key_for(...))`. Both keys go through `physical_profile_key` / `_canonical_key`, so they land on the same `ProfileLease`. Every reader I traced holds the lease before `_session`: spend `_lock_for`, country split `@serialized_browser`, inventory `_machine_read`, turn-off `@serialized_browser`, commands `campaign_commands.open_session`, plan `_plan_in_read_session` via serialized_browser. So the new `_require_checkout_lease` does not reject any production path I found.
- **Lease taken twice.** This is only a re-entrant depth increase in the same thread. `leak_watch` compares depth, so nesting is not reported as a leak (there is a test for this).
- **Merged `open_one` block.** `grant is None` returns `_slot_refusal_text()` before `try_lease`, so there is no lease to leak. The refusal from `try_lease` and the `opened is None` path both close `lease_stack` in `finally`. `_slot_refusal_text` catches every exception and falls back to the old text.
- **Order of `leak_watch`.** The write path enters leak_watch before publish_lease and the read path before `holding`, so the check runs after the operation's own release. The publish job enters it after `_publish_serial`, so the baseline depth is 1. This is correct.
- **ACL-2369 HeavyGate change.** The first action always passes, so a gate refusal can no longer happen while no other action is active. The checkout lease check runs before the gate. There is no new deadlock.

## Findings

### F1: ACL-2369 still memory-blocks Check plan on a warm profile when another action is running (medium, spec/owner decision)
- `src/fb_api/warm.py:404-414`: `_allowed` exempts only `n == 0`. With one action active (`n >= 1`), memory is still checked (`memory_low`), and `checkout` passes `fail_on_memory=True` (warm.py:1358), so the action is refused at once.
- Scenario: a Read spend is running on warm profile A, memory is 91% used, and the user presses Check plan on warm profile B. Check plan gets "Not enough free memory - close a profile and retry (free about 600 MB more)". `tests/test_acl2400_round3.py:218` (`test_check_plan_on_a_held_gate_returns_the_memory_text_and_code`) asserts exactly this.
- Issue #1369 says: "Do not block actions on a profile that is already open (warm); the gate applies only to opening a new profile". It also says: "Check plan on an already open profile is not blocked by the memory gate." The PR (and its changelog) narrows this to "first action is never gated; a second simultaneous action is". The narrower reading keeps the ACL-2255/2400 safety for concurrent actions and is stated openly. However, it does not meet the literal Issue text. The owner needs to confirm this reading, or the gate for `n >= 1` on warm profiles must also go.

### F2: autouse fixture switches off the new G1 check for the whole suite (low, test quality)
- `tests/conftest.py:117` (`_warm_checkout_lease_not_required`) patches `warm._require_checkout_lease` to a no-op unless a test asks for `strict_checkout_lease`.
- As a result, no end-to-end test (publish_api `_open_warm`, plan check, commands, spend read) proves that real callers reach `checkout` with the lease held. A future caller that forgets the lease will pass every test except the 5 strict unit tests. I checked the current callers manually (see above) and they are fine. Suggested follow-up: run at least one publish-job API test and one `_session` read test with `strict_checkout_lease`.

### F3: #1502 criteria "real subprocess tests exercise the callers" and "mixed-path pairs" are only partly covered (low, acceptance)
- `tests/test_acl1862_lease_windows.py` part 2 uses `serialized_browser` wrapping dummy functions, `Rig.checkout`, and `_warm_session` with `_warm_session_held` patched out. No subprocess test goes through the publish (create) path, the `campaign_commands` command path, or a real `_session` read against a second process.
- The publish-job G4 coverage is only an identity check (`tests/test_acl1862_lease_checkout_leak.py:192`, `publish_jobs._leak_watch is pl.leak_watch`). It does not show that a leak inside a publish job is detected and released.
- The mechanism is the same lease everywhere, so the residual risk is low. Strictly, though, the "callers, not only the lock helper" and "create/command" pairs are not proven. Record this in the Issue, or add follow-up tests.

### F4: the side-path lease in `_warm_session` is taken after the machine-wide read lease (low, latent)
- `src/analysis_agent/account_spend_api.py:607` takes `target_activity(profile)` and `read_session(...)` (the one machine-wide `account-structure-read.lock`) before `_open_session` -> `_warm_session` (line 524). Line 524 waits up to `READ_WAIT_SECONDS` = 600 s for the profile lease.
- `account_refresh_state.read_lease` says this lock must be taken AFTER the physical profile lease. For a caller that already holds the lease (every caller found today) nothing changes. For the lease-less "side path" this code exists for, the order is inverted: while a publish job holds the profile, such a caller would hold the machine read lock and the profile's target lease for up to 600 s. Every other account read on the machine would stall during that time. The publish job's own `target_activity(profile)` (publish_jobs.py:843, 120 s limit) could then fail with "still being used by another ACL account read".
- No production path triggers this today, so it is not blocking. A fail-fast (`timeout=0`) or taking the lease before `_session` would be safer.

### F5: the deadline branch of the HeavyGate memory refusal calls `wait_view` unguarded (very low)
- `src/fb_api/warm.py:493-494`: unlike the `fail_on_memory` branch, which wraps `wait_view` in `try/except`, the new `memory_low` deadline message calls `self.wait_view(why)` directly while holding `self._cond`. If the memory or settings reader raises there, the caller gets a raw exception instead of `WarmError(code="memory_low")`. `ProfileUsage.read` catches its own errors, so I could not make it fail in practice. This is only defensive hardening.

### Nits (not defects)
- `report.html:20833` (ACL-2369): when `shortGb` is set, the queue count `tail` comes after the closing sentence ("...you don't need. (2 ahead)"). Before, it came right after the numbers. Cosmetic.
- `tests/test_acl1448_israel_time.py`: `_israel_period()` is computed separately in the fixture and in the test. A run that crosses Israel midnight between the two calls would mismatch. This is a negligible flake window.

## Per-PR acceptance

- **1507 / #1506**: The fixture and assertions use today+3 / +5 in Israel time. The zero-padded preview text matches `report.html` `dayText` / `MONTHS`. The "first day is today" test is kept (`test_a_first_day_of_today_moves_its_start_on_when_the_hour_has_passed`). The remaining literal dates (server-side plan params; `test_a_saved_draft_fills_the_date_fields_back` with 2026-12-01) only assert date parts that `refreshTodayStart` does not change, so they stay green on later dates. Met.
- **1505 / #1504**: `applyTarget` clears `.cb-targeting-hidden` and `.cb-targeting-hint` before `cbTargetingCommit`. The change is scoped to `applyTarget`. The new test checks code, name and hint. With an empty field the behaviour is unchanged. For a non-saved name the old `ok=false` branch is kept; the old hidden code is now cleared, which is the intended "replace" semantics. Met.
- **1503 / #1502**: G1 (checkout fails closed, journal `path=checkout`) and G4 (`leak_watch` on read, write and publish, journal `profile_lease_leaked` with no ids) are implemented correctly. Re-entrancy and the "alias converges" criterion are tested. The subprocess and mixed-path criteria are partial (F2, F3).
- **1490 / #1369**: A cold open names the amount to free (`memory_hold_text`, `_free_more_sentence`, `_slot_refusal_text`, dashboard `short_mb`). `format_mb` / `free_more_mb` are tested, including the floor. The default and the env reading are unchanged. A warm first action is not gated; a warm action alongside another action is still gated (F1).
