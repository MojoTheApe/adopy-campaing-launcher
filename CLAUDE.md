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

Read [the shared tracker workflow](docs/TRACKER_AGENT_WORKFLOW.md) and `tracker/config.json` before task work. Automatically run `python -m tracker start ISSUE` and verify **In progress before the first code edit**. Use **High / Medium / Low**, default **Medium**. After full review and merge, run the verified `python -m tracker merged` transition and confirm the UI update. GitHub Issues are the source of truth; In master is the final stage.

Tracker Design v2 behavior and template reuse are documented in [tracker/README.md](tracker/README.md). Classify natural-language requests using the shared workflow: choose Module and Type, add Design when applicable, and require evidence for Blocked and approved scope for Required. List/Board and UI filters do not change the shared GitHub start/queue/merge workflow.
