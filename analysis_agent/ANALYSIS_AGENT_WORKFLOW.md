# Мастер-workflow: агент анализа FB-статистики

## Быстрый старт — утренний отчёт (новое)

```powershell
# Запуск из корня fb_agents:
python run_morning.py

# или через unified CLI:
python run.py morning

# с явным путём к Excel:
python run_morning.py -e "C:\Users\EvgeniSedin\Desktop\cursor example.xlsx"

# только одна страна:
python run_morning.py --country Mexico

# без Google Sheet (если не настроен):
python run_morning.py --no-gsheet
```

Результат: `analysis_agent/morning_report.md` — 11 секций по каждой стране:
1. Evgeni vs команда (CM comparison)
2. Тренд последних 7 дней
3. Рабочие комбинации (device + targeting + creative)
4. Таргетинг breakdown
5. Креативы breakdown
6. Контекст для AI (копируй в чат Cursor)
7. Гипотезы (копируй в чат Cursor)
8–9. Статусы аккаунтов из Google Sheet
10. Что закрыть (оптимизация)
11. Что открыть (2 кампании на аккаунт)

**Настройка Google Sheet** (один раз): добавь в `.env`:
```
MY_CM_NAME=Evgeni Sedin
GSHEET_ACCOUNTS_ID=<ID из URL твоего Google Sheet>
GSHEET_ACCOUNTS_SHEET=auto_last
```
Поделись таблицей с сервисным аккаунтом из `config/google_credentials.json` (роль Viewer).

---

Единая точка входа: [`run.py`](../run.py) (подкоманды **analyze** / **upload-bridge** / **upload-plan**) и прямой вызов [`analyze_fb_stats.py`](../src/analysis_agent/analyze_fb_stats.py). Детальная инструкция по персональным CM-отчётам — [WORKFLOW_Country_CM_Report.md](WORKFLOW_Country_CM_Report.md).

**Контракт Markdown-отчёта (v2):** [REPORT_CONTRACT.md](REPORT_CONTRACT.md).

---

## Карта скриптов и «агентов» (оркестр)

| Компонент | Файл | Роль |
|-----------|------|------|
| **Agent 1 — аналитика** | [`src/analysis_agent/analyze_fb_stats.py`](../src/analysis_agent/analyze_fb_stats.py), `python run.py analyze …` | Excel → метрики → комбо Strong/Interesting → Markdown (консоль или `-o`). **Только отчёт**, без upload. |
| **Agents 1+8 — мост** | [`run_analysis_then_upload.py`](../run_analysis_then_upload.py), `python run.py upload-bridge …` | Тот же анализ комбо + сразу сборка **upload .xlsx** по шаблону UploadFB. |
| **Интерактивный мост** | [`run_upload_interactive.py`](../run_upload_interactive.py) | Диалог в терминале: путь к отчёту → краткий вывод → страна → N комбо → имя аккаунта → вызов того же моста. |
| **Agent 8 — только план** | [`run_upload_from_plan.py`](../run_upload_from_plan.py), `python run.py upload-plan PLAN.csv …` | Готовый **plan CSV** (строки account/country/asset/…) → upload .xlsx; повторного анализа stats нет. |
| **Knowledge Base** | [`knowledge_base/`](../knowledge_base/), [`src/knowledge_agent/`](../src/knowledge_agent/) | Справочник таргетов/офферов/креативов для контекста в отчёте (`--with-context`). **Отдельного агента «придумать новый креатив» пока нет** — см. отложенную фазу в [`docs/superpowers/specs/2026-04-10-fb-agents-phases-2-4-deferred.md`](../docs/superpowers/specs/2026-04-10-fb-agents-phases-2-4-deferred.md). |

Пути по умолчанию: [`config/paths.py`](../config/paths.py) (`FB_STATS_EXCEL`, `UPLOAD_FB_EXCEL`).

### Утренняя рутина в 5 шагов

1. Обновить большой отчёт (Excel) и при необходимости Google Sheet с аккаунтами.
2. **Только аналитика:** `python run.py analyze --country all --top 2 -o analysis_agent\_full_analytics_run.md` (или `--short` для быстрого обзора).
3. Прочитать файл: сначала блок **How to read**, затем для каждой страны — **Account → CM → Combos → Offers**.
4. Если нужен файл на заливку в FB: `python run.py upload-bridge -a "Имя листа аккаунта" -c "Страна" --top 2` (или интерактивно `run_upload_interactive.py`).
5. Если план уже правили руками в CSV: `python run.py upload-plan path\to\plan.csv -o output\upload_plan.xlsx`.

---

## Назначение

Скрипт читает Excel со статистикой кампаний, считает метрики (CPL, CPA, CR%, ROI и т.д.), парсит `Adset Name` / `Ad Title`, строит связки (комбо) с категориями Strong / Interesting, собирает **единый Markdown-отчёт** (модули `report_model.py` / `report_markdown.py`) и выводит его в консоль или в файл (`-o`).

**Полный отчёт (без `--short`):** структура **v2** — для каждой страны: **Account breakdown** → **CM breakdown** → **Combos** → **Offer comparison** и **Recommendation** (топ-N на оффер; `--country` ограничивает список стран в отчёте).

**Короткий режим (`--short`):** только таблица топ-комбо по странам.

Запуск из корня `fb_agents`:

- `python run.py analyze …` — рекомендуемый вход;
- `python run.py …` без подкоманды — то же, что **analyze** (совместимость со старыми командами);
- напрямую: `python src/analysis_agent/analyze_fb_stats.py …`.

---

## Входные данные

### Обязательные колонки Excel

`Country`, `Account Alias`, `Adset Name`, `Ad Title`, `# Leads`, `# FTDs`, `$ Spend`, `CM`, `Ad Asset`, `$ Net`.

При отсутствии любой из них скрипт завершится с `ValueError` и списком недостающих имён.

### Путь к файлу

- По умолчанию: см. [`config/paths.py`](../config/paths.py) (`EXCEL_PATH_DEFAULT`, функция `get_excel_path()`).
- Переопределение: переменная окружения **`FB_STATS_EXCEL`** (полный путь к `.xlsx`) или аргумент **`--excel` / `-e`**.

---

## Сценарии (команда → результат)

| Задача | Команда (из корня `fb_agents`) | Что на выходе |
|--------|--------------------------------|---------------|
| Полный отчёт по **всем** странам (база для CM + офферы + рекомендации) | `python run.py analyze --country all --top 2 -o analysis_agent/_full_analytics_run.md` | Один файл: обзор чтения, затем для **каждой** страны блок Account → CM → Combos → Offer/Recommendation. Эквивалент slash-команды **`/jenya-agent-start`**. |
| То же (совместимость: без слова `analyze`) | `python run.py --country all --top 2 -o analysis_agent/_full_analytics_run.md` | То же. |
| Только **upload** из stats + шаблон | `python run.py upload-bridge -a "Facebook…" -c Chile --top 2` | `output/upload_from_analysis.xlsx` (см. `-h`). |
| Только **Agent 8** из CSV | `python run.py upload-plan plan.csv -o output/upload_plan.xlsx` | Готовый upload-файл. |
| Короткий отчёт: только топ-N комбо по странам (+ проблемные с CM в таблице) | `python run.py --short --top 3` | Без полного CM/offers цикла; удобно для быстрого обзора. |
| Контекст таргетов из Knowledge Base под строками комбо | Добавить **`--with-context`** | Тексты из [`config/knowledge_base.json`](../config/knowledge_base.json) и при необходимости markdown в [`knowledge_base/`](../knowledge_base/). |
| Анализ только строк с заданным **Ad Asset** | **`--asset Amazon`** (регистр не важен) | Фильтр по колонке `Ad Asset`; дальше те же этапы. |
| Офферный блок **только для одной страны** | `--country "Mexico"` (имя как в колонке `Country`) | **`Offer comparison` / `Recommendation` только для этой страны.** Блоки **`CM breakdown` по-прежнему печатаются для всех стран** из Excel (если не включён `--short`). |

`--top N` — сколько связок рекомендовать **на каждый оффер** в секции Recommendation (по умолчанию 2).

---

## Типовые артефакты

| Файл | Как получить |
|------|----------------|
| `analysis_agent/_full_analytics_run.md` | Полный прогон с `-o` (см. таблицу выше). |
| Произвольный путь | `-o "C:\path\report.txt"` |
| Excel для директора по маркетингу (RU/EN) | Отдельный скрипт `analysis_agent/build_marketing_director_report.py` и workflow в [WORKFLOW_Country_CM_Report.md](WORKFLOW_Country_CM_Report.md) (§3A). |

---

## Связанные материалы

| Документ / команда | Назначение |
|--------------------|------------|
| [WORKFLOW_Country_CM_Report.md](WORKFLOW_Country_CM_Report.md) | Персональный отчёт по стране/CM, шаблон §3, отчёт для директора. |
| [`.cursor/commands/jenya-agent-start.md`](../.cursor/commands/jenya-agent-start.md) | Инструкция для AI: полный прогон и чтение `_full_analytics_run.md`. |
| Skill `jenya-agent-start` | То же для Cursor Skills. |
| [docs/project-architecture.md](../docs/project-architecture.md) | Агент 1 в общей схеме проекта. |

---

## Ошибки и проверки

- **`FileNotFoundError`** — нет файла по пути из `paths` / `FB_STATS_EXCEL` / `--excel`.
- **`Missing expected columns`** — привести выгрузку к списку колонок выше.
- **`No qualifying combos for … to recommend`** — для страны нет строк, попавших в агрегат комбо (часто мало данных или фильтр `--asset` сузил выборку).
- При **`--asset`** и пустом результате скрипт выведет подсказку со списком доступных значений `Ad Asset`.

---

## Быстрый старт (PowerShell)

```powershell
Set-Location "C:\Users\EvgeniSedin\fb_agents"
python run.py analyze --country all --top 2 --output analysis_agent\_full_analytics_run.md
```

С контекстом KB и явным Excel:

```powershell
python run.py analyze -e "C:\Users\EvgeniSedin\Desktop\cursor example.xlsx" --country all --top 2 --with-context -o analysis_agent\_full_analytics_run.md
```
