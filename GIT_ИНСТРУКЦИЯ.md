# Сохранение в Git

Репозиторий уже инициализирован в папке `fb_agents`. Первый коммит сделан.

## Как сохранять прогресс в Git дальше

Открой терминал в Cursor (или PowerShell) и выполни:

```powershell
cd C:\Users\EvgeniSedin\fb_agents

# Посмотреть, что изменилось
git status

# Добавить все изменённые файлы
git add -A

# Сохранить с комментарием
git commit -m "Обновил отчёт CM по странам"
```

Вместо `"Обновил отчёт CM по странам"` можно писать любой короткий текст — что именно сделал.

## Подсказки

- **Только часть файлов:**  
  `git add analysis_agent/CM_report_tables.md` — потом `git commit -m "..."`

- **История коммитов:**  
  `git log --oneline`

- **Удалённый репозиторий (GitHub/GitLab):**  
  Если создашь репозиторий на GitHub/GitLab, выполни (подставь свой URL):
  ```powershell
  git remote add origin https://github.com/ТВОЙ_ЛОГИН/fb_agents.git
  git push -u origin master
  ```
  После этого прогресс будет и в облаке.
