# Хаб телеметрии ACL (`acl-hub`)

Принимает пачки событий от машин (тот же контракт, что у старого `deploy/event-log`), складывает их в D1 и сразу строит таблицы `machines`, `jobs`, `runs`. Владелец читает через `/api/*`. Дизайн: `docs/superpowers/specs/2026-11-17-acl-telemetry-hub-design.md`.

## Что нужно

- корпоративный аккаунт Cloudflare и тариф Workers Paid ($5/мес): на бесплатном у Worker 10 мс CPU на запрос, разбор пачки в них не укладывается;
- API-токен этого аккаунта с правами Workers Scripts Edit и D1 Edit — в машинном `.env` владельца как `CLOUDFLARE_API_TOKEN` (не в репо);
- Node 22.5+ (для тестов) и `npx wrangler`.

## Развёртывание

Все команды из папки `deploy/acl-hub`. Сначала `npx wrangler whoami`: должен быть новый аккаунт.

```
npx wrangler d1 create acl_hub                      # напечатает database_id
cp wrangler.toml wrangler.local.toml                # в копии: database_id = <id>; копия в .gitignore
npx wrangler d1 migrations apply acl_hub --remote --config wrangler.local.toml
npx wrangler secret put OWNER_KEY --config wrangler.local.toml   # длинная случайная строка, только у владельца
npx wrangler deploy --config wrangler.local.toml    # напечатает https://acl-hub.<поддомен>.workers.dev
curl -i https://acl-hub.<поддомен>.workers.dev/api/machines     # ожидание 401
```

В машинный `.env` владельца:

```
ACL_HUB_URL=https://acl-hub.<поддомен>.workers.dev
ACL_HUB_OWNER_KEY=<тот же OWNER_KEY>
```

## Проверка и перенос истории

1. Приём: на машине владельца `ACL_EVENT_LOG_URL=<адрес хаба>` и один цикл отправки; затем `GET /api/machines` с `Authorization: Bearer <OWNER_KEY>` показывает машину.
2. Перенос: `python scripts/acl_hub_backfill.py --db <копия event_log.sqlite>`. Повтор безопасен.
3. Сверка: `npx wrangler d1 execute acl_hub --remote --command "SELECT count(*) FROM events"` = число не-heartbeat событий локальной базы; прогоны Йотама за 16.11 — 12 строк (10 `ok`, 1 `failed`, 1 `not_started`).
4. Переключение машин: `DEFAULT_URL` в `src/acl_event_log/shipper.py` = адрес хаба, выпуск. **Только вместе** с `CYCLE_S = 120`: старый Worker на KV такую частоту не выдержит. Старый Worker не удалять 2 недели.
5. Архив владельца: `python scripts/acl_log_pull.py` (при заданных `ACL_HUB_URL`/`ACL_HUB_OWNER_KEY` читает хаб).

## Контракт

| Запрос | Кто | Ответы |
|---|---|---|
| `POST /v1/batch` (gzip ndjson ≤ 1 МБ, `X-ACL-Machine`, `X-ACL-Batch`) | машина, `Bearer <ACL_LIBRARY_TOKEN>` | `200 {accepted, duplicates, rejected}`, `400`, `401`, `413`, `503` |
| `POST /v1/backfill` (gzip ndjson, `X-ACL-Batch`) | владелец, `Bearer <OWNER_KEY>` | как выше |
| `GET /api/events?after=&limit=` | владелец | ndjson, заголовок `X-Next-After` |
| `GET /api/runs?from=&to=&manager=&outcome=&account=` | владелец | `{runs: [...]}` |
| `GET /api/jobs?from=&to=&manager=` | владелец | `{jobs: [...]}` |
| `GET /api/machines` | владелец | `{machines: [...]}` |
| `POST /v1/session` `{google_access_token}` | сотрудник домена из `ALLOWED_EMAIL_DOMAINS` | `200 {key, email, role, expires_at}` (ключ на 30 дней), `400`, `401` (Google не признал токен), `403` (email не подтверждён / чужой домен / чужой OAuth-клиент / юзер отключён), `503` (Google недоступен) |
| `GET /v1/my-accounts` (`If-None-Match`) | юзер, `Bearer <ключ юзера>` | `200 {accounts: [...]}` с `ETag` (активные и снятые, у снятых `removed_at`), `304`, `401` |
| `GET /api/users` | админ | `{users: [...]}` с `active_accounts` |
| `POST /api/assignments` `{user_email, rows:[{account_name, ad_account_id, page_id, page_name, country, multilogin_instance, folder_id, profile_id}]}` | админ | `200 {ok, assigned}`, одной транзакцией; повтор снимает `removed_at`; лишние поля (в т.ч. `proxy_*`, `tracking_link`) — `400`; неизвестный юзер создаётся как `buyer` |
| `POST /api/assignments/remove` `{user_email, account_names}` | админ | `200 {ok, removed}`; ставит `removed_at`, строки не удаляются |
| `POST /api/users/role` `{email, role}` | только `OWNER_KEY` | `200`, `400`, `404`; так задаётся первый админ (`admin` / `buyer`) |

Админ = владелец (`OWNER_KEY` или cookie) либо юзер с ролью `admin`; юзер без прав получает `403`, без ключа — `401`. Ключ юзера: `<email>.<exp>.<hmac>`, HMAC-SHA256 секретом `USER_KEY_SECRET`; отключённый юзер не проходит. Токен Google хаб не хранит. Миграция `0008` (номера 0006/0007 заняты другими ветками). Секрет `USER_KEY_SECRET` ставится `wrangler secret put`; `ALLOWED_EMAIL_DOMAINS` и `GOOGLE_CLIENT_ID` — в `wrangler.local.toml`.

`heartbeat` в `events` не пишется, только обновляет `machines`. Событие с похожим на токен содержимым отклоняется. Исход прогона: `ok` / `failed` / `not_started`; отпечаток сбоя — 12 hex от нормализованного текста ошибки.

## Тесты

```
cd deploy/acl-hub && node --test
```
