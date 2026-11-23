# ACL Telemetry Hub, часть 1: приём в D1 и выводы — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Новый Worker `acl-hub` в корпоративном аккаунте Cloudflare принимает пачки событий от машин байеров (тот же контракт `POST /v1/batch`), складывает их в D1 и сразу строит таблицы `machines / jobs / runs`; клиент шлёт раз в 2 минуты и сразу при сбое, присылает паспорт машины и нагрузку; история из локальной SQLite владельца переносится в хаб.

**Architecture:** Один Worker (ES-модули, без зависимостей) + одна D1. Приём: распаковка gzip → разбор ndjson → `INSERT OR IGNORE` в `events` → для новых событий чистые функции `derive*()` дают SQL-апсерты в `machines/jobs/runs`. Владелец читает через `/api/*` по `OWNER_KEY` (Cloudflare Access — в части 2). Тесты Worker — `node --test` с поддельной D1 поверх встроенного `node:sqlite` (Node 24).

**Tech Stack:** Cloudflare Workers + D1, wrangler 4, Node 24 (`node:test`, `node:sqlite`), Python 3 stdlib + `psutil`, pytest.

**Spec:** `docs/superpowers/specs/2026-11-17-acl-telemetry-hub-design.md`

## Global Constraints

- Процесс: Issue заводит главная сессия трекера до кода; ветка `work/acl-<N>-telemetry-hub` от `work/main`: `git worktree add ../acl-<N>-hub --no-track -b work/acl-<N>-telemetry-hub work/main`; пуш только явным refspec; в общий чекаут `C:\fb_agents` не коммитить.
- Контракт машины не меняется: `POST /v1/batch`, тело gzip ndjson ≤ 1 МБ, заголовки `Authorization: Bearer <ACL_LIBRARY_TOKEN>`, `X-ACL-Machine` (`^[A-Za-z0-9_-]{1,64}$`), `X-ACL-Batch` (`^[A-Za-z0-9-]{1,128}$`); ответы `200 / 400 / 401 / 413 / 503`. Повторная пачка → `200`.
- Проверка токена — как в `deploy/event-log/worker.js` (`authorize`): GitHub `GET /repos/MojoTheApe/acl-lib`, свежесть 1 ч, при недоступности GitHub — доверие записи до 24 ч; хранится только sha256.
- Worker без npm-зависимостей. Тесты: `cd deploy/acl-hub && node --test`.
- Секреты Worker: `OWNER_KEY` (только `wrangler secret put`). В репо — только имена.
- `heartbeat` в `events` не пишется, только обновляет `machines`.
- Событие, payload которого совпадает с `/EAA[A-Za-z0-9]{20,}|access_token=/i`, не пишется; счётчик `rejected` в ответе.
- Исход прогона: `ok` если `success === true`; `not_started` если `error` начинается с `Not started`; иначе `failed`.
- Клиент: сбой журнала никогда не меняет ход задания (всё в `try/except`), отправка только в фоновом потоке. `CYCLE_S = 120`.
- Комментарии в коде — по-русски; пользовательский текст (ответы API с `error`) — по-английски.
- Тесты Python — только затронутые файлы; полный набор — в CI.

## Review Focus

1. **Порядок событий внутри и между пачками.** `publish.stats` приходит раньше `publish.result`, `job.finished` может прийти раньше последнего `publish.result` (другая пачка). Ожидание: итоговые `runs/jobs` одинаковы при любом порядке. Тест — Task 4 `derive is order-independent`.
2. **Повторная доставка.** Машина пересылает ту же пачку после таймаута. Ожидание: ни одной лишней строки, счётчики `jobs` не удваиваются. Тест — Task 3 `duplicate batch adds nothing`.
3. **Старая сборка у байера** (без `machine.profile`, без `load`, `CYCLE_S=3600`). Ожидание: хаб принимает, `runs` строятся, пустые поля — `NULL`. Тест — Task 4 `stats without load`.
4. **Битые строки в пачке** (обрезанный JSON, без `event_id`, неизвестный `type`). Ожидание: остальные строки пачки приняты, ответ `200` с `rejected`. Тест — Task 3 `bad lines are skipped`.
5. **Нет сети / хаб отвечает 503** у байера. Ожидание: задание не ждёт, события остаются в ящике, повтор с backoff. Тест — Task 7 `publish failure wakes shipper without blocking`.

---

## Файлы

```
deploy/acl-hub/
  wrangler.toml            — имя, D1-привязка DB, cron (пусто до части 3)
  package.json             — "test": "node --test"
  README.md                — развёртывание и проверка (Task 9)
  migrations/0001_init.sql — схема D1
  src/index.js             — роутер fetch()
  src/auth.js              — authorize(token, env), sameSecret(a, b)
  src/ingest.js            — handleBatch(request, env, {machine, batchId})
  src/derive.js            — classify(), fingerprint(), normalizeReason(), deriveStatements()
  src/api.js               — /api/events, /api/runs, /api/jobs, /api/machines
  test/fake-d1.js          — поддельная D1 на node:sqlite
  test/*.test.js
src/acl_event_log/shipper.py         — адрес хаба, CYCLE_S
src/acl_event_log/machine_profile.py — collect_profile(), load_snapshot()  (новый)
src/acl_event_log/events.py          — stats_payload(load=...)
src/acl_event_log/__init__.py        — emit_machine_profile()
src/web/publish_jobs.py:1404         — будить отправку при сбое
src/web/publish_api.py:929           — передать load
scripts/acl_hub_backfill.py          — перенос истории (новый)
scripts/acl_log_pull.py              — чтение из хаба
tests/test_acl_event_log_hub.py      — тесты клиента
tests/test_acl_hub_backfill.py
```

---

### Task 1: Каркас Worker, схема D1, поддельная D1

**Files:**
- Create: `deploy/acl-hub/wrangler.toml`, `package.json`, `migrations/0001_init.sql`, `src/index.js`, `test/fake-d1.js`, `test/schema.test.js`

**Interfaces:**
- Produces: `FakeD1` (`test/fake-d1.js`): `new FakeD1()`, `.applyMigrations(dir)`, `.prepare(sql).bind(...args)` → стейтмент с `.run()` → `{success, meta:{changes, last_row_id}}`, `.all()` → `{results}`, `.first()` → строка | `null`; `.batch(stmts)` → массив результатов `run()`, всё в одной транзакции (откат при ошибке); `.exec(sql)`.
- Produces: `src/index.js` `export default { fetch(request, env, ctx) }`, для неизвестного пути — `404 {"error":"not found"}`.

- [ ] **Step 1: Схема `migrations/0001_init.sql`** (это контракт для частей 2–5, писать дословно)

```sql
CREATE TABLE events (
  seq_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE, type TEXT NOT NULL, ts TEXT NOT NULL,
  machine_id TEXT, machine TEXT, manager TEXT, flow_version TEXT, channel TEXT,
  job_id TEXT, account TEXT, campaign TEXT, stream_id TEXT, seq INTEGER,
  batch_id TEXT, received_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX events_ts ON events(ts);
CREATE INDEX events_job ON events(job_id);
CREATE INDEX events_machine_ts ON events(machine_id, ts);
CREATE INDEX events_type_ts ON events(type, ts);

CREATE TABLE machines (
  machine_id TEXT PRIMARY KEY, machine TEXT, manager TEXT, flow_version TEXT,
  first_seen_at TEXT, last_event_at TEXT, last_heartbeat_at TEXT,
  outbox_bytes INTEGER, dropped_total INTEGER DEFAULT 0,
  profile TEXT, profile_at TEXT
);

CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY, machine_id TEXT, manager TEXT, account TEXT,
  created_at TEXT, started_at TEXT, finished_at TEXT, duration_s REAL,
  slot_wait_s REAL, status TEXT, cancelled INTEGER, dry_run INTEGER, reviewed INTEGER,
  n_planned INTEGER, n_ok INTEGER DEFAULT 0, n_failed INTEGER DEFAULT 0,
  n_not_started INTEGER DEFAULT 0
);
CREATE INDEX jobs_created ON jobs(created_at);
CREATE INDEX jobs_manager ON jobs(manager, created_at);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,               -- job_id || '|' || campaign
  job_id TEXT, campaign TEXT, machine_id TEXT, manager TEXT, account TEXT,
  finished_at TEXT, outcome TEXT,        -- ok | failed | not_started
  stop_kind TEXT, error TEXT, fingerprint TEXT,
  duration_s REAL, calls INTEGER, writes INTEGER,
  campaign_id TEXT, published_as TEXT, created_any INTEGER, reused_any INTEGER,
  spec_hash TEXT, country TEXT, daily_budget REAL, adset_names TEXT, ad_names TEXT,
  n_ads INTEGER, load TEXT, flow_version TEXT
);
CREATE INDEX runs_finished ON runs(finished_at);
CREATE INDEX runs_manager ON runs(manager, finished_at);
CREATE INDEX runs_fingerprint ON runs(fingerprint);

CREATE TABLE tokens (token_sha TEXT PRIMARY KEY, checked_at INTEGER NOT NULL);
```

- [ ] **Step 2: `wrangler.toml`**: `name = "acl-hub"`, `main = "src/index.js"`, `compatibility_date = "2026-11-01"`, `[vars] LIBRARY_REPO = "MojoTheApe/acl-lib"`, `[[d1_databases]] binding = "DB"`, `database_name = "acl_hub"`, `database_id = "REPLACE_WITH_D1_ID"`, `migrations_dir = "migrations"`. `package.json` как у `deploy/event-log`, `"test": "node --test"`.

- [ ] **Step 3: Тест `test/schema.test.js`**

```js
test("migration creates all tables", async () => {
  const db = new FakeD1(); db.applyMigrations(new URL("../migrations/", import.meta.url));
  const { results } = await db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all();
  const names = results.map((r) => r.name).filter((n) => !n.startsWith("sqlite_"));
  assert.deepEqual(names, ["events", "jobs", "machines", "runs", "tokens"]);
});
test("batch rolls back on error", async () => {
  // второй стейтмент нарушает UNIQUE(event_id) → в events 0 строк
});
test("unknown path is 404", async () => {
  const res = await worker.fetch(new Request("https://h/nope"), { DB: db });
  assert.equal(res.status, 404);
});
```

- [ ] **Step 4:** `cd deploy/acl-hub && node --test` → FAIL (нет модулей).
- [ ] **Step 5:** Реализовать `FakeD1` поверх `DatabaseSync` (`batch` — `BEGIN`/`COMMIT`/`ROLLBACK`) и роутер-заглушку.
- [ ] **Step 6:** `node --test` → PASS.
- [ ] **Step 7: Commit** `git add deploy/acl-hub && git commit -m "feat(acl-hub): каркас Worker, схема D1, поддельная D1 для тестов"`

---

### Task 2: Авторизация машин и владельца

**Files:**
- Create: `deploy/acl-hub/src/auth.js`, `deploy/acl-hub/test/auth.test.js`

**Interfaces:**
- Consumes: `FakeD1`, таблица `tokens`.
- Produces: `authorize(token: string, env) -> Promise<"ok"|"denied"|"unavailable">`; `sameSecret(a: string, b: string) -> Promise<boolean>`; `bearer(request) -> string`; `isOwner(request, env) -> Promise<boolean>` (`sameSecret(bearer, env.OWNER_KEY)`).

- [ ] **Step 1: Тесты** — перенести сценарии `deploy/event-log/worker.test.js` про токен, заменив `MemoryKV` на `FakeD1`:
  - `fresh token record skips GitHub` — запись `checked_at = now-10min` → `"ok"`, `fetch` не вызван;
  - `github 200 stores sha only` — после `"ok"` в `tokens` одна строка, `token_sha` = sha256 токена, исходного токена нет ни в одной колонке;
  - `github 401 denies`; 
  - `github down within 24h grace is ok, after 24h unavailable`;
  - `owner key compare` — `isOwner` true для `OWNER_KEY`, false для пустого и для токена библиотеки.
- [ ] **Step 2:** `node --test test/auth.test.js` → FAIL.
- [ ] **Step 3:** Реализовать, перенеся логику `authorize` из `deploy/event-log/worker.js` (User-Agent `acl-hub`), KV → `INSERT OR REPLACE INTO tokens`.
- [ ] **Step 4:** → PASS.
- [ ] **Step 5: Commit** `feat(acl-hub): проверка токена библиотеки и ключа владельца на D1`

---

### Task 3: Приём пачки `POST /v1/batch`

**Files:**
- Create: `deploy/acl-hub/src/ingest.js`, `deploy/acl-hub/test/ingest.test.js`
- Modify: `deploy/acl-hub/src/index.js`

**Interfaces:**
- Consumes: `authorize`, `deriveStatements` (Task 4; до него — заглушка, возвращающая `[]`).
- Produces: `ingestEvents(env, events: object[], {batchId, machineHeader}) -> Promise<{accepted, duplicates, rejected}>` (используется и бэкфиллом в Task 6); `handleBatch(request, env) -> Response`.

Правила: заголовки и размер проверяются до распаковки (`413` если `Content-Length` или фактическое тело > 1 МБ); распаковка `DecompressionStream("gzip")`; строка принимается, если это объект с `event_id` (строка ≤ 128), `type` (строка ≤ 64), `ts` (строка); `heartbeat` → только апсерт `machines` (`last_heartbeat_at`, `outbox_bytes`, `dropped_total = dropped_total + payload.dropped`); остальные → `INSERT OR IGNORE INTO events`; `deriveStatements` вызывается только для событий с `meta.changes === 1`; ошибка D1 → `503 {"error":"storage unavailable"}`; `authorize` = `unavailable` → `503`, `denied` → `401`.

- [ ] **Step 1: Тесты** (`fixtures` — 6 настоящих событий Йотама 16.11 по `KLD_BFL_H23`: `job.created`, `job.started`, `publish.stats` ×1, `publish.result` checkpoint, `publish.result` Not started, `job.finished`, с обезличенными id):
  - `accepts batch and stores events` → `200`, `{accepted: 6, duplicates: 0, rejected: 0}`, в `events` 6 строк;
  - `duplicate batch adds nothing` → второй раз `{accepted: 0, duplicates: 6}`, строк 6, `jobs.n_failed` = 1, `n_not_started` = 1;
  - `heartbeat updates machine but is not stored` → `events` 0, `machines.last_heartbeat_at` = ts;
  - `bad lines are skipped` → пачка из 3 строк: битый JSON, без `event_id`, нормальная → `{accepted: 1, rejected: 2}`;
  - `secret-looking payload is rejected` → payload с `"access_token=abc"` → `rejected: 1`, строки нет;
  - `oversized body is 413`, `bad machine header is 400`, `no token is 401`, `github down without record is 503`.
- [ ] **Step 2:** → FAIL. **Step 3:** реализовать. **Step 4:** → PASS.
- [ ] **Step 5: Commit** `feat(acl-hub): приём пачек в D1 с защитой от повторов и секретов`

---

### Task 4: Выводы — machines / jobs / runs

**Files:**
- Create: `deploy/acl-hub/src/derive.js`, `deploy/acl-hub/test/derive.test.js`

**Interfaces:**
- Produces:
  - `normalizeReason(text: string) -> string` — порт `src/failure_signatures.normalise_reason` без `scrub`: uuid → `<id>`, hex ≥ 12 → `<id>`, числа `\d+(?:[.,]\d+)?` → `#`, пробелы схлопнуть, `toLowerCase`, обрезка 160 + `…`; пустое → `(no reason)`.
  - `fingerprint(stopKind: string|null, error: string) -> Promise<string>` — первые 12 hex `SHA-1(`${stopKind || "unknown"}|${normalizeReason(error)}`)`.
  - `classify(result: object) -> "ok"|"failed"|"not_started"` (правило из Global Constraints).
  - `deriveStatements(db, event: object) -> Promise<D1PreparedStatement[]>`.

Что делает `deriveStatements` (каждое событие апсертит только свои колонки, поэтому порядок не важен):

| Событие | Изменения |
|---|---|
| любое | `machines`: `machine, manager, flow_version` (последние), `first_seen_at = MIN`, `last_event_at = MAX` |
| `machine.profile` | `machines.profile = payload`, `profile_at = ts` |
| `job.created` | `jobs`: `machine_id, manager, account, created_at, n_planned = len(campaign_names), dry_run, reviewed` |
| `job.started` | `jobs.started_at, slot_wait_s` |
| `job.finished` | `jobs.finished_at, duration_s, status, cancelled` |
| `publish.spec` | `runs`: `spec_hash, country, daily_budget, adset_names = JSON(adsets[].name), ad_names = JSON(ads[].name), n_ads` |
| `publish.stats` | `runs`: `duration_s, calls, writes, stop_kind, spec_hash, load = JSON(payload.load) | NULL` |
| `publish.result` | `runs`: `finished_at = ts, outcome, error, fingerprint (только failed), campaign_id, published_as, created_any = (created непустой), reused_any, flow_version`; затем `jobs.n_ok/n_failed/n_not_started` пересчитать `SELECT count(*) … FROM runs WHERE job_id = ?` |

`run_id = job_id + "|" + (event.campaign || payload.campaign_name)`; у `publish.*` без `job_id` — выводов нет. Колонки `runs.machine_id/manager/account` ставит любое `publish.*`.

- [ ] **Step 1: Тесты:**
  - `normalizeReason matches python` — три пары вход/выход, посчитанные вызовом `normalise_reason` (без секретов), например `"Graph API stopped: ... (checkpoint). ... ad 1, ad 2"` → `"graph api stopped: … ad #, ad #"`;
  - `classify` — `success:true` → ok; `"Not started: Graph API stopped earlier"` → not_started; `checkpoint` → failed;
  - `yotam H23 sequence` — фикстура Task 3 → в `runs` 2 строки: `_45_16.11` `failed`, `stop_kind = "checkpoint"`, `calls = 85`, `writes = 257`, `fingerprint` 12 hex; `_50_16.11` `not_started`, `fingerprint IS NULL`; `jobs` `n_planned = 4`, `n_failed = 1`, `n_not_started = 1`, `duration_s = 1476.9`;
  - `derive is order-independent` — те же события в обратном порядке → те же строки `runs` и `jobs`;
  - `stats without load` → `runs.load IS NULL`, остальное заполнено;
  - `same failure same fingerprint` — две ошибки, отличающиеся только числами («needs about 494 Meta calls; this run can spare 88» и «… 24 … 4») → одинаковый отпечаток.
- [ ] **Step 2:** → FAIL. **Step 3:** реализовать, подключить в `ingest.js`. **Step 4:** → PASS (вместе с `ingest.test.js`).
- [ ] **Step 5: Commit** `feat(acl-hub): выводы прогонов, заданий и машин при приёме`

---

### Task 5: API владельца

**Files:**
- Create: `deploy/acl-hub/src/api.js`, `deploy/acl-hub/test/api.test.js`
- Modify: `deploy/acl-hub/src/index.js`

**Interfaces:**
- Consumes: `isOwner`.
- Produces (все `GET`, `Authorization: Bearer <OWNER_KEY>`, иначе `401`):
  - `/api/events?after=<seq_id>&limit=<≤1000, по умолч. 500>` → `application/x-ndjson`, строки — событие в форме клиента (`payload` разобран), заголовок `X-Next-After: <последний seq_id>`;
  - `/api/runs?from=<ISO>&to=<ISO>&manager=&outcome=` → `{runs:[…]}` по `finished_at`, до 2000, свежие сверху;
  - `/api/jobs?from=&to=&manager=` → `{jobs:[…]}`;
  - `/api/machines` → `{machines:[…]}` с `profile` разобранным.
  Части 2–3 используют эти же маршруты.

- [ ] **Step 1: Тесты:** `requires owner key` (401 без ключа и с токеном библиотеки); `events paging` (3 события, `limit=2` → 2 строки и `X-Next-After`, следующий запрос → 1); `runs filter by manager and outcome`; `machines parse profile`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5: Commit** `feat(acl-hub): API чтения для владельца`

---

### Task 6: Перенос истории

**Files:**
- Modify: `deploy/acl-hub/src/index.js` (маршрут `POST /v1/backfill`), `deploy/acl-hub/test/ingest.test.js`
- Create: `scripts/acl_hub_backfill.py`, `tests/test_acl_hub_backfill.py`

**Interfaces:**
- Consumes: `ingestEvents(env, events, {batchId, machineHeader: null})`.
- Produces: `POST /v1/backfill` (только `OWNER_KEY`, тело gzip ndjson, `X-ACL-Batch`), машина берётся из каждого события. Python: `iter_batches(db_path: Path, size: int = 500) -> Iterator[tuple[str, bytes]]` (batch_id = `backfill-<min seq>-<max seq>`, gzip ndjson в форме клиента), `main(argv) -> int` с `--db`, `--url` (по умолчанию `ACL_HUB_URL`), ключ из `ACL_HUB_OWNER_KEY`.

- [ ] **Step 1: Тесты:** JS `backfill requires owner key`, `backfill keeps each event's machine`; Python `test_iter_batches_round_trips` (SQLite из 3 событий → одна пачка, после gunzip те же `event_id` и разобранный `payload`), `test_main_posts_all_batches_and_reports` (подменённый `post` получил 2 пачки при `size=2`, вывод `accepted=3`), `test_main_without_key_exits_1`.
- [ ] **Step 2–4:** FAIL → реализовать → PASS (`node --test`, `pytest tests/test_acl_hub_backfill.py -v`).
- [ ] **Step 5: Commit** `feat(acl-hub): перенос истории из локальной базы владельца`

---

### Task 7: Клиент — адрес хаба и отправка почти сразу

**Files:**
- Modify: `src/acl_event_log/shipper.py` (`DEFAULT_URL`, `CYCLE_S`), `src/web/publish_jobs.py:1404`
- Test: `tests/test_acl_event_log_hub.py`

**Interfaces:**
- Produces: `shipper.CYCLE_S = 120`; `shipper.DEFAULT_URL = "https://acl-hub.<поддомен>.workers.dev"` — значение подставляется в Task 9 после развёртывания, до этого остаётся прежний адрес (старый Worker `acl-event-log`), чтобы ветка не ломала отправку.
- В `publish_jobs.py` после `_log_event("publish.result", …)`: если `res.get("success") is not True` — `wake_shipper()` в `try/except`.

- [ ] **Step 1: Тесты:**
  - `test_cycle_is_two_minutes` — `shipper.CYCLE_S == 120`;
  - `test_publish_failure_wakes_shipper_without_blocking` — подменить `src.acl_event_log.wake_shipper` счётчиком и `emit` на запись; вызвать участок записи результата с `{"success": False, …}` → счётчик 1; с `{"success": True}` → 0; если `wake_shipper` бросает, исключение не выходит наружу.
- [ ] **Step 2–4:** FAIL → реализовать → PASS: `pytest tests/test_acl_event_log_hub.py -v`.
- [ ] **Step 5: Commit** `feat(event-log): отправка раз в 2 минуты и сразу при сбое кампании`

---

### Task 8: Клиент — паспорт машины и нагрузка

**Files:**
- Create: `src/acl_event_log/machine_profile.py`
- Modify: `src/acl_event_log/__init__.py`, `src/acl_event_log/shipper.py` (`start_shipper`), `src/acl_event_log/events.py` (`stats_payload`), `src/web/publish_api.py:929`
- Test: `tests/test_acl_event_log_hub.py`

**Interfaces:**
- Produces:
  - `collect_profile() -> dict` с ключами `ram_gb, cpu_model, cores_logical, cores_physical, windows, screen_w, screen_h, scale_pct, on_battery, uptime_h, mlx_running, adspower_running, chrome_procs, disk_free_gb`; каждое поле в своём `try`, при сбое `None`; без имён пользователя, путей и серийников.
  - `load_snapshot() -> dict`: `cpu_pct` (`psutil.cpu_percent(interval=None)`), `mem_pct`, `mem_avail_mb`, `chrome_procs`, `chrome_mem_mb`.
  - `emit_machine_profile() -> None` — `emit("machine.profile", collect_profile())` один раз за процесс; вызывается из `start_shipper()` до запуска потока.
  - `stats_payload(..., load: dict | None = None)` → `out["load"] = load` если не `None`; в `publish_api.py` передать `load=load_snapshot()` в `try`.

- [ ] **Step 1: Тесты:** `test_collect_profile_keys_and_no_identity` (все ключи есть; ни одно значение не содержит `os.getlogin()` и `Path.home()`); `test_collect_profile_survives_failures` (подменить `psutil.virtual_memory` на бросающий → `ram_gb is None`, остальные заполнены); `test_profile_emitted_once` (два вызова `emit_machine_profile` → одно событие); `test_stats_payload_carries_load`; `test_stats_payload_without_load_unchanged` (ключа `load` нет).
- [ ] **Step 2–4:** FAIL → реализовать → PASS.
- [ ] **Step 5: Commit** `feat(event-log): паспорт машины и нагрузка на каждой кампании`

---

### Task 9: Развёртывание, перенос, переключение (с владельцем)

**Files:**
- Create: `deploy/acl-hub/README.md`
- Modify: `deploy/acl-hub/wrangler.toml` (`database_id`), `src/acl_event_log/shipper.py` (`DEFAULT_URL`), `scripts/acl_log_pull.py`
- Test: `tests/test_acl_log_pull.py` (существующий или новый)

**Interfaces:**
- `acl_log_pull.py`: если задан `ACL_HUB_URL` — читает `/api/events?after=` с `ACL_HUB_OWNER_KEY` и пишет в ту же локальную SQLite через `store.ingest_segment`-совместимую вставку (ключ сегмента `hub:<after>-<next>`); иначе — прежнее поведение.

- [ ] **Step 1: Тест pull:** `test_pull_from_hub_pages_until_empty` (подменённый HTTP отдаёт 2 страницы → в SQLite 3 события, второй прогон добавляет 0).
- [ ] **Step 2:** FAIL → реализовать → PASS. Commit `feat(event-log): локальный архив читает хаб`.
- [ ] **Step 3: README** — команды из папки `deploy/acl-hub` (их выполняет владелец под новым аккаунтом, `wrangler whoami` должен показать новый Account ID):

```
npx wrangler d1 create acl_hub            # id → wrangler.toml
npx wrangler d1 migrations apply acl_hub --remote
npx wrangler secret put OWNER_KEY
npx wrangler deploy
curl -i https://acl-hub.<поддомен>.workers.dev/api/machines          # ожидание 401
```

- [ ] **Step 4: Живая проверка приёма** — с машины владельца одна тестовая пачка (`ship_once` с `ACL_EVENT_LOG_URL` = хаб) → `GET /api/machines` показывает машину владельца.
- [ ] **Step 5: Перенос истории** — `python scripts/acl_hub_backfill.py --db <копия event_log.sqlite>`; сверка: число событий в хабе (`SELECT count(*) FROM events` через `wrangler d1 execute acl_hub --remote`) = число не-heartbeat событий в локальной базе; `runs` по Йотаму за 16.11 = 12 строк, 10 `ok`, 1 `failed`, 1 `not_started` (сверено с `acl_log_report.py --runs` 17.11).
- [ ] **Step 6: Переключение** — `DEFAULT_URL` = адрес хаба; commit `feat(event-log): машины пишут в acl-hub`; PR в main через главную сессию трекера; после выпуска — `GET /api/machines` показывает Yotam и Gil с новой версией. Старый Worker не удалять 2 недели (сборки без обновления продолжают писать туда; их забирает `acl_log_pull` по старому адресу и `acl_hub_backfill`).
- [ ] **Step 7:** Записать в Obsidian (дневник изменений) адрес хаба, дату переключения, сверку чисел.
