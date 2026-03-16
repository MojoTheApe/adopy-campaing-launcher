# Агент 8: Сбор кампании из Excel под FB — исследование файла Upluad FB.xlsx

Глубокий разбор каждого листа и закономерностей для реализации «сбора кампании» под выгрузку в Facebook.

---

## 1. Список листов и роли

| Лист | Роль | Связи |
|------|------|--------|
| **Accounts** | Справочник аккаунтов: Account, Domain, Pixel, Asset, Link | Main, campaign sheets (Pixel = Main!C2) |
| **Main** | Текущий контекст «один аккаунт»: имя, домен, пиксель, заголовок Jira, LP, ссылки | VLOOKUP из Accounts и LP's |
| **LP's** | Справочник офферов: Brand offer, Country, Country 1, Lp's (URL), Stock | Main (Jira/LP), Body |
| **Ad copy** | Тексты объявлений: Ad copy name, Title, Body, Link Description, Asset, Language | Колонка Copy (N) в campaign sheets |
| **Countries** | Коды стран и язык: CO, код, Language, число (180) | VLOOKUP по коду страны → Locales, Language |
| **Campaign name** | Справочник форматов имён кампаний (country, OS, Asset, Creative, Targeting, Device, Language, Bid Strategy) | Шаблон для колонки Campaign Name |
| **Video file name** | Номер креатива (L) → имя файла видео (A=число, B=filename.mp4, C=.mp4) | Колонка L (Creative number) |
| **Image FIle name** | Номер/ид изображения → имя файла (B=A&C, C=.jpeg/.jpg) | Колонка L (Creative number) в Image-кампаниях |
| **Body** | Шаблоны URL LP по офферу (Lps base + path + VLOOKUP LP's Country) | Сборка ссылки для объявления |
| **Interests** | Targeting (J) → JSON таргетинга FB (колонка B — длинный JSON) | VLOOKUP(J, Interests!A:B, 2) в campaign sheets |
| **Upload all** | Сводный лист под выгрузку: те же колонки, что и campaign sheets; Pixel = Main!C2 | Как один из campaign-листов |
| **CO Cib, GT BID, EC Favorita, MX Amz, UAE AI Trader, Latam AIT, CO EP, CL ML, UAE Slk** | Листы-шаблоны кампаний по связке страна+оффер; один лист = один «тип» кампании (страна + asset) | Pixel из Main, структура FB |

---

## 2. Единая структура колонок campaign-листов (до ~49 колонки)

Во всех листах-кампаниях (включая Upload all) первые колонки совпадают:

| Колонка | Заголовок | Откуда значение | Пример |
|---------|-----------|----------------|--------|
| A | Campaign ID | пусто / ручной | — |
| B | Pixel | **=Main!$C$2** | 2104660127051375 |
| C | Campaign name - country | Код страны (фикс на листе) | CO, GT, EC, MX, BH, CL, UAE |
| D | Asset | Оффер (фикс на листе) | Cib, BID, Fav, AMZ, Ait, Ep, ML, Slk |
| E | OS | All / IOS | All, IOS |
| F | Icons | "_" | _ |
| G, H | — | "+", "-" | +, - |
| I | Creative | Image / Video | Image, Video |
| J | Targeting | Интерес (фикс по строке) | FX, Day, Trading, HV_MX, FX_IOS, InvestmentPlus, HNW, … |
| K | Age | Возраст | 25, 35, 40 |
| L | Creative number | Номер креатива (ид изображения или видео) | 1377, 442, JF, 1163, 1389, Blue, rman, 1740, … |
| M | Date | Дата запуска | 02.03, 16.02, 20.02, … |
| N | Copy | Название/текст ад-копи (связь с Ad copy) | ES Learn how Bancolombia, AR Ai Trader The App, … |
| O | Campaign Name | **Формула** | см. ниже |
| P–… | Campaign Status, Objective, Buying Type, Budget, … | Параметры кампании FB | |
| AB | Ad Set ID | | |
| AE | Ad Set Name | | |
| … | | | |
| AL | Optimized Conversion Tracking Pixels 1 | База ссылки? | |
| AO | Link | **=AL& B** (база + Pixel) | Финальная ссылка объявления |
| AP | Countries | **VLOOKUP(C, Countries!A:E, 2)** или **VLOOKUP(AP, Countries!B:C, 2)** | Код для локалей |
| AU, AV | Age Min, Age Max | Из Age (K) | 25, 35, 40 |
| AW | Locales | **VLOOKUP(C, Countries!A:E, 2)** или по AP → язык | |

**Закономерность имени кампании (колонка O):**

Формула во всех campaign-листах одна и та же по смыслу:

```
Campaign Name (O) = C & F & D & F & E & F & I & F & L & F & J & F & K & F & M
```

То есть: **Country _ Asset _ OS _ Creative _ CreativeNumber _ Targeting _ Age _ Date**

Пример результата: `CO_Cib_All_Image_1377_FX_35_02.03`, `EC_Fav_All_Image_1389_Trading_35_16.02`, `MX_AMZ_IOS_Image_JF_HV_MX_40_20.02`.

---

## 3. Закономерности по листам

### 3.1 Accounts (источник правды по аккаунту)

- **A** Account — имя (например FacebookUL574 EC Fav).
- **B** Domain, **C** Pixel — для подстановки в Main и в campaign (Pixel = Main!C2).
- **D** Asset — бренд+оффер (для Jira и выбора листа/оффера).
- **E** Link — ссылка (Jira/tracking), не обязательна для сбора FB.

Выбор «текущего» аккаунта делается через Main (одна строка Main = один выбранный Account).

### 3.2 Main (одна строка = один аккаунт для Jira и для кампаний)

- **A** Account name — выбранный аккаунт (или формула F2 — дубль заголовка Jira).
- **B** Domain = VLOOKUP(A, Accounts!A:E, 2).
- **C** Pixel = VLOOKUP(A, Accounts!A:E, 3).
- **D** Jira ticket header = VLOOKUP(A, Accounts!A:D, 4) — заголовок из Asset.
- **E** суффикс " - EC - Media" и т.п.
- **F** = D&" "&A&E — полный заголовок Jira.
- **G, H, I** — Scope, Please duplicate…, LP to copy.
- **J** LP URL = VLOOKUP(D, LP's!A:D, 4).
- **K, L** — Requested domain, A2 (Account).
- Далее ссылки (M&B, M&N&O, и т.д.), **AF** — Final link.

Для агента 8 важно: из Main берётся **Pixel (C)** и при необходимости **Account/Domain** для подстановки в листы кампаний.

### 3.3 LP's

- **A** Brand offer (название оффера, совпадает с тем, что в Accounts.Asset / Main.D).
- **B** Country (текст " - UAE - Media" и т.д.), **C** Country 1 (код UAE, EC, …).
- **D** Lp's — URL лендинга.
- **E** Stock.

Используется для Main (Jira) и для Body (сборка URL).

### 3.4 Campaign-листы (CO Cib, GT BID, EC Favorita, …)

- Каждый лист = фиксированная пара **страна (C) + оффер (D)**. Остальное варьируется по строкам: OS, Creative, Targeting, Age, Creative number, Date, Copy.
- **Pixel** всегда из Main: **B = Main!$C$2**.
- **Campaign Name** = конкатенация C, F, D, F, E, F, I, F, L, F, J, F, K, F, M с разделителем "_".
- **Link (AO)** = AL & B (часто AL — база URL, B — Pixel).
- **Countries (AP)** и **Locales (AW)** — через VLOOKUP в **Countries** по коду страны (C или AP).
- **Interests** (таргетинг FB): в части листов есть обращение к **Interests**: по значению **J (Targeting)** берётся JSON из Interests!B (VLOOKUP(J, Interests!A:B, 2)).

То есть: для каждой строки кампании нужны C, D, E, I, J, K, L, M, N; B (Pixel) общий с Main; O и AO считаются по формулам.

### 3.5 Ad copy

- **A** Ad copy name — соответствует тому, что в колонке **Copy (N)** на campaign-листах (или начало текста).
- **B** Title, **C** Body, **D** Link Description — текст объявления для FB.
- **E** Asset, **F** Language — для отбора по офферу и языку.

Связь: N на листе кампании = имя или ключ к строке в Ad copy (Title/Body/Link Description).

### 3.6 Countries

- **A** — код страны (UAE, MX, PE, CL, CO, …), **B** — код (AE, MX, …), **C** — Language (Arabic, Spanish, …), **D** — 180.
- Используется: VLOOKUP(код_страны, Countries!A:E, 2) и VLOOKUP(…, 5) для Locales/доп. полей.

### 3.7 Video file name / Image FIle name

- **Video file name**: A = число (1730, 2730, …), B = имя файла (CAC-730_Salik_AR_1.mp4), C = .mp4. Колонка **L** (Creative number) на campaign-листе может совпадать с A или быть другим идом — нужен маппинг L → B.
- **Image FIle name**: A = номер/ид, B = A&C (например 1.jpeg), C = расширение. Аналогично L → имя файла изображения.

То есть для сборки строки под FB нужно по **L** и типу креатива (I = Image/Video) определить имя файла из этих листов.

### 3.8 Body

- Примеры строк: base URL (A) + путь (D, E, F через VLOOKUP LP's). **E** = VLOOKUP(Main!E2, LP's!B:C, 2) — код страны из LP's. Используется для формирования финальной ссылки/тела по офферу и стране.

### 3.9 Interests

- **A** = название таргетинга (Day, Reading, InvestmentIOS, InvestmentPlus, SM, …), **B** = JSON для FB API. На campaign-листе в колонке **J** указано значение A; для выгрузки в FB нужен VLOOKUP(J, Interests!A:B, 2).

### 3.10 Upload all

- Такой же формат, как у campaign-листов. Содержит несколько строк под выгрузку; Pixel = Main!C2. По сути «свод» выбранных строк из разных campaign-листов или один сценарий. Для агента 8: либо заполняем Upload all по тем же правилам, либо собираем данные из выбранного campaign-листа.

---

## 4. Поток данных для «сборки кампании под FB»

1. **Вход:** выбранный аккаунт (или строка Accounts) → Main: Account (A), Domain (B), Pixel (C), Asset (D).
2. **Выбор листа кампании:** по коду страны + Asset определяем, какой лист использовать (CO Cib, EC Favorita, …), или используем Upload all.
3. **Строки кампании:** на выбранном листе каждая строка 2..N — одна комбинация: Country (C), Asset (D), OS (E), Creative (I), Targeting (J), Age (K), Creative number (L), Date (M), Copy (N).
4. **Подстановки:**
   - B (Pixel) = Main!C2 (Pixel выбранного аккаунта).
   - Campaign Name (O) = C & "_" & D & "_" & E & "_" & I & "_" & L & "_" & J & "_" & K & "_" & M.
   - Link (AO) = AL & B (если AL задан на листе).
   - Countries/Locales = VLOOKUP по коду страны в Countries.
   - Текст объявления (Title, Body, Link Description) = по N (Copy) из Ad copy.
   - Таргетинг JSON = VLOOKUP(J, Interests!A:B, 2).
   - Имя файла креатива = по L и I из Video file name или Image FIle name.
5. **Выход:** таблица/набор строк в формате колонок FB (Campaign Name, Ad Set Name, Link, Countries, Locales, Age Min/Max, интересы JSON, Title, Body, Creative file name и т.д.) для загрузки в FB (вручную или через API).

---

## 5. Ответы на вопросы (зафиксировано)

1. **Источник строк кампании:** В каждом табе для аплоуда есть колонки: страна, оффер, таргетинг, креатив, эд копи. Вручную всё кроме креатива выбирается из списков; в Excel есть пивот/подстановки: названия видео и фото, тексты по слотам. Нужно изучить этот алгоритм и проставлять поля по нему. Решения «что открывать в какой стране» можно писать в формате **названия кампании** — из него автоматически выводить страну, оффер и остальное (связка с агентом 1 анализа).
2. **Один аккаунт или много:** Сейчас сборка под один аккаунт в Main. Желаемая схема: план аплоуда (в каких аккаунтах какие страны/офферы/креативы/таргеты/эд копи) → скрипт выгружает **один Excel**, в котором **каждый лист (таб) = один аккаунт**.
3. **Формат вывода:** Только Excel (как в п.2 — один файл, листы по аккаунтам).
4. **Link и колонки:** Обращаться к колонкам по **названию** (не по буквам), т.к. в части табов порядок колонок менялся. Link подтягивается из таба Accounts после выбора аккаунта в Main; в мультиаккаунтном выводе в каждом листе свой Link (из строки этого аккаунта в Accounts). Имя листа = идентификатор/номер аккаунта.

---

## 6. Реализуемость и как лучше сделать

**Да, так сделать можно.** Кратко:

- **Вход:** план аплоуда — список комбинаций (аккаунт, страна, оффер, таргетинг, креатив, эд копи). Варианты: ручной список (CSV/Excel/форма в коде), или вывод агента 1 в формате «название кампании» с парсингом в (страна, оффер, …), или связка с агентом 7 (согласованные связки).
- **Логика:** Для каждой пары (аккаунт, список комбинаций) создаём один лист в выходном Excel. Заголовки строки — как в текущих campaign-листах (по **названиям колонок**). По каждой комбинации: подставляем страну, оффер, таргетинг, креатив, эд копи; Pixel и Link берём из Accounts по аккаунту; Campaign Name считаем по формуле (страна_оффер_OS_креатив_номер_таргет_возраст_дата); названия видео/фото и тексты — через те же правила, что в Excel (lookup по Creative number, Ad copy name и т.д. из листов Video file name, Image FIle name, Ad copy); Countries/Locales — VLOOKUP по коду страны в Countries; Interests — по Targeting из листа Interests.
- **Выход:** один .xlsx: лист на аккаунт, имя листа = аккаунт (или номер). В каждом листе таблица готова под выгрузку в FB (те же колонки, что в Upluad FB).

**Рекомендация:** реализовать в два шага. (1) Один аккаунт: на входе один аккаунт + список комбо (страна, оффер, таргет, креатив, копи) → один лист в Excel или заполнение «Upload all». (2) Несколько аккаунтов: на входе план (список аккаунтов и для каждого список комбо) → один файл, несколько листов. Имена колонок везде определять по заголовку (первая строка), а не по индексу.

---

## 7. Пошаговая логика агента 8 (спека)

| Шаг | Действие |
|-----|----------|
| 1 | **Вход:** план аплоуда (файл или аргументы): для каждого аккаунта — список комбинаций (страна, оффер, OS, Creative, Targeting, Age, Creative number, Date, Copy). Опционально: парсинг «названия кампании» из агента 1 в эти поля. |
| 2 | Загрузить справочники из Upluad FB.xlsx: Accounts, LP's, Ad copy, Countries, Interests, Video file name, Image FIle name, Body (и при необходимости один campaign-лист как образец заголовков). |
| 3 | Для каждого аккаунта из плана: взять из Accounts Domain, Pixel, Link для этого аккаунта. |
| 4 | Для каждой комбинации (строка кампании): подставить страну, оффер, таргетинг, креатив, Copy; вычислить Campaign Name по шаблону; по Creative number и типу креатива — имя файла из Video/Image file name; по Copy — Title/Body/Link Description из Ad copy; по Targeting — JSON из Interests; по стране — Countries/Locales; Link = из Accounts для этого аккаунта. |
| 5 | **Выход:** записать один .xlsx: один лист на аккаунт (имя листа = аккаунт), в листе — строки с колонками по названиям, как в исходном шаблоне. |

---

## 8. Как запустить агента 8

**Вход:** CSV-файл плана с колонками: `account`, `country`, `asset`, `os`, `creative`, `targeting`, `age`, `creative_number`, `date`, `copy`.

**Команда (из корня fb_agents):**

```bash
python run_upload_from_plan.py <plan.csv> [-o output/upload_plan.xlsx] [-e путь/к/Upluad FB.xlsx]
```

**Выход:** один .xlsx; каждый лист — один аккаунт (имя листа = имя аккаунта, до 31 символа); строки с колонками как в шаблоне «Upload all» (Pixel, Campaign name - country, Asset, Creative, Targeting, Copy, Campaign Name, Link, Countries, Locales и т.д.). Данные подставляются из Upluad FB.xlsx: Pixel и Link из Accounts, тексты из Ad copy по полю Copy, таргетинг из Interests, Countries/Locales из Countries, имена файлов креативов из Video file name / Image FIle name.

Пример плана: **output/plan_example.csv**.

**Все колонки аплоуд-файла:** в выходном Excel заполняются все колонки из шаблона (Upload all). Источники: (1) план — обязательные поля `account`, `country`, `asset`, `os`, `creative`, `targeting`, `age`, `creative_number`, `date`, `copy` плюс любые дополнительные колонки с именами как в шаблоне (или в формате `campaign_status`, `campaign_daily_budget` и т.д.); (2) подстановки из Upluad FB.xlsx (Pixel, Link, Ad copy, Countries, Interests, Video/Image file name); (3) значения по умолчанию для полей FB (Campaign Status, Campaign Objective, Buying Type, Ad Set Name, Gender, Destination Type, Optimized Event и др.). Имена колонок в плане можно писать с пробелами (Campaign Status) или с подчёркиваниями (campaign_status).

Дальше: парсинг «названия кампании» из агента 1 в поля плана, опционально ввод плана через Excel вместо CSV.
[Trader AI] all + 25+ + OTHER#6 — Strong, 2 dep, ROI 2.04
