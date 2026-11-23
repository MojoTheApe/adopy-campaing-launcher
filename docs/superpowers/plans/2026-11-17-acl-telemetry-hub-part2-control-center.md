# ACL Telemetry Hub, часть 2: Control Center — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Страница владельца на том же Worker `acl-hub` (`https://acl-hub.acl-hub.workers.dev/`): живая лента, байеры за день, разбор одного прогона, сбои по отпечаткам, машины — по данным D1 части 1.

**Architecture:** Worker отдаёт одну HTML-страницу (строка в `src/ui.js`, без сборки, ванильный JS) и JSON-маршруты отчётов (`src/report.js`, чистые SQL-запросы к таблицам части 1). Вход — ключ владельца один раз на странице `/login` → подписанная cookie на 30 дней; API принимает cookie или прежний `Bearer OWNER_KEY`.

**Tech Stack:** Cloudflare Workers + D1 (схема части 1, без миграций), Node 24 `node:test` + поддельная D1, ванильный HTML/CSS/JS.

**Spec:** `docs/superpowers/specs/2026-11-17-acl-telemetry-hub-design.md` (§4.5)

## Global Constraints

- База — ветка `work/acl-2745-telemetry-hub` (часть 1); схема D1 не меняется.
- Весь текст страницы и API — по-английски; комментарии кода — по-русски.
- Без внешних скриптов и шрифтов: страница целиком из Worker (данные байеров не уходят на CDN).
- Каждый маршрут `/api/*` и `/` требует владельца: cookie `acl_hub_s` или `Bearer OWNER_KEY`; иначе `/` → 302 на `/login`, `/api/*` → 401.
- Cookie: `acl_hub_s=<exp>.<hex HMAC-SHA256(OWNER_KEY, "acl-hub-session:" + exp)>`, `HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=2592000`. Смена `OWNER_KEY` гасит все сессии.
- День считается в поясе владельца: параметр `tz` (минуты к UTC), по умолчанию `120` (Израиль, зима).
- Страница обновляет ленту раз в 15 с, остальные вкладки — по кнопке/смене дня. Никаких запросов к Meta.
- Тесты: `cd deploy/acl-hub && npm test`.

## Review Focus

1. **Подделка или просрочка cookie** — ожидание 302/401, не данные. Тест Task 1.
2. **Пустой день / новый байер без прогонов** — таблица с нулями, не ошибка. Тест Task 2 `summary empty day`.
3. **Прогон без `publish.stats` или без `job.created`** (старые сборки, обрыв) — карточка прогона открывается, пустые поля «—». Тест Task 2 `run without stats`.
4. **Граница суток в поясе владельца** — прогон в 23:30 по Израилю попадает в свой день, а не в следующий UTC. Тест Task 2 `summary respects tz`.
5. **HTML-инъекция из данных** (имя кампании/ошибка с `<script>`) — на странице текст, не разметка. Тест Task 3 `ui escapes data`.

---

### Task 1: Вход владельца по cookie

**Files:** Create `deploy/acl-hub/src/session.js`, `deploy/acl-hub/test/session.test.js`; Modify `src/auth.js` (`isOwner`), `src/index.js`.

**Interfaces — Produces:**
- `makeSession(env, nowSec) -> Promise<string>` (значение cookie), `checkSession(env, value, nowSec) -> Promise<boolean>`.
- `isOwner(request, env)` — true при верном Bearer **или** верной cookie.
- `GET /login` → HTML-форма (поле ключа); `POST /login` (form `key`) → верный ключ: 303 на `/` + `Set-Cookie`; неверный: 401 + форма с «Wrong key»; `POST /logout` → cookie с `Max-Age=0`, 303 на `/login`.

- [ ] Тесты: `session round trip`; `tampered session rejected`; `expired session rejected`; `session dies when OWNER_KEY changes`; `login sets secure cookie` (флаги из Global Constraints); `wrong key is 401`; `api accepts cookie`; `root without session redirects to login`.
- [ ] FAIL → реализовать (HMAC через `crypto.subtle`, сравнение через `sameSecret`) → PASS → commit `feat(acl-hub): вход владельца по cookie`.

### Task 2: API отчётов

**Files:** Create `deploy/acl-hub/src/report.js`, `deploy/acl-hub/test/report.test.js`; Modify `src/api.js` (маршруты).

**Interfaces — Produces** (все за владельцем):
- `GET /api/summary?day=YYYY-MM-DD&tz=120` → `{day, tz, buyers: [{manager, machines: [str], first_at, last_at, active_h, jobs, campaigns, ok, failed, not_started, median_s, version}]}`. Окно: `[day 00:00 − tz, +24 ч)` в UTC. `campaigns/ok/failed/not_started/median_s` — из `runs.finished_at` в окне; `jobs` — `jobs.created_at` в окне; `first_at/last_at` — min/max `events.ts` машин этого менеджера в окне (кроме heartbeat — их нет в events); `active_h = round((last−first)/3600, 1)`; `version` — `machines.flow_version`. Сортировка: по `campaigns` убыв.
- `GET /api/live?limit=100` → `{events: [{ts, manager, machine, type, account, campaign, level, text, run_id}]}` — последние события, кроме `raw.*`; `level`: `error` для `publish.result` с исходом failed, `warn` для not_started и `plan.checked` с `blocking_rules`, иначе `info`; `text` — `describe(ev)` (чистая функция, английский: «Published PAUSED», «Failed: <error ≤140>», «Plan check: blocked by pixel_ok», «Job finished: 2 campaigns, 0 ok», …); `run_id` для `publish.*`.
- `GET /api/run?id=<run_id>` → `{run, job, steps: [{ts, step, ok, note}], fingerprint: {n, machines, managers, first_at, last_at} | null}`; `steps` — `raw.run` того же `job_id` с `ts` от `publish.spec` этой кампании (или начала задания) до `runs.finished_at` включительно, `note` = `payload.diag.step || payload.diag.error || ""` ≤ 200 символов. Нет прогона → 404.
- `GET /api/failures?days=7` → `{failures: [{fingerprint, n, machines, managers, versions, first_at, last_at, stop_kind, sample_error, last_run_id}]}` по `runs.outcome='failed'`, сортировка по `n` убыв.
- `GET /api/machines` — уже есть (часть 1).

- [ ] Тесты (фикстура Йотама + 2 синтетических прогона другого менеджера): `summary per buyer` (Йотам 16.11: campaigns 2, failed 1, not_started 1); `summary empty day`; `summary respects tz` (при tz=120 прогон 21:30Z 16.11 = 23:30 по Израилю — в дне 16.11; прогон 22:30Z — в дне 17.11); `live levels and text` (checkpoint → error, Not started → warn); `run detail with steps and fingerprint`; `run without stats`; `run 404`; `failures grouped by fingerprint`.
- [ ] FAIL → реализовать → PASS → commit `feat(acl-hub): отчёты для Control Center`.

### Task 3: Страница

**Files:** Create `deploy/acl-hub/src/ui.js` (`export const PAGE_HTML`, `export const LOGIN_HTML(error)`), `deploy/acl-hub/test/ui.test.js`; Modify `src/index.js` (`GET /` → PAGE_HTML за владельцем).

Вид: светлая тема, системный шрифт, шапка «ACL Control Center» + вкладки **Live · Buyers · Failures · Machines**, выбор дня (по умолчанию сегодня в поясе браузера, `tz` = −`getTimezoneOffset()`), кнопка **Log out**. Данные вставляются только через `textContent`/`escapeHtml`.
- **Live:** список событий (время, байер, аккаунт, кампания, текст), красная/жёлтая метка уровня, обновление раз в 15 с; клик по событию с `run_id` → карточка прогона.
- **Buyers:** таблица `/api/summary` (байер, часы, задания, кампании, ok/failed/not started, медиана, версия); клик по строке → Live с фильтром по байеру (клиентский).
- **Run** (модальное окно): исход, ошибка простым текстом, шаги по времени (провальные красным), «same failure: N times on M machines».
- **Failures:** таблица `/api/failures?days=7`, клик → последний прогон.
- **Machines:** версия, последний сигнал (красным, если > 30 мин назад и машина работала сегодня), ящик, паспорт (если есть).

- [ ] Тесты: `root serves page for owner`; `page has no external resources` (нет `http://`/`https://` кроме собственного origin в `src`/`href`); `ui escapes data` (в PAGE_HTML все вставки данных идут через `textContent` или `esc()`: проверка, что нет `innerHTML = ` с конкатенацией данных — статический grep по шаблону `innerHTML\s*=\s*[^'"\`]` отсутствует); `login page has form`.
- [ ] Проверка глазами: `npx wrangler dev --remote` или выкладка, вход ключом, все вкладки на данных переноса; скриншоты.
- [ ] commit `feat(acl-hub): страница Control Center`.

### Task 4: Выкладка

- [ ] Ревью ветки свежим ревьюером (Opus), исправления с тестами.
- [ ] `npx wrangler deploy` (ключ из машинного .env владельца, без вывода), проверка входа и вкладок вживую, скриншоты владельцу.
- [ ] Запись в Obsidian; передача ветки главной сессии трекера (в выпуск дешборда не входит: это только Worker).
