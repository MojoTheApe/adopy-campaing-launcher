// Хаб телеметрии ACL: роутер Worker.
//
// POST /v1/batch    — машина кладёт сжатую пачку событий (токен библиотеки команды).
// POST /v1/backfill — владелец переносит историю (OWNER_KEY).
// GET  /api/*       — владелец читает события и выводы (OWNER_KEY).
//
// Привязка: D1 `DB`. Секрет: `OWNER_KEY`. Переменная: `LIBRARY_REPO`.
import { json } from "./respond.js";
import { handleBackfill, handleBatch } from "./ingest.js";
import { handleApi } from "./api.js";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const { method } = request;
    if (method === "POST" && url.pathname === "/v1/batch") return handleBatch(request, env);
    if (method === "POST" && url.pathname === "/v1/backfill") return handleBackfill(request, env);
    if (method === "GET" && url.pathname.startsWith("/api/")) return handleApi(request, env, url);
    return json({ error: "not found" }, 404);
  },
};
