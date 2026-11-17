// Отчёты Control Center поверх таблиц части 1 (только чтение).
import { json } from "./respond.js";
import { classify } from "./derive.js";

// Время в формате клиента (`…+00:00`), чтобы строковые сравнения с ts в базе были честными.
const iso = (ms) => new Date(ms).toISOString().replace("Z", "+00:00");
const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
const cut = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
const splitList = (s) => (s ? String(s).split(",").filter(Boolean).sort() : []);

function dayWindow(url) {
  const day = url.searchParams.get("day") || "";
  const tz = Number.parseInt(url.searchParams.get("tz") ?? "120", 10);
  if (!DAY_RE.test(day) || !Number.isFinite(tz) || Math.abs(tz) > 14 * 60) return null;
  const [y, m, d] = day.split("-").map(Number);
  const start = Date.UTC(y, m - 1, d) - tz * 60000;
  return { day, tz, from: iso(start), to: iso(start + 86400000) };
}

function median(values) {
  if (!values.length) return null;
  const v = [...values].sort((a, b) => a - b);
  const mid = v.length >> 1;
  return v.length % 2 ? v[mid] : Math.round(((v[mid - 1] + v[mid]) / 2) * 10) / 10;
}

export async function summary(env, url) {
  const w = dayWindow(url);
  if (!w) return json({ error: "bad day or tz" }, 400);
  const db = env.DB;
  const all = async (sql, ...args) => (await db.prepare(sql).bind(...args).all()).results;
  const runs = await all(
    "SELECT manager, outcome, duration_s FROM runs WHERE finished_at >= ? AND finished_at < ?", w.from, w.to);
  const jobs = await all(
    "SELECT manager, count(*) AS n FROM jobs WHERE created_at >= ? AND created_at < ? GROUP BY manager", w.from, w.to);
  const act = await all(
    "SELECT manager, min(ts) AS first_at, max(ts) AS last_at, group_concat(DISTINCT machine) AS machines "
    + "FROM events WHERE ts >= ? AND ts < ? GROUP BY manager", w.from, w.to);
  const versions = await all("SELECT manager, flow_version FROM machines ORDER BY last_event_at");

  const by = new Map();
  const row = (manager) => {
    if (!by.has(manager)) {
      by.set(manager, { manager, machines: [], first_at: null, last_at: null, active_h: 0, jobs: 0,
        campaigns: 0, ok: 0, failed: 0, not_started: 0, median_s: null, version: null, _d: [] });
    }
    return by.get(manager);
  };
  for (const r of runs) {
    const b = row(r.manager);
    b.campaigns++;
    if (r.outcome in b) b[r.outcome]++;
    if (r.duration_s !== null && r.duration_s !== undefined) b._d.push(r.duration_s);
  }
  for (const j of jobs) row(j.manager).jobs = j.n;
  for (const a of act) {
    const b = row(a.manager);
    Object.assign(b, { first_at: a.first_at, last_at: a.last_at, machines: splitList(a.machines) });
    b.active_h = Math.round((Date.parse(a.last_at) - Date.parse(a.first_at)) / 360000) / 10;
  }
  const latest = new Map(versions.map((v) => [v.manager, v.flow_version]));
  const buyers = [...by.values()].filter((b) => b.manager).map((b) => {
    const { _d, ...rest } = b;
    return { ...rest, median_s: median(_d), version: latest.get(b.manager) ?? null };
  }).sort((a, b) => b.campaigns - a.campaigns || a.manager.localeCompare(b.manager));
  return json({ day: w.day, tz: w.tz, buyers });
}

// Короткий английский текст события для ленты.
export function describe(ev, p, job) {
  switch (ev.type) {
    case "publish.result": {
      const outcome = classify(p);
      if (outcome === "ok") return { level: "info", text: p.reused && Object.keys(p.reused).length && !(p.created && Object.keys(p.created).length) ? "Published (reused existing)" : "Published PAUSED" };
      if (outcome === "not_started") return { level: "warn", text: cut(p.error, 160) };
      return { level: "error", text: "Failed: " + cut(p.error || (p.blocking_failures || [])[0] || "unknown error", 140) };
    }
    case "job.created": return { level: "info", text: `Job started: ${(p.campaign_names || []).length} campaign(s)${p.dry_run ? " (dry run)" : ""}` };
    case "job.started": return { level: "info", text: "Job running" };
    case "job.finished": {
      const n = job ? `: ${job.n_ok} ok, ${job.n_failed} failed, ${job.n_not_started} not started` : "";
      return { level: job && job.n_failed ? "warn" : "info", text: `Job finished in ${Math.round(p.duration_s || 0)} s${n}` };
    }
    case "plan.checked": {
      const blocked = p.blocking_rules || [];
      return blocked.length ? { level: "warn", text: "Plan check: blocked by " + blocked.join(", ") }
        : { level: "info", text: "Plan check: ok" + (p.error_kind ? ` (${p.error_kind})` : "") };
    }
    case "review.opened": return { level: "info", text: "Review opened" };
    case "review.closed": return { level: "info", text: `Review closed (${p.reason || "cancel"})` };
    case "review.confirmed": return { level: "info", text: "Review confirmed" };
    case "machine.profile": return { level: "info", text: "Machine passport" + (p.ram_gb ? `: ${p.ram_gb} GB RAM` : "") };
    case "command.executed": return { level: "info", text: "Command: " + cut(p.command || p.kind || "", 80) };
    default: return { level: "info", text: ev.type };
  }
}

// Лента: всё, кроме сырых шагов и шумных служебных событий.
const LIVE_SKIP = ["publish.spec", "publish.stats", "account.state", "test.ping"];

export async function live(env, url) {
  const limit = Math.min(Math.max(Number.parseInt(url.searchParams.get("limit") || "100", 10) || 100, 1), 300);
  const { results } = await env.DB.prepare(
    "SELECT e.ts, e.type, e.machine, e.manager, e.account, e.campaign, e.job_id, e.payload, "
    + "j.n_ok, j.n_failed, j.n_not_started FROM events e LEFT JOIN jobs j ON j.job_id = e.job_id "
    + `WHERE e.type NOT LIKE 'raw.%' AND e.type NOT IN (${LIVE_SKIP.map(() => "?").join(", ")}) `
    + "ORDER BY e.ts DESC LIMIT ?",
  ).bind(...LIVE_SKIP, limit).all();
  const events = results.map((r) => {
    const p = JSON.parse(r.payload || "{}");
    const job = r.n_ok === null || r.n_ok === undefined ? null : { n_ok: r.n_ok, n_failed: r.n_failed, n_not_started: r.n_not_started };
    const campaign = r.campaign || p.campaign_name || null;
    return { ts: r.ts, type: r.type, machine: r.machine, manager: r.manager, account: r.account, campaign,
      ...describe(r, p, job),
      run_id: r.type === "publish.result" && r.job_id && campaign ? `${r.job_id}|${campaign}` : null };
  });
  return json({ events });
}

export async function runDetail(env, url) {
  const id = url.searchParams.get("id") || "";
  const db = env.DB;
  const run = await db.prepare("SELECT * FROM runs WHERE run_id = ?").bind(id).first();
  if (!run) return json({ error: "run not found" }, 404);
  const job = await db.prepare("SELECT * FROM jobs WHERE job_id = ?").bind(run.job_id).first();
  const spec = await db.prepare("SELECT min(ts) AS ts FROM events WHERE job_id = ? AND type = 'publish.spec' AND campaign = ?")
    .bind(run.job_id, run.campaign).first("ts");
  const from = spec || job?.created_at || "";
  const to = run.finished_at || "9999";
  const { results } = await db.prepare(
    "SELECT ts, payload FROM events WHERE job_id = ? AND type = 'raw.run' AND ts >= ? AND ts <= ? ORDER BY ts LIMIT 500",
  ).bind(run.job_id, from, to).all();
  const steps = results.map((r) => {
    const p = JSON.parse(r.payload || "{}");
    const d = p.diag || {};
    return { ts: r.ts, step: p.step || "", ok: p.ok !== false, note: cut(d.step || d.error || p.note || "", 200) };
  });
  let fingerprint = null;
  if (run.fingerprint) {
    const f = await db.prepare(
      "SELECT count(*) AS n, count(DISTINCT machine_id) AS machines, group_concat(DISTINCT manager) AS managers, "
      + "min(finished_at) AS first_at, max(finished_at) AS last_at FROM runs WHERE fingerprint = ?",
    ).bind(run.fingerprint).first();
    fingerprint = { ...f, managers: splitList(f.managers) };
  }
  return json({ run: { ...run, load: run.load ? JSON.parse(run.load) : null }, job: job || null, steps, fingerprint });
}

export async function failures(env, url) {
  const days = Math.min(Math.max(Number.parseInt(url.searchParams.get("days") || "7", 10) || 7, 1), 36500);
  const since = iso(Date.now() - days * 86400000);
  const { results } = await env.DB.prepare(
    "SELECT fingerprint, count(*) AS n, count(DISTINCT machine_id) AS machines, group_concat(DISTINCT manager) AS managers, "
    + "group_concat(DISTINCT flow_version) AS versions, min(finished_at) AS first_at, max(finished_at) AS last_at, "
    + "max(stop_kind) AS stop_kind, "
    + "(SELECT r2.error FROM runs r2 WHERE r2.fingerprint = runs.fingerprint ORDER BY r2.finished_at DESC LIMIT 1) AS sample_error, "
    + "(SELECT r3.run_id FROM runs r3 WHERE r3.fingerprint = runs.fingerprint ORDER BY r3.finished_at DESC LIMIT 1) AS last_run_id "
    + "FROM runs WHERE outcome = 'failed' AND fingerprint IS NOT NULL AND finished_at >= ? "
    + "GROUP BY fingerprint ORDER BY n DESC, last_at DESC LIMIT 200",
  ).bind(since).all();
  return json({ failures: results.map((r) => ({ ...r, managers: splitList(r.managers), versions: splitList(r.versions) })) });
}
