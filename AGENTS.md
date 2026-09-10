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

Read [the current shared workflow](docs/TRACKER_AGENT_WORKFLOW.md) and `tracker/config.json`. Use `python scripts/task_tracker.py` for all task operations, including automatic `start ISSUE` before the first code edit. The pinned shared engine is canonical; the older local tracker package is retained only for compatibility. Choose Module and Type automatically, use Medium by default, preserve project-local Archive and explicit Release scope. After full review/merge, perform the verified merged transition and check the UI.
