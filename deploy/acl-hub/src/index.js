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
import { isOwner, sameSecret } from "./auth.js";
import { clearCookie, makeSession, setCookie } from "./session.js";
import { LOGIN_HTML, PAGE_HTML } from "./ui.js";

const html = (body, status = 200, headers = {}) => new Response(body, { status,
  headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-frame-options": "DENY",
    "referrer-policy": "no-referrer", "content-security-policy": "frame-ancestors 'none'", ...headers } });

async function handleLogin(request, env) {
  if (request.method === "GET") return html(LOGIN_HTML());
  let key = "";
  try { key = String((await request.formData()).get("key") || ""); } catch (_) { /* пусто */ }
  if (!(await sameSecret(key, env.OWNER_KEY || ""))) return html(LOGIN_HTML("Wrong key"), 401);
  const value = await makeSession(env, Math.floor(Date.now() / 1000));
  return new Response(null, { status: 303, headers: { location: "/", "set-cookie": setCookie(value) } });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const { method } = request;
    if (url.pathname === "/login" && (method === "GET" || method === "POST")) return handleLogin(request, env);
    if (method === "POST" && url.pathname === "/logout") {
      return new Response(null, { status: 303, headers: { location: "/login", "set-cookie": clearCookie() } });
    }
    if (method === "GET" && url.pathname === "/") {
      if (!(await isOwner(request, env))) return new Response(null, { status: 302, headers: { location: "/login" } });
      return html(PAGE_HTML);
    }
    if (method === "POST" && url.pathname === "/v1/batch") return handleBatch(request, env);
    if (method === "POST" && url.pathname === "/v1/backfill") return handleBackfill(request, env);
    if (method === "GET" && url.pathname.startsWith("/api/")) return handleApi(request, env, url);
    return json({ error: "not found" }, 404);
  },
};
