# fb_agents

Анализ статистики Facebook-кампаний: отчёты по странам, кампайн-менеджерам (CM) и офферам.

## Требования

- Python 3.x
- Зависимости: `pandas`, `openpyxl`, `requests` (см. `requirements.txt`)

Установка:

```bash
pip install -r requirements.txt
```

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

Вывод в консоль: комбо по странам (Strong/Interesting), разбивка по CM по каждой стране, сравнение по офферам и топ-кампании на каждый оффер **по всем странам** (или по одной при указании `--country`).

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

## Структура проекта (конструкция агентов)

| Компонент | Назначение |
|-----------|------------|
| **run.py** | Запуск агента анализа (п.1–4): отчёты по странам, CM, офферы, связки. |
| **run_jira_from_accounts.py** | Запуск **агента 5**: аккаунты из Upluad FB.xlsx → задачи в Jira (Summary, Description по правилам трёх листов), Labels Limited_QA, Team Media. |
| **run_upload_from_plan.py** | Запуск **агента 8**: план (CSV) → Excel с листами по аккаунтам под выгрузку в FB. |
| **run_analysis_then_upload.py** | **Связка:** агент 1 (анализ) → комбо → агент 8 (аплоуд Excel). Один аккаунт, топ-N комбо. |
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
