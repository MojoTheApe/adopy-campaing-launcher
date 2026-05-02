---
description: Запустить run_morning.py и разобрать утренний отчёт по странам
---

Ты — шаг **«Run Morning»**: запусти `run_morning.py`, дождись завершения, прочитай результат и сделай разбор.

## 1. Запуск скрипта

Из корня `fb_agents` (PowerShell):

```powershell
Set-Location "C:\Users\EvgeniSedin\fb_agents"
python run_morning.py
```

Дождись строки `[run_morning] Report saved:` в выводе — это сигнал успеха.

Если нужен явный путь к Excel:

```powershell
python run_morning.py -e "C:\Users\EvgeniSedin\Desktop\cursor example.xlsx"
```

Только одна страна:

```powershell
python run_morning.py --country Mexico
```

## 2. После успешного выполнения

1. **Прочитай** `analysis_agent/morning_report.md`.
2. **Покажи пользователю сводку:**
   - Сколько стран и комбо обработано (из строки `Done. N countries, M combos`).
   - По каждой стране: топ-3 рабочих комбо (тип, оффер, dep, ROI) и статусы аккаунтов.
   - Выдели страны, где есть Strong-комбо с ROI > 0.2 — они приоритет.
   - Выдели страны, где все аккаунты Disabled — там нечего запускать.
3. **Напомни следующие шаги:**
   - Для анализа конкретной страны: скопировать секцию `### 4. Контекст для AI` и вставить в чат с вопросом «почему эти связки работают».
   - Для гипотез: скопировать секцию `### 5. Гипотезы` и попросить «дай 5 гипотез как зацепить ЦА».
   - Для заливки: `python run.py upload-bridge -a "FacebookXXX" -c "Country" --top 2`.

## 3. Ошибки

| Ошибка | Что делать |
|--------|------------|
| `FileNotFoundError` | Проверь путь в `config/paths.py` или передай `-e` |
| `Missing expected columns` | Привести Excel к колонкам: Country, Account Alias, Adset Name, Ad Title, # Leads, # FTDs, $ Spend, CM, Ad Asset, $ Net |
| Google Sheet не читается | Добавь `--no-gsheet` или проверь `config/token_accounts.json` |
| `Permission denied` на .xlsx | Закрой файл в Excel и повтори |

При любой ошибке — вывести traceback и подсказать исправление.
