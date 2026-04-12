# Workflow: персональный CM-отчёт по каждой стране

**См. также:** мастер-workflow всех режимов агента анализа (CLI, `--country` / `--short` / `--with-context` / `--asset`, артефакты) — [ANALYSIS_AGENT_WORKFLOW.md](ANALYSIS_AGENT_WORKFLOW.md).

Цель: для **каждой страны** получить блок как в примере *Guatemala — Evgeni Sedin*: таблица CM → сильные стороны → слабые стороны → 2 кампании к открытию.

## Запуск из Cursor

- **`/jenya-agent-start`** (Jenya agent start) — полный прогон аналитики по **всем** странам и подсказка, куда смотреть дальше: [`.cursor/commands/jenya-agent-start.md`](../.cursor/commands/jenya-agent-start.md).
- **`/marketing-director-report`** — сборка **Excel для директора по маркетингу** (RU и/или EN): сводка по 10 странам, таблицы **CM** и **Ad Asset**, блок **двух лучших связок** на лист. Команда: [`.cursor/commands/marketing-director-report.md`](../.cursor/commands/marketing-director-report.md).

---

## 0. Предпосылки

- Excel с колонками: `Country`, `CM`, `# Leads`, `# FTDs`, `$ Spend`, `$ Net`, `Adset Name`, `Ad Title`, `Ad Asset`, … (как в `cursor example.xlsx`).
- Репозиторий: `fb_agents`, скрипт: `src/analysis_agent/analyze_fb_stats.py`.

---

## 1. Один прогон — база для всех стран

Сгенерируй **полный** отчёт один раз (в нём уже есть разбивка по **всем** странам из Excel):

```powershell
cd C:\Users\EvgeniSedin\fb_agents

python src/analysis_agent/analyze_fb_stats.py `
  --excel "C:\Users\EvgeniSedin\Desktop\cursor example.xlsx" `
  --top 2 `
  --output analysis_agent\_full_analytics_run.md
```

- `--top 2` — в рекомендациях по офферам до **2** кампаний на оффер (можно `--top 3`).
- При необходимости: `--with-context` (контекст таргетов из KB).

**Важно:** `--country X` в скрипте **не отключает** печать `CM breakdown` по остальным странам — он влияет на блок сравнения офферов. Для «все страны» удобнее **не** указывать `--country` (или `all`).

---

## 2. Список стран

В сгенерированном файле найди все заголовки вида:

```text
CM breakdown for <Country>:
```

Либо возьми уникальные значения из колонки `Country` в Excel — должны совпасть с `Country Final` после обработки в скрипте.

---

## 3. Шаблон одного странового отчёта (повторять для каждой страны)

Для страны **`{Country}`** и CM **`{Имя}`** (например Evgeni Sedin):

### 3.1 Таблица CM

1. В `_full_analytics_run.md` найди блок **`CM breakdown for {Country}`**.
2. Перенеси строки в Markdown-таблицу:

| CM | Deps | Leads | Spend | Net | CPL | CPA | CR% | ROI |

3. Цифры возьми из ASCII-таблицы скрипта (те же метрики: Deposits, Leads, Spend, Net, CPL, CPA, CR%, ROI).

### 3.2 «Что лучше остальных»

Сравни **свою** строку CM с коллегами в **этой же стране**:

- **ROI** выше → лучше окупаемость при том же спенде.
- **CPA** ниже → дешевле депозит.
- **CPL** ниже → дешевле лид (смотреть вместе с CR).
- **CR%** выше → лиды конвертятся лучше.
- **Deps** больше при сопоставимом ROI → больше масштаб.

Запиши 2–4 тезиса.

### 3.3 «Что хуже»

Типичные углы:

- меньше **Deps**, чем у лидера при схожем спенде;
- **ROI < 1** при значимом spend;
- **0 депозитов** при большом числе лидов;
- завышенный **CPL** при низком CR.

### 3.4 Две кампании к открытию

1. В том же файле найди **`Offer comparison for {Country}`**.
2. Ниже — рекомендации **`Recommendation: open top … campaigns per offer`**.
3. Выбери **2 связки**:
   - приоритет офферу с **большим объёмом FTD** и нормальным ROI в таблице офферов;
   - либо топ-1–2 из рекомендаций по этому офферу.

Скопируй формулировку вида: `device + interest + age + creative + … | Deposits … ROI …`.

---

## 3A. Отчёт для директора (Excel RU / EN)

Отдельный артефакт — **не** замена персонального текстового отчёта из §3, а **презентабельная книга** для руководителя: один лист **Summary**, по листу на каждую из **10 стран** из рабочего списка, внутри — **кампейн-менеджеры → ассеты → две лучшие связки** (тексты связок задаются в скрипте локализации).

**Быстрый запуск из Cursor:** **`/marketing-director-report`** (см. файл команды в `.cursor/commands/`).

**Данные:**

| Элемент | Описание |
|--------|----------|
| Источник | `cursor example.xlsx` (путь из `config/paths.py` или `FB_STATS_EXCEL`) |
| Период | месяц **максимальной** даты в колонке `Date` |
| Страны | Albania, Argentina, Bahrain, Chile, Colombia, Ecuador, Guatemala, Kuwait, Mexico, United Arab Emirates |
| Срез | сохраняется в `analysis_agent/_stats_doc_countries_lastmonth.xlsx` (создаётся командой или автоматически при первом запуске генератора) |

**Команды (PowerShell, корень `fb_agents`):**

```powershell
Set-Location "C:\Users\EvgeniSedin\fb_agents"
python analysis_agent/build_marketing_director_report.py --lang both
```

- `--lang ru` → только `analysis_agent/Marketing_Director_Report.xlsx`
- `--lang en` → только `analysis_agent/Marketing_Director_Report_EN.xlsx`
- `--lang both` → оба файла

Опционально тот же срез в markdown (для архива или разбора в чате):

```powershell
python src/analysis_agent/analyze_fb_stats.py --excel analysis_agent/_stats_doc_countries_lastmonth.xlsx --country all --top 2 --output analysis_agent/_full_analytics_lastmonth.md
```

**Скрипт:** `analysis_agent/build_marketing_director_report.py` — при необходимости правь **формулировки связок** в словарях `TOP_COMBOS_RU` / `TOP_COMBOS_EN` и строки интерфейса в `STRINGS`.

---

## 4. Порядок работы «по всем странам»

| Шаг | Действие |
|-----|----------|
| 1 | Выполнить §1 → получить `_full_analytics_run.md` |
| 2 | Составить список стран из §2 |
| 3 | Для страны #1 выполнить §3 → вставить в общий документ (например `Evgeni_Sedin_Report_by_Country.md`) |
| 4 | Повторить §3 для страны #2, #3, … |
| 5 | Страны без FTD / без рекомендаций: кратко «данных мало» + сослаться на соседние гео или общий топ |

**Ускорение:** держи открытыми два окна — полный `.md` и файл итогового отчёта; для каждой страны только поиск `CM breakdown for` / `Offer comparison for`.

---

## 5. Обновление после новой выгрузки

1. Заменить Excel на новый файл (или тот же путь с обновлёнными данными).
2. Повторить §1 (перезаписать `_full_analytics_run.md`).
3. Пересобрать страничные блоки или diff-ом сравнить старый и новый полный отчёт.

---

## 6. Ссылки в репозитории

| Файл | Назначение |
|------|------------|
| `src/analysis_agent/analyze_fb_stats.py` | Расчёт метрик и печать отчёта |
| `analysis_agent/build_marketing_director_report.py` | Сборка Excel для директора (RU/EN), §3A |
| `analysis_agent/_stats_doc_countries_lastmonth.xlsx` | Отфильтрованный срез под 10 стран и месяц из `Date` |
| `analysis_agent/Marketing_Director_Report.xlsx` | Книга на русском |
| `analysis_agent/Marketing_Director_Report_EN.xlsx` | Книга на английском |
| `.cursor/commands/marketing-director-report.md` | Slash-команда Cursor для §3A |
| `analysis_agent/CM_by_Country_Report.md` | Ручная сводка CM по странам (можно синхронизировать с §3.1) |
| `analysis_agent/Evgeni_Sedin_Report_by_Country.md` | Пример готового мульти-странового персонального отчёта |

---

## 7. Опционально (автоматизация)

Если нужно **без ручного копирования** таблиц CM в Markdown — добавить в `analyze_fb_stats.py` режим вывода только выбранной страны и формат `| CM | Deps | … |` (отдельный PR/задача).
