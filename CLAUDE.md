# ACL: инструкции для Claude

Перед изменениями прочитайте [общие правила совместной разработки](docs/AI_COLLABORATION.md)
и [процесс ACL](docs/DEVELOPMENT_WORKFLOW.md). Те же документы указаны
в `AGENTS.md` для Codex. Не поддерживайте отдельную
копию правил и не восстанавливайте старое постоянное разделение файлов между
дешборд- и RPA-сессиями: владельцы определяются текущей задачей.

Production — `master` в `MojoTheApe/adopy-campaing-launcher`; явно указывайте
базу PR. Машинные пути и дополнительные push-адреса проверяются в конкретном
клоне, а не копируются из исторического Windows-чекаута.

Технический контекст: [распространение](docs/DISTRIBUTION.md),
[история релизов](docs/RELEASES.md). Конкретные указания пользователя и
согласованные исключения имеют приоритет над общими правилами процесса.

Готовый промпт для начала задачи и переноса правил в другие проекты:
[CLAUDE_START_PROMPT.md](docs/CLAUDE_START_PROMPT.md).

## Task tracker — mandatory

Read [the current shared workflow](docs/TRACKER_AGENT_WORKFLOW.md) and `tracker/config.json`. Use `python scripts/task_tracker.py` for all task operations, including automatic `start ISSUE` before the first code edit. The pinned shared engine is canonical; the older local tracker package is retained only for compatibility. Choose Module and Type automatically, use Medium by default, preserve project-local Archive and explicit Release scope. After full review/merge, perform the verified merged transition and check the UI.
