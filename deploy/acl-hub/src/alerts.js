// Алерты владельцу: сбой кампании (сразу при приёме), зависшее задание и утренний дайджест (cron каждые 5 мин).
import { broadcast, botReady, cut, escapeHtml as h, hubUrl, runLink } from "./telegram.js";
import { summary } from "./report.js";

const HOLD_MS = 30 * 60 * 1000;          // один и тот же сбой — не чаще раза в 30 мин
const FRESH_MS = 2 * 3600 * 1000;         // сбой старше 2 ч (машина была без сети) — без алерта, он будет в дайджесте
const STUCK_MS = 45 * 60 * 1000;          // задание идёт дольше 45 мин без конца
const STUCK_LOOKBACK_MS = 3 * 3600 * 1000;
const DIGEST_HOUR = 7;                    // дайджест после 07:00 по Израилю
const TZ = "Asia/Jerusalem";
const IN_LIST = 90;
const iso = (ms) => new Date(ms).toISOString().replace("Z", "+00:00");

// Смещение Израиля к UTC в минутах на момент ms (с учётом летнего времени).
export function israelOffsetMin(ms) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: TZ, hourCycle: "h23", year: "numeric",
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  const local = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute);
  return Math.round((local - Math.floor(ms / 60000) * 60000) / 60000);
}
export const ownerDay = (now, shiftDays = 0) =>
  new Date(now + israelOffsetMin(now) * 60000 + shiftDays * 86400000).toISOString().slice(0, 10);
const localTime = (ts) => new Date(ts).toLocaleTimeString("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });

// Антиспам: отправлять ли и сколько похожих было придержано.
async function admit(db, key, now) {
  const row = await db.prepare("SELECT last_sent_at, suppressed FROM alert_log WHERE key = ?").bind(key).first();
  if (row && now - Date.parse(row.last_sent_at) < HOLD_MS) {
    await db.prepare("UPDATE alert_log SET suppressed = suppressed + 1 WHERE key = ?").bind(key).run();
    return null;
  }
  await db.prepare("INSERT INTO alert_log (key, last_sent_at, suppressed) VALUES (?, ?, 0) "
    + "ON CONFLICT(key) DO UPDATE SET last_sent_at = excluded.last_sent_at, suppressed = 0").bind(key, iso(now)).run();
  return { held: row ? row.suppressed : 0 };
}

async function failedRows(db, ids) {
  const out = [];
  for (let i = 0; i < ids.length; i += IN_LIST) {
    const part = ids.slice(i, i + IN_LIST);
    const { results } = await db.prepare(
      "SELECT r.rowid AS rid, r.*, j.dry_run AS job_dry_run, m.until AS mute_until, fh.hint AS hint, "
      + "(SELECT count(*) FROM runs r3 WHERE r3.fingerprint = r.fingerprint) AS fp_total FROM runs r "
      + "LEFT JOIN jobs j ON j.job_id = r.job_id LEFT JOIN mutes m ON m.fingerprint = r.fingerprint "
      + "LEFT JOIN failure_hints fh ON fh.fingerprint = r.fingerprint "
      + `WHERE r.outcome = 'failed' AND r.run_id IN (${part.map(() => "?").join(", ")})`,
    ).bind(...part).all();
    out.push(...results);
  }
  return out;
}

// Кнопки под алертом. callback_data ≤ 64 байт: rowid прогона вместо длинного run_id.
export function alertButtons(env, r) {
  return { reply_markup: { inline_keyboard: [
    [{ text: "Open run", url: runLink(env, r.run_id) }, { text: "Mute 24h", callback_data: `m:${r.fingerprint || "-"}` }],
    [{ text: "Explain", callback_data: `e:${r.rid}` }, { text: "Create task", callback_data: `t:${r.rid}` }],
  ] } };
}

const isNew = (r) => Boolean(r.fingerprint) && Number(r.fp_total) <= 1;

function singleText(env, r, verdict, same) {
  return [
    `🔴 <b>Campaign failed</b>${isNew(r) ? " 🆕 NEW" : ""} — ${h(r.manager || r.machine_id || "unknown")} · at ${localTime(r.finished_at)}`,
    `${h(r.account || "—")} · ${h(r.campaign)}`,
    r.stop_kind ? `Stop: ${h(r.stop_kind)}` : null,
    h(cut(r.error || "no error text", 350)),
    same && same.n > 1 ? `Same error: ${same.n}× in 24 h on ${same.m} machine(s)` : null,
    r.hint ? `💡 Known fix: ${h(cut(r.hint, 300))}` : null,
    verdict.held ? `(${verdict.held} more like this were not sent in the last 30 min)` : null,
    `<a href="${h(runLink(env, r.run_id))}">Open run</a>${r.fingerprint ? ` · mute: /mute ${r.fingerprint}` : ""}`,
  ].filter(Boolean).join("\n");
}

function groupText(env, rows, verdict) {
  const first = rows[0];
  const fresh = rows.some(isNew) ? " 🆕 NEW" : "";
  const lines = [`🔴 <b>${rows.length} campaigns failed</b>${fresh} — ${h(first.manager || first.machine_id || "unknown")} · at ${localTime(first.finished_at)}`,
    h(first.account || "—")];
  for (const r of rows.slice(0, 5)) lines.push(`• ${h(cut(r.campaign, 60))} — ${h(cut(r.error || "no error text", 120))}`);
  if (rows.length > 5) lines.push(`…and ${rows.length - 5} more`);
  if (verdict.held) lines.push(`(${verdict.held} more like this were not sent in the last 30 min)`);
  const fps = [...new Set(rows.map((r) => r.fingerprint).filter(Boolean))].slice(0, 3);
  lines.push(`<a href="${h(runLink(env, first.run_id))}">Open first run</a>${fps.length ? " · mute: " + fps.map((f) => `/mute ${f}`).join(" ") : ""}`);
  return lines.join("\n");
}

// Сбои одной пачки: одно сообщение на задание (несколько упавших кампаний — одним списком).
export async function notifyFailedRuns(env, runIds, now = Date.now()) {
  if (!botReady(env) || !runIds.length) return;
  let rows;
  try {
    rows = (await failedRows(env.DB, [...new Set(runIds)])).filter((r) => r.job_dry_run !== 1
      && !(r.mute_until && Date.parse(r.mute_until) > now)
      && r.finished_at && now - Date.parse(r.finished_at) < FRESH_MS);
  } catch (err) {
    console.error(`alerts: cannot read runs: ${err?.message || err}`);
    return;
  }
  const byJob = new Map();
  for (const r of rows) (byJob.get(r.job_id) || byJob.set(r.job_id, []).get(r.job_id)).push(r);
  for (const [jobId, list] of byJob) {
    try {
      list.sort((a, b) => String(a.finished_at).localeCompare(String(b.finished_at)));
      if (list.length === 1) {
        const r = list[0];
        const verdict = await admit(env.DB, `fail:${r.fingerprint || r.run_id}`, now);
        if (!verdict) continue;
        const same = r.fingerprint ? await env.DB.prepare(
          "SELECT count(*) AS n, count(DISTINCT machine_id) AS m FROM runs WHERE fingerprint = ? AND finished_at >= ?",
        ).bind(r.fingerprint, iso(now - 24 * 3600000)).first() : null;
        await broadcast(env, singleText(env, r, verdict, same), alertButtons(env, r));
      } else {
        const verdict = await admit(env.DB, `job:${jobId}`, now);
        if (!verdict) continue;
        await broadcast(env, groupText(env, list, verdict), alertButtons(env, list[0]));
      }
    } catch (err) {
      console.error(`alerts: job ${jobId}: ${err?.message || err}`);
    }
  }
}

async function stuckJobs(env, now) {
  const { results } = await env.DB.prepare(
    "SELECT * FROM jobs WHERE finished_at IS NULL AND started_at IS NOT NULL AND started_at < ? AND started_at > ? "
    + "AND NOT (coalesce(n_planned, 0) > 0 AND n_ok + n_failed + n_not_started >= n_planned)",
  ).bind(iso(now - STUCK_MS), iso(now - STUCK_LOOKBACK_MS)).all();
  for (const j of results) {
    const key = `stuck:${j.job_id}`;
    if (await env.DB.prepare("SELECT 1 FROM alert_log WHERE key = ?").bind(key).first()) continue;
    await env.DB.prepare("INSERT INTO alert_log (key, last_sent_at) VALUES (?, ?)").bind(key, iso(now)).run();
    const mins = Math.round((now - Date.parse(j.started_at)) / 60000);
    await broadcast(env, [
      `🟠 <b>Job running for ${mins} min</b> — ${h(j.manager || j.machine_id || "unknown")}`,
      `${h(j.account || "—")} · ${j.n_planned ?? "?"} campaign(s), ${j.n_ok} ok, ${j.n_failed} failed so far`,
      "No end reported: the dashboard may have stopped or the job is hanging.",
    ].join("\n"));
  }
}

const dayLabel = (day) => new Date(day + "T12:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });

// День по байерам (дайджест и /today). day — YYYY-MM-DD по Израилю.
export async function dayText(env, day, title) {
  const tz = israelOffsetMin(Date.parse(day + "T12:00:00Z"));
  const res = await summary(env, new URL(`https://x/?day=${day}&tz=${tz}`));
  const { buyers } = await res.json();
  const lines = [`<b>${h(title)} · ${dayLabel(day)}</b>`];
  if (!buyers.length) lines.push("No activity.");
  for (const b of buyers) {
    const hours = b.first_at ? ` · active ${b.active_h} h` : "";
    lines.push(`${h(b.manager)}: ${b.campaigns} campaigns · ${b.ok} ok · ${b.failed} failed · ${b.not_started} not started`
      + ` · ${b.jobs} job(s)${hours}`);
  }
  return lines;
}

export async function failuresText(env, now, hours = 24, limit = 5) {
  const { results } = await env.DB.prepare(
    "SELECT fingerprint, count(*) AS n, count(DISTINCT machine_id) AS m, max(stop_kind) AS stop_kind, "
    + "(SELECT r2.error FROM runs r2 WHERE r2.fingerprint = runs.fingerprint ORDER BY r2.finished_at DESC LIMIT 1) AS err "
    + "FROM runs WHERE outcome = 'failed' AND fingerprint IS NOT NULL AND finished_at >= ? "
    + "GROUP BY fingerprint ORDER BY n DESC LIMIT ?",
  ).bind(iso(now - hours * 3600000), limit).all();
  if (!results.length) return [`No failures in the last ${hours} h.`];
  return results.map((f) => `• ${f.n}× on ${f.m} machine(s) — ${h(f.stop_kind || "")}: ${h(cut(f.err, 140))} (/mute ${f.fingerprint})`);
}

// Дайджест за вчера: после 07:00 по Израилю, один раз; отметка — только после удачной отправки (иначе повтор через 5 мин).
async function digest(env, now) {
  const localHour = new Date(now + israelOffsetMin(now) * 60000).getUTCHours();
  if (localHour < DIGEST_HOUR) return;
  const day = ownerDay(now, -1);
  const key = `digest:${day}`;
  if (await env.DB.prepare("SELECT 1 FROM bot_state WHERE k = ?").bind(key).first()) return;
  const lines = await dayText(env, day, "☀️ Yesterday");
  lines.push("", "<b>Top failures (24 h)</b>", ...(await failuresText(env, now)));
  const { results: quiet } = await env.DB.prepare(
    "SELECT machine, manager FROM machines WHERE coalesce(last_heartbeat_at, last_event_at) < ? AND last_event_at > ?",
  ).bind(iso(now - 24 * 3600000), iso(now - 7 * 86400000)).all();
  if (quiet.length) lines.push("", "Quiet > 24 h: " + quiet.map((m) => h(m.machine || m.manager)).join(", "));
  const unfinished = await unfinishedRuns(env, now);
  if (unfinished.length) lines.push("", `⚠️ ${unfinished.length} unfinished campaign(s) left in Meta — /unfinished`);
  const open = await env.DB.prepare("SELECT count(*) AS n FROM task_requests WHERE status = 'open'").first("n");
  if (open) lines.push(`📝 ${open} open task request(s) — /tasks`);
  lines.push("", `<a href="${h(hubUrl(env))}/">Open Control Center</a>`);
  if (await broadcast(env, lines.join("\n"))) {
    await env.DB.prepare("INSERT OR IGNORE INTO bot_state (k, v) VALUES (?, ?)").bind(key, iso(now)).run();
  }
}

export async function runScheduled(env, cron, now) {
  if (!botReady(env)) return;
  try { await digest(env, now); } catch (err) { console.error(`digest: ${err?.message || err}`); }
  try { await stuckJobs(env, now); } catch (err) { console.error(`stuck jobs: ${err?.message || err}`); }
}

// Недоделанные кампании: последний сбой кампании на аккаунте оставил что-то в Meta,
// и после него этой кампании не было успеха. За 14 дней.
export async function unfinishedRuns(env, now = Date.now()) {
  const { results } = await env.DB.prepare(
    "SELECT r.rowid AS rid, r.* FROM runs r WHERE r.outcome = 'failed' AND r.created_any = 1 AND r.finished_at >= ? "
    + "AND NOT EXISTS (SELECT 1 FROM runs r2 WHERE r2.account = r.account AND r2.campaign = r.campaign "
    + "AND r2.outcome = 'ok' AND r2.finished_at > r.finished_at) "
    + "AND r.finished_at = (SELECT max(r4.finished_at) FROM runs r4 WHERE r4.account = r.account "
    + "AND r4.campaign = r.campaign AND r4.outcome = 'failed' AND r4.created_any = 1) "
    + "ORDER BY r.finished_at DESC LIMIT 50",
  ).bind(iso(now - 14 * 86400000)).all();
  return results;
}
