# ACL Telemetry Hub, часть 3: Telegram-бот

Spec: `docs/superpowers/specs/2026-11-17-acl-telemetry-hub-design.md` §4.6. База: `work/acl-hub-part2` (Worker развёрнут с неё).

- Миграция `0002_alerts.sql`: `alert_log` (антиспам), `mutes`, `bot_state`.
- `src/telegram.js`: отправка (HTML, ≤ 4000 символов), чаты из `TG_ALLOWED_CHATS`; без токена/чатов — молчит.
- `src/alerts.js`: `notifyFailedRuns` — алерт о новом сбое кампании сразу при приёме (`ctx.waitUntil`, после ответа машине), не чаще раза в 30 мин на отпечаток × машину, с числом придержанных; тишина `/mute`; перенос истории не алертит. Cron `*/5`: задание без конца > 45 мин — один алерт. Cron `0 5 * * *`: дайджест за вчера по байерам + топ сбоев + машины без сигнала > 24 ч, раз в сутки.
- `src/bot.js`: `POST /tg/webhook` — секрет `X-Telegram-Bot-Api-Secret-Token`, только чаты владельца; `/today`, `/yesterday`, `/failures`, `/mute <id> [h]`, `/unmute <id>`, иначе справка.
- `src/ui.js`: `/#run=<id>` открывает карточку прогона (ссылка из алерта).
- Тесты: `test/telegram.test.js` (15) + схема + ui. `npm test`.

Решения: «машина молчит» как отдельный алерт не делается — пока клиент шлёт heartbeat раз в час, он давал бы ложные тревоги; вместо него «зависшее задание» и строка «Quiet > 24 h» в дайджесте. Дайджест в 05:00 UTC = 07:00 по Израилю зимой (летом 08:00).
