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

Read [the shared tracker workflow](docs/TRACKER_AGENT_WORKFLOW.md) and `tracker/config.json` before task work. Automatically run `python -m tracker start ISSUE` and verify **In progress before the first code edit**. Use **High / Medium / Low**, default **Medium**. After full review and merge, run the verified `python -m tracker merged` transition and confirm the UI update. GitHub Issues are the source of truth; In master is the final stage.
