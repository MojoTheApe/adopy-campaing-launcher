// Общий журнал действий ACL (ACL-2347): приём пачек от машин и выдача владельцу.
//
// POST /v1/batch    — машина кладёт сжатую пачку (токен библиотеки команды).
// GET  /v1/list     — владелец: список ключей объектов (READ_KEY).
// GET  /v1/object   — владелец: тело одного объекта (READ_KEY).
//
// Привязка: KV `AUTH` (бесплатный тариф, карта не нужна): записи о токенах под `tok:`,
// пачки под `v1/`. Секрет: `READ_KEY`. Переменная: `LIBRARY_REPO`.
// Лимит бесплатного KV: 1000 записей в сутки, поэтому машины отправляют пачки раз в час.
// Тело пачки Worker не распаковывает и не разбирает (экономит лимит CPU); единственная
// граница чистки — клиент, аудит делает скрипт владельца. Токены не логируются.

const MAX_BODY = 1024 * 1024;
const FRESH_MS = 60 * 60 * 1000;          // запись о токене свежа 1 час
const GRACE_MS = 24 * 60 * 60 * 1000;     // при падении GitHub верим записи до 24 часов
const MACHINE_RE = /^[A-Za-z0-9_-]{1,64}$/;
const BATCH_RE = /^[A-Za-z0-9-]{1,128}$/;
const DEFAULT_REPO = "MojoTheApe/acl-lib";
// Пачка живёт в KV 90 дней: KV — транспорт, архив — SQLite владельца (бесплатный лимит 1 ГБ).
const BATCH_TTL_S = 90 * 24 * 60 * 60;

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

function bearer(request) {
  const m = /^Bearer\s+(\S+)$/.exec(request.headers.get("Authorization") || "");
  return m ? m[1] : "";
}

async function sha256hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Сравнение за постоянное время: сравниваются хэши одинаковой длины.
async function sameSecret(a, b) {
  if (!a || !b) return false;
  const [x, y] = await Promise.all([sha256hex(a), sha256hex(b)]);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

// "ok" | "denied" | "unavailable". Сам токен нигде не хранится: ключ KV — sha256.
async function authorize(token, env) {
  if (!token) return "denied";
  const key = "tok:" + (await sha256hex(token));
  const now = Date.now();
  let checkedAt = 0;
  try {
    const cached = await env.AUTH.get(key);
    if (cached) checkedAt = Number(JSON.parse(cached).checked_at) || 0;
  } catch (_) { /* повреждённая запись = нет записи */ }
  if (checkedAt && now - checkedAt < FRESH_MS) return "ok";
  let status = 0;
  try {
    const resp = await fetch(`https://api.github.com/repos/${env.LIBRARY_REPO || DEFAULT_REPO}`, {
      headers: {
        Authorization: `Bearer ${token}`,
        "User-Agent": "acl-event-log",
        Accept: "application/vnd.github+json",
      },
    });
    status = resp.status;
  } catch (_) { status = 0; }
  if (status === 200) {
    await env.AUTH.put(key, JSON.stringify({ checked_at: now }), { expirationTtl: 86400 });
    return "ok";
  }
  if (status === 401 || status === 403 || status === 404) {
    await env.AUTH.delete(key);
    return "denied";
  }
  // GitHub лежит (5xx или сеть): рабочая машина продолжает писать по записи не старше суток.
  return checkedAt && now - checkedAt < GRACE_MS ? "ok" : "unavailable";
}

async function handleBatch(request, env) {
  const auth = await authorize(bearer(request), env);
  if (auth === "denied") return json({ error: "unauthorized" }, 401);
  if (auth === "unavailable") return json({ error: "auth unavailable" }, 503);

  const machine = request.headers.get("X-ACL-Machine") || "";
  const batch = request.headers.get("X-ACL-Batch") || "";
  if (!MACHINE_RE.test(machine) || !BATCH_RE.test(batch)) return json({ error: "bad headers" }, 400);

  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > MAX_BODY) return json({ error: "too large" }, 413);
  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_BODY) return json({ error: "too large" }, 413);
  const head = new Uint8Array(body, 0, Math.min(2, body.byteLength));
  if (head.length < 2 || head[0] !== 0x1f || head[1] !== 0x8b) return json({ error: "not gzip" }, 400);

  const day = new Date().toISOString().slice(0, 10);
  const key = `v1/dt=${day}/machine=${machine}/${batch}.ndjson.gz`;
  // Пачка уже есть (повтор): это тоже успех, объект не перезаписывается и запись не тратится.
  try {
    if ((await env.AUTH.get(key, { type: "arrayBuffer" })) !== null) return json({ ok: true, duplicate: true });
    await env.AUTH.put(key, body, { expirationTtl: BATCH_TTL_S });
  } catch (_) {
    // Исчерпан суточный лимит записей или сбой KV: машина повторит позже, события лежат в её ящике.
    return json({ error: "storage unavailable" }, 503);
  }
  return json({ ok: true, duplicate: false });
}

async function readerAllowed(request, env) {
  return sameSecret(bearer(request), env.READ_KEY || "");
}

async function handleList(request, env, url) {
  if (!(await readerAllowed(request, env))) return json({ error: "unauthorized" }, 401);
  const prefix = url.searchParams.get("prefix") || "v1/";
  if (!prefix.startsWith("v1/")) return json({ error: "bad prefix" }, 400);
  const cursor = url.searchParams.get("cursor") || undefined;
  const page = await env.AUTH.list({ prefix, cursor, limit: 1000 });
  return json({ keys: page.keys.map((k) => k.name), cursor: page.list_complete ? null : page.cursor });
}

async function handleObject(request, env, url) {
  if (!(await readerAllowed(request, env))) return json({ error: "unauthorized" }, 401);
  const key = url.searchParams.get("key") || "";
  if (!key.startsWith("v1/")) return json({ error: "bad key" }, 400);
  const body = await env.AUTH.get(key, { type: "arrayBuffer" });
  if (!body) return json({ error: "not found" }, 404);
  return new Response(body, { headers: { "content-type": "application/octet-stream" } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const { method } = request;
    if (method === "POST" && url.pathname === "/v1/batch") return handleBatch(request, env);
    if (method === "GET" && url.pathname === "/v1/list") return handleList(request, env, url);
    if (method === "GET" && url.pathname === "/v1/object") return handleObject(request, env, url);
    return json({ error: "not found" }, 404);
  },
};
