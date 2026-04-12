# Контракт отчёта аналитики (Markdown)

## Версия структуры

- Константа в коде: `REPORT_STRUCTURE_VERSION` в [`src/analysis_agent/report_markdown.py`](../src/analysis_agent/report_markdown.py) (сейчас **`2`**).

## Входной Excel (обязательные колонки)

Те же, что проверяет [`analyze_fb_stats.main`](../src/analysis_agent/analyze_fb_stats.py):

`Country`, `Account Alias`, `Adset Name`, `Ad Title`, `# Leads`, `# FTDs`, `$ Spend`, `CM`, `Ad Asset`, `$ Net`.

Переопределение пути: `FB_STATS_EXCEL` или флаг `-e` / `--excel`.

## Режимы

| Режим | Как включить | Выход |
|--------|----------------|--------|
| Короткий | `python run.py analyze --short` (`-s`) | Одна секция «top combos by country» (таблица). |
| Полный | без `--short` | Структура **v2** ниже. |

## Полный отчёт (structure v2)

1. **Заголовок** — `# FB campaigns analytics report`
2. **Преамбула фильтров** (если были `--asset`, `--accounts`, `--focus-countries`)
3. **## How to read this report** — порядок чтения, версия структуры, путь к файлу, счётчики строк / стран / комбо
4. Для **каждой страны** (или одной, если `--country` не `all`):
   - `## Country: <name>`
   - **Account breakdown** — агрегат по `Account Alias`
   - **CM breakdown** — по `CM`
   - **Combos (Strong / Interesting)** — до 3 связок на страну; в строках комбо есть **TopAccount** / **TopCM** (если посчитались в `aggregate_combos`)
   - **Offer comparison** + **Recommendation** (топ кампаний на оффер, `--top` задаёт N)

## Короткий отчёт

Секция `## Short report: top combos by country` — компактная таблица без разбивки по аккаунтам/CM/офферам.

## Связь с upload

Генерация **upload .xlsx** не входит в этот контракт; для неё используйте `python run.py upload-bridge` или `run_upload_interactive.py` (см. [ANALYSIS_AGENT_WORKFLOW.md](ANALYSIS_AGENT_WORKFLOW.md)).
