---
description: Excel для директора по маркетингу (RU/EN) — сводка по странам, CM, ассеты, топ-2 связки
---

Выполни **полный пайплайн** отчёта для директора по маркетингу (как в сессии с `build_marketing_director_report.py`).

### 1. Данные

- Источник: **`cursor example.xlsx`** (путь по умолчанию — [`config/paths.py`](config/paths.py), переменная `FB_STATS_EXCEL` может переопределить).
- **Список стран не фиксирован:** берутся **все** значения колонки `Country` в этом файле за отчётный месяц (после `strip`, без пустых).
- Период: **календарный месяц максимальной даты** в колонке `Date` (все строки этого месяца).

### 2. Срез `_stats_doc_countries_lastmonth.xlsx`

Скрипт **`analysis_agent/build_marketing_director_report.py`** при каждом запуске пересобирает **`analysis_agent/_stats_doc_countries_lastmonth.xlsx`** из того же Excel и месяца (все страны среза). Отдельный ручной шаг с белым списком стран **не нужен**.

### 3. Сгенерировать книги Excel

Из корня **`fb_agents`**:

```powershell
Set-Location "C:\Users\EvgeniSedin\fb_agents"
python analysis_agent/build_marketing_director_report.py --lang both
```

- **`--lang ru`** — только `analysis_agent/Marketing_Director_Report.xlsx`
- **`--lang en`** — только `analysis_agent/Marketing_Director_Report_EN.xlsx`
- **`--lang both`** — оба файла

Если сохранение падает с **Permission denied** — попроси пользователя **закрыть** открытый `.xlsx` в Excel и повторить команду.

### 4. Опционально — полный markdown-аналитики на том же срезе

Для директора не обязательно; полезно для чата/архива:

```powershell
python src/analysis_agent/analyze_fb_stats.py --excel analysis_agent/_stats_doc_countries_lastmonth.xlsx --country all --top 2 --output analysis_agent/_full_analytics_lastmonth.md
```

### 5. После успеха — ответ пользователю

- Указать **полные пути** к `Marketing_Director_Report.xlsx` и/или `Marketing_Director_Report_EN.xlsx`.
- Кратко: что внутри — лист **Read me / Как читать**, **Summary / Сводка**, по листу на страну (**CM → By ad asset → Top two combinations**).
- Ссылка на workflow: [`analysis_agent/WORKFLOW_Country_CM_Report.md`](analysis_agent/WORKFLOW_Country_CM_Report.md) — раздел «Отчёт для директора (Excel)».

При ошибке — traceback и проверка наличия колонок `Date`, `Country`, `CM`, `Ad Asset`, `# FTDs`, `# Leads`, `$ Spend`, `$ Net`.
