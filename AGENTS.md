# ACL: инструкции для агентов

Перед изменениями прочитайте [общие правила совместной разработки](docs/AI_COLLABORATION.md)
и [процесс ACL](docs/DEVELOPMENT_WORKFLOW.md). Эти документы одинаковы
для Codex, Claude и других участников; отдельные копии правил не нужны.

Production — `master` в `MojoTheApe/adopy-campaing-launcher`; явно указывайте
базу PR. Работайте в отдельной рабочей папке своей задачи. Проверяйте актуальное
владение файлами и сохраняйте чужие изменения и машинную конфигурацию.

Технический контекст: [распространение](docs/DISTRIBUTION.md),
[история релизов](docs/RELEASES.md). Конкретные указания пользователя и
согласованные исключения имеют приоритет над общими правилами процесса.

## Task tracker — mandatory

Read [the tracker workflow](docs/TRACKER_AGENT_WORKFLOW.md), `tracker/config.json`, and the pinned guide (`python scripts/task_tracker.py guide`) before every task, including in resumed chats. Every development request, including “fix this now”, requires a real Issue before code. Use `python scripts/task_tracker.py work ID` or `work --title ... --body-file task.md --module ... --type ...` to find/create it and verify In Progress. Choose Module and Type yourself; default priority is Medium. Planning requests create classified tasks without starting them. Release is one explicit `--release` flag. Use the shared execution slot and preserve Archive. Before merge, include one `Tracker issues: ID` line and run `python scripts/task_tracker.py check-pr PR_NUMBER`. After all work is reviewed, merged and verified, perform the protected merged transition and verify the shared UI. Follow all existing project review, CI and operational rules; an earlier CI exception does not authorize another PR.

Every new task starts with an agent-written plain-English Description explaining what changes, why it matters and the expected result, followed by acceptance criteria and optional technical notes. Write it from the request and verified context even if the owner did not dictate it. Use `--body-file` and `tracker/task-template.md`; missing or placeholder descriptions are rejected. Preserve existing task requirements/history, and review examples with the owner before bulk retrospective edits.
