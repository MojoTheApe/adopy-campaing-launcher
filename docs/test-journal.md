---
kind: journal
name: test-journal
created: 2026-07-24
status: active
tags:
  - agent10
  - journal
related:
  - 00-START-HERE
  - agent10-recorded-flow-runbook
---

# Дневник тестов — Agent 10

Короткие записи после каждого живого прогона, для контекста между
сессиями. Формат: дата/время, аккаунт, докуда дошло, что нового.
Подробности — в [[agent10-recorded-flow-runbook]], сюда — только суть.

## 2026-07-24

- **FacebookDQ204 CR** — сломан, `net::ERR_INVALID_AUTH_CREDENTIALS`
  (протухшая сессия/прокси в Multilogin). Переключились обратно на
  `FacebookUL921 CR Ait`.
- Код переведён в режим "не тыкается — пропускаем, идём дальше" для всех
  шагов после Audience Locations (Detailed targeting, Languages,
  Placements, Ad level) — раньше отдельные шаги жёстко останавливали весь
  прогон. Цель — за один прогон увидеть полную картину, что реально
  работает, а не чинить по одному шагу за раз.

<!-- новые записи добавлять сверху -->
