# ACL-2629 - upload original videos from a Web Worker in the profile

Issue: MojoTheApe/adopy-campaing-launcher#1629. Status: implemented, **not verified live** (see section 9).

## 1. Problem

Until now every chunk of a large video travelled Python -> CDP `Runtime.evaluate` as **base64**, was decoded with
`atob` on the **main thread** of the Ads Manager tab (`stage()` in `token_capture._HELPER_JS`) and posted as an
8 MB chunk. A 247 MB video froze the tab (ACL-2000), so ACL compresses videos above 45 MB with ffmpeg
(ACL-2000/2621). The owner does not want compression. Constraint kept: every Graph call stays **inside the profile,
through its proxy** (never one IP for all accounts), no `file_url` hosting, no upload from Python.

## 2. Data path (chosen)

```
file on disk
  -> Python (UploadServer, src/fb_api/upload_server.py)       reads one piece (<= 8 MB), checks its SHA-256
  -> CDP Fetch.fulfillRequest (binary body, browser process)  no atob, nothing on the page main thread
  -> Web Worker of the helper's world (blob worker)           fetch("https://adsmanager.facebook.com/__acl_upload/<token>/<offset>/<length>")
  -> worker: SHA-256 per piece, Blob of the pieces, FormData  one multipart POST, up to 64 MB
  -> https://<graph host>/<v>/act_X/advideos  (page proxy)    upload_phase=transfer, token in the body
```

* **The protocol loop stays in Python** (`creator._video_chunked`): start -> transfer (offsets from Meta) -> finish,
  operation journal (ACL-1856/2618), progress steps "uploaded X/Y MB", reconcile by readback, `waterfall_id`. This is
  a deliberate choice against moving the whole loop into the worker: resume, journaling, budget and the dashboard
  keep working unchanged, and **every call still goes through `write_policy.check` (Python) and the page helper's
  policy (JS)** - one call, one check. The worker only does the heavy part of a `transfer` call: it moves bytes.
* The page main thread never sees file bytes. The helper (isolated world) holds the policy and the session token;
  the token is posted to the worker in a message of that world only, and stays in the **body** of the POST
  (never the URL). The worker's POST uses `redirect: 'error'`, the helper's own credentials mode and the Graph host
  the page itself used.
* Pieces: 8 MB each (`UPLOAD_PIECE_MAX`), so a worker holds one piece in memory while checking it; the assembled
  chunk is a `Blob` (the browser keeps big blobs on disk), not an `ArrayBuffer`.
* Interception: only the prefix `https://adsmanager.facebook.com/__acl_upload/*` is paused (`Fetch.enable` with that
  pattern; the app's own traffic is never paused). A **light work tab** (ACL-2344) already intercepts its whole
  origin: `UploadServer` does not call `Fetch.enable` there (it would replace the pattern) and the document's CSP
  is changed to `worker-src blob:` (the document has no script of its own, so only the helper can start a worker).
  A dedicated worker has its own CDP session: `Target.setAutoAttach {waitForDebuggerOnStart, flatten}` attaches it
  paused, the server enables the narrow pattern on that session and resumes it - so the first request cannot race
  the interception. Requests are answered on the session that asked.
* Python pumps the tab's events while a job runs (`PageTab.pump` instead of `sleep` between polls), so a piece
  request waits milliseconds, not a poll interval.
* `UploadServer` serves **only** windows registered for the current chunk, **only exact announced pieces** (offset,
  length), and re-checks the piece's SHA-256 before it leaves (a file that changed after announcing is refused).
  Everything else with the prefix is `Fetch.failRequest BlockedByClient`: no request with that prefix can reach Meta.
* The websocket client masked frames with a per-byte Python loop (seconds for 8 MB). `_xor_mask` uses a big-integer
  XOR with the same bytes (tested against the loop).

## 3. CSP assumptions and how they are checked

* **Light work tab (default since ACL-2591):** the document is written by us (`Fetch.fulfillRequest`), so its CSP is
  ours: `worker-src blob:`. A blob worker inherits the creator's CSP; `connect-src` is unrestricted. This path does
  not depend on Meta's CSP.
* **Ads Manager tab (fallback when the light tab is not used):** Meta's CSP is not known offline. **Not verified.**
  The design does not guess: `probe_worker()` (below) runs the whole path once before any file is read or
  compressed; if the worker is blocked, the run falls back (section 7).
* Assumed but only checkable live: a dedicated worker's requests are reported on its own CDP session (handled), or on
  the page session (handled too: the answer goes to whichever session asked; a second paused copy fails harmlessly).

## 4. Probe (the live-check entry point)

`InPageGraph.probe_worker()` -> helper `probeWorker`: creates the worker, serves a 4 KB random in-memory chunk in
four 1 KB pieces, the worker fetches and hash-checks them. It proves CSP, worker attach, interception and binary
transfer, **without any request to Meta** and not counted in the call budget. The creator probes once per run
(`_decide_worker_path`), only when a video is larger than 16 MB, before reading or compressing anything. Reasons
returned: `no worker API`, `worker blocked: SecurityError`, `worker did not start`, piece errors.

## 5. Chunk sizing and the plan estimate

* Chunk cap `UPLOAD_CHUNK_MAX` = **64 MB** (served chunks). Actual size = `min(end_offset - start_offset from Meta, 64 MB,
  remaining)` exactly as before: Meta's window always wins. The old staged path keeps 8 MB (`UPLOAD_STAGED_CHUNK_MAX`).
* `UPLOAD_FILE_MAX` / `VIDEO_MAX_BYTES` = **4 GB** (Meta's limit); the old path keeps 1536 MB.
* `upload_calls(size)` = `ceil(size / 64 MB) + 3`: a 2.5 GB file is **43 calls** (was 195 for 1.5 GB at 8 MB).
  `planned_upload_size` returns the **original** size (no ffmpeg planning); `estimate_upload_calls` and Check plan use it.
  Videos up to 16 MB still go in one request (`VIDEO_SINGLE_MAX_BYTES_WORKER`, was 200 MB): the one-request path
  still stages base64 in the page, so it must stay small now that compression no longer protects the tab.
* Open point for the live check: if Meta offers windows smaller than 64 MB the call count rises. The estimate is an
  upper bound only if Meta accepts 64 MB; the live check must record the `end_offset - start_offset` Meta returns.

## 6. Resume, memory limits

* Resume is unchanged and now covers served chunks: unknown result -> same offset -> Meta names the right offset
  (`error_data`) -> continue from it; "page did not answer / worker unavailable" (`no_file`, nothing sent) retries the
  same chunk with back-off. Tests: lost answer, unsent, refused-with-offset: Meta ends with exactly the file's bytes
  (sha256 equal), no duplicate video.
* Memory: Python reads one piece at a time (<= 8 MB; `LargeFile` still refuses larger slices); the worker holds one
  piece plus the `Blob` handle list; the page main thread holds nothing. A changed file is caught by the piece hash
  and by a size check before every chunk.

## 7. Write policy and fallback

* Python (`write_policy._check_upload`): `ServedChunk` accepted for the transfer field up to 64 MB; plain bytes stay
  at 8 MB; all other rules unchanged (phases, keys, offsets, waterfall, session id, no file on start/finish).
* Page helper (`sourceOk`, mirrors it): exactly `{token, pieces}`; pieces contiguous from 0, each 1..8 MB with a hex
  SHA-256, at most 512; total 1..64 MB and equal to `expect.bytes`; the token matches the policy's pattern; only for
  the `transfer` phase, only through a job (`startCall`), only into `video_file_chunk`. An old policy table without
  the new keys fails closed. The staged path keeps its 8 MB check.
* **Fallback:** if the probe fails (or the switch is off, or the transport has no worker path) the run takes the old
  path - base64 staging, 8 MB chunks, ffmpeg compression (ACL-2000) - and says so in the warnings. A worker that
  becomes unavailable mid-run returns `no_file` (nothing sent) and is retried like a page stall.
* **Kill switch:** `ACL_VIDEO_WORKER_UPLOAD=0|false|no|off` (default on). Compression is **kept as the explicit
  fallback**, not removed; it no longer runs by default when the worker path works.

## 8. Tests (all fake: no browser, no Meta)

`tests/test_acl2629_worker_upload.py`: policy and parity (Python and Node), estimate and kill switch, `UploadServer`
against a fake CDP tab, `InPageGraph` probe and served write, the helper in Node with a fake Worker running the real
worker source against a fake Meta (start/transfer/finish, md5 of reassembled bytes equal to the source, windows from
Meta, a 12 MB chunk, corrupted/short/unanswered pieces, blocked or silent worker, lost answer, 15 malformed sources,
abort, old-policy fail-closed, mutated cap), the creator on a serving fake transport (no compression, 64 MB cap
vs bigger Meta window, smaller window, three interruption kinds, bad offsets, policy cap, file changed, probe failure
fallback, kill switch). Existing ACL-1891/2000/2084/2370 tests that describe the compressing path now run with the
kill switch off.

## 9. What the live check must show (separate step, pilot test account, campaign PAUSED, inside Multilogin)

1. `probe_worker` returns ok in the light work tab, and what it returns in an Ads Manager tab (CSP).
2. A worker session is attached and which session reports the piece requests (worker or page).
3. A 2.5 GB video uploads with the **same md5** as the source, campaign PAUSED, tab responsive during the upload
   (page still answers `Runtime.evaluate`; recorded timings), no compression step in the log.
4. Meta's `end_offset - start_offset` per chunk (is 64 MB accepted? what window does Meta offer?), total calls vs 43.
5. Per chunk time and CDP message size (an 8 MB piece is an ~11 MB websocket message); no browser memory blow-up.
6. Interrupt (kill the tab / the controller) mid-upload and publish again: no duplicate video, upload resumes.
7. No request with the `__acl_upload` prefix is visible to Meta (network log), and no Graph call leaves the profile.

## 10. Review round 1 (fixes)

- Auto-attach: every target that attaches while it is on is resumed; non-workers are also detached; after each chunk or
  probe `disarm()` turns auto-attach off, detaches worker sessions and removes the narrow `Fetch` pattern.
- Fail closed: if `Fetch.enable` fails on a worker session the worker is NOT resumed and its target is closed.
- No hung request: a failed `Fetch.fulfillRequest` is followed by `Fetch.failRequest`; every worker piece fetch has a
  60 s deadline (`no_file`, "timed out", so the existing retry/back-off runs); a served chunk has an overall deadline
  (120 s + length at 100 KiB/s) that aborts the job and reports an unknown result (reconciled by readback).
- `source` is refused unless the path is `act_X/advideos` and the field is `video_file_chunk`; `ServedChunk.offset`
  must equal `start_offset`; the worker POST carries the same referrer as other calls; `Network.setBypassServiceWorker`
  is set on worker sessions (best effort; live item 7 still watches for it).
- Smaller Meta windows: the creator re-estimates whenever the offered window shrinks and stops cleanly, before sending
  more, when the calls planned for the file (estimate + 5 % slack) would run out; the message says so and names the
  kill switch. The unfinished Meta session is simply abandoned (nothing is duplicated).

## 11. Live result and round 3 (probe diagnostics + inline mode)

Live on TEST9095 (normal Ads Manager tab, light tab not used): the probe reported "worker did not start" and the old
path refused a 2421 MB video (nothing created). "Did not start" means the helper got neither a synchronous exception
(CSP `worker-src` blocking a blob worker throws `SecurityError`, reported as "worker construct failed") nor an error
event: the worker was created but never posted `ready`. By Chrome semantics the candidates are, in order: (a) the
controller closed or never resumed the worker - `Fetch.enable` failing on a dedicated worker's session fails closed
(round 1, B2) and the worker is closed; (b) `Target.setAutoAttach` on the page session does not report the dedicated
worker created from an isolated world; (c) the attach event is handled too late (events are only handled inside CDP
calls and `pump`); (d) CSP reported through an error event. Meta's CSP cannot be read without a login (a plain request
is redirected to the login page, no CSP header), so this could not be settled offline. The design therefore covers
both outcomes:

* **Diagnostics.** A failed probe now says which stage failed and why: `worker: <construct failed: Name: message |
  error event before ready: ... | created but sent no ready message in 8 s>`, `inline: <reason>`, any
  `securitypolicyviolation` the document reported meanwhile (`[CSP: worker-src blocked blob:]`), and the controller
  side: `controller: attach events N, workers seen N, workers ready N, refused N, piece requests N, served N,
  errors N (last: ...)`. No URL or token is in it. It goes into the run's warnings.
* **Inline mode (no Worker needed).** When no worker can run, the same runner (one code, `makeRunner`, stringified into the
  worker or called in the helper's world) fetches the pieces from the same intercepted URLs with `fetch()` from the
  helper's world: still binary `Fetch.fulfillRequest` bodies, still one piece at a time hashed with WebCrypto, kept as
  Blobs, the multipart POST sent by `fetch` with the same options (token in the body, `redirect: 'error'`, referrer).
  Nothing is base64-decoded in the page. It uses the page session's narrow `Fetch` pattern, so it does not depend on
  worker auto-attach.
  The probe tries the worker first and falls back to inline; the chosen mode is kept and the worker's failure reason is
  logged as a warning ("Upload worker not used, continuing inline: ..."), so the next live run tells exactly why.
* Safety rules unchanged: Python and page-helper policy per call, exact announced pieces only, hash re-check, fail
  closed, deadlines, disarm on every exit. If both stages fail the run falls back to compression as before.
* Live check addition: record `upload_mode` (worker or inline) and the failure text; for inline, check the main-thread
  responsiveness (the SHA-256 of an 8 MB piece is async, but the Blob/FormData work is on the page thread).

### Round 4 correction: service workers

`Network.setBypassServiceWorker` is a no-op without `Network.enable`, which this package never turns on (a test bans
it), so it is **not used** (removed from both the page session and worker sessions; nothing is left to revert in
`disarm()`). Earlier statements here and in section 2 (N5) that a service worker is bypassed are withdrawn. Instead
the page helper **refuses both modes when `navigator.serviceWorker.controller` is set** (`swControlled()`): the probe
reports "page is controlled by a service worker" for the worker and the inline stage, and the run falls back to
compression; a served chunk in such a page returns `no_file` before any piece moves. It is not proven that CDP
Fetch catches a fetch that a service worker passes on, so the path stays closed there. Live item 7 still watches for
any `__acl_upload` request on the network.
