---
kind: runbook
name: agent10-recorded-flow-runbook
created: 2026-07-23
status: draft
version: 1
tags:
  - rpa
  - agent10
  - runbook
  - troubleshooting
related:
  - agent10-post-conversion-location-handoff.md
---

# Agent 10 — шаги открытия кампании и реакция системы

Справочник по `run_recorded_campaign_flow()` (`src/fb_ads_rpa/meta_dom.py`) —
что происходит на каждом шаге, что может пойти не так, и как система на
это реагирует. Пишется как runbook для диагностики живых сбоев: если
прогон упал на шаге X, ищи X в таблице ниже и смотри, что делать.

**Статус верификации:** шаги 1–13 (Create → Locations) проверены живыми
прогонами сегодня много раз. Шаги 14–20 (Detailed targeting → draft
hand-off) дописаны 2026-07-23 вручную из полной codegen-записи
(`output/codegen_conversion_location_sanitized.py`), **без живой
проверки** — аккаунт был недоступен для тестов (коллега работал в том
же браузере + общая нестабильность аккаунта после 60+ прогонов за день).
Эти шаги нужно проверить живым прогоном в первую очередь, начиная
именно с них.

## Общие механизмы, работающие на каждом шаге

Прежде чем читать таблицу по шагам — три сквозных механизма, которые
оборачивают почти каждое действие:

| Механизм | Что делает | Почему |
|---|---|---|
| `_wait_loading()` | Ждёт (до 30 сек), пока со страницы исчезнет любой текст, содержащий "Loading" | Meta показывает полноэкранный оверлей после почти каждого перехода (Create → визард, Continue → следующий шаг), и **периодически сам по себе**, даже без наших действий — если под ним кликнуть, клик пропадает без ошибки. Формулировка текста плавает ("Loading...", "Loading creation" — видели живьём оба варианта) |
| `_click()` / `_fill()` | До 5 попыток, между попытками — `_wait_loading()` + пауза 0.6с. Клик идёт через настоящий CDP-клик мышью (`_cdp_mouse_click`), а не через `element.click()` в JS | Синтетический JS-клик на кастомных React-компонентах Meta часто не срабатывает (подтверждено на нескольких контролах: сайдбар-дерево, дропдаун Conversion location) — реальный клик мышью через CDP надёжнее |
| `_shot(label)` | Скриншот через CDP (`Page.captureScreenshot`) с префиксом `recorded_` в `output/rpa_screenshots/` | Единственный способ диагностировать сбой без доступа к живому браузеру в момент падения |

**Важный урок сегодняшнего дня:** если два скриншота с разных шагов
оказались побайтово идентичны (проверяется через `md5sum`) — это
означает, что страница **реально не менялась** между этими шагами (не
баг самого скрипта скриншотов), и почти всегда означает, что кто-то
(человек) вручную взаимодействовал с тем же браузером параллельно со
скриптом, либо профиль Multilogin был перезапущен и Playwright
подключился к старой/иной вкладке.

## Пошаговая таблица

| # | Шаг (fail-код) | Что происходит | Селектор | Известные живые сбои | Реакция системы |
|---|---|---|---|---|---|
| 1 | `create` | Клик "Create" в списке кампаний | `role=button, name="Create"` | Промах клика, если "Loading..." висит дольше обычного (аккаунт с 30+ черновиками отвечает медленнее) | 5 попыток; если все неудачны — возврат `"create"`, скриншот `fail_create` не делается (падает до первого `_shot`) |
| 2 | — (best-effort) | Клик "Manual campaign setup", если появился выбор Guided/Manual | `get_by_text("Manual campaign setup")` | Не появляется в части сессий — это нормально | Не блокирующий; исключение проглатывается |
| 3 | `objective_leads` | Клик по заголовку "Leads" (фокус), затем чек радио "Leads" | `role=heading`→`role=radio, name="Leads"` | Живо наблюдали: клик регистрируется как **hover-preview** (справа появляется панель "Good for..."), но сам радио не отмечается — тогда следующий Continue недоступен | 5 попыток на радио; при неудаче — `fail_objective_leads` + `"objective_leads"` |
| 4 | `objective_continue` | Клик "Continue" | `role=button, name="Continue"` | Кнопка задизейблена, если шаг 3 не прошёл по-настоящему (см. выше) — тогда клик просто ничего не делает, скрипт зависает в retry на 5 попыток | `fail_objective_continue` + `"objective_continue"` |
| 5 | `campaign_name` | Заполнение имени кампании | `get_by_placeholder("Enter your campaign name here")` — **нет реального ARIA label** | — | `fail_campaign_name` |
| 6 | `daily_budget` | Заполнение бюджета | 3 фоллбэка подряд: `role=spinbutton name="Budget"` → `role=textbox name="Budget"` → `get_by_placeholder("Please enter amount")` | Реальный accessible name поля — не то, что ожидаешь на глаз | `fail_daily_budget` |
| 7 | `next_to_adset` | Клик "Next" (Campaign → Ad set) | `role=button, name="Next"` (role наследуется от предка, не с самого текстового узла) | — | `fail_next_to_adset` |
| 8 | `adset_name` | Заполнение имени ad set | `get_by_placeholder("Enter your ad set name here...")` | Повторное использование одного имени во многих прогонах → Meta показывает автокомплит из истории имён, который перекрывает Conversion location ниже. **Не нажимать Escape** (закрывает весь визард) — код кликает по нейтральному заголовку "Ad set name", чтобы убрать фокус | `fail_adset_name` |
| 9 | `conversion_location_open` | Открытие дропдауна Conversion location | `click_conversion_location_dropdown()` — три стратегии внутри (value-text match → JS click → label-walk) | — | `fail_conversion_location_open` |
| 10 | `conversion_location_menu` | Ожидание, что меню реально открылось | Ждём появления `role=radio, name="Website"` (не `[role=listbox]` — этот кастомный дропдаун не использует стандартные роли) | — | `fail_conversion_location_menu` |
| 11 | `conversion_website` / `conversion_website_verify` | Клик радио "Website" (Single), затем проверка | Прямой клик радио; фоллбэк `pick_website_conversion_option()`. Проверка — **собственный текстовый узел** элемента равен ровно "Website" (не "Website and instant forms") | Меню авто-схлопывается сразу после выбора — старый локатор радио уже не резолвится, стандартная `is_website_conversion_selected()` может зацепить текст зелёного баннера-рекомендации вместо реального контрола | `fail_conversion_website` / `fail_conversion_website_verify` |
| 12 | `conversion_event_open` / `conversion_event_pick` / `conversion_event_verify` | Открыть селектор события → ввести "Lead" в поиск → выбрать → проверить | Клик по "Select an event or search by" → `page.keyboard.type("Lead")` → выбор опции по тексту → проверка тем же own-text паттерном, что и Website | На аккаунте без истории событий списки "Active/Inactive events" пустые по умолчанию — **обязательно печатать "Lead" в поиск**, иначе выбирать нечего. `is_lead_conversion_event_selected()` (старая функция) даёт ложный негатив, читая соседнее поле Dataset — **не использовать**, использовать `_conversion_event_is_lead()` (own-text) | Три отдельных fail-кода на каждую под-стадию |
| 13 | `audience_locations_open` (soft) → `audience_location_search` → `audience_location_pick` → `audience_location_remove_default` | Ждать секцию Locations (до 16 сек, 8×2с) → клик "Show more controls" (если свёрнуто) → клик "Further limit the reach of your ads" → выбрать нижнее радио с тем же названием → Continue (если появится) → заполнить поиск локации → выбрать результат "Country" → удалить дефолтный "United States" | `scroll_to_section` + комбинация text/role селекторов | **Самый нестабильный сегмент сегодня.** Секция Locations иногда рендерится полноценной панелью сразу, иногда свёрнута за "Show more controls", а иногда открытый дропдаун Conversion location зависает и блокирует всё ниже (см. общий урок про md5-совпадающие скриншоты — почти всегда посторонний человек/сессия) | Открытие Locations — best-effort (не хард-фейл сам по себе); дальше — 4 хард-гейта с отдельными кодами |
| 14 | `detailed_targeting_fill` | Выключить "Use as a suggestion" (если включено) → открыть "Detailed targeting" → заполнить интересы в `#DETAILED` | `#DETAILED` — реальный стабильный id (не хэш-класс) | **Не проверено живьём.** В записи это `fill()` одной строкой через запятую в поле без ARIA-label — предположение, что поле принимает так; не факт, что реально создаст отдельные чипы интересов | `fail_detailed_targeting_fill` |
| 15 | `languages` | Открыть "Languages" → ввести язык в `#language_targeting_tokenizer` → выбрать опцию | По умолчанию захардкожен "Spanish (All)" (параметр `language`) | **Не проверено живьём** | `fail_languages` |
| 16 | (best-effort, без хард-фейла) | Placements: "Show more settings" → "Platforms" → снять Messenger/Audience Network/Threads/Allow limited spending → "Placement controls" → снять Search results → раскрыть Stream/Feed списки → снять/включить конкретные плейсменты Reels/Feed/Stories/Notifications | Комбинация ролей + 2 хэш-класс чекбокса из записи заменены на текстовый поиск "Story" | **Не проверено живьём.** Каждый чекбокс — try/except по отдельности, ни один не блокирует остальные | Нет хард-фейла — если что-то не найдётся, просто пропускается |
| 17 | `next_to_ad` | Клик "Next" (Ad set → Ad) | `role=button, name="Next"` | — | `fail_next_to_ad` |
| 18 | `ad_name` | Заполнить имя объявления | `get_by_placeholder("Enter your ad name here...")`; если параметр `ad_name` пуст — дефолт `f"{campaign_name} Ad"` | **Не проверено живьём** | `fail_ad_name` |
| 19 | `website_url` (только если параметр передан) | Заполнить Website URL | `role=textbox, name="Website URL"` | **Не проверено живьём**. В тестовых данных сегодня (`dry_run_slots_cr_navtest.json`) поле `link` у кампании пустое — в следующем тесте этот шаг **будет пропущен** (gated `if website_url:`) | `fail_website_url` |
| 20 | `creative_setup` (только если параметр передан) | "Set up creative" → тип "Video ad" → выбрать файл по имени → Next×5 → Done | Поиск кнопки по имени файла (`creative_filename`) | **Не проверено живьём. Самый рискованный шаг** — в записи выбор шёл по конкретному "2046.mp4", наше обобщение ищет кнопку по регэкспу с именем файла из `AdDraft.creative_filename`; в тестовых данных сегодня это поле тоже пустое → **шаг будет пропущен** в следующем тесте | `fail_creative_setup` |
| 21 | `headline` / (primary_text без хард-фейла) | Заполнить Primary text и Headline | `role=textbox, name="Headline"` / `name~="Primary text"` с фоллбэком на placeholder | **Не проверено живьём**; в записи это был выбор из готовых предложенных вариантов (chips), а не заполнение поля — наша версия сознательно заполняет реальные поля из `AdDraft.ad_title`/`ad_body`, чтобы обобщалось на любой креатив, а не копировало текст одного теста | `fail_headline` (только для headline; primary_text — best-effort) |
| 22 | `success_draft_ready` | Закрыть панель (если есть) → перейти на вкладку "Campaigns" | `get_by_label("Close")` / `role=tab, name~="Campaigns"` | — | Всегда best-effort; функция возвращает `""` (успех) независимо от результата этого шага — **Publish никогда не нажимается**, это стандартное ограничение Agent 10 |

## Что проверить первым делом при следующем живом тесте

Учитывая, что шаги 14–22 не проверены, а тестовые данные сегодня
(`output/dry_run_slots_cr_navtest.json`) не содержат `link`/`copy`/
`creative_file` — при следующем прогоне реально исполнятся и потребуют
проверки:

1. Шаг 14 (Detailed targeting) — единственный из непроверенных шагов,
   который точно исполнится (`targeting` в тестовых слотах непустой).
2. Шаг 15 (Languages) — исполнится всегда (дефолт "Spanish (All)").
3. Шаг 16 (Placements) — исполнится всегда, но не хард-фейлит.
4. Шаги 17-18 (Next → Ad name) — исполнятся.
5. Шаги 19-21 (Website URL / Creative / Headline-Primary text) —
   **будут пропущены** в текущих тестовых данных; чтобы проверить их,
   нужно добавить `link`, `copy`, `creative_file` в тестовый JSON.
6. Шаг 22 (draft hand-off) — исполнится, независимо от прочего.

#rpa #agent10 #runbook #troubleshooting
