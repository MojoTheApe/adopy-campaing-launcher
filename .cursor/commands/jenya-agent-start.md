---
description: Jenya agent start — полный прогон аналитики FB по всем странам (WORKFLOW_Country_CM_Report.md)
---

Ты — шаг **«Jenya agent start»**: один полный прогон `analyze_fb_stats.py` для **каждой страны** из Excel. Не фильтруй одну страну: используй режим **все страны** (по умолчанию `--country all` — CM breakdown и Offer comparison печатаются в цикле по всем гео).

**Команда (PowerShell), из корня `fb_agents`:**

```powershell
Set-Location "C:\Users\EvgeniSedin\fb_agents"
python src/analysis_agent/analyze_fb_stats.py --country all --top 2 --output analysis_agent/_full_analytics_run.md
```

Путь к Excel не указывай, если достаточно дефолта из `config/paths.py`. Иначе:

```powershell
python src/analysis_agent/analyze_fb_stats.py --excel "C:\ПУТЬ\к\файлу.xlsx" --country all --top 2 --output analysis_agent/_full_analytics_run.md
```

Опционально: `--with-context`.

**Не делай:** отдельные прогоны только для Мексики или одной страны — этот старт именно **полный отчёт по всем странам** в одном файле.

---

### После успешного выполнения (обязательно)

1. Путь к файлу: `analysis_agent/_full_analytics_run.md` (абсолютный путь на диске пользователя).
2. **Прочитай файл инструментом Read** и **покажи в ответе**:
   - если строк **≤ 250** — весь файл;
   - если **> 250** — (а) начало до первого `CM breakdown` (~строки 1–160), (б) один полный блок **`CM breakdown for …`** для **первой страны по порядку в файле** (образец), (в) соответствующий блок **`Offer comparison for …`** для той же страны, (г) список всех стран из строк вида `CM breakdown for …:` (кратко, одна строка или маркированный список), (д) фраза: полный путь + «остальные страны — в этом же файле, открой в редакторе».
3. Напомни: дальнейшие шаги — [`analysis_agent/WORKFLOW_Country_CM_Report.md`](analysis_agent/WORKFLOW_Country_CM_Report.md) (персональный CM-отчёт по стране). Для **всех режимов** агента (`--short`, `--with-context`, `--asset`, смысл `--country`) — [`analysis_agent/ANALYSIS_AGENT_WORKFLOW.md`](analysis_agent/ANALYSIS_AGENT_WORKFLOW.md).

При ошибке скрипта — выведи traceback/stderr и подскажи проверить Excel и путь.
