# Трекер ACL: надёжность и предложение по структуре

Срез GitHub Issues от 9 сентября 2026. Автор анализа и самопроверки: Codex.
Исправление синхронизации и кнопка All опубликованы; новая классификация ниже —
предложение, метки и категории задач по этому документу не изменялись.

## Что обнаружено и исправлено

В журнале синхронизации обнаружено девять обновлений с `Synced 0 issues`
между обновлениями с 65 задачами. Сервер принимал пустую выборку за успех,
перезаписывал HTML и менял digest. Открытые браузеры получали новый digest и
перезагружались на пустую доску. Следующее непустое обновление возвращало задачи.
Это подтверждённый сценарий исчезновения; он не исключает отдельных проблем
со свёрнутыми категориями или фильтрами. Почему upstream иногда отдаёт пустую
выборку без ошибки процесса, пока не установлено.

Исправление сохраняет последнюю рабочую доску при неожиданном пустом ответе,
показывает задержку синхронизации и автоматически восстанавливает обновления
при следующем корректном ответе. На живом сервере во время установки получен
ещё один пустой ответ: 64 задачи сохранились; следующий запрос восстановил
успешную синхронизацию. Первоначально было 65 задач, в текущей выборке GitHub —
64; защиту не строили вокруг жёстко заданного количества. Она не доказывает
полноту любой непустой выборки.

All снимает фильтр статуса и раскрывает все категории. Выбор статуса и вручную
раскрытые карточки сохраняются после перезагрузки. Новые стили и кнопки теперь
тоже меняют digest, поэтому доходят до открытой вкладки без изменения Issues.
Новый действительно пустой проект показывает «No tasks yet». Намеренная очистка
ранее заполненной доски требует отдельного `--allow-empty`, не включённого в таймер.

Проверено: 81 тест; браузерный сценарий на 64 задачах — фильтры, All, повторная
перезагрузка, сохранение раскрытия, отказ от пустого обновления и зелёный In master.

## Что мешает вести проект сейчас

| Наблюдение в текущем срезе | Последствие |
|---|---|
| Next — 30, Later — 6, Idea — 10, Blocked — 1, In master — 17 | Нет отдельного способа увидеть начатую работу. Next смешивает очередь и возможную работу в процессе. |
| Review и Shipped — 0 | Это текущие метки, а не доказательство отсутствия выпущенных функций. Пустые статусы интерфейс скрывает. |
| Исполнителей и milestones нет ни у одной из 64 задач | По доске нельзя уверенно ответить «кто делает» и «что входит в ближайший результат». |
| Семь категорий разного смысла | Рядом стоят пользовательские сценарии, аналитика, дизайн и эксплуатация нескольких машин. |
| Many accounts, many machines содержит 15 задач | Внутри смешаны установщик, обновления, телеметрия, права, интерфейс и сам трекер. |
| У 12 задач подкатегория получилась просто Tasks | Старый Markdown-снимок не знает часть новых ID; содержимое GitHub актуально, группировка отстаёт. |
| Главный счётчик показывает Shipped 0/64 | Создаёт впечатление нулевого прогресса, хотя 17 задач отмечены In master. |
| track:dashboard/rpa/analysis/content/ops | Это область реализации, а не назначенный исполнитель. |

Например, `acl-103` всё ещё содержит вопросы про доступ и устройство BI;
назначить ей In progress только из-за старой метки Next было бы неверно.
`acl-341` зависит от стенда `acl-340`; Later скрывает конкретную зависимость.
`acl-331` и `acl-335` описывают общую проблему синхронизации библиотек, хотя
пользовательские функции у них разные. Такие связи должны быть видны явно.

## Вариант A: небольшая уборка

Сохранить семь разделов и существующие подкатегории. Переименовать Next в Backlog,
добавить In progress. Later включить в Backlog с низким приоритетом или явной
зависимостью. Idea оставить отдельным входящим списком. Добавить исполнителя,
приоритет и выборку ближайшей работы.

Плюс: мало перемещений, легко привыкнуть. Минус: смешение модулей и сценариев
остаётся, а перенос в новый проект всё ещё требует разбирать структуру ACL.
Подойдёт, если важнее быстро привести в порядок только этот проект.

## Вариант B: модули + единый процесс — рекомендую

Основная иерархия: **проект → модуль → задача**. Большие инициативы связывают
задачи из разных модулей через родительскую задачу или milestone. Маленькие
пункты проверки остаются чекбоксами; работа со своим исполнителем, PR и
критерием приёмки получает отдельный Issue. GitHub поддерживает метки,
связанные подзадачи и зависимости: [официальное руководство](https://docs.github.com/en/issues/tracking-your-work-with-issues/learning-about-issues/planning-and-tracking-work-for-your-team-or-project).

### Статусы

Основной путь: **Backlog → In progress → Review → In master → Done**.

| Статус | Условие входа | Условие следующего перехода |
|---|---|---|
| Backlog | Задача принята, описан ожидаемый результат, работа ещё не начата | Исполнитель действительно взял её в работу |
| In progress | Есть исполнитель и активная работа | Подготовлен проверяемый результат/PR |
| Review | Изменения готовы к проверке; есть PR и результаты проверок | Вся работа по задаче проверена и слита в основную ветку |
| In master | Полный результат задачи находится в master | Подтверждены нужное развёртывание и проверка в целевой среде |
| Done | Результат доступен пользователям и принят | При повторном дефекте открывается связанная задача или задача явно возвращается в работу |

Для задач без кода, например получения доступа, после проверки результата
допустим переход прямо в Done. В шаблоне название In master следует выводить
как «In main» или «Merged» в зависимости от основной ветки проекта.

**Ideas / Inbox** — отдельная вкладка для предложений до принятия в работу.
Я бы сохранил различие с Backlog: «может быть полезно» и «мы решили делать» —
разные обещания. Технически на первом этапе можно сохранить `status:idea`;
основной рабочий вид просто не смешивает его с принятой очередью. Если команда
этого различия не использует, более простой вариант — единый Backlog с меткой
`type:idea` до разбора. Не нужны одновременно Idea и Later как два неясных склада.

**Blocked** — дополнительный признак с причиной и зависимостью, а не отдельный
этап. Тогда видно «In progress, blocked by #22», и исходный этап не теряется.
Для `acl-111` можно предложить Backlog + Blocked до получения нужного доступа;
если исполнитель подтвердит, что работа началась, будет In progress + Blocked.

«Что делать следующим» лучше показывать сохранённой выборкой Focus: высокий
приоритет, текущий milestone и отсутствие блокировки. Дополнительный статус
Ready имеет смысл лишь когда действительно появится отдельная очередь готовых
к старту задач; сейчас он усложнит процесс без достаточной пользы.

### Модули ACL

Ниже предварительное разнесение всех 64 задач; оно не является оценкой их
готовности и не меняет уже назначенные статусы.

| Модуль | Содержание | Задач |
|---|---|---:|
| Accounts & Access — Аккаунты и доступ | Аккаунты, страны, профили, роли, подключение новых аккаунтов, интеграция с Jira | 16 |
| Data & Reporting — Данные и отчёты | BI/API/RPA-источники метрик, периоды, сверка данных, аналитические рекомендации | 8 |
| Campaigns — Кампании | Билдер, таргетинг, публикация, управление объявлениями, надёжность RPA-сценариев | 19 |
| Landing Pages & Tracking — Лендинги и трекинг | Размещение LP, QA, домены, Keitaro и ссылки | 4 |
| Creatives & Content — Креативы и контент | Анализ материалов, тексты, библиотека заголовков, брифы, генерация | 8 |
| Platform & Operations — Платформа и эксплуатация | Установка, обновления, телеметрия, общие механизмы и инструменты проекта | 9 |

Design станет типом работы/меткой UX у соответствующего модуля. Исправление
карточки аккаунта живёт в Accounts, а не в отдельном контейнере для любого
интерфейса. RPA остаётся способом реализации: модуль определяется пользовательским
результатом. Синхронизация библиотек может иметь общий родительский Issue в Platform
и дочерние задачи в Campaigns и Creatives — дублировать задачи не нужно.

Текущие длинные подкатегории я бы заменил необязательными родительскими задачами
с конечным результатом. Например, «Новый аккаунт готов к запуску» объединит
Accounts, Landing Pages и Campaigns, не меняя постоянную структуру модулей.

### Минимум полей и представлений

- Одна основная метка `module:*`, одна `status:*`; номера Issue и ACL ID сохраняются.
- Тип: bug / feature / improvement / research / operations. Это отдельное измерение.
- Приоритет: high / normal / low. Срок — только если есть реальное обязательство.
- Ответственный человек и, для агентной работы, ссылка на PR/задачу агента в описании.
  Название модели не заменяет ответственного и не является GitHub-assignee без аккаунта.
- Критерий готовности, зависимости, связанные PR; milestone для ближайшего результата.

Основной вид — модули. Дополнительные виды над теми же Issues: Focus, In progress,
Review, Blocked, In master / Awaiting release, Ideas и All. В дальнейшем полезны
поиск по ID/тексту и «Свернуть всё». Счётчики должны разделять задачи в master и
выпущенные, а не выдавать одно за другое. У фильтра всегда виден выбранный режим;
пустая выборка объясняется текстом и предлагает All.

## Как переносить трекер в новые проекты

Сейчас перенос ограничивают зашитые ACL-заголовок и ID, числовые area-метки,
Markdown-снимок с историей проекта, фиксированный домен и зависимость VPS от
GitHub CLI внутри контейнера OpenClaw. Параметр `--repository` уже существует,
но одного его недостаточно для готового универсального шаблона.

Предлагаю отдельный переносимый каталог трекера с версионированным движком и
небольшим конфигом проекта. Настройки: название, repository, основная ветка,
необязательный префикс ID, модули и порядок, статусы и подписи, палитра, URL,
период обновления. Секреты и история задач в шаблон не входят. Новый репозиторий
получает чистую конфигурацию; сами задачи остаются в его GitHub Issues.

Пример **будущего** конфига (текущий код такой формат ещё не читает):

```json
{
  "project": {"name": "New Project", "repository": "owner/repo", "base_branch": "main"},
  "modules": [
    {"id": "accounts", "name": "Accounts & Access"},
    {"id": "campaigns", "name": "Campaigns"}
  ],
  "workflow": ["backlog", "in_progress", "review", "merged", "done"],
  "intake": "idea",
  "refresh_seconds": 60
}
```

В комплект входят шаблоны Issue/PR, установка нужных меток, пример конфигурации
сервера, инструкции AGENTS/CLAUDE и проверка перехода в merged. Обновление движка
отделяется от данных проекта, чтобы исправление исчезающих задач не приходилось
вручную повторять в каждом форке. При появлении второго проекта лучше извлечь
ядро из ACL в отдельный шаблон, не копировать целиком репозиторий лаунчера.
GitHub template repositories позволяют повторно использовать структуру и файлы:
[документация](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-template-repository).
Метки и исходные Issues следует настраивать отдельно при инициализации проекта.

Для независимого post-merge обновления статусов нужен отдельный обработчик
с подтверждённой связью PR → Issue и проверкой полного завершения. Правило в
инструкциях агента уже есть, но серверный обработчик merge пока не установлен.
Его не следует строить как «любой закрытый PR завершает любую упомянутую задачу».

## Порядок внедрения

1. Исправление исчезновения и All — уже опубликованы.
2. Выбрать вариант A или B. Для B утвердить названия модулей и модель Ideas/Blocked.
3. Подготовить код конфигурации и новые представления на сохранённой копии Issues.
4. Получить от исполнителей фактическую начатую работу. Предварительно Next + Later
   дают 36 задач Backlog, но это не основание автоматически менять рабочие статусы.
5. Сформировать конкретный план старые → новые метки для каждого Issue. Проверить
   сохранность 64 ID, ссылок, тел, чекбоксов, PR и возможность повторного запуска/отката.
6. Применить согласованное разнесение и проверить совпадение GitHub и UI. Старые
   метки удалять только после перехода всех потребителей на новую схему.
7. На втором проекте проверить чистую установку переносимого каталога и обновление
   его движка. Этот шаг лучше проверит универсальность, чем абстрактные настройки.

## Предварительное распределение задач по модулям

Этот список подготовлен по текущим заголовкам, содержимому Issues и функциям
проекта. Пограничные задачи синхронизации библиотек и телеметрии требуют общего
родителя/зависимостей. Статусы, исполнители и приоритеты здесь не назначаются.

### Accounts & Access

| Issue | Задача |
|---|---|
| [#12 · acl-102](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/12) | Account map: spend, leads, CPL, CPA, CR by country |
| [#28 · acl-121](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/28) | Read `account_status` and `disable_reason` |
| [#29 · acl-123](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/29) | "Account Disabled" — say what a person must do |
| [#30 · acl-131](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/30) | Status column also fed by the API route |
| [#31 · acl-132](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/31) | Post status changes to WhatsApp — KLD |
| [#32 · acl-133](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/32) | Post Inhouse issues to the internal chat |
| [#33 · acl-201](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/33) | Read new-account messages from the group |
| [#34 · acl-202](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/34) | Turn the message into a row in the account map |
| [#69 · acl-601](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/69) | The team lead's permission is a flag, not a mechanism |
| [#70 · acl-602](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/70) | A buyer sees their own goals, the lead sees everyone's |
| [#71 · acl-141](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/71) | Dialogs that look like the platform |
| [#72 · acl-142](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/72) | Adding and removing an account, made easy |
| [#73 · acl-144](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/73) | Finding a profile's folder costs ten seconds, every time |
| [#74 · acl-145](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/74) | The map joins the workbook by name, and the names are drifting |
| [#75 · acl-143](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/75) | Take from Jira what it already knows about an account |
| [#76 · acl-703](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/76) | Eleven doors into one country, and four lead to the same room |

### Data & Reporting

| Issue | Задача |
|---|---|
| [#13 · acl-103](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/13) | Read spend from the company BI, not the workbook |
| [#14 · acl-105](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/14) | Timeframe that survives real use |
| [#15 · acl-107](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/15) | Read cost through the token — prove it on one test account |
| [#16 · acl-108](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/16) | Read cost through the RPA |
| [#17 · acl-109](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/17) | The two routes must agree, and disagreement must be visible |
| [#18 · acl-110](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/18) | Compare with another period |
| [#19 · acl-106](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/19) | An empty period should say why it is empty |
| [#54 · acl-412](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/54) | Close the loop: recommendation becomes an action |

### Campaigns

| Issue | Задача |
|---|---|
| [#21 · acl-111](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/21) | Open and close through the token |
| [#22 · acl-112](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/22) | Request `ads_management` on the app |
| [#23 · acl-113](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/23) | Open and close through the RPA |
| [#24 · acl-116](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/24) | One switch chooses the route |
| [#25 · acl-117](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/25) | Every open and close leaves a record |
| [#26 · acl-114](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/26) | Stop-loss rule written at campaign creation |
| [#27 · acl-115](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/27) | Try Meta's official MCP on one test account |
| [#39 · acl-303](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/39) | Yotam's second supervised run |
| [#40 · acl-348](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/40) | The builder belongs to an account, not to a recommendation |
| [#41 · acl-347](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/41) | A missing tracking link warns before upload, it does not forbid |
| [#42 · acl-342](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/42) | Publishing no longer needs `Upload FB.xlsx` |
| [#43 · acl-343](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/43) | An empty tracking link must warn, not block |
| [#44 · acl-344](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/44) | Cover the decisions a run is lost on |
| [#45 · acl-340](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/45) | A bench of recorded Ads Manager pages for `meta_dom` |
| [#46 · acl-349](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/46) | A test that blinks teaches people not to look |
| [#47 · acl-341](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/47) | `recipe_player` is 1,064 statements at zero |
| [#48 · acl-333](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/48) | Exporting to the spreadsheet can still overwrite someone |
| [#49 · acl-331](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/49) | A target added on one machine does not reach the others |
| [#52 · acl-336](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/52) | The builder takes its ad titles from the library |

### Landing Pages & Tracking

| Issue | Задача |
|---|---|
| [#35 · acl-211](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/35) | Copy the landing page to the account's domain |
| [#36 · acl-212](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/36) | Limited QA checklist before the page goes live |
| [#37 · acl-222](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/37) | Create the Keitaro link from ACL |
| [#38 · acl-223](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/38) | Tracking-domain management |

### Creatives & Content

| Issue | Задача |
|---|---|
| [#55 · acl-423](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/55) | Transcribe video, not one still frame |
| [#56 · acl-424](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/56) | Everything into one prompt for new materials |
| [#57 · acl-501](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/57) | Generate headlines from the winners |
| [#58 · acl-512](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/58) | The brief carries account and country context |
| [#59 · acl-521](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/59) | Produce the material |
| [#60 · acl-522](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/60) | Hand the material to campaign upload |
| [#50 · acl-334](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/50) | Ad titles are a section, not a tab in someone's spreadsheet |
| [#51 · acl-335](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/51) | An ad text added on one machine does not reach the others |

### Platform & Operations

| Issue | Задача |
|---|---|
| [#61 · acl-304](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/61) | Install in one file |
| [#62 · acl-322](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/62) | Selector fixes travel back automatically |
| [#63 · acl-312](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/63) | Telemetry on by default |
| [#64 · acl-313](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/64) | A changed account layout teaches every machine |
| [#65 · acl-650](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/65) | One store, two views: Obsidian and a wall |
| [#66 · acl-653](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/66) | Next to "In review" — the branch and the commit |
| [#67 · acl-654](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/67) | "In master" is a state of its own |
| [#68 · acl-655](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/68) | The plan itself has a history |
| [#53 · acl-402](https://github.com/MojoTheApe/adopy-campaing-launcher/issues/53) | Turn a repeated failure into a rule |
