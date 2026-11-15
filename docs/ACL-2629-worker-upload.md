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

## 12. ACL-2637: a separate tab without a service worker

Live (TEST9095, 2421 MB): the Ads Manager tab is **always** controlled by Facebook's service worker, so the round-4
refusal fired on every real run. The refusal stays; the upload moves to a context where it does not apply:

* `InPageGraph.probe_worker()` probes its own tab first. If the helper answers `controlled: true` (this page has a
  `navigator.serviceWorker.controller`), the transport opens ONE more tab of the **same browser** (`PageTab.open` on
  the tab's own CDP endpoint = same Multilogin profile, same proxy) and loads the light document there
  (`light_tab.open_upload_document`): our own `Fetch.fulfillRequest`, origin-wide interception, nothing reaches Meta.
* If that document is controlled, it is reloaded with `Page.reload {ignoreCache: true}` (shift-reload; Chrome skips
  the service worker for it and the new document is uncontrolled). The state is then **checked**: still controlled
  or unreadable -> the tab is closed and the probe fails with
  `...; clean upload tab: upload tab is controlled by a service worker even after a reload that bypasses it`.
* The token goes there world-to-world (`receive_from`, ACL-2344 hand-over); the helper there gets exactly the policy
  of the session. Probe and served `transfer` chunks run there (`_write_served` delegates; Python policy is still
  checked once in `write`, the helper of the clean tab checks it again; referrer = the act's campaigns URL).
* A service worker claiming the clean page mid-upload (`clients.claim()`): inline mode re-checks the controller
  before every piece; worker mode aborts on `controllerchange` while pieces are still moving. Both return `no_file`
  ("a service worker took control of the page during the upload"), nothing posted.
* Closed: on a new probe, on a lost clean tab (`no_file`, "clean upload tab lost"), and when the run ends
  (`CampaignCreator.run` -> `transport.close_upload_context()`); at process exit by `light_tab._OPEN`.
* Fallback message: when the old path cannot take the file (> 1536 MB after the compression attempt) the run stops
  at plan with ONE line ("the in-profile upload is not available (<reason>) and the fallback upload accepts at most
  1536 MB (<compression reason>). Nothing was uploaded.") instead of "uploading the original" + a size refusal.

Assumed, checkable only live: (1) the light document on `adsmanager.facebook.com/__acl_work` is controlled after a
plain navigation and uncontrolled after the hard reload; (2) a new tab of the profile does not disturb the work tab
(it becomes the active tab of the window, like the ACL-2344 capture tab; focus emulation keeps both running);
(3) Meta accepts the upload POST from that document like from the light work tab.

Review fixes (ACL-2637): a light lease switched to Ads Manager (ACL-2593) hands the clean upload tab to the new
transport (`adopt_upload_context`; closed if the new transport cannot take it). A service worker taking control
sends the worker `stopPieces` (not `abort`): ignored once the pieces are in, so a started POST is never cancelled.
The buyer line for a file the fallback cannot take carries only the first part of the probe reason; the full text
is a step, and the "compressed the slower way" warning is dropped in that case.

## 13. ACL-2639: large videos with the upload protocol of Ads Manager ("file API")

The first ACL-2639 version used Meta's documented "FB Video Ads" protocol (`POST act_X/video_ads` +
`rupload.facebook.com/video-ads-upload/...` with `Authorization: OAuth`). Live run 15.11: the start is refused for the
Ads Manager page token (`(#10) Permission Denied`), so the creator fell back to ~485 chunked calls and stopped on the
call budget. That protocol is **removed** (edge, keys, version/URL checks, the object-path naming write); the creator
now sends what Ads Manager itself sends (recorded live 15.11, values masked):

1. `POST /act_X/advideos` (Graph, the page access token as before): `upload_phase=start, file_size, file_extension=mp4,
   waterfall_id=<32 lowercase hex, random per upload>, supports_chunking=true, supports_file_api=true,
   partition_start_offset=0, partition_end_offset=<file_size>`. No composer/source fields. Answer: `video_id`,
   `upload_session_id`, `start_offset`, `end_offset`.
2. The bytes go to the **resumable service of the page**, not Graph (not counted in the Graph call budget):
   `https://<rupload-<place>.up>.facebook.com/fb_video/<waterfall_id>-0-<file_size>?fb_dtsg_ag=<token>&__a=1`,
   `credentials: include` (the session cookies of the Ads Manager origin), no access token, no `Authorization`.
   `GET` -> `{dc, offset}` (bytes already held = the resume point). `POST`, headers `offset`, `X-Entity-Length`
   (= file size), `X-Entity-Type: application/octet-stream`, body = the file bytes from `offset`, at most 512 MB per POST
   (served piece by piece from the file to the worker/inline runner, each piece hashed, as in section 2).
   Only one partition (0 .. size).
3. `POST /act_X/advideos`: `upload_phase=transfer, upload_session_id, video_id, start_offset=0, end_offset=<size>,
   partition offsets, fbuploader_video_file_chunk=<file handle>`. `start_offset == end_offset == size` in the answer
   means Meta holds every byte.
4. `POST /act_X/advideos`: `upload_phase=finish, upload_session_id, video_id, title=<ACL tag>`. The title is what the
   later search by tag finds, so the separate naming call (`POST /<id>`) and its policy/JS allowance are gone.

**Where the values come from.** The service host is `AdsVideoUploadConfig.resumable_service_name` /
`resumable_service_domain` of the Ads Manager page (observed `rupload-fra3-1.up` / `rupload-ber1-1.up` +
`facebook.com`), the query token is `DTSGInitData.async_get_token` (62 characters). The helper reads both from the
inline scripts next to the existing `DTSGInitialData` scan, validates them (name `^rupload-[a-z0-9-]{1,30}\.up$`,
domain exactly `facebook.com`, token pattern) and carries them with the access token in the world-to-world hand-over
(`handOver` / `listen`: fields `d` and `r`, optional) - so a light work tab, a clean `__acl_work` upload tab and an
adopted upload context (`adopt_upload_context`) have them too. They are session secrets: they never leave the helper
(the helper builds the address itself; Python sends only the path `/fb_video/<waterfall>-0-<size>`, the three headers
and the served body), `clean()` masks the token in every answer, nothing is logged, put in telemetry, steps or
exceptions. `InPageGraph.upload_service_ready()` asks the helper (local, no Meta call) before the start: no service or
token -> `_ResumableRefused("upload service address not available on this page")` and the chunked path runs.

**The file handle** is in the answer to the last POST (that answer was not captured live; parsed defensively): the
string field `h`, otherwise any string value of the answer (or of one nested object) with the observed shape
`N:<base64 name>:<mime>:<id>:e:<expiry>:<signature>` (`write_policy.FILE_HANDLE_RE`). An HTTP 4xx/5xx is a refusal
(resume from the offset read by GET); a 2xx answer without a handle: one GET, and if the service holds every byte but
there is no handle, the upload stops as refused-before-finish (`_ResumableRefused`, nothing finished) and the chunked
fallback runs. Failures after a transfer was accepted are real stops, never a fallback.

**Policy (Python `write_policy`, mirrored in the page helper).** advideos phases of this protocol are told from the
chunked ones by their marker keys (`supports_file_api`, `fbuploader_video_file_chunk`, `video_id`) and checked with a
closed key set and exact values per phase (`_check_file_api`; title must be the ACL tag name). The service request is
`check_rupload`: only GET and POST, path `^/fb_video/[0-9a-f]{32}-0-[0-9]{1,13}$`, query keys only `fb_dtsg_ag` and
`__a`, headers only `offset` / `X-Entity-Length` / `X-Entity-Type`, the POST body only a served window of the file
starting at `offset` and ending inside it. The helper refuses any request that carries its own address, host, query or
credentials (closed spec keys).

**Budget and resume.** Graph calls per video: start, transfer, finish (+ one status read when an earlier run is
continued or a finish answer is lost): `RESUMABLE_FIXED_CALLS = 4`, independent of the size (2.4 GB = 5 service POSTs
of 512 MB). A dropped POST: GET the offset, continue from it (never from zero), at most `RESUMABLE_MAX_RESUMES`. The
journal keeps `video_id`, `upload_session_id`, `waterfall_id`, file size and md5 after the start; a stopped run is
continued from the offset the service reports. If the offset is unreadable, or every byte is in but the handle is gone,
the old session is forgotten and a new one starts (a finished video is found by its title first). The emergency flag
`ACL_VIDEO_RESUMABLE_UPLOAD=0` still sends everything through the chunked path.

Assumptions checkable only live: the answer body of the last rupload POST (handle field), and that the service accepts
the CORS preflight of the custom headers from the clean `__acl_work` origin like it does from Ads Manager itself.

### 13.1 Review fixes (files over one segment, adoption, masking)

* **Multi-POST is unverified live.** After the FIRST partial (non-final) segment POST, whatever its status, ONE offset GET
  must show `offset + length`; otherwise the upload is refused (`the upload service did not keep a partial piece`): nothing
  is finished yet, so the chunked fallback is safe. A 4xx/5xx whose GET shows the bytes kept is continued normally.
* **Resumes.** A resume counts against `RESUMABLE_MAX_RESUMES` only when it made no progress (the reported offset is not
  above the one we had); a GET showing progress is free. Two 4xx answers in a row: the upload service address and the
  session token are read again once (the page re-scans its scripts; a clean upload tab gets them by an upload-only
  hand-over of the same session) before more resumes are spent.
* **Handle.** The tail after `:e:<digits>` may be 1-6 `:`-separated parts. The GET answer is parsed too (the service may
  give `h` once the upload is complete). A 2xx answer without a handle logs a step with key names and value lengths only.
* **Adoption of a pending video.** `processing/ready` alone is never trusted: the finish with the title is sent once;
  success or a definite "already finished" answer counts. The journal keeps `transferred=true` after a confirmed transfer;
  a continuation of such a video sends only the finish and never starts a second video.
* **Before bytes moved,** a request refused before sending (Python policy, page helper, empty service after a world reset)
  or an unusable offset read is a refusal (fallback), not a run stop; afterwards it is a stop with the reason.
* **Transfer failures are a fallback, honestly.** "Meta did not take all the bytes" and "the transfer answer was lost
  twice" happen BEFORE the finish, so nothing was published: the chunked path runs and the unfinished video of this
  session stays at Meta (never published, no finished duplicate). Only failures after the transfer was accepted are
  real stops.
* **Masking.** The helper masks the session token raw and URL-encoded and the `fb_dtsg`/`fb_dtsg_ag` parameters in every
  answer (error JSON and non-JSON/HTML bodies). `GraphClient.upload_service_ready()` defaults to "not supported here".

### 13.2 Follow-ups

* **Refresh (honest).** `refreshUpload()` first asks the page's own module registry (`require('DTSGInitData')`,
  `require('AdsVideoUploadConfig')`) where `require` is visible, validates with the same policy patterns, then re-scans the
  inline scripts for what is missing; a tab without the Ads Manager page (clean `__acl_work` tab) keeps its current values.
  The helper runs in an isolated world, where `require` is normally NOT visible, so in practice the re-scan of the page's
  inline scripts is what updates the values; the clean tab then receives them by an upload-only hand-over (same session
  token accepted, another token rejected, no token -> `no_token`).
* **Continuing an earlier upload.** All bytes in and the service returns the handle to a GET: straight to the transfer.
  The status read asks `status,title`: a title equal to our ACL tag is adopted with no write. Published but the repeated
  finish not confirmed: stop as "unknown" (Publish again later); a definite refusal of the repeated finish while the title is
  absent: the entry is forgotten (the title lookup that runs first on every publish protects from a duplicate).
  A `retired` helper is a stop even before bytes moved.
* **Budget of the chunked fallback.** The plan counts the file-API upload as primary (`resumable_calls()`); with the flag
  off or after a refusal the chunked plan assumes Meta's real windows (`CHUNK_WINDOW_LIVE` = 5 MB, not the 64 MB ceiling).
  The chunked allowance of a worker-path upload is the run's remaining budget minus the estimate of the rest of the
  campaign (never above the hard `max_calls`); the projection stop (`small pieces`) compares against that allowance.
