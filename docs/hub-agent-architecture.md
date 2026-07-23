---
kind: spec
name: hub-agent-architecture
agent: architect
task: "[[global-rollout-rpa-agent]]"
created: 2026-07-22
status: draft
version: 1
tags:
  - architecture
  - spec
  - rpa
  - api
  - data-model
  - security
related:
  - global-rollout-overview.md
  - agent-update-strategy.md
---

# Hub + Local Agent — техническая спека

Детальный технический дизайн для разработчиков. Читать после
`global-rollout-overview.md` (там бизнес-контекст и то, чем это
НЕ является — не путать с `service-architecture.md`).

## 1. Цель и границы

Дать любому сотруднику компании запускать создание кампании (Agent 10,
`run_recorded_campaign_flow` в `src/fb_ads_rpa/meta_dom.py`) со своего
компьютера, через уже открытый на его машине Multilogin-профиль, при
этом дашборд и все данные (аккаунты, результаты, история запусков) —
общие, на одном hub'е.

**В границах спеки:** data model очереди задач, API контракт hub↔agent,
security-модель, жизненный цикл задачи, наблюдаемость.

**Вне границ:** сам RPA-движок (`run_recorded_campaign_flow`,
`ads_manager_flow.py`, `RpaPublisher`) — переиспользуется как есть,
это не переписывается. Агент — это существующий код
`scripts/run_rpa_publish.py`, обёрнутый в цикл опроса очереди вместо
CLI-аргументов.

## 2. Архитектура на уровне компонентов

```
┌────────────────────────────────────────────────────────────┐
│ HUB (домен, общий для всех)                                  │
│                                                              │
│  ┌────────────┐  ┌────────────┐  ┌─────────────────────┐    │
│  │ Web Dashboard│  │ Job Queue  │  │ Shared DB           │    │
│  │ (FastAPI+   │  │ API        │  │ (см. §3)            │    │
│  │  Jinja2 —   │  │ /api/v1/   │  │ users, accounts,    │    │
│  │  переиспо-  │  │ jobs/*     │  │ jobs, artifacts,     │    │
│  │  льзует     │  │ /api/v1/   │  │ campaign_stats/combo │    │
│  │  src/web/)  │  │ agent/*    │  │ (из service-arch.)   │    │
│  └────────────┘  └────────────┘  └─────────────────────┘    │
└──────────────────────────┬───────────────────────────────────┘
                           │ HTTPS, агент — инициатор (poll)
        ┌──────────────────┼──────────────────┐
        ▼                                     ▼
┌───────────────────┐               ┌───────────────────┐
│ Local Agent (юзер А)│               │ Local Agent (юзер Б)│
│ ─ poll loop         │               │ ─ poll loop         │
│ ─ Multilogin        │               │ ─ Multilogin        │
│ ─ Playwright        │               │ ─ Playwright        │
│   (run_recorded_    │               │   (run_recorded_    │
│    campaign_flow)   │               │    campaign_flow)   │
│ ─ screenshot upload │               │ ─ screenshot upload │
└───────────────────┘               └───────────────────┘
```

## 3. Data model (общая БД)

Новые таблицы, специфичные для этой инициативы. Существующие таблицы
из `service-architecture.md` (`campaign_stats`, `combo`, `kb_*` и т.д.)
могут сосуществовать в той же БД, если аналитическая часть тоже туда
переезжает — это отдельное решение, не блокирует эту спеку.

```python
class User(Base):
    """Сотрудник компании с доступом к hub."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # cm | admin
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentToken(Base):
    """Токен, которым локальный агент аутентифицируется на hub.

    Один активный токен на пользователя. Не путать с auth дашборда
    (SSO/логин) -- это отдельный секрет, который живёт в локальном
    конфиге агента (%APPDATA%\\fb_agents\\local.env), никогда не в git.
    """
    __tablename__ = "agent_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(128))  # хранить только хэш
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class AdAccountAssignment(Base):
    """Кто на данный момент отвечает за какой рекламный аккаунт.

    Заменяет плоский config/ad_account_map.csv -- та же информация
    (multilogin_instance, folder_id, account_label, country), плюс
    привязка к конкретному user_id, которой в CSV сейчас нет вообще.
    """
    __tablename__ = "ad_account_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    account_label: Mapped[str] = mapped_column(String(200))  # напр. "FacebookUL921 CR Ait"
    ad_account_id: Mapped[str] = mapped_column(String(50))
    country: Mapped[str] = mapped_column(String(10))
    multilogin_instance: Mapped[str] = mapped_column(String(10))
    multilogin_folder_id: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(default=True)


class Job(Base):
    """Одна задача 'опубликовать кампанию' в очереди."""
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assigned_to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    ad_account_assignment_id: Mapped[int] = mapped_column(
        ForeignKey("ad_account_assignments.id")
    )
    campaign_payload: Mapped[dict] = mapped_column(JSON)  # slots-json эквивалент
    status: Mapped[str] = mapped_column(String(20), default="queued")
    # queued | claimed | running | succeeded | failed | cancelled | expired
    result: Mapped[dict | None] = mapped_column(JSON)  # RpaPublishResult.to_job_dict()
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class JobArtifact(Base):
    """Скриншот/лог, загруженный агентом по ходу выполнения задачи."""
    __tablename__ = "job_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    label: Mapped[str] = mapped_column(String(200))  # напр. "recorded_success_website_selected"
    file_path: Mapped[str] = mapped_column(String(500))  # путь в общем хранилище (не на машине юзера)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
```

Примечание: `campaign_payload` и `result` — те же структуры, что уже
используются сегодня (`slots-json` формат из `scripts/run_rpa_publish.py`
и `RpaPublishResult.to_job_dict()` из `src/fb_ads_rpa/draft.py`).
Схема не переизобретает формат данных, только оборачивает его в
очередь с привязкой к пользователю.

## 4. API контракт

### 4.1 Со стороны дашборда (создание задачи пользователем)

```
POST /api/v1/jobs
Body: {
  "ad_account_assignment_id": 12,
  "campaign_payload": { ...slots-json как сегодня... }
}
→ 201 { "job_id": "uuid", "status": "queued" }

GET /api/v1/jobs/{job_id}
→ { "job_id", "status", "result", "error", "artifacts": [...] }

GET /api/v1/jobs?assigned_to=me&status=queued
→ список задач (для отображения "в очереди у меня")
```

Сервер при `POST /jobs` проверяет: `ad_account_assignment.user_id`
должен совпадать с текущим авторизованным пользователем ИЛИ вызывающий
имеет роль `admin` — рядовой сотрудник не может поставить задачу на
чужой аккаунт.

### 4.2 Со стороны локального агента (выполнение задачи)

Агент **инициирует** все запросы (poll), hub никогда не стучится в
агента напрямую — не нужно открывать входящие порты на машине
сотрудника.

```
GET /api/v1/agent/jobs/next
Headers: Authorization: Bearer <agent_token>
→ 200 { "job_id", "campaign_payload", "ad_account": {...} }   (задача есть)
→ 204                                                          (задач нет)
```

Сервер находит первую `queued`-задачу, где
`ad_account_assignment.user_id == текущий_агент.user_id`
(из токена), помечает её `claimed`, `claimed_at = now()`, отдаёт агенту.
Если агент не прислал статус в течение таймаута (см. §6) — задача
возвращается в `queued` автоматически (не виснет вечно).

```
POST /api/v1/agent/jobs/{job_id}/status
Body: { "status": "running" | "succeeded" | "failed", "result": {...}, "error": "..." }
→ 200 OK

POST /api/v1/agent/jobs/{job_id}/artifacts
Body: multipart/form-data { "label": "...", "file": <screenshot.png> }
→ 200 OK
```

### 4.3 Опрос vs push

Рекомендуется простой long-poll (агент ждёт до N секунд ответа от
`GET /jobs/next`, сервер держит соединение открытым, если задача
появляется — отдаёт сразу) вместо WebSocket — меньше движущихся
частей, устойчивее к разрывам сети на клиентских машинках.
Интервал опроса по умолчанию: 5–10 секунд между попытками.

## 5. Security-модель

- **Auth агента** — Bearer-токен на пользователя (`AgentToken`),
  хранится в локальном конфиге агента, никогда в git (см.
  `agent-update-strategy.md`, раздел про личный конфиг вне репозитория).
  Токен можно отозвать точечно (`revoked_at`) без переиздания токенов
  всем остальным.
- **Скоуп токена** — токен привязан к `user_id`, а не к конкретному
  аккаунту; какие именно аккаунты доступны — определяется таблицей
  `AdAccountAssignment`, не токеном. Смена назначения аккаунта не
  требует перевыпуска токена.
- **Секреты LLM (Anthropic/OpenAI/Gemini) остаются только на hub.**
  Агенту они не нужны — вся работа агента это Multilogin + Playwright,
  без обращения к LLM API. Если в будущем понадобится LLM-driven шаг
  внутри агента (как в `llm_agent.py` сегодня) — прокидывать через
  hub (агент просит hub "проанализируй этот скриншот", а не хранит
  свой ключ), чтобы ключ не размножался на N машин.
- **Auth дашборда** (кто логинится смотреть/ставить задачи) — отдельный
  вопрос от auth агента, см. открытый вопрос в
  `global-rollout-overview.md` (SSO vs своя система).

## 6. Жизненный цикл задачи

```
queued ──(agent claims)──► claimed ──(agent starts)──► running
                                                          │
                            ┌─────────────────────────────┤
                            ▼                             ▼
                       succeeded                       failed
```

- `queued → claimed`: происходит на `GET /agent/jobs/next`.
- `claimed → running`: агент шлёт `POST .../status {"status":"running"}`
  сразу после старта (до того как открывать браузер) — если агент
  падает между claim и этим шагом, hub видит "claimed, но давно не
  running" и может вернуть задачу в очередь.
- Таймаут: если `claimed_at` старше N минут (рекомендуется 10-15,
  учитывая, что живые прогоны сегодня иногда упирались в "Loading..."
  на десятки секунд) без перехода в `running` — задача автоматически
  возвращается в `queued` (job может быть подхвачена повторно тем же
  или другим доступным агентом, если у обоих есть доступ к аккаунту).
- `running → succeeded/failed`: финальный статус, `result`/`error`
  заполняются из того же `RpaPublishResult`, что сегодня возвращает
  `ads_manager_flow.py::publish_campaign`.
- Ручная отмена (`cancelled`) — пользователь может отменить свою же
  `queued`-задачу из дашборда; `claimed`/`running` отменить нельзя
  (RPA уже пошла руками по реальному Ads Manager, останавливать
  на полпути опаснее, чем дать доработать до драфта).

## 7. Наблюдаемость

- Каждый `JobArtifact` (скриншот) заливается на hub сразу по ходу
  выполнения — та же логика `_shot()` из `run_recorded_campaign_flow`,
  только вместо записи в локальный `output/rpa_screenshots/` идёт
  `POST /agent/jobs/{id}/artifacts`.
- Дашборд показывает live-статус задачи (poll `GET /jobs/{id}` раз в
  несколько секунд, как уже реализовано для `/publish-jobs/{id}` в
  текущем `src/web/app.py`).
- Метрика для мониторинга: доля `failed`/`expired` задач за
  скользящее окно, по аккаунту. Резкий рост — сигнал, что конкретный
  аккаунт "устал" от частых RPA-попыток (см. операционное наблюдение в
  `global-rollout-overview.md`) и стоит сделать паузу именно по нему,
  не по всей системе.

## 8. Что переиспользуется как есть (не переписывать)

| Существующий код | Роль в новой архитектуре |
|---|---|
| `src/fb_ads_rpa/meta_dom.py` (`run_recorded_campaign_flow` и хелперы) | RPA-движок внутри local agent, без изменений |
| `src/fb_ads_rpa/ads_manager_flow.py` (`publish_campaign`) | Вызывается агентом так же, как сегодня из `RpaPublisher` |
| `src/fb_ads_rpa/draft.py` (`RpaPublishResult`, `CampaignDraft`) | Формат данных для `campaign_payload`/`result` в очереди — без изменений |
| `scripts/run_rpa_publish.py` | Логика "как собрать drafts и вызвать publish_campaign" — оборачивается в poll-цикл, а не CLI-аргументы |
| `src/web/app.py` (`/publish-jobs*`) | Прототип job-модели уже существует локально (`start_job`, `get_job`, polling) — расширяется до общей БД + привязки к агентам, не переписывается с нуля |

## 9. Не входит в эту спеку (открытые вопросы/будущее)

- Хостинг hub'а и auth дашборда — см. `global-rollout-overview.md`.
- Планировщик кампаний "на определённое время, пока сотрудник не за
  компьютером" — сегодняшняя модель требует, чтобы агент был запущен
  (машина включена, Multilogin открыт); отложенный запуск без
  присутствия человека — отдельная задача, не в этой фазе.
- Автоматический выбор "какой агент взять, если аккаунт доступен
  нескольким людям" — в MVP один аккаунт = один назначенный
  сотрудник, без балансировки нагрузки между несколькими агентами.

#architecture #spec #rpa #api #data-model #security #job-queue
