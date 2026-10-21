# Scheduled Account Reads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dates never open a browser profile; the campaign list is read structure-only by a server-side queue
fed by three daily windows, attention (expand) and commands — replacing the Read spend / Read campaigns buttons.

**Architecture:** `campaign_inventory` gets a structure-only read. A new module `account_refresh` owns freshness,
a single-worker priority queue, the 08/13/18 windows (persisted, cross-process lock) and stop classification. Thin
HTTP endpoints in `app.py`; the campaign table and account map drop read buttons and Meta columns and talk to the
new endpoints. Commands refresh the account in their own session after a write.

**Tech Stack:** Python 3.14 (FastAPI/Starlette app in `src/web/app.py`), vanilla JS in Jinja templates, pytest.

**Spec:** `docs/superpowers/specs/2026-10-21-scheduled-account-reads-design.md` (ACL-1063)

## Global Constraints

- No timer and no polling of Meta. Reads start only from: a window's first page open, attention, a command, Retry.
- A period change starts **zero** reads.
- At most **one** profile read at a time, across all dashboard processes on the machine.
- Windows: `08:00`, `13:00`, `18:00` local time; a missed window is not caught up.
- Fresh = last snapshot state `ok` or `partial` with `finished_at_utc` less than **3600 s** old.
- Priority: `action` > `retry` > `attention` > `schedule`.
- Structure-only read: steps `campaigns`, `adsets`, `ads` only — no Insights, no account spend, no country split.
- Setting `ACL_AUTO_REFRESH`: `1` on / `0` off; unset → on when the server port is `9090`, off otherwise.
  TEST9095 launch sets `ACL_AUTO_REFRESH=1` (owner decision).
- All user-facing text in English (labels, banner, errors). Code comments in Russian like the surrounding code.
- Existing endpoints `/account-spend/read` and `/campaign-inventory/read` stay (Retry tooling, scripts, tests).
- Every read still goes through `browser_operation.reading()` and `_as._lock_for(account)`; no cooldown bypass.

## Review Focus

1. Several tabs / reloads in one window → still exactly one sweep (pinned in Task 3).
2. 9090 and 9095 both running → one sweep, never two profiles at once (Task 3 lock test).
3. MLX agent down (`WinError 10061`, `launcher.mlx.yt` unreachable) on the first account → queue halts with one
   banner, other rows do not each show the failure (Task 2).
4. Hand-edited campaign + Pause → nothing written, account refreshed at the front of the queue (Task 5).
5. Rules: snapshots no longer carry Insights → `reconcile_spend` returns `not_reconciled`; Observe/Suggest keep
   working, Automatic is blocked with "Not reconciled" (Task 1 test pins this, not a regression to "fix").

---

### Task 1: Structure-only inventory read

**Files:**
- Modify: `src/analysis_agent/campaign_inventory.py` (`STEPS` :67, `read_into` :627, `finalize` :720,
  `read_via_api` :737, `start_read` :804)
- Test: `tests/test_acl1063_structure_read.py`

**Interfaces:**
- Produces: `STRUCTURE_STEPS = ("campaigns", "adsets", "ads")`;
  `read_into(snap, client, act, *, structure_only: bool = False) -> None`;
  `read_via_api(account, date_from="", date_to="", *, structure_only=False, trigger="") -> Snapshot`;
  `run_read(account: str, *, trigger: str) -> Snapshot` — synchronous structure-only read under
  `browser_operation.reading()` + `_as._lock_for(account)`, stores the result with `put_result`, returns it.
- Snapshot written by a structure-only read: `date_from == date_to == ""`, `insights == {}`, steps only the three.

- [ ] **Step 1: Write failing tests** (fake client like the existing ones in `tests/test_acl1798_campaign_inventory.py`)
  - `test_structure_only_reads_no_insights`: fake client records requested edges; after
    `read_into(snap, client, "act_1", structure_only=True)` no `insights` request was made and
    `set(snap.steps) == {"campaigns", "adsets", "ads"}`.
  - `test_structure_only_finalize_ok`: all three steps ok → `snap.state == "ok"` (missing insight steps are not "partial").
  - `test_run_read_stores_snapshot`: `run_read("A", trigger="schedule")` with a monkeypatched reader → `get_snapshot("A").state == "ok"` and `date_from == ""`.
  - `test_rules_without_insights_not_reconciled`: `reconcile_spend(structure_snapshot, window, 100.0)["status"] == "not_reconciled"`.
- [ ] **Step 2: Run** `C:/acl-test-9095q/.venv/Scripts/python.exe -m pytest tests/test_acl1063_structure_read.py -q` → FAIL.
- [ ] **Step 3: Implement** the three signatures above; telemetry call in `read_via_api` passes
  `notes["trigger"] = trigger`.
- [ ] **Step 4: Run** the new test plus `tests/test_acl1798_campaign_inventory.py tests/test_acl1815_rules_evidence.py` → PASS.
- [ ] **Step 5: Commit** `[ACL-1063] campaign inventory: structure-only read`.

### Task 2: Refresh queue with freshness and stop classification

**Files:**
- Create: `src/analysis_agent/account_refresh.py`
- Test: `tests/test_acl1063_refresh_queue.py`

**Interfaces:**
- Consumes: `campaign_inventory.run_read`, `campaign_inventory.get_snapshot`.
- Produces:
  - `FRESH_S = 3600.0`; `PRIORITY = {"action": 0, "retry": 1, "attention": 2, "schedule": 3}`
  - `is_fresh(account: str, *, now: float | None = None) -> bool`
  - `classify(snap: Snapshot) -> Literal["ok", "account", "global"]` — `global` when `error_code`/`error` shows
    MLX agent unreachable (`10061`, `launcher.mlx.yt`), checkpoint, call ceiling, active cooldown, MLX batch refusal;
    everything else failed → `account`.
  - `class RefreshQueue(reader: Callable[[str, str], Snapshot] = run_read)` with
    `request(account: str, trigger: str, *, force: bool = False) -> dict` (`{"queued": bool, "reason": "fresh"|"queued"|"running"|"halted"}`;
    `force` skips the fresh check and clears a halt for `retry`/`action`),
    `state() -> dict` (`{"running": str|None, "queued": [{"account","trigger"}], "halt": {"reason","until"}|None,
    "sweep": {"window","done","total"}|None, "rows": {account: {"list_at","state","error"}}}`),
    `start()` / `stop()` for the single daemon worker.
  - module singleton `queue() -> RefreshQueue`.
- Skips record telemetry `skipped_fresh` (existing `account_spend_api._telemetry`).

- [ ] **Step 1: Write failing tests** (inject a fake reader and a fake clock; no threads — call `queue._drain_once()`)
  - `test_fresh_account_skipped`: snapshot ok 10 min old → `request("A","attention") == {"queued": False, "reason": "fresh"}`.
  - `test_priority_order`: enqueue schedule B, attention C, action D → reader called D, C, B.
  - `test_duplicate_merged_and_upgraded`: schedule A then action A → one entry, trigger `action`.
  - `test_account_failure_continues`: reader fails A with proxy `ERR_INVALID_AUTH_CREDENTIALS` → B still read; `state()["rows"]["A"]["state"] == "failed"`; `halt is None`.
  - `test_global_failure_halts`: reader fails A with `WinError 10061` → B not read; `state()["halt"]["reason"]` mentions Multilogin.
  - `test_retry_clears_halt`: after global halt, `request("A","retry", force=True)` → read again.
  - `test_period_has_no_entry_point`: module exposes no function taking `date_from` (assert via `inspect`).
- [ ] **Step 2: Run** `... -m pytest tests/test_acl1063_refresh_queue.py -q` → FAIL.
- [ ] **Step 3: Implement.** Heap ordered by `(PRIORITY[trigger], seq)`; one worker thread; halt is lifted when the
  cooldown in `snap`/stop log has passed (resume the remaining entries of the same window).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `[ACL-1063] account refresh queue`.

### Task 3: Windows, page-open trigger, setting and cross-process lock

**Files:**
- Modify: `src/analysis_agent/account_refresh.py`
- Modify: `C:\fb_agents\.claude\launch.json` is **not** touched here (owner's TEST9095 env is set at deploy, Task 8)
- Test: `tests/test_acl1063_windows.py`

**Interfaces:**
- Produces:
  - `WINDOWS = ("08:00", "13:00", "18:00")`
  - `current_window(now: datetime) -> str | None` — the latest window start ≤ now on that date (`None` before 08:00).
  - `auto_refresh_enabled(port: int) -> bool` per Global Constraints.
  - `on_page_open(accounts: list[str], *, now: datetime | None = None, port: int) -> dict`
    (`{"sweep": "started"|"already_done"|"disabled"|"no_window", "window": str|None, "queued": int}`).
  - State file `<paths.get_local_config_dir()>/account_refresh.json` → `{"windows_done": {"2026-10-21": ["08:00"]}}`,
    guarded by an OS file lock `account_refresh.lock` in the same dir; the worker also takes this lock per read so
    two processes never read at once.

- [ ] **Step 1: Write failing tests** (tmp config dir via the existing `ACL_CONFIG_DIR` fixture pattern)
  - `test_window_boundaries`: 07:59 → None; 08:00 → "08:00"; 12:59 → "08:00"; 13:00 → "13:00"; 23:00 → "18:00".
  - `test_first_open_sweeps_non_fresh`: 3 accounts, 1 fresh → `queued == 2`, `sweep == "started"`.
  - `test_second_open_same_window_no_sweep`: second call → `already_done`, no new queue entries.
  - `test_missed_window_not_caught_up`: last done "08:00", now 19:00 → sweeps "18:00" only (one sweep).
  - `test_disabled_on_other_port`: `ACL_AUTO_REFRESH` unset, port 9095 → `disabled`; with `ACL_AUTO_REFRESH=1` → started.
  - `test_lock_blocks_second_process`: hold the lock in a subprocess → `_drain_once()` does not call the reader.
- [ ] **Step 2: Run** `... -m pytest tests/test_acl1063_windows.py -q` → FAIL.
- [ ] **Step 3: Implement** (lock: `msvcrt.locking` on Windows, `fcntl.flock` elsewhere — follow any existing lock
  helper in `src/` if one exists, e.g. `profile_lease.py`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `[ACL-1063] scheduled windows and page-open trigger`.

### Task 4: HTTP endpoints

**Files:**
- Modify: `src/web/app.py` (next to `/campaign-inventory/read` :4317)
- Test: `tests/test_acl1063_endpoints.py`

**Interfaces:**
- Consumes: Task 2/3 functions.
- Produces:
  - `POST /account-refresh/page-open` form `accounts` (repeated) → `on_page_open(...)` JSON (port = the server's port).
  - `POST /account-refresh/request` form `account`, `trigger` ∈ {`attention`, `retry`} → `queue().request(...)`
    (`retry` → `force=True`); 400 on other triggers.
  - `GET /account-refresh/state` → `queue().state()` (local only, never touches Meta).
  - Queue worker started on app startup (same place other background services start).

- [ ] **Step 1: Write failing tests** with the app test client: page-open returns `sweep`; request with
  `trigger=schedule` → 400; state returns keys `running, queued, halt, sweep, rows`; none of the three handlers
  calls `campaign_inventory.read_via_api` synchronously (monkeypatch it to raise).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `[ACL-1063] account refresh endpoints`.

### Task 5: Commands refresh the account in their own session

**Files:**
- Modify: `src/fb_api/commands.py` (`run_op` :576, `_run` :609, stale path :635)
- Modify: `src/web/campaign_commands.py` (`run_execute`)
- Test: `tests/test_acl1063_commands_refresh.py`

**Interfaces:**
- Produces: `run_op(..., after_write: Callable[[Any], None] | None = None)` — called with the open `sess` after a
  successful write+verify, inside the same session. `campaign_commands.run_execute` passes a callback that runs
  `campaign_inventory.read_into(snap, sess.client, sess.act, structure_only=True)`, finalizes and `put_result`s it.
- On the stale path (`stale=True`) `run_execute` calls `account_refresh.queue().request(account, "action", force=True)`.
- Builder publish: when a publish job finishes successfully (`src/web/publish_jobs.py`, job done hook), call
  `account_refresh.queue().request(account, "action", force=True)`.
- Test addition: `test_publish_done_requests_refresh` — finishing a fake publish job enqueues `("A","action")`.

- [ ] **Step 1: Write failing tests**
  - `test_after_write_called_once_with_session`: fake session; successful op → callback called once with that session.
  - `test_after_write_not_called_on_stale`: stale op → callback not called; queue got `("A","action")`.
  - `test_refresh_failure_does_not_fail_command`: callback raises → op still `done`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** new test + `tests/test_acl1800_campaign_commands.py` → PASS.
- [ ] **Step 5: Commit** `[ACL-1063] commands refresh the account after writing`.

### Task 6: Campaign table without read buttons

**Files:**
- Modify: `src/web/templates/_campaign_table.html` (`queueRun` :310, `readOne` :960, `init` :1234, row/header
  buttons, Meta metrics column group)
- Modify: `src/web/app.py` `/campaign-inventory/tree` and `/children` — stop emitting Meta Insights cells
- Test: `tests/test_acl1063_table_ui.py` (template/static assertions in the style of `tests/test_acl1800_home_wiring.py`)

**Interfaces:**
- Consumes: Task 4 endpoints.
- Behaviour:
  - `init()` POSTs `/account-refresh/page-open` once with the accounts of this mounted block (later mounts append).
  - Expanding an account row or a campaign POSTs `/account-refresh/request` `trigger=attention` (server decides freshness).
  - While `state().queued` or `running` is non-empty, poll `GET /account-refresh/state` every 3 s (local), stop when empty.
  - Row label: `list HH:MM` | `updating…` | failure reason (one line, ellipsis, full text in `title`) + `Retry` button.
  - One banner above the table when `halt` is set; one line `Refreshing accounts {done}/{total}` while a sweep runs.
  - Removed: row `Read campaigns`, `Read campaigns in <country>`, `readAll`, `Meta metrics` button/columns.
  - `Maximum` period renders structure + BI (no `periodOk` gate on rendering).

- [ ] **Step 1: Write failing tests**: template contains no `Read campaigns` string and no `Meta metrics`;
  contains `/account-refresh/page-open`, `/account-refresh/state`, `Retry`, `Refreshing accounts`; tree endpoint JSON
  rows have no `meta` key; a period change URL renders with zero calls to `/campaign-inventory/read`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** new test + `tests/test_acl1798_home_campaigns_browser.py tests/test_acl1800_home_wiring.py tests/test_acl1800_account_tree.py`; update assertions in those that pin the removed buttons (they describe the old behaviour) → PASS.
- [ ] **Step 5: Commit** `[ACL-1063] campaign table: refresh status instead of read buttons`.

### Task 7: Account map without Read spend

**Files:**
- Modify: `src/web/templates/report.html` (`amapReadSpend` :8645, `amapReadAll` :10394, `amapReadAllControls` :10385,
  "Read again" :9634, header links "Read all spend in <country>", Meta account-spend column)
- Test: `tests/test_acl1063_account_map_ui.py`

- [ ] **Step 1: Write failing tests**: rendered page has no `Read spend`, `Read all spend`, `Read again`,
  `Account spend` column header; the BI `Spend` column remains; `amapApply` still reloads with `date_from/date_to`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** (delete the dead JS paths, not just hide them). **Step 4: Run**
  new test + `tests/test_account_map_period.py tests/test_review_spend_regressions.py tests/test_acl1826_mlx_session_lost.py`;
  update assertions that pin removed controls → PASS.
- [ ] **Step 5: Commit** `[ACL-1063] account map: no Read spend controls`.

### Task 8: Changelog, PR, TEST9095

**Files:**
- Modify: `CHANGELOG.md`
- Deploy-only (not committed): TEST9095 launch env `ACL_AUTO_REFRESH=1`

- [ ] **Step 1:** CHANGELOG entry (English): dates never read accounts; three daily refresh windows; Retry per row.
- [ ] **Step 2:** Run only the touched tests (`tests/test_acl1063_*.py` + those listed in Tasks 1–7). Full suite runs in CI only.
- [ ] **Step 3:** Commit, push branch, open PR to `main` referencing ACL-1063; tracker handoff to review.
- [ ] **Step 4 (separate live check task, owner present):** on TEST9095 with `ACL_AUTO_REFRESH=1`: jump through 10
  periods → telemetry shows no `api_read`; first open in a window → accounts read one by one; Pause on a hand-edited
  campaign → nothing written + refresh. Log to Obsidian `Дешборд — Дневник изменений`.
