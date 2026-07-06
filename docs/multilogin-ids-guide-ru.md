# Multilogin: где найти `folder_id` и `profile_id`

Эти два идентификатора нужны в файле `config/ad_account_map.csv`, чтобы RPA-публикатор
мог запустить правильный браузерный профиль для нужного Facebook-аккаунта.

---

## Что это такое

| Поле | Описание |
|------|----------|
| `folder_id` | UUID **папки** внутри Multilogin, в которой лежит профиль |
| `profile_id` | UUID конкретного **браузерного профиля** |

Оба выглядят как UUID-строки вида `550e8400-e29b-41d4-a716-446655440000`.

---

## Способ 1 — Через локальное API приложения (Рекомендуется, работает в десктопе)

Когда Multilogin запущен, он поднимает локальный HTTP-сервер на порту **35000**.
Открыть прямо в браузере или через curl — никакой авторизации не нужно.

### Шаг 1: получить список папок

Вставьте этот адрес в браузер (или откройте любым HTTP-клиентом):

```
http://localhost:35000/profile/folder
```

Ответ (JSON):
```json
{
  "data": [
    { "folderID": "550e8400-e29b-41d4-a716-446655440000", "name": "Facebook MX" },
    { "folderID": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "name": "Facebook BR" }
  ]
}
```

Скопируйте `folderID` нужной папки — это ваш **`folder_id`**.

### Шаг 2: получить профили в папке

```
http://localhost:35000/profile?folderID=<вставьте_folder_id>
```

Ответ:
```json
{
  "data": [
    { "profileID": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d", "name": "FB_MX_574" },
    { "profileID": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed", "name": "FB_MX_575" }
  ]
}
```

Скопируйте `profileID` нужного профиля — это ваш **`profile_id`**.

---

## Способ 2 — Правый клик в приложении (не всегда работает)

1. Откройте десктопное приложение **Multilogin**.
2. В левой панели найдите нужную папку.
3. **Кликните правой кнопкой на папку** → ищите пункт **«Copy folder ID»** или **«Скопировать ID папки»**.
4. Если пункта нет — используйте Способ 1 (localhost API).

> В некоторых версиях приложения UUID папки виден как тултип при наведении курсора на имя папки.

---

## Способ 2 — Multilogin API

Если у вас есть доступ к API Multilogin, ID можно получить программно.

### Получить все папки

```
GET https://api.multilogin.com/profile/folder
Authorization: Bearer <ваш_токен>
```

Пример ответа:
```json
{
  "data": [
    { "folderID": "550e8400-...", "name": "Facebook Accounts MX" },
    { "folderID": "6ba7b810-...", "name": "Facebook Accounts BR" }
  ]
}
```

Скопируйте `folderID` нужной папки → это `folder_id`.

### Получить профили в папке

```
GET https://api.multilogin.com/profile?folderID=<folder_id>
Authorization: Bearer <ваш_токен>
```

Пример ответа:
```json
{
  "data": [
    { "profileID": "9b1deb4d-...", "name": "FB_MX_Account_574" },
    { "profileID": "1b9d6bcd-...", "name": "FB_MX_Account_575" }
  ]
}
```

Скопируйте `profileID` нужного профиля → это `profile_id`.

---

## Способ 3 — Через веб-интерфейс Multilogin

1. Откройте [https://app.multilogin.com](https://app.multilogin.com) в браузере.
2. Перейдите в раздел **Profiles** и откройте нужную папку.
3. **Кликните на профиль**, чтобы открыть его детали.
4. URL изменится примерно на:
   ```
   https://app.multilogin.com/profiles/550e8400-e29b-41d4-a716-446655440000
   ```
   Последний UUID-сегмент — это **profile_id**.
5. Для **folder_id**: кликните на папку — URL станет:
   ```
   https://app.multilogin.com/folder/6ba7b810-...
   ```

---

## Как заполнить конфиг

**Вариант А — через дашборд** (рекомендуется):

1. Откройте утренний отчёт.
2. В разделе Campaign Builder найдите аккаунт с надписью **«X Missing: folder_id, profile_id»**.
3. Заполните поля ввода и нажмите **Save config**.
4. Данные сразу сохранятся в `config/ad_account_map.csv`.

**Вариант Б — редактирование CSV вручную**:

```csv
account_name,ad_account_id,page_id,multilogin_instance,folder_id,profile_id,...
FacebookUL574 EC Fav,act_1234567890,,A,550e8400-e29b-41d4-a716-446655440000,9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d,...
```

> **`multilogin_instance`** — `A` (по умолчанию) или `B`, если используются два отдельных экземпляра Multilogin.

> **`account_name`** должно точно совпадать с названием аккаунта на дашборде (регистр важен).

---

## Устранение неполадок

| Симптом | Причина | Решение |
|---------|---------|---------|
| «Account not found in ad_account_map.csv» | Строка отсутствует или название не совпадает | Добавьте строку; `account_name` должен точно совпадать с меткой аккаунта на дашборде |
| «Multilogin error: profile not found» | Неверный `profile_id` | Проверьте по Способу 1 или 2 |
| «Multilogin error: folder not found» | Неверный `folder_id` | Проверьте название папки в приложении Multilogin |
| Профиль запускается, но не тот Facebook-аккаунт | `profile_id` указывает на другой профиль | Убедитесь, что в профиле есть нужные Facebook cookie |
