// Приём событий: пачка машины (POST /v1/batch) и перенос истории владельцем (Task 6).
// Каждое событие пишется один раз (UNIQUE event_id); выводы строятся только для новых.
import { json } from "./respond.js";
import { authorize, bearer, isOwner } from "./auth.js";
import { deriveStatements, machineStatement } from "./derive.js";

export const MAX_BODY = 1024 * 1024;
const MAX_UNPACKED = 16 * 1024 * 1024;     // защита от «бомбы»: распакованная пачка не больше 16 МБ
const MACHINE_RE = /^[A-Za-z0-9_-]{1,64}$/;
export const BATCH_RE = /^[A-Za-z0-9-]{1,128}$/;
const SECRET_RE = /EAA[A-Za-z0-9]{20,}|access_token=/i;
const CHUNK = 50;                           // событий на один batch D1

const str = (v, max) => (typeof v === "string" && v.length > 0 && v.length <= max ? v : null);

// Событие пригодно: объект с event_id, type, ts. Остальные поля берутся как есть.
function valid(ev) {
  return ev && typeof ev === "object" && !Array.isArray(ev)
    && str(ev.event_id, 128) && str(ev.type, 64) && str(ev.ts, 64);
}

function insertEvent(db, ev, batchId, receivedAt) {
  const p = JSON.stringify(ev.payload ?? {});
  return db.prepare(
    "INSERT OR IGNORE INTO events (event_id, type, ts, machine_id, machine, manager, flow_version, channel, "
    + "job_id, account, campaign, stream_id, seq, batch_id, received_at, payload) "
    + "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
  ).bind(ev.event_id, ev.type, ev.ts, ev.machine_id ?? null, ev.machine ?? null, ev.manager ?? null,
    ev.flow_version ?? null, ev.channel ?? null, ev.job_id ?? null, ev.account ?? null, ev.campaign ?? null,
    ev.stream_id ?? null, Number.isInteger(ev.seq) ? ev.seq : null, batchId, receivedAt, p);
}

function heartbeatStatement(db, ev) {
  const p = ev.payload || {};
  return db.prepare(
    "UPDATE machines SET last_heartbeat_at = max(coalesce(last_heartbeat_at, ''), ?), "
    + "outbox_bytes = ?, dropped_total = coalesce(dropped_total, 0) + ? WHERE machine_id = ?",
  ).bind(ev.ts, Number(p.outbox_bytes) || 0, Number(p.dropped) || 0, ev.machine_id ?? null);
}

// Общая часть приёма. Бросает исключение при сбое D1 (вызывающий отвечает 503).
export async function ingestEvents(env, events, { batchId }) {
  const out = { accepted: 0, duplicates: 0, rejected: 0 };
  const receivedAt = new Date().toISOString();
  const good = [];
  for (const ev of events) {
    if (!valid(ev) || SECRET_RE.test(JSON.stringify(ev.payload ?? {}))) { out.rejected++; continue; }
    good.push(ev);
  }
  for (let i = 0; i < good.length; i += CHUNK) {
    const part = good.slice(i, i + CHUNK);
    const stmts = [];
    const kinds = [];
    for (const ev of part) {
      const m = machineStatement(env.DB, ev);
      if (m) { stmts.push(m); kinds.push(null); }
      if (ev.type === "heartbeat") { stmts.push(heartbeatStatement(env.DB, ev)); kinds.push("beat"); }
      else { stmts.push(insertEvent(env.DB, ev, batchId, receivedAt)); kinds.push(ev); }
    }
    const results = await env.DB.batch(stmts);
    const derived = [];
    for (let k = 0; k < results.length; k++) {
      const kind = kinds[k];
      if (kind === "beat") { out.accepted++; continue; }
      if (!kind) continue;
      if (results[k].meta.changes === 1) {
        out.accepted++;
        derived.push(...(await deriveStatements(env.DB, kind)));
      } else out.duplicates++;
    }
    if (derived.length) await env.DB.batch(derived);
  }
  return out;
}

// Распаковать gzip с ограничением размера; null — если не влезло.
async function gunzipText(body) {
  const reader = new Response(body).body.pipeThrough(new DecompressionStream("gzip")).getReader();
  const parts = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_UNPACKED) { await reader.cancel(); return null; }
    parts.push(value);
  }
  const all = new Uint8Array(size);
  let at = 0;
  for (const p of parts) { all.set(p, at); at += p.byteLength; }
  return new TextDecoder().decode(all);
}

export function parseLines(text) {
  return text.split("\n").filter((l) => l.trim()).map((line) => {
    try { return JSON.parse(line); } catch (_) { return null; }
  });
}

// Тело пачки: проверка размера и gzip, распаковка, разбор. Ответ-ошибка или {events}.
export async function readBatchBody(request) {
  const declared = Number(request.headers.get("Content-Length") || 0);
  if (declared > MAX_BODY) return { error: json({ error: "too large" }, 413) };
  const body = await request.arrayBuffer();
  if (body.byteLength > MAX_BODY) return { error: json({ error: "too large" }, 413) };
  const head = new Uint8Array(body, 0, Math.min(2, body.byteLength));
  if (head.length < 2 || head[0] !== 0x1f || head[1] !== 0x8b) return { error: json({ error: "not gzip" }, 400) };
  let text;
  try { text = await gunzipText(body); } catch (_) { return { error: json({ error: "bad gzip" }, 400) }; }
  if (text === null) return { error: json({ error: "too large" }, 413) };
  return { events: parseLines(text) };
}

export async function handleBatch(request, env) {
  const auth = await authorize(bearer(request), env);
  if (auth === "denied") return json({ error: "unauthorized" }, 401);
  if (auth === "unavailable") return json({ error: "auth unavailable" }, 503);
  const machine = request.headers.get("X-ACL-Machine") || "";
  const batchId = request.headers.get("X-ACL-Batch") || "";
  if (!MACHINE_RE.test(machine) || !BATCH_RE.test(batchId)) return json({ error: "bad headers" }, 400);
  const { error, events } = await readBatchBody(request);
  if (error) return error;
  try {
    return json({ ok: true, ...(await ingestEvents(env, events, { batchId })) });
  } catch (_) {
    // Сбой D1: машина повторит позже, события лежат в её ящике.
    return json({ error: "storage unavailable" }, 503);
  }
}

// Перенос истории владельцем: машина берётся из каждого события, заголовок машины не нужен.
export async function handleBackfill(request, env) {
  if (!(await isOwner(request, env))) return json({ error: "unauthorized" }, 401);
  const batchId = request.headers.get("X-ACL-Batch") || "";
  if (!BATCH_RE.test(batchId)) return json({ error: "bad headers" }, 400);
  const { error, events } = await readBatchBody(request);
  if (error) return error;
  try {
    return json({ ok: true, ...(await ingestEvents(env, events, { batchId })) });
  } catch (_) {
    return json({ error: "storage unavailable" }, 503);
  }
}
