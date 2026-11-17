// Выводы из событий: машины, задания, прогоны кампаний. Каждое событие апсертит только свои
// колонки, поэтому итог не зависит от порядка событий внутри и между пачками.

const REASON_LIMIT = 160;
const NO_REASON = "(no reason)";
const UUID_RE = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;
const HEX_RE = /\b[0-9a-f]{12,}\b/gi;
const NUM_RE = /\d+(?:[.,]\d+)?/g;

// Порт src/failure_signatures.normalise_reason (scrub делает клиент; uuid → <uuid>, как у scrub).
export function normalizeReason(text) {
  let out = String(text || "").replace(UUID_RE, "<uuid>").replace(HEX_RE, "<id>").replace(NUM_RE, "#");
  out = out.replace(/\s+/g, " ").trim().toLowerCase();
  if (out.length > REASON_LIMIT) out = out.slice(0, REASON_LIMIT).trimEnd() + "…";
  return out || NO_REASON;
}

// Отпечаток только по тексту ошибки: stop_kind приходит другим событием (publish.stats),
// и отпечаток, зависящий от него, менялся бы от порядка доставки.
export async function fingerprint(error) {
  const text = normalizeReason(error);
  const digest = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 12);
}

export function classify(result) {
  if (result && result.success === true) return "ok";
  if (String(result?.error || "").startsWith("Not started")) return "not_started";
  return "failed";
}

// Любое событие с machine_id: машина, её последняя подпись и время. null — если машины нет.
export function machineStatement(db, ev) {
  if (!ev.machine_id) return null;
  const latest = "excluded.last_event_at >= coalesce(machines.last_event_at, '')";
  return db.prepare(
    "INSERT INTO machines (machine_id, machine, manager, flow_version, first_seen_at, last_event_at) "
    + "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(machine_id) DO UPDATE SET "
    + `machine = CASE WHEN ${latest} THEN coalesce(excluded.machine, machines.machine) ELSE machines.machine END, `
    + `manager = CASE WHEN ${latest} THEN coalesce(excluded.manager, machines.manager) ELSE machines.manager END, `
    + `flow_version = CASE WHEN ${latest} THEN coalesce(excluded.flow_version, machines.flow_version) `
    + "  ELSE machines.flow_version END, "
    + "first_seen_at = min(coalesce(machines.first_seen_at, excluded.first_seen_at), excluded.first_seen_at), "
    + "last_event_at = max(coalesce(machines.last_event_at, ''), excluded.last_event_at)",
  ).bind(ev.machine_id, ev.machine ?? null, ev.manager ?? null, ev.flow_version ?? null, ev.ts, ev.ts);
}

// Апсерт строки: ключ + общие колонки (только если пусто) + колонки события (перезаписываются).
// Имена колонок задаёт код, не данные.
function upsert(db, table, key, keyValue, common, own) {
  const cols = [key, ...Object.keys(common), ...Object.keys(own)];
  const vals = [keyValue, ...Object.values(common), ...Object.values(own)].map((v) => (v === undefined ? null : v));
  const sets = [
    ...Object.keys(common).map((c) => `${c} = coalesce(${table}.${c}, excluded.${c})`),
    ...Object.keys(own).map((c) => `${c} = excluded.${c}`),
  ];
  const tail = sets.length ? `DO UPDATE SET ${sets.join(", ")}` : "DO NOTHING";
  return db.prepare(
    `INSERT INTO ${table} (${cols.join(", ")}) VALUES (${cols.map(() => "?").join(", ")}) ON CONFLICT(${key}) ${tail}`,
  ).bind(...vals);
}

function recountJob(db, jobId) {
  const n = (o) => `(SELECT count(*) FROM runs WHERE job_id = ?1 AND outcome = '${o}')`;
  return db.prepare(
    `UPDATE jobs SET n_ok = ${n("ok")}, n_failed = ${n("failed")}, n_not_started = ${n("not_started")} WHERE job_id = ?1`,
  ).bind(jobId);
}

const bool = (v) => (v === undefined || v === null ? null : v ? 1 : 0);
const num = (v) => (v === undefined || v === null || v === "" || Number.isNaN(Number(v)) ? null : Number(v));
const nonEmpty = (o) => (o && typeof o === "object" ? Object.keys(o).length > 0 : false);

function errorText(p) {
  if (p.error) return String(p.error);
  const rows = Array.isArray(p.blocking_failures) ? p.blocking_failures : [];
  return rows.length ? String(rows[0]) : "";
}

export async function deriveStatements(db, ev) {
  const p = ev.payload && typeof ev.payload === "object" ? ev.payload : {};
  const jobId = ev.job_id;
  if (ev.type === "machine.profile" && ev.machine_id) {
    return [db.prepare("UPDATE machines SET profile = ?, profile_at = ? WHERE machine_id = ?")
      .bind(JSON.stringify(p), ev.ts, ev.machine_id)];
  }
  if (!jobId) return [];
  const jobCommon = { machine_id: ev.machine_id, manager: ev.manager, account: ev.account };

  if (ev.type.startsWith("job.")) {
    let own = {};
    if (ev.type === "job.created") {
      own = { created_at: ev.ts, n_planned: Array.isArray(p.campaign_names) ? p.campaign_names.length : null,
        dry_run: bool(p.dry_run), reviewed: bool(p.reviewed) };
    } else if (ev.type === "job.started") {
      own = { started_at: ev.ts, slot_wait_s: num(p.slot_wait_s) };
    } else if (ev.type === "job.finished") {
      own = { finished_at: p.finished_at || ev.ts, duration_s: num(p.duration_s), status: p.status ?? null,
        cancelled: bool(p.cancelled) };
    }
    return [upsert(db, "jobs", "job_id", jobId, jobCommon, own), recountJob(db, jobId)];
  }

  if (!ev.type.startsWith("publish.")) return [];
  const campaign = ev.campaign || p.campaign_name;
  if (!campaign) return [];
  const runCommon = { job_id: jobId, campaign, ...jobCommon, flow_version: ev.flow_version };
  let own = {};
  if (ev.type === "publish.spec") {
    const adsets = Array.isArray(p.adsets) ? p.adsets : [];
    const ads = Array.isArray(p.ads) ? p.ads : [];
    own = { spec_hash: p.spec_hash ?? null, country: p.country ?? null, daily_budget: num(p.daily_budget),
      adset_names: JSON.stringify(adsets.map((a) => a?.name ?? null)),
      ad_names: JSON.stringify(ads.map((a) => a?.name ?? null)), n_ads: ads.length };
  } else if (ev.type === "publish.stats") {
    own = { duration_s: num(p.duration_s), calls: num(p.calls), writes: num(p.writes), stop_kind: p.stop_kind ?? null,
      load: p.load && typeof p.load === "object" ? JSON.stringify(p.load) : null };
    if (p.spec_hash) own.spec_hash = p.spec_hash;
  } else if (ev.type === "publish.result") {
    const outcome = classify(p);
    const error = errorText(p);
    own = { finished_at: ev.ts, outcome, error: error || null,
      fingerprint: outcome === "failed" ? await fingerprint(error) : null,
      campaign_id: p.campaign_id ?? null, published_as: p.published_as || null,
      created_any: nonEmpty(p.created) ? 1 : 0, reused_any: nonEmpty(p.reused) ? 1 : 0 };
  } else return [];
  return [upsert(db, "runs", "run_id", `${jobId}|${campaign}`, runCommon, own), recountJob(db, jobId)];
}
