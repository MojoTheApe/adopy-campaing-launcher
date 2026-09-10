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

Read [the tracker workflow](docs/TRACKER_AGENT_WORKFLOW.md), `tracker/config.json`, and the pinned guide (`python scripts/task_tracker.py guide`) before every task, including in resumed chats. Every development request, including “fix this now”, requires a real Issue before code. Use `python scripts/task_tracker.py work ID` or `work --title ... --module ... --type ...` to find/create it and verify In Progress. Choose Module and Type yourself; default priority is Medium. Planning requests create classified tasks without starting them. Release is one explicit `--release` flag. Use the shared execution slot and preserve Archive. Before merge, include one `Tracker issues: ID` line and run `python scripts/task_tracker.py check-pr PR_NUMBER`. After all work is reviewed, merged and verified, perform the protected merged transition and verify the shared UI. Follow all existing project review, CI and operational rules; an earlier CI exception does not authorize another PR.
