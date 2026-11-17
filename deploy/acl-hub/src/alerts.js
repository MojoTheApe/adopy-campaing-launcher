// Алерты владельцу: сбой кампании (сразу при приёме), зависшее задание и утренний дайджест (cron).
import { broadcast, botReady, cut, escapeHtml as h, hubUrl, runLink } from "./telegram.js";
import { summary } from "./report.js";

const HOLD_MS = 30 * 60 * 1000;          // тот же сбой на той же машине — не чаще раза в 30 мин
const STUCK_MS = 45 * 60 * 1000;          // задание идёт дольше 45 мин без конца
const OWNER_TZ_MIN = 120;                 // день владельца — по Израилю (зима)
const iso = (ms) => new Date(ms).toISOString().replace("Z", "+00:00");

// Антиспам: отправлять ли и сколько похожих было придержано. Отметка пишется сразу.
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

export async function notifyFailedRuns(env, runIds, now = Date.now()) {
  if (!botReady(env) || !runIds.length) return;
  for (const id of [...new Set(runIds)]) {
    try {
      const r = await env.DB.prepare("SELECT * FROM runs WHERE run_id = ? AND outcome = 'failed'").bind(id).first();
      if (!r) continue;
      if (r.fingerprint) {
        const mute = await env.DB.prepare("SELECT until FROM mutes WHERE fingerprint = ?").bind(r.fingerprint).first("until");
        if (mute && Date.parse(mute) > now) continue;
      }
      const verdict = await admit(env.DB, `fail:${r.fingerprint || id}:${r.machine_id || ""}`, now);
      if (!verdict) continue;
      const same = r.fingerprint ? await env.DB.prepare(
        "SELECT count(*) AS n, count(DISTINCT machine_id) AS m FROM runs WHERE fingerprint = ? AND finished_at >= ?",
      ).bind(r.fingerprint, iso(now - 24 * 3600000)).first() : null;
      const lines = [
        `🔴 <b>Campaign failed</b> — ${h(r.manager || r.machine_id || "unknown")}`,
        `${h(r.account || "—")} · ${h(r.campaign)}`,
        r.stop_kind ? `Stop: ${h(r.stop_kind)}` : null,
        h(cut(r.error || "no error text", 350)),
        same && same.n > 1 ? `Same error: ${same.n}× in 24 h on ${same.m} machine(s)` : null,
        verdict.held ? `(${verdict.held} more like this were not sent in the last 30 min)` : null,
        `<a href="${h(runLink(env, id))}">Open run</a>${r.fingerprint ? ` · mute: /mute ${r.fingerprint}` : ""}`,
      ];
      await broadcast(env, lines.filter(Boolean).join("\n"));
    } catch (_) { /* алерт не должен ломать приём */ }
  }
}

async function stuckJobs(env, now) {
  const { results } = await env.DB.prepare(
    "SELECT * FROM jobs WHERE finished_at IS NULL AND started_at IS NOT NULL AND started_at < ? AND started_at > ?",
  ).bind(iso(now - STUCK_MS), iso(now - 24 * 3600000)).all();
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

// День по байерам (дайджест и /today). day — YYYY-MM-DD в поясе владельца.
export async function dayText(env, day, title) {
  const res = await summary(env, new URL(`https://x/?day=${day}&tz=${OWNER_TZ_MIN}`));
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

export const ownerDay = (now, shiftDays = 0) =>
  new Date(now + OWNER_TZ_MIN * 60000 + shiftDays * 86400000).toISOString().slice(0, 10);

async function digest(env, now) {
  const day = ownerDay(now, -1);
  const key = `digest:${day}`;
  if (await env.DB.prepare("SELECT 1 FROM bot_state WHERE k = ?").bind(key).first()) return;
  await env.DB.prepare("INSERT INTO bot_state (k, v) VALUES (?, ?)").bind(key, iso(now)).run();
  const lines = await dayText(env, day, "☀️ Yesterday");
  lines.push("", "<b>Top failures (24 h)</b>", ...(await failuresText(env, now)));
  const { results: quiet } = await env.DB.prepare(
    "SELECT machine, manager FROM machines WHERE coalesce(last_heartbeat_at, last_event_at) < ? AND last_event_at > ?",
  ).bind(iso(now - 24 * 3600000), iso(now - 7 * 86400000)).all();
  if (quiet.length) lines.push("", "Quiet > 24 h: " + quiet.map((m) => h(m.machine || m.manager)).join(", "));
  lines.push("", `<a href="${h(hubUrl(env))}/">Open Control Center</a>`);
  await broadcast(env, lines.join("\n"));
}

export const DIGEST_CRON = "0 5 * * *";

export async function runScheduled(env, cron, now) {
  if (!botReady(env)) return;
  if (cron === DIGEST_CRON) await digest(env, now);
  else await stuckJobs(env, now);
}
