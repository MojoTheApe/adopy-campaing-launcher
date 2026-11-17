// API владельца (OWNER_KEY; Cloudflare Access добавляется в части 2). Только чтение.
import { json } from "./respond.js";
import { isOwner } from "./auth.js";
import { failures, live, runDetail, summary } from "./report.js";

const EVENT_COLS = ["event_id", "stream_id", "seq", "type", "ts", "machine_id", "machine", "manager",
  "flow_version", "channel", "job_id", "account", "campaign"];
const MAX_ROWS = 2000;

const int = (v, dflt, max) => {
  const n = Number.parseInt(v ?? "", 10);
  return Number.isFinite(n) && n >= 0 ? Math.min(n, max) : dflt;
};

// Фильтры периода и менеджера, общие для runs и jobs.
function where(url, tsCol, extra = {}) {
  const parts = [];
  const args = [];
  const from = url.searchParams.get("from");
  const to = url.searchParams.get("to");
  if (from) { parts.push(`${tsCol} >= ?`); args.push(new Date(from).toISOString()); }
  if (to) { parts.push(`${tsCol} < ?`); args.push(new Date(to).toISOString()); }
  for (const [param, col] of Object.entries({ manager: "manager", ...extra })) {
    const v = url.searchParams.get(param);
    if (v) { parts.push(`${col} = ?`); args.push(v); }
  }
  return { sql: parts.length ? `WHERE ${parts.join(" AND ")}` : "", args };
}

async function events(env, url) {
  const after = int(url.searchParams.get("after"), 0, Number.MAX_SAFE_INTEGER);
  const limit = int(url.searchParams.get("limit"), 500, 1000) || 500;
  const { results } = await env.DB.prepare(
    `SELECT seq_id, ${EVENT_COLS.join(", ")}, payload FROM events WHERE seq_id > ? ORDER BY seq_id LIMIT ?`,
  ).bind(after, limit).all();
  const body = results.map((r) => {
    const ev = { hub_seq: r.seq_id };
    for (const c of EVENT_COLS) ev[c] = r[c];
    ev.payload = JSON.parse(r.payload || "{}");
    return JSON.stringify(ev);
  }).join("\n");
  const next = results.length ? results[results.length - 1].seq_id : after;
  return new Response(body ? body + "\n" : "", {
    headers: { "content-type": "application/x-ndjson", "X-Next-After": String(next) },
  });
}

async function runs(env, url) {
  const { sql, args } = where(url, "finished_at", { outcome: "outcome", account: "account" });
  const { results } = await env.DB.prepare(
    `SELECT * FROM runs ${sql} ORDER BY finished_at DESC LIMIT ${MAX_ROWS}`).bind(...args).all();
  return json({ runs: results });
}

async function jobs(env, url) {
  const { sql, args } = where(url, "created_at");
  const { results } = await env.DB.prepare(
    `SELECT * FROM jobs ${sql} ORDER BY created_at DESC LIMIT ${MAX_ROWS}`).bind(...args).all();
  return json({ jobs: results });
}

async function machines(env) {
  const { results } = await env.DB.prepare("SELECT * FROM machines ORDER BY last_event_at DESC").all();
  return json({ machines: results.map((m) => ({ ...m, profile: m.profile ? JSON.parse(m.profile) : null })) });
}

async function taskRequests(env) {
  const { results } = await env.DB.prepare(
    "SELECT t.*, r.manager, r.account, r.campaign, r.stop_kind, r.error, fh.hint FROM task_requests t "
    + "LEFT JOIN runs r ON r.run_id = t.run_id LEFT JOIN failure_hints fh ON fh.fingerprint = t.fingerprint "
    + "WHERE t.status = 'open' ORDER BY t.created_at").all();
  return json({ requests: results });
}

const ROUTES = { "/api/task-requests": taskRequests, "/api/events": events, "/api/runs": runs, "/api/jobs": jobs, "/api/machines": machines,
  "/api/summary": summary, "/api/live": live, "/api/run": runDetail, "/api/failures": failures };

export async function handleApi(request, env, url) {
  const route = ROUTES[url.pathname];
  if (!route) return json({ error: "not found" }, 404);
  if (!(await isOwner(request, env))) return json({ error: "unauthorized" }, 401);
  try {
    return await route(env, url);
  } catch (_) {
    return json({ error: "bad request" }, 400);
  }
}
