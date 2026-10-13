# ACL repository adoption (Adopy Campaign Manager)

ACL uses the Master Repo layout and main/stage process. Read the verified
[cutover and activation gate](docs/repository/ADOPTION.md): main is the integration
branch, stage is permanent, and master is retained only for old source installations.
Normal admissions wait for ACL-1248's verified promotion and Tracker handoff.
Start with [AGENTS.md](AGENTS.md) and the [current system](docs/system/PROJECT.md).

# fb_agents

Анализ статистики Facebook-кампаний: отчёты по странам, кампайн-менеджерам (CM) и офферам.

## Установка (ACL — Adopy Campaign Manager)

Требуется **Python 3.11+**. Двойной клик по `install_acl.bat` (или из консоли):

```bash
install_acl.bat
```

Он ставит зависимости, создаёт машинный каталог конфигурации и проверяет
готовность. Проверку можно запускать и отдельно в любой момент:

```bash
python scripts/acl_doctor.py
```

### Код в гите, машинное — локально

Это разделение обязательно: ACL ставится на компьютер каждого байера, у всех
разные аккаунты, а рабочий репозиторий читают посторонние разработчики.
Поэтому личное **не коммитится** и живёт в `%LOCALAPPDATA%\ACL\`
(переопределяется переменной `ACL_CONFIG_DIR`):

| Файл | Где | Шаблон |
|---|---|---|
| `ad_account_map.csv` — аккаунты и профили Multilogin | `%LOCALAPPDATA%\ACL\` | `config/ad_account_map.example.csv` |
| `.env` — токен Multilogin, пути, ключи | `%LOCALAPPDATA%\ACL\` | `.env.example` |
| `oauth_client.json`, `token_accounts.json` — Google OAuth | `%LOCALAPPDATA%\ACL\` | выдаёт владелец Workspace / создаётся при входе |

Версии зависимостей закреплены через `==`, а не `>=` — см. пояснение в
`requirements.txt`. Инструменты разработчика: `requirements-dev.txt`.

### Своё окружение и обновления

Установщик создаёт `.venv` в папке проекта и ставит зависимости туда: чужие
питон-проекты байера не подтянут под ACL другую версию `pandas` или
`playwright`, а обновление ACL ничего им не сломает. Лаунчер требует `.venv` и
не откатывается молча на системный Python.

Автообновление после установки выключено. Его включает отдельно
`enable_stable_updates.bat`, и только когда checkout совпадает с защищённым
`origin/main` и точным annotated `acl-release-*` tag с успешным CI. Каждый
release получает отдельное Python-окружение по SHA; неуспешный запуск
возвращает предыдущий release и помещает broken SHA в quarantine.

Отключить на конкретной машине — удалить файл `%LOCALAPPDATA%\ACL\auto_update`.

Собственные браузеры Playwright не нужны: ACL управляет браузером Mimic из
Multilogin по CDP.

## Данные

Скрипт читает Excel-файл с колонками: **Country**, **Account Alias**, **Adset Name**, **Ad Title**, **# Leads**, **# FTDs**, **$ Spend**, **CM**, **Ad Asset**, **$ Net**.

**Путь к файлу:** сначала берётся из переменной окружения **`FB_STATS_EXCEL`**. Если она не задана — используется значение по умолчанию из `config/paths.py`. Так у каждого может быть свой файл без правок в репозитории.

Пример (PowerShell, одна сессия):
```powershell
cd fb_agents
$env:FB_STATS_EXCEL = "C:\Users\YourName\Desktop\my_data.xlsx"
python run.py
```

**Правило:** оффер (Offer) всегда берётся из колонки **Ad Asset**.

## Запуск

Из **корня проекта** (`fb_agents`):

```powershell
cd fb_agents
python run.py
```

Либо напрямую скрипт (тоже из корня проекта):

```powershell
python src/analysis_agent/analyze_fb_stats.py
```

Вывод — **Markdown**: комбо по странам (Strong/Interesting), разбивка по CM, сравнение по офферам и топ-кампании на каждый оффер **по всем странам** (или по одной при указании `--country`). Тот же текст уходит в консоль или в файл с `-o`.

### Быстрый полный отчёт в файл

Одна команда, предсказуемый путь (удобно для Cursor / дашборда):

```powershell
cd fb_agents
python run.py --country all --top 2 --output output/latest_analytics.md
```

Или обёртка (из корня репозитория):

```powershell
.\run_quick_analytics.ps1
```

### Параметры командной строки (CLI)

| Параметр | Короткий | Описание |
|----------|----------|----------|
| `--excel` | `-e` | Путь к Excel (переопределяет переменную окружения и значение по умолчанию) |
| `--country` | `-c` | Страна для отчёта по офферам: `all` — все страны (по умолчанию), либо название одной страны |
| `--top` | `-t` | Сколько топ-кампаний на оффер выводить (по умолчанию: 2) |
| `--output` | `-o` | Сохранить полный отчёт в файл вместо вывода в консоль |

Примеры:

```powershell
# Из корня fb_agents
# Отчёт по офферам по всем странам (по умолчанию), топ-2 на оффер
python run.py

# Только Ecuador для блока по офферам
python run.py --country Ecuador

# Все страны, топ-3 кампании на оффер
python run.py --top 3

# Указать свой Excel и сохранить отчёт в output/
python run.py --excel C:\data\stats.xlsx --output output/report.txt

# Справка по параметрам
python run.py --help
```

## Утренний отчёт (Morning Report)

Ежедневная сводка: сравнение CM, тренд по депозитам за N дней, топ-комбо, разбивка по таргетингу и креативам — всё по странам.

**CLI (Markdown):**

```bash
python run_morning.py [-e path/to/stats.xlsx] [--cm "Evgeni Sedin"] [--trend-days 7] [--country UAE] [--no-gsheet] [-o output/morning.md]
```

- `-e` / `--excel` — путь к Excel (по умолчанию `FB_STATS_EXCEL`).
- `--cm` — CM, чью строку подсвечивать в блоке сравнения (по умолчанию `MY_CM_NAME` из `.env` или `Evgeni Sedin`).
- `--trend-days` — окно тренда в днях (по умолчанию 7).
- `--country` — ограничить одной страной.
- `--no-gsheet` — не читать Google Sheet со списком аккаунтов.
- `-o` / `--output` — сохранить Markdown в файл.

## Web UI (интерактивный дашборд)

Тот же утренний отчёт в браузере с **drill-down**: по клику на любую строку (CM, тренд, топ-комбо, таргетинг, креативы) открываются исходные записи из Excel.

### Ярлык на рабочем столе (рекомендуется)

Один раз запусти установщик — и дашборд будет открываться двойным кликом:

```bat
install_dashboard_shortcut.bat
```

Скрипт создаёт ярлык **Morning Dashboard** на рабочем столе:

1. Двойной клик по ярлыку → открывается консоль и запускается `start_dashboard.bat`
2. Браузер открывается на http://127.0.0.1:9090/ (первый отчёт строится ~15–20 с)
3. Закрой окно консоли (или Ctrl+C), чтобы остановить сервер

> Если ярлык уже был создан раньше через Cursor — запусти `install_dashboard_shortcut.bat` ещё раз, чтобы обновить его.

### Запуск вручную

```bash
pip install -r requirements.txt   # нужны fastapi, uvicorn, jinja2

# Самый простой (двойной клик или из терминала):
start_dashboard.bat               # откроет браузер на http://127.0.0.1:9090/

# Или вручную:
python serve.py                   # порт 9090, браузер откроется сам
python serve.py --no-browser      # только терминал
python serve.py --port 8765       # другой порт, если 9090 занят
```

- Данные и настройки — те же, что у `run_morning.py` (`FB_STATS_EXCEL`, GSheet, `.env`).
- Отчёт кэшируется; кнопка **Refresh** (POST `/refresh`) пересобирает его из Excel.
- По умолчанию слушает только `127.0.0.1` (однопользовательский режим).

## Агент 5: аккаунты → Jira

Отдельный агент по строкам из **Upluad FB.xlsx** (лист Accounts: пустой **Link** и заполненный **Asset**) **составляет** задачу (Summary + Description) и по умолчанию **открывает в браузере** страницу создания задачи — ты вставляешь данные и публикуешь сам. Один аккаунт — одна задача.

**Запуск (из корня проекта):**

```powershell
python run_jira_from_accounts.py
```

Опции: `--excel путь/к/файлу.xlsx`, `--dry-run` (только список без браузера), `--api` (создание через Jira API; Excel не меняется), `--pause` (пауза после каждого аккаунта; по умолчанию открываются все вкладки сразу — например 5 аккаунтов → 5 вкладок Jira).

**Учётные данные:** создай в папке `fb_agents` файл **`.env`** (скопируй из `.env.example`), пропиши `JIRA_EMAIL` и `JIRA_API_TOKEN` — скрипт подхватит их сам. Либо задавай переменные в терминале перед запуском. Инструкция по токену — в **docs/agent5_accounts_jira.md**.  
**Важно:** если Jira возвращает 403 «IP not in allowlist» — попроси админа Jira добавить твой IP в список разрешённых.

Удобный запуск: двойной клик по **`run_jira_api.bat`** (создаёт задачи через API; нужен настроенный `.env`).

Подробное описание правил и полей Jira — в **docs/agent5_accounts_jira.md**.

---

## Агент 8: план аплоуда → Excel под FB

По **плану** (CSV: аккаунт, страна, оффер, таргетинг, креатив, эд копи и т.д.) собирает один Excel: **каждый лист = один аккаунт**, строки готовы под выгрузку в FB. Данные (Pixel, Link, тексты, интересы) подставляются из **Upluad FB.xlsx** (Accounts, Ad copy, Countries, Interests, Video/Image file name).

**Запуск (из корня проекта):**

```powershell
python run_upload_from_plan.py <plan.csv> [-o output/upload_plan.xlsx] [-e путь/к/Upluad FB.xlsx]
```

**Формат плана (CSV):** колонки `account`, `country`, `asset`, `os`, `creative`, `targeting`, `age`, `creative_number`, `date`, `copy`. Пример: **output/plan_example.csv**.

Подробности и исследование листов — **docs/agent8_excel_fb_research.md**.

---

## Связка: анализ → аплоуд (вариант 2)

Один запуск: сначала **агент 1** (анализ статистики, комбо Strong/Interesting), затем **агент 8** (сборка Excel под FB из этих комбо).

```powershell
python run_analysis_then_upload.py --account "FacebookUL574 EC Fav" [-e путь/к/stats.xlsx] [-u путь/к/Upluad FB.xlsx] [-o output/upload_from_analysis.xlsx] [-c EC] [-t 10] [--plan-csv output/plan.csv]
```

- **--account** (обязательно) — имя аккаунта для листа в аплоуд-файле.
- **-e** — Excel со статистикой (по умолчанию из `FB_STATS_EXCEL` / config).
- **-u** — Upluad FB.xlsx (по умолчанию из config).
- **-o** — куда записать итоговый upload Excel (по умолчанию `output/upload_from_analysis.xlsx`).
- **-c** — только комбо по стране (например `EC`); по умолчанию все.
- **-t** — сколько комбо брать (по умолчанию 10).
- **--plan-csv** — при необходимости сохранить промежуточный план в CSV.

Комбо (страна, оффер, таргет, креатив, возраст, лучший Ad Title) автоматически превращаются в строки плана; по ним собирается тот же аплоуд-Excel, что и из ручного плана.

**Удобный пошаговый режим:** запусти `python run_upload_interactive.py` — скрипт возьмёт файл со статистикой (по умолчанию `cursor example.xlsx`), покажет **список стран из колонки Country**, попросит выбрать страну (номером или названием), сколько комбо взять и имя аккаунта, затем выгрузит Excel в `output/upload_from_analysis.xlsx`. Страна везде берётся только из колонки **Country**.

---

## Content Machine (Telegram)

**Назначение:** отдельная ветка пайплайна — **стратегический контент-план недели** (брифы в SQLite) и далее сборка черновиков постов (агенты исследования, авторы, редактор, compliance) через оркестратор. Публикация в канал — через бот и планировщик из `run_content_machine.py bot`.

**Планировщик (стратегия)** против **пайплайна черновика:** календарный агент ([`src/content_machine/agents/calendar_agent.py`](src/content_machine/agents/calendar_agent.py)) генерирует брифы с учётом столпов бренда и **недельного операторского ввода**. Остальная цепочка ([`src/content_machine/agents/orchestrator.py`](src/content_machine/agents/orchestrator.py)) уже превращает бриф в текст поста после `process`.

**Недельный ввод:** JSON или YAML (`weekly_goals`, `required_coverage`, `forbidden_topics`, `max_offer_posts_per_day`, `max_offer_posts_per_week`, `exclude_dates`, `notes`). Пример: [`config/content_plan_week.example.json`](config/content_plan_week.example.json). Подключение:

- переменная **`CM_WEEK_PLAN_PATH`** — путь к файлу;
- или **`CM_WEEK_PLAN_JSON`** — объект в одной строке JSON.

Даты из `exclude_dates` (ISO `YYYY-MM-DD`) в план **не попадают**.

**Запуск плана (из корня `fb_agents`):**

```powershell
python run_content_machine.py plan
python run_content_machine.py plan -c "акцент на волатильности"
python run_content_machine.py plan -w config/content_plan_week.example.json
```

Флаг **`-w` / `--week-plan`** задаёт файл для этого запуска (имеет приоритет над `CM_WEEK_PLAN_*` в оркестраторе только если вы вызываете `plan` через CLI с этим флагом). Просмотр созданных брифов: `python run_content_machine.py list`.

После согласования календаря: `python run_content_machine.py process` — прогон всех статусов **brief** через остальных агентов.

**Проверки плана:** пустые темы, неизвестные значения enum из [`src/content_machine/schema.py`](src/content_machine/schema.py), два оффера `sales`/`copytrading_offer` подряд в один день, превышение дневного/недельного лимита коммерческих постов. При ошибках календарь **один раз** перезапрашивает модель для этого дня с текстом проблемы; оставшиеся нарушения пишутся в лог `WARNING`.

Подробнее по ключам см. блок Content Machine в **`.env.example`**.

---

## Структура проекта (конструкция агентов)

| Компонент | Назначение |
|-----------|------------|
| **run.py** | Запуск агента анализа (п.1–4): отчёты по странам, CM, офферы, связки (Markdown). Режимы и флаги: [`analysis_agent/ANALYSIS_AGENT_WORKFLOW.md`](analysis_agent/ANALYSIS_AGENT_WORKFLOW.md). |
| **run_quick_analytics.ps1** | Полный прогон с записью в `output/latest_analytics.md` (см. § «Быстрый полный отчёт в файл»). |
| **run_morning.py** | Утренний отчёт (CM-сравнение, тренд, топ-комбо, таргетинг, креативы) в Markdown. |
| **serve.py** | Web UI для утреннего отчёта: FastAPI + HTMX, drill-down по строкам (см. § «Web UI»). |
| **run_jira_from_accounts.py** | Запуск **агента 5**: аккаунты из Upluad FB.xlsx → задачи в Jira (Summary, Description по правилам трёх листов), Labels Limited_QA, Team Media. |
| **run_upload_from_plan.py** | Запуск **агента 8**: план (CSV) → Excel с листами по аккаунтам под выгрузку в FB. |
| **run_analysis_then_upload.py** | **Связка:** агент 1 (анализ) → комбо → агент 8 (аплоуд Excel). Один аккаунт, топ-N комбо. |
| **run_content_machine.py** | Content Machine: `plan`, `process`, `bot`, `list`, очередь ревью, аналитика публикаций; см. § Content Machine. |
| **run_jira_api.bat** | Удобный запуск агента 5 с `--api` (двойной клик; нужен `.env` с JIRA_EMAIL и JIRA_API_TOKEN). |
| **.env** / **.env.example** | Учётные данные для Jira API (`.env` в git не попадает). |
| **config/paths.py** | Пути к Excel (FB_STATS_EXCEL, UPLOAD_FB_EXCEL). |
| **src/analysis_agent/** | Агент анализа (комбо, CM, офферы). |
| **src/jira_agent/** | Агент 5: сборка текста задачи из Accounts + Main + LP's, создание через Jira API. |
| **src/upload_agent/** | Агент 8: сборка Excel из плана (Accounts, Ad copy, Countries, Interests, Video/Image file name). |
| **output/** | Отчёты, `jira_task_paste.txt`, `upload_plan.xlsx`, `plan_example.csv`. |
| **docs/** | ROADMAP, AGENTS_MASTER_PLAN, agent5_accounts_jira.md, agent8_excel_fb_research.md, КАК_НАЧАТЬ_РАБОТУ, Git. |

## Отчёты

- **Комбо по странам** — лучшие связки (страна, устройство, интерес, возраст, креатив, текст) с метриками.
- **CM по странам** — таблица по каждому кампайн-менеджеру: Deposits, Leads, Spend, Net, CPL, CPA, CR%, ROI.
- **По офферам по странам** — сравнение офферов (из Ad Asset) и рекомендация топ-N кампаний на каждый оффер (по умолчанию все страны).


## Task tracker

[English task UI](https://tracker.team7.cloud) · [Workflow and commands](docs/TASK-BOARD.md) · [Portable tracker package](tracker/README.md). Agents must set In progress before implementation; priorities are High / Medium / Low.
