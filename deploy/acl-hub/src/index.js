// Хаб телеметрии ACL: роутер Worker.
//
// POST /v1/batch    — машина кладёт сжатую пачку событий (токен библиотеки команды).
// POST /v1/backfill — владелец переносит историю (OWNER_KEY).
// GET  /api/*       — владелец читает события и выводы (OWNER_KEY).
//
// Привязка: D1 `DB`. Секрет: `OWNER_KEY`. Переменная: `LIBRARY_REPO`.

export const json = (obj, status = 200, headers = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", ...headers } });

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    return json({ error: "not found" }, 404);
  },
};
