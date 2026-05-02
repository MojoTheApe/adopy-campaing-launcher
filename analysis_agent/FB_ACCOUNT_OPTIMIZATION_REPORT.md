# Отчёт по оптимизации аккаунтов Facebook (MX, GT, EC, UAE)

Данные: выгрузка из Excel (`Country`, `Account Alias`, метрики по адсетам). Сгенерированный сырой прогон агента анализа: [`_account_optimization_run.md`](_account_optimization_run.md).

**Команда воспроизведения** (из корня `fb_agents`):

```powershell
python run.py --country all --top 2 --with-context `
  --accounts "FacebookMKTO1211,Facebook662,Facebook_8661,FacebookUL643,FacebookMKTO1203,Facebook_6218,FacebookMKTO1239,FacebookMKTO1235,FacebookMKTO1227,FacebookMKTO1215" `
  --focus-countries "Mexico,Guatemala,Ecuador,United Arab Emirates" `
  -o analysis_agent/_account_optimization_run.md
```

Фильтр `--focus-countries` обязателен: в файле одни и те же `Account Alias` могут встречаться с разными значениями колонки `Country` (другие гео).

---

## Соответствие ярлык → `Account Alias` в Excel

| Ваш ярлык | Страна (колонка `Country`) | `Account Alias` |
|-----------|----------------------------|-----------------|
| FacebookMKTO1211 MX | Mexico | FacebookMKTO1211 |
| Facebook662 MX | Mexico | Facebook662 |
| Facebook_8661 GT | Guatemala | Facebook_8661 |
| FacebookUL643 GT | Guatemala | FacebookUL643 |
| FacebookMKTO1203 EC | Ecuador | FacebookMKTO1203 |
| Facebook_6218 UAE | United Arab Emirates | Facebook_6218 |
| FacebookMKTO1239 UAE | United Arab Emirates | FacebookMKTO1239 |
| FacebookMKTO1235 UAE | United Arab Emirates | FacebookMKTO1235 |
| FacebookMKTO1227 UAE | United Arab Emirates | FacebookMKTO1227 |
| FacebookMKTO1215 UAE | United Arab Emirates | FacebookMKTO1215 |

*Тот же **Facebook662** также используется в UAE (отдельно от MX).*

---

## Mexico

**Аккаунты в скоупе:** FacebookMKTO1211, Facebook662.

**Закрыть / срезать:** явных кандидатов на полное закрытие нет — оба аккаунта дают FTD и положительный Net. Ниже окупаемость и выше CPA у **Facebook662** (ROI ~0.15, CPA ~$1868 при 4 FTD) vs **FacebookMKTO1211** (ROI ~0.24, CPA ~$818 при 3 FTD). Имеет смысл **сместить часть бюджета** с 662 на 1211, если цель — эффективность, а не только объём.

**Открыть / масштабировать (оффер Amazon):**

1. **ios + Day + 40 + ccblc - pro** (PRO) — 2 FTD, сильная связка; в данных топ по объёму FTD на **Facebook662** (см. блок Recommendation в `_account_optimization_run.md`).
2. **all + Mix4 + 40 + CCblc - pro** (PRO) — 1 FTD, лучший ROI среди рекомендованных; топ-аккаунт по комбо — **FacebookMKTO1211**.

Практика: дублировать победные связки на аккаунт с лучшей экономикой (1211), сохранив на 662 то, что стабильно даёт Day-стек.

---

## Guatemala

**Аккаунты в скоупе:** Facebook_8661, FacebookUL643.

**Закрыть / срезать:**

- **Facebook_8661** — **0 FTD**, ~$730 спенда, Net $0. Логично **остановить или сильно срезать** кампании на этом аккаунте в GT до смены оффера/креатива/лендинга.
- **Banco Industrial** по стране: 0 FTD при ~$1262 спенда — не вкладывать дальше в текущем виде (см. Offer comparison).

**Открыть / масштабировать:**

- На **FacebookUL643** есть 1 FTD по **AI Trader** при положительном Net на уровне страны — **аккуратно масштабировать** те адсеты, что дали FTD; параллельно тестировать новые связки.
- В отчёте **нет** квалифицированных комбо Strong/Interesting для Guatemala (`No qualifying combos …`), поэтому новые запуски — из гипотез и тестов, а не из автоматического топа комбо.

---

## Ecuador

**Аккаунты в скоупе:** FacebookMKTO1203 (единственный в списке для EC).

**Закрыть / срезать:**

- Аккаунт один — **не «закрывать» целиком**, но **срезать** направления без депозитов: **AI Trader** и **Al Trading** (0 FTD при суммарно ~$3.8k спенда), пока нет обновлённых крео/лендингов.
- **Favorita** — основной вклад в FTD и Net; спенд и результат сосредоточены здесь.

**Открыть / масштабировать:**

- **Favorita:** связка **all + trading + 35 + 3509 - the app** (две агрегированные «сильные» линии в комбо-отчёте) — **масштабировать** на **FacebookMKTO1203**; TopAccount в рекомендации совпадает с этим аккаунтом.

---

## United Arab Emirates

**Аккаунты в скоупе:** Facebook_6218, FacebookMKTO1239, FacebookMKTO1235, FacebookMKTO1227, FacebookMKTO1215, Facebook662.

**Закрыть / срезать:**

- **Facebook662** (UAE): **0 FTD**, ~$1.36k спенда — **пауза или минимальный тестовый бюджет** до новой стратегии (в MX этот же alias работает иначе — решения по гео раздельно).
- **FacebookMKTO1215**: **0 FTD**, ~$1.57k — **срезать / остановить** текущие связки.
- **FacebookMKTO1239**, **FacebookMKTO1235**, **Facebook_6218**: **0 FTD** при заметном или тестовом спенде — **не разгонять**; либо пауза, либо только жёстко лимитированные тесты.
- По офферам: **AA Acady, AI Trader, ChatGPTPlatform, Finance Eagle, Trader AI** — 0 FTD при спенде; не открывать широко в текущем виде.

**Открыть / масштабировать:**

- Единственный устойчивый сигнал на выбранных аккаунтах — **FacebookMKTO1227** с FTD по **Adnoc**.
- Рекомендация из прогона: **ios + HV + 25 + 1267 2 - golden** на **FacebookMKTO1227** — **масштабировать** в рамках оффера Adnoc при контроле CPA.

---

## Примечание по методике

- «Закрыть» = остановка или резкое снижение бюджета на аккаунте/оффере при **0 FTD** и ощутимом спенде или при явно худшей экономике среди парных аккаунтов.
- «Открыть» = связки из секции **Recommendation** в `_account_optimization_run.md` (классы Strong/Interesting) плюс поля **TopAccount** / **TopCM** для привязки к аккаунту.
- При обновлении Excel перезапустите команду выше и сверьте цифры.
