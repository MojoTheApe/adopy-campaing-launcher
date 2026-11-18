// Забор из старого журнала acl-event-log (KV) — по кнопке Refresh в Control Center.
//
// Машины, которые ещё не обновились (или у которых в .env остался адрес старого журнала), шлют пачки туда
// раз в час. Хаб сам скачивает пачки, которых ещё не переносил, и разбирает их обычным приёмом событий.
// Секреты: `LEGACY_LOG_URL`, `LEGACY_READ_KEY`. Без них кнопка отвечает «not configured».
import { json } from "./respond.js";
import { isOwner } from "./auth.js";
import { gunzipText, ingestEvents, parseLines } from "./ingest.js";
import { notifyFailedRuns } from "./alerts.js";

// Лимит подзапросов Worker (50 на бесплатном тарифе): список + не больше MAX_FETCH пачек за нажатие.
export const MAX_FETCH = 30;
const UA = "acl-hub-legacy-pull";

async function getJson(env, path) {
  const r = await fetch(env.LEGACY_LOG_URL.replace(/\/+$/, "") + path,
    { headers: { Authorization: `Bearer ${env.LEGACY_READ_KEY}`, "User-Agent": UA } });
  if (!r.ok) throw new Error(`legacy ${r.status}`);
  return r;
}

async function listKeys(env) {
  const keys = [];
  let cursor = null;
  for (let page = 0; page < 10; page++) {
    const q = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
    const data = await (await getJson(env, `/v1/list${q}`)).json();
    keys.push(...(data.keys || []));
    cursor = data.cursor;
    if (!cursor) break;
  }
  return keys;
}

async function knownKeys(db, keys) {
  const known = new Set();
  for (let i = 0; i < keys.length; i += 90) {
    const part = keys.slice(i, i + 90);
    const { results } = await db.prepare(
      `SELECT key FROM legacy_segments WHERE key IN (${part.map(() => "?").join(",")})`).bind(...part).all();
    for (const r of results) known.add(r.key);
  }
  return known;
}

// Перенести новые пачки. Возвращает {segments, accepted, duplicates, rejected, remaining, failed_segments}.
export async function pullLegacy(env, now = Date.now()) {
  const keys = await listKeys(env);
  const known = await knownKeys(env.DB, keys);
  const fresh = keys.filter((k) => !known.has(k)).sort();
  const out = { segments: 0, accepted: 0, duplicates: 0, rejected: 0, failed_segments: 0,
    remaining: Math.max(0, fresh.length - MAX_FETCH) };
  const failedRuns = [];
  for (const key of fresh.slice(0, MAX_FETCH)) {
    let events;
    try {
      const text = await gunzipText(await (await getJson(env, `/v1/object?key=${encodeURIComponent(key)}`)).arrayBuffer());
      if (text === null) throw new Error("too large");
      events = parseLines(text);
    } catch (_) {
      out.failed_segments++;   // битую или недоступную пачку не помечаем — попробуем при следующем нажатии
      continue;
    }
    const res = await ingestEvents(env, events, { batchId: `legacy:${key}`.slice(0, 120), failedRuns });
    await env.DB.prepare("INSERT OR IGNORE INTO legacy_segments (key, pulled_at) VALUES (?, ?)")
      .bind(key, new Date(now).toISOString()).run();
    out.segments++;
    for (const n of ["accepted", "duplicates", "rejected"]) out[n] += res[n] || 0;
  }
  // Свежие сбои байеров (моложе 2 ч) уходят в бот как обычно; старые notifyFailedRuns сам пропускает.
  if (failedRuns.length) await notifyFailedRuns(env, failedRuns, now);
  return out;
}

export async function handleRefresh(request, env) {
  if (!(await isOwner(request, env))) return json({ error: "unauthorized" }, 401);
  if (!env.LEGACY_LOG_URL || !env.LEGACY_READ_KEY) return json({ error: "not configured" }, 503);
  try {
    return json({ ok: true, ...(await pullLegacy(env)) });
  } catch (_) {
    return json({ error: "old log unavailable" }, 502);
  }
}
