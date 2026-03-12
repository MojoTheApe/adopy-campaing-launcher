# fb_agents

Анализ статистики Facebook-кампаний: отчёты по странам, кампайн-менеджерам (CM) и офферам.

## Требования

- Python 3.x
- Зависимости: `pandas`, `openpyxl` (см. `requirements.txt`)

Установка:

```bash
pip install -r requirements.txt
```

## Данные

Скрипт читает Excel-файл с колонками: **Country**, **Account Alias**, **Adset Name**, **Ad Title**, **# Leads**, **# FTDs**, **$ Spend**, **CM**, **Ad Asset**, **$ Net**.

**Путь к файлу:** сначала берётся из переменной окружения **`FB_STATS_EXCEL`**. Если она не задана — используется путь по умолчанию в коде (`EXCEL_PATH_DEFAULT` в `analyze_fb_stats.py`). Так у каждого может быть свой файл без правок в репозитории.

Пример (PowerShell, одна сессия):
```powershell
$env:FB_STATS_EXCEL = "C:\Users\YourName\Desktop\my_data.xlsx"
python analyze_fb_stats.py
```

**Правило:** оффер (Offer) всегда берётся из колонки **Ad Asset**.

## Запуск

Из корня проекта:

```powershell
cd fb_agents\analysis_agent
python analyze_fb_stats.py
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
# Отчёт по офферам по всем странам (по умолчанию), топ-2 на оффер
python analyze_fb_stats.py

# Только Ecuador для блока по офферам
python analyze_fb_stats.py --country Ecuador

# Все страны, топ-3 кампании на оффер
python analyze_fb_stats.py --top 3

# Указать свой Excel и сохранить отчёт в файл
python analyze_fb_stats.py --excel C:\data\stats.xlsx --output report.txt

# Справка по параметрам
python analyze_fb_stats.py --help
```

## Структура

- `analysis_agent/analyze_fb_stats.py` — основной скрипт анализа.
- `analysis_agent/` — сгенерированные отчёты (например `CM_report_tables.md`, `report_cm_by_country.txt`) при сохранении в файл.
- `docs/` — инструкции по Git и сохранению прогресса.

## Отчёты

- **Комбо по странам** — лучшие связки (страна, устройство, интерес, возраст, креатив, текст) с метриками.
- **CM по странам** — таблица по каждому кампайн-менеджеру: Deposits, Leads, Spend, Net, CPL, CPA, CR%, ROI.
- **Мексика по офферам** — сравнение офферов (из Ad Asset) и рекомендация топ-2 кампаний на каждый оффер.
