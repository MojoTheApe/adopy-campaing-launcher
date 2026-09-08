# RPA test coverage, 2026-09-08

Tests for the parts of the RPA that decide **what opens the ad account and what
is done with it**, plus one defect found while writing them.

## Goal

The owner asked that campaign runs stop breaking when the dashboard changes.
The measured starting point was 18% coverage of `src/fb_ads_rpa` and
`src/meta_campaign_agent` — 8,502 of 10,353 statements untested, eleven modules
at exactly zero.

This candidate covers the boundary the dashboard can break a run through, and
the safety gates that keep an ad account off a plain browser. It does not
attempt the DOM layer; see *Excluded*.

## Scope

- Base SHA: `80136098ac86c8a9e0b3668ba9cd8a67773c2d68` (`master`).
- Head SHA: `53647875c7f9ca29464664a4885c957a77bd6460`.
- Branch: `feature/board-full-tests`, in its own worktree.
- Claude authored these; Codex reviews, fixes and merges.
- Five commits, 1,547 lines added. Nine new test files; one source file
  changed (`src/fb_ads_rpa/pacing.py`, +9 −2).

## Included

**Browser boundary** — `tests/test_browser_boundary_contract.py`, 7 checks.
A run and a dashboard spend read share one live Multilogin profile. The only
protection is the shared `browser_operation` lock, and it holds only while
every path into a browser goes through it. Checked: reentrancy (a plain `Lock`
would deadlock the publisher against itself), the lock held for the whole call,
released after an exception, a second thread waiting, every browser path
carrying `@serialized_browser`, and `account_spend._LOCK is
publish_jobs._publish_serial` — a private `RLock` in a new module would switch
the protection off in silence.

**Multilogin profiles** — `tests/test_mlx_profiles.py`, 20 checks. Pairing an
account with the wrong profile opens it from another browser identity. The
checks favour the cases where the right answer is "unknown": an ambiguous
Windows short name resolves to nothing; a directory at the right depth that is
not a UUID is not a folder; an ordinary page title is not a profile name. The
ad account is read from the profile's own history file — no network, no driving
the account's browser — and the most frequent id wins over the newest, so one
stray click does not rename the owner.

**Relaunch gate** — `tests/test_browser_relaunch_gate.py`, 13 checks.
`MultiloginDirectCDPProvider` can attach to a profile MLX started (proxy and
MLX Agent alive) or relaunch it itself — a command that carries no proxy and
disables the agent, putting the account on Facebook from this machine's real
IP. The second is refused; the refusal names which of the two causes applied,
the profile is not touched on refusal, and `FB_RPA_ALLOW_UNPROXIED_RELAUNCH=0`
means no rather than "the variable is set".

**Publisher gates** — `tests/test_rpa_publisher_gates.py`, 30 checks.
Draft-only: anything but DRAFT is refused, case and spacing are a way of
writing rather than another status, and a draft carrying build errors returns a
report without opening a browser. Plain-browser gate: closed, and the refusal
names both the danger and the next step. Also: attach-only has no fallbacks at
all; an already-open profile goes straight to DirectCDP instead of an API that
answers 401; an unrecognised failure propagates instead of being treated as a
reason to fall back; a dead tab is distinguished from a dead browser.

**Browser helpers** — `tests/test_browser_helpers.py`, 29 checks. Mostly the
Mimic-version choice: Chromium refuses a profile built by a newer build, which
reads on screen as "the profile will not open". The profile version comes from
`Last Version`, or from the newest Snapshots folder compared **numerically** —
by string, "9" would beat "149". Also the difference between disconnecting and
closing: for a persistent context `playwright.stop()` closes the window itself,
so hand-off must not call it.

**Telemetry, pacing, config** — 47 checks. `record` never raises: a log line
must not cost a campaign build. Records with `ok: null` stay out of the failure
rate instead of passing as successes. One broken line does not lose the rest of
the history. Config defaults hold (attach-only on, publish through the
browser), and `FB_MLX_ATTACH_ONLY=0` means no while junk and empty do not mean
"off" — otherwise a typo in `.env` quietly opens the fallbacks.

**`meta_dom` pure helpers** — `tests/test_meta_dom_helpers.py`, 30 checks.
Labels compare by words rather than spacing; "Website" and "Website and instant
forms" are different menu rows and different conversion locations, so the match
is exact; pauses must not repeat, which is why `_human_jitter` exists at all.

## Defect found and fixed

`src/fb_ads_rpa/pacing.py` — `begin_teach_wait` announces that the RPA is
waiting for the human, and the comment beside it promises that pacing checks
will pause. They did not. `_HUMAN_TAKEOVER` was missing from the `global`
declaration, so the assignment made a local and the module flag never changed:
the RPA asked the human to click and carried on acting in the same browser.

The test was written first and failed on the current code. The fix is the name
added to `global`.

## Excluded

- `meta_dom` beyond the four pure helpers: 3,175 statements, 103 of its 136
  functions take a page. It needs a bench of recorded Ads Manager pages —
  tracked as `acl-340`, with the scrubbing and staleness questions written out.
  A fake page object would raise the number and protect nothing.
- `recipe_player` (1,064 statements, 0%) waits on the same bench — `acl-341`.
- Everything in `browser` and `rpa_publisher` that needs live CDP.

## Verification

Every guard was checked in reverse: broken deliberately, and the test fails.

| Broken | Fails |
|---|---|
| `@serialized_browser` removed from `read_via_rpa` | queue check |
| private `RLock` instead of the shared one | shared-lock check |
| `Lock` instead of `RLock` | reentrancy check |
| relaunch gate removed | 7 of 13 |
| ambiguous short name takes the first | ambiguity check |
| newest id instead of the most frequent | stray-click check |
| telemetry raises | the module's promise |
| records without a verdict counted | failure-rate check |
| attach-only off by any value | 9 checks |
| draft-only removed | 4 |
| plain browser always allowed | 6 |
| versions compared as strings | snapshot choice |
| "Website" matched by substring | 4 |
| pauses made identical | repeat check |

The reentrancy check first **hung** instead of failing — a nested `with` on a
plain `Lock` waits forever. Rewritten to a non-blocking acquire: a guard must
fail, not stall CI.

Commands on this head:

```
python scripts/acl_scan_secrets.py    no secrets
python scripts/acl_smoke.py           OK
python -m pytest tests/ -q            853 passed
```

`master` has 677. Coverage of the two packages went 18% → 21%; `run_telemetry`
16% → 80%, `rpa_publisher` 25% → 44%, `browser` 27% → 35%, `mlx_profiles`
0% → 47%, `config` 28% → 58%.

## Not verified

- CI on Windows / Python 3.11 and 3.13 — not runnable here.
- Nothing was run against a live Multilogin profile or a real campaign. These
  are unit checks of decisions taken before a browser opens.
- The `pacing` fix is not exercised on a live run: the human-handoff path needs
  a supervised Windows session.

## Note for the reviewer

`scripts/acl_scan_secrets.py` refused an earlier version of
`tests/test_mlx_profiles.py`: it carried a real `act=…` copied from a live
account screenshot, which would have reached this repository and every buyer's
machine. Identifiers in these tests are now assembled from parts so no
recognisable literal appears in the files. Worth keeping in mind for the
recorded-page bench in `acl-340`, where the same problem arrives at scale.

## Next

Codex reviews and merges. After that the open question is `acl-340` — the bench
— which is the only way the DOM layer gets covered.
