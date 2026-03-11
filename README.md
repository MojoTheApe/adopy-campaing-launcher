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

Путь к файлу задаётся в `analysis_agent/analyze_fb_stats.py` переменной `EXCEL_PATH_DEFAULT` (по умолчанию — файл на рабочем столе).

**Правило:** оффер (Offer) всегда берётся из колонки **Ad Asset**.

## Запуск

Из корня проекта:

```powershell
cd fb_agents\analysis_agent
python analyze_fb_stats.py
```

Вывод в консоль: комбо по странам (Strong/Interesting), разбивка по CM по каждой стране, сравнение по офферам для Мексики и рекомендация топ-2 кампаний на каждый оффер.

Сохранение полного отчёта в файл:

```powershell
python analyze_fb_stats.py > report_cm_by_country.txt 2>&1
```

## Структура

- `analysis_agent/analyze_fb_stats.py` — основной скрипт анализа.
- `analysis_agent/` — сгенерированные отчёты (например `CM_report_tables.md`, `report_cm_by_country.txt`) при сохранении в файл.
- `docs/` — инструкции по Git и сохранению прогресса.

## Отчёты

- **Комбо по странам** — лучшие связки (страна, устройство, интерес, возраст, креатив, текст) с метриками.
- **CM по странам** — таблица по каждому кампайн-менеджеру: Deposits, Leads, Spend, Net, CPL, CPA, CR%, ROI.
- **Мексика по офферам** — сравнение офферов (из Ad Asset) и рекомендация топ-2 кампаний на каждый оффер.
