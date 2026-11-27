// Приём событий: пачка машины (POST /v1/batch) и перенос истории владельцем (Task 6).
// Каждое событие пишется один раз (UNIQUE event_id); выводы строятся только для новых.
import { json } from "./respond.js";
import { authorize, bearer, isOwner } from "./auth.js";
import { classify, derivable, deriveStatements, machineStatement } from "./derive.js";
import { notifyFailedRuns } from "./alerts.js";

export const MAX_BODY = 1024 * 1024;
const MAX_UNPACKED = 16 * 1024 * 1024;     // защита от «бомбы»: распакованная пачка не больше 16 МБ
const MACHINE_RE = /^[A-Za-z0-9_-]{1,64}$/;
export const BATCH_RE = /^[A-Za-z0-9-]{1,128}$/;
// Настоящий токен; почищенные клиентом формы (`access_token=<hidden>`, `<v>`, `<token>`) пропускаются.
const SECRET_RE = /EAA[A-Za-z0-9]{20,}/;   // токен Meta: заглавные EAA (хэши в нижнем регистре сюда не попадают)
const PARAM_SECRET_RE = /access_token(?:=|%3D)(?!<(?:hidden|v|token)>)[^&"'\s<]{8,}/i;
const EVENT_COLS = ["event_id", "type", "ts", "machine_id", "machine", "manager", "flow_version", "channel",
  "job_id", "account", "campaign", "stream_id", "seq", "batch_id", "received_at", "payload", "derived"];
// D1: не больше 100 параметров в запросе → 5 строк × 17 колонок = 85.
const ROWS_PER_INSERT = 5;
const STMTS_PER_BATCH = 100;
const DERIVE_EVENTS_PER_BATCH = 30;
const IN_LIST = 90;

const looksSecret = (text) => SECRET_RE.test(text) || PARAM_SECRET_RE.test(text);
const str = (v, max) => (typeof v === "string" && v.length > 0 && v.length <= max ? v : null);

// Событие пригодно: объект с event_id, type, ts. Остальные поля берутся как есть.
function valid(ev) {
  return ev && typeof ev === "object" && !Array.isArray(ev)
    && str(ev.event_id, 128) && str(ev.type, 64) && str(ev.ts, 64);
}

function eventRow(ev, batchId, receivedAt) {
  return [ev.event_id, ev.type, ev.ts, ev.machine_id ?? null, ev.machine ?? null, ev.manager ?? null,
    ev.flow_version ?? null, ev.channel ?? null, ev.job_id ?? null, ev.account ?? null, ev.campaign ?? null,
    ev.stream_id ?? null, Number.isInteger(ev.seq) ? ev.seq : null, batchId, receivedAt,
    JSON.stringify(ev.payload ?? {}), derivable(ev.type) ? 0 : 1];
}

function insertEvents(db, rows) {
  const one = `(${EVENT_COLS.map(() => "?").join(", ")})`;
  return db.prepare(`INSERT OR IGNORE INTO events (${EVENT_COLS.join(", ")}) VALUES ${rows.map(() => one).join(", ")}`)
    .bind(...rows.flat());
}

// Повтор того же heartbeat и более старый heartbeat машину не меняют.
function heartbeatStatement(db, ev) {
  const p = ev.payload || {};
  return db.prepare(
    "UPDATE machines SET last_heartbeat_at = ?, outbox_bytes = ?, dropped_total = coalesce(dropped_total, 0) + ? "
    + "WHERE machine_id = ? AND coalesce(last_heartbeat_at, '') < ?",
  ).bind(ev.ts, Number(p.outbox_bytes) || 0, Number(p.dropped) || 0, ev.machine_id ?? null, ev.ts);
}

// Одна строка машины на пачку: подпись из самого позднего события, границы времени по всем.
function machineStatements(db, events) {
  const byMachine = new Map();
  for (const ev of events) {
    if (!ev.machine_id) continue;
    const m = byMachine.get(ev.machine_id);
    if (!m) { byMachine.set(ev.machine_id, { latest: ev, first: ev.ts, last: ev.ts }); continue; }
    if (ev.ts < m.first) m.first = ev.ts;
    if (ev.ts >= m.last) { m.last = ev.ts; m.latest = ev; }
  }
  return [...byMachine.values()].map((m) => machineStatement(db, m.latest, m.first, m.last));
}

async function runBatches(db, stmts) {
  const results = [];
  for (let i = 0; i < stmts.length; i += STMTS_PER_BATCH) results.push(...(await db.batch(stmts.slice(i, i + STMTS_PER_BATCH))));
  return results;
}

// Общая часть приёма. Бросает исключение при сбое D1 (вызывающий отвечает 503, машина повторит).
// Выводы строятся для событий с derived = 0 — и новых, и тех, чьи выводы сорвались в прошлой доставке.
// failedRuns (необязательно): сюда кладутся run_id новых сбоев — для алертов.
export async function ingestEvents(env, events, { batchId, failedRuns = null }) {
  const out = { accepted: 0, duplicates: 0, rejected: 0 };
  const receivedAt = new Date().toISOString();
  const good = [];
  for (const ev of events) {
    if (!valid(ev) || looksSecret(JSON.stringify(ev))) { out.rejected++; continue; }
    good.push(ev);
  }
  const beats = good.filter((ev) => ev.type === "heartbeat");
  const rest = good.filter((ev) => ev.type !== "heartbeat");

  const stmts = machineStatements(env.DB, good);
  const nMachines = stmts.length;
  for (const ev of beats) stmts.push(heartbeatStatement(env.DB, ev));
  const sizes = [];
  for (let i = 0; i < rest.length; i += ROWS_PER_INSERT) {
    const rows = rest.slice(i, i + ROWS_PER_INSERT).map((ev) => eventRow(ev, batchId, receivedAt));
    stmts.push(insertEvents(env.DB, rows));
    sizes.push(rows.length);
  }
  const results = await runBatches(env.DB, stmts);
  results.slice(nMachines, nMachines + beats.length).forEach((r) => {
    if (r.meta.changes === 1) out.accepted++; else out.duplicates++;
  });
  results.slice(nMachines + beats.length).forEach((r, k) => {
    out.accepted += r.meta.changes;
    out.duplicates += sizes[k] - r.meta.changes;
  });

  const candidates = new Map(rest.filter((ev) => derivable(ev.type)).map((ev) => [ev.event_id, ev]));
  const pending = [];
  const ids = [...candidates.keys()];
  for (let i = 0; i < ids.length; i += IN_LIST) {
    const part = ids.slice(i, i + IN_LIST);
    const { results: rows } = await env.DB.prepare(
      `SELECT event_id FROM events WHERE derived = 0 AND event_id IN (${part.map(() => "?").join(", ")})`,
    ).bind(...part).all();
    for (const r of rows) pending.push(candidates.get(r.event_id));
  }
  for (let i = 0; i < pending.length; i += DERIVE_EVENTS_PER_BATCH) {
    const part = pending.slice(i, i + DERIVE_EVENTS_PER_BATCH);
    const derived = [];
    for (const ev of part) derived.push(...(await deriveStatements(env.DB, ev)));
    derived.push(env.DB.prepare(`UPDATE events SET derived = 1 WHERE event_id IN (${part.map(() => "?").join(", ")})`)
      .bind(...part.map((ev) => ev.event_id)));
    await env.DB.batch(derived);   // выводы и флаг — одной транзакцией
    if (failedRuns) {
      for (const ev of part) {
        const campaign = ev.campaign || ev.payload?.campaign_name;
        if (ev.type === "publish.result" && ev.job_id && campaign && classify(ev.payload || {}) === "failed") {
          failedRuns.push(`${ev.job_id}|${campaign}`);
        }
      }
    }
  }
  return out;
}

// Распаковать gzip с ограничением размера; null — если не влезло.
export async function gunzipText(body) {
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

export async function handleBatch(request, env, ctx) {
  const auth = await authorize(bearer(request), env);
  if (auth === "denied") return json({ error: "unauthorized" }, 401);
  if (auth === "unavailable") return json({ error: "auth unavailable" }, 503);
  const machine = request.headers.get("X-ACL-Machine") || "";
  const batchId = request.headers.get("X-ACL-Batch") || "";
  if (!MACHINE_RE.test(machine) || !BATCH_RE.test(batchId)) return json({ error: "bad headers" }, 400);
  const { error, events } = await readBatchBody(request);
  if (error) return error;
  const failedRuns = [];
  let response;
  try {
    response = json({ ok: true, ...(await ingestEvents(env, events, { batchId, failedRuns })) });
  } catch (_) {
    // Сбой D1: машина повторит позже, события лежат в её ящике.
    response = json({ error: "storage unavailable" }, 503);
  }
  // Сбои, выводы которых уже записаны, алертятся и при 503: на повторе они не будут «новыми».
  // Алерт уходит после ответа машине: отправка не держит приём.
  if (failedRuns.length) {
    const job = notifyFailedRuns(env, failedRuns);
    if (ctx && typeof ctx.waitUntil === "function") ctx.waitUntil(job); else await job;
  }
  return response;
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
