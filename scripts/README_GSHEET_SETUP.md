# Настройка Google Sheets API для синхронизации таргетов

## Шаги (один раз)

### 1. Создать проект в Google Cloud Console

1. Открыть [console.cloud.google.com](https://console.cloud.google.com)
2. Нажать **Select a project → New Project**
3. Дать имя, например `fb-agents-kb`
4. Нажать **Create**

### 2. Включить Google Sheets API

1. В левом меню: **APIs & Services → Library**
2. Найти **Google Sheets API**, нажать **Enable**

### 3. Создать Service Account и скачать ключ

1. **APIs & Services → Credentials → Create Credentials → Service Account**
2. Имя: `fb-agents-kb-reader` → **Create and Continue** → **Done**
3. Кликнуть на созданный сервисный аккаунт → вкладка **Keys**
4. **Add Key → Create new key → JSON** → скачать файл
5. Переименовать файл в `google_credentials.json` и положить в папку `config/`
   ```
   fb_agents/
   └── config/
       └── google_credentials.json   ← сюда
   ```
   > ⚠️ Файл НЕ попадёт в git (добавлен в .gitignore автоматически)

### 4. Поделиться таблицей с сервисным аккаунтом

1. Открыть нужную Google Sheet с таргетами
2. Нажать **Share**
3. Вставить email сервисного аккаунта (вида `fb-agents-kb-reader@fb-agents-kb.iam.gserviceaccount.com`)
4. Выбрать роль **Viewer** → **Send**

### 5. Скопировать Sheet ID в .env

URL таблицы выглядит так:
```
https://docs.google.com/spreadsheets/d/1aBcDeFgHiJkLmNoPqRsTuV/edit#gid=0
                                        ^^^^^^^^^^^^^^^^^^^^^^^^
                                        это и есть GSHEET_TARGETS_ID
```

В `.env`:
```
GSHEET_TARGETS_ID=1aBcDeFgHiJkLmNoPqRsTuV
GSHEET_TARGETS_SHEET=Sheet1
GOOGLE_CREDENTIALS_JSON=config/google_credentials.json
```

---

## Ожидаемый формат таблицы

Скрипт ожидает **чередующиеся строки** в колонке A:

| Строка | Значение |
|--------|----------|
| 1 | `EF` |
| 2 | `Exchange trading, Currency exchange, Forex broker` |
| 3 | `HV` |
| 4 | `Investing, Wealth management, Financial planning` |
| 5 | `FX` |
| 6 | `Forex trading, Currency trading` |
| ... | ... |

- Нечётные строки — **код таргета** (как он используется в кампейнах)
- Чётные строки — **FB интересы через запятую**
- Пустые строки игнорируются

---

## Запуск синхронизации

Скрипт `sync_targets_from_gsheet.py` удалён (ACL-1241): таргеты и тексты живут в
общей библиотеке — приватном репозитории GitHub (`docs/INTEGRATIONS.md`).

После выполнения обновятся:
- `knowledge_base/targets.md`
- `config/knowledge_base.json` (секция `targets`)
