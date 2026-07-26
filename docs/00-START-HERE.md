---
kind: index
name: 00-start-here
created: 2026-07-24
status: active
tags:
  - index
  - agent10
---

# START HERE — Agent 10 continuation

Открывай эту заметку первой в каждой новой сессии/контекстном окне. Здесь —
текущий статус одной строкой на каждый пункт и ссылки на детали. Каждый
раз, когда контекстное окно заканчивается, **обновляй именно эту заметку**
(секцию "Прямо сейчас" внизу) перед тем как закончить — так следующая
сессия не начинает с нуля.

## Репозиторий и окружение

- **Правильный репозиторий:** `C:\Users\EvgeniSedin\fb_agents` — **НЕ**
  `C:\fb_agents` (устаревшая копия-дубликат с собственным `.claude/launch.json`,
  из-за которой уже была путаница с пропавшими аккаунтами на дашборде).
- **Ветка:** `feat/agent10-post-conversion-location`.
- **Дашборд:** запускать напрямую через Bash: `cd "C:\Users\EvgeniSedin\fb_agents" && python serve.py`
  (НЕ через `preview_start({name:...})` — для этого проекта он резолвится не туда).
- **Тестовый CLI-запуск:**
  ```
  python scripts/run_rpa_publish.py --account "FacebookUL921 CR Ait" --country CR --slots-json output/dry_run_slots_cr_navtest.json --dry-run
  ```

## Карта документов (открывать по необходимости)

- [[agent10-recorded-flow-runbook]] — таблица всех шагов `run_recorded_campaign_flow`, что может пойти не так, как система реагирует. **Читать перед каждым живым тестом.**
- [[agent10-codegen-via-multilogin]] — вывод: Playwright Inspector НЕ работает на Multilogin/Mimic, использовать `click_recorder.py` ("Record clicks" на дашборде).
- [[agent10-codegen-training-handoff]] — предыдущий подробный handoff (2026-07-23/24), детали фиксов сессии.
- [[agent10-post-conversion-location-handoff]] — самый первый handoff (2026-07-21), где всё начиналось (до Website).
- [[global-rollout-overview]], [[hub-agent-architecture]], [[agent-update-strategy]] — отдельная параллельная тема: перевод на shared multi-user hosting (НЕ путать с тестированием RPA-флоу).

## Тестовые аккаунты

| Аккаунт | Статус | Комментарий |
|---|---|---|
| `FacebookUL921 CR Ait` | ✅ Рабочий, основной тестовый | Использован в 60+ прогонах сегодня, config полный |
| `FacebookDQ204 CR` | ❌ Сломан (2026-07-24) | `net::ERR_INVALID_AUTH_CREDENTIALS` — сессия/прокси протухла, нужен ручной relogin в Multilogin Desktop прежде чем тестировать снова |

## Прямо сейчас (обновлять каждую сессию)

**2026-07-24, конец контекстного окна:**

- Переключаемся обратно на `FacebookUL921 CR Ait` (DQ204 недоступен).
- Последний код-фикс (own-text + CDP-click для "Further limit the reach of
  your ads" reopen → "Continue", в `run_recorded_campaign_flow`,
  `src/fb_ads_rpa/meta_dom.py`) **ещё ни разу не проверен живым тестом до
  конца** — предыдущие попытки прерывались (сначала ранний сбой на
  `create`, потом DQ204 завис).
- Тестовые данные (`output/dry_run_slots_cr_navtest.json`) дополнены
  реальными `copy: "ES Text 4 Ait"` (есть title+body в Upload FB.xlsx) —
  теперь при полном прогоне должны исполниться и шаги Headline/Primary
  text, не только Detailed targeting/Languages/Placements.
- **Следующий шаг:** запустить `run_rpa_publish.py` на `FacebookUL921 CR Ait`
  с этими же тестовыми данными, довести до конца (draft hand-off) или до
  первой реальной поломки, зафиксировать в
  [[agent10-recorded-flow-runbook]].
- Незакоммиченные изменения (проверить `git status` в начале сессии):
  `meta_dom.py`, `click_recorder.py`, `codegen_recorder.py`, `app.py`,
  `report.html` — фиксы recorder'а (account-based CDP lookup) + own-text
  клик для Further-limit-reach. Коммитить после успешной живой проверки.

#index #agent10
