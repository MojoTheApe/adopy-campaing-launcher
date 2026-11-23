// Webhook Telegram: команды и кнопки владельца. Секрет webhook + список чатов; чужие чаты молча игнорируются.
import { json } from "./respond.js";
import { sameSecret } from "./auth.js";
import { allowedChats, answerButton, cut, escapeHtml as h, runLink, sendTo } from "./telegram.js";
import { dayText, failuresText, ownerDay, unfinishedRuns } from "./alerts.js";

const FP_RE = /^[0-9a-f]{12}$/;
const iso = (ms) => new Date(ms).toISOString().replace("Z", "+00:00");
const when = (ts) => (ts ? new Date(ts).toLocaleString("en-GB", { timeZone: "Asia/Jerusalem", day: "numeric",
  month: "short", hour: "2-digit", minute: "2-digit" }) : "—");

const HELP = [
  "<b>ACL Control bot</b>",
  "/today · /yesterday — buyers by day",
  "/failures — top failures, last 24 h",
  "/unfinished — campaigns left half-made in Meta",
  "/buyer &lt;name&gt; · /machine &lt;name&gt; — one buyer or machine",
  "/tasks — open task requests · /done &lt;n&gt; — close one",
  "/cleared &lt;n&gt; — drop a campaign from /unfinished (fixed by hand in Meta)",
  "/hint &lt;id&gt; &lt;text&gt; — save the fix for a failure (shown in every next alert)",
  "/mute &lt;id&gt; [hours] · /unmute &lt;id&gt;",
  "Buttons under an alert: Open run, Mute 24h, Explain, Create task.",
].join("\n");

const outcomeMark = (o) => (o === "ok" ? "✅" : o === "failed" ? "🔴" : "⚪");

async function unfinishedText(env, now) {
  const rows = await unfinishedRuns(env, now);
  if (!rows.length) return "No unfinished campaigns in the last 14 days.";
  const lines = [`<b>${rows.length} unfinished campaign(s)</b> — something was created in Meta, then it stopped. `
    + "\"Publish again\" reuses what is there."];
  for (const r of rows.slice(0, 15)) {
    lines.push(`[${r.rid}] ${h(r.manager || "—")} · ${h(r.account || "—")} · <a href="${h(runLink(env, r.run_id))}">${h(cut(r.campaign, 60))}</a> · ${when(r.finished_at)}`);
  }
  if (rows.length > 15) lines.push(`…and ${rows.length - 15} more`);
  lines.push("", "Fixed one by hand in Meta? /cleared &lt;n&gt;");
  return lines.join("\n");
}

// Имя по началу (без регистра): единственное совпадение или точное.
async function findName(env, column, query) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return { usage: true };
  const { results } = await env.DB.prepare(`SELECT DISTINCT ${column} AS name FROM machines WHERE ${column} IS NOT NULL`).all();
  const names = results.map((r) => r.name);
  const exact = names.find((n) => n.toLowerCase() === q);
  if (exact) return { name: exact };
  // Сначала по началу имени или слова, иначе — по вхождению.
  let hits = names.filter((n) => n.toLowerCase().split(/\s+/).some((w) => w.startsWith(q)) || n.toLowerCase().startsWith(q));
  if (!hits.length) hits = names.filter((n) => n.toLowerCase().includes(q));
  if (hits.length === 1) return { name: hits[0] };
  return { many: hits.slice(0, 10) };
}

async function buyerText(env, query, now) {
  const f = await findName(env, "manager", query);
  if (f.usage) return "Usage: /buyer &lt;name&gt;";
  if (!f.name) return f.many.length ? `Which one? ${f.many.map(h).join(", ")}` : `No buyer matches "${h(query)}".`;
  const today = (await dayText(env, ownerDay(now), "Today")).filter((l, i) => i === 0 || l.startsWith(h(f.name) + ":"));
  if (today.length === 1) today.push("No campaigns today.");
  const { results } = await env.DB.prepare(
    "SELECT run_id, campaign, account, outcome, finished_at, stop_kind FROM runs WHERE manager = ? ORDER BY finished_at DESC LIMIT 8",
  ).bind(f.name).all();
  const lines = [`<b>${h(f.name)}</b>`, ...today.slice(1), "", "<b>Last runs</b>"];
  if (!results.length) lines.push("None yet.");
  for (const r of results) {
    lines.push(`${outcomeMark(r.outcome)} ${when(r.finished_at)} · ${h(r.account || "—")} · <a href="${h(runLink(env, r.run_id))}">${h(cut(r.campaign, 50))}</a>${r.stop_kind ? ` · ${h(r.stop_kind)}` : ""}`);
  }
  const unfinished = (await unfinishedRuns(env, now)).filter((r) => r.manager === f.name).length;
  if (unfinished) lines.push("", `⚠️ ${unfinished} unfinished — /unfinished`);
  return lines.join("\n");
}

async function machineText(env, query, now) {
  const f = await findName(env, "machine", query);
  if (f.usage) return "Usage: /machine &lt;name&gt;";
  if (!f.name) return f.many.length ? `Which one? ${f.many.map(h).join(", ")}` : `No machine matches "${h(query)}".`;
  const m = await env.DB.prepare("SELECT * FROM machines WHERE machine = ? ORDER BY last_event_at DESC LIMIT 1").bind(f.name).first();
  const p = m.profile ? JSON.parse(m.profile) : null;
  const day = await env.DB.prepare(
    "SELECT count(*) AS n, sum(outcome = 'ok') AS ok, sum(outcome = 'failed') AS failed FROM runs WHERE machine_id = ? AND finished_at >= ?",
  ).bind(m.machine_id, iso(now - 24 * 3600000)).first();
  const lines = [`<b>${h(m.machine)}</b> — ${h(m.manager || "—")}`, `Version: ${h(m.flow_version || "—")}`,
    `Last signal: ${when(m.last_heartbeat_at || m.last_event_at)}`,
    `Outbox: ${m.outbox_bytes ? Math.round(m.outbox_bytes / 1024) + " KB" : "empty"} · dropped total ${m.dropped_total || 0}`,
    `Last 24 h: ${day.n || 0} campaigns, ${day.ok || 0} ok, ${day.failed || 0} failed`];
  if (p) {
    lines.push(`Passport: ${[p.ram_gb && p.ram_gb + " GB RAM", p.cores_logical && p.cores_logical + " cores", p.windows,
      p.screen_w && `${p.screen_w}×${p.screen_h}`, p.mlx_running ? "Multilogin on" : null, p.adspower_running ? "AdsPower on" : null]
      .filter(Boolean).map(h).join(" · ")}`);
  } else lines.push("Passport: not sent yet (needs the new client)");
  return lines.join("\n");
}

async function tasksText(env) {
  const { results } = await env.DB.prepare(
    "SELECT t.*, r.manager, r.campaign, r.error FROM task_requests t LEFT JOIN runs r ON r.run_id = t.run_id "
    + "WHERE t.status = 'open' ORDER BY t.created_at").all();
  if (!results.length) return "No open task requests.";
  const lines = [`<b>${results.length} open task request${results.length > 1 ? "s" : ""}</b> — the tracker session picks them up`];
  for (const t of results.slice(0, 15)) {
    lines.push(`[${t.id}] ${when(t.created_at)} · ${h(t.manager || "—")} · ${h(cut(t.error || t.campaign || "", 120))}${t.fingerprint ? ` (${t.fingerprint})` : ""}`);
  }
  lines.push("", "Done? /done &lt;n&gt;");
  return lines.join("\n");
}

async function answer(env, text, now) {
  const raw = String(text || "").trim();
  const [cmd, ...args] = raw.split(/\s+/);
  const name = (cmd || "").split("@")[0].toLowerCase();
  if (name === "/today") return (await dayText(env, ownerDay(now), "Today")).join("\n");
  if (name === "/yesterday") return (await dayText(env, ownerDay(now, -1), "Yesterday")).join("\n");
  if (name === "/failures") return ["<b>Failures, last 24 h</b>", ...(await failuresText(env, now))].join("\n");
  if (name === "/unfinished") return unfinishedText(env, now);
  if (name === "/buyer") return buyerText(env, args.join(" "), now);
  if (name === "/machine") return machineText(env, args.join(" "), now);
  if (name === "/tasks") return tasksText(env);
  if (name === "/done") {
    const id = Number.parseInt(args[0], 10);
    if (!Number.isFinite(id)) return "Usage: /done &lt;number from /tasks&gt;";
    const r = await env.DB.prepare("UPDATE task_requests SET status = 'done' WHERE id = ? AND status = 'open'").bind(id).run();
    return r.meta.changes ? `Closed task request ${id}.` : `No open task request ${id}.`;
  }
  if (name === "/cleared") {
    const rid = Number.parseInt(args[0], 10);
    const run = Number.isFinite(rid) ? await env.DB.prepare("SELECT run_id FROM runs WHERE rowid = ?").bind(rid).first() : null;
    if (!run) return "Usage: /cleared &lt;number from /unfinished&gt;";
    await env.DB.prepare("INSERT OR REPLACE INTO bot_state (k, v) VALUES (?, ?)").bind(`cleared:${run.run_id}`, iso(now)).run();
    return `Removed [${rid}] from /unfinished.`;
  }
  if (name === "/hint") {
    const fp = (args[0] || "").toLowerCase();
    const hint = raw.replace(/^\S+\s+\S+\s*/, "").trim();   // переносы строк сохраняются
    if (!FP_RE.test(fp) || !hint) return "Usage: /hint &lt;12-character id from an alert&gt; &lt;what to do&gt;";
    await env.DB.prepare("INSERT INTO failure_hints (fingerprint, hint, updated_at) VALUES (?, ?, ?) "
      + "ON CONFLICT(fingerprint) DO UPDATE SET hint = excluded.hint, updated_at = excluded.updated_at")
      .bind(fp, cut(hint, 1000), iso(now)).run();
    return `Saved. Every next alert with ${fp} will show this fix.`;
  }
  if (name === "/mute" || name === "/unmute") {
    const fp = (args[0] || "").toLowerCase();
    if (!FP_RE.test(fp)) return `Usage: ${name} &lt;12-character id from an alert&gt;${name === "/mute" ? " [hours]" : ""}`;
    if (name === "/unmute") {
      await env.DB.prepare("DELETE FROM mutes WHERE fingerprint = ?").bind(fp).run();
      return `Unmuted ${fp}.`;
    }
    const hours = Math.min(Math.max(Number.parseFloat(args[1] || "24") || 24, 0.5), 24 * 30);
    await mute(env, fp, hours, now);
    return `Muted ${fp} for ${hours} h.`;
  }
  return HELP;
}

// keepLonger — кнопка не укорачивает уже стоящую более долгую тишину; /mute ставит ровно заданное.
async function mute(env, fp, hours, now, keepLonger = false) {
  const set = keepLonger ? "until = max(mutes.until, excluded.until)" : "until = excluded.until";
  await env.DB.prepare(`INSERT INTO mutes (fingerprint, until) VALUES (?, ?) ON CONFLICT(fingerprint) DO UPDATE SET ${set}`)
    .bind(fp, new Date(now + hours * 3600000).toISOString()).run();
}

const parse = (text) => { try { return JSON.parse(text || "{}"); } catch (_) { return {}; } };

// Разбор одного сбоя без ИИ: ошибка, последние шаги, что осталось в Meta, известное решение.
async function explainText(env, run, now) {
  const hint = run.fingerprint ? await env.DB.prepare("SELECT hint FROM failure_hints WHERE fingerprint = ?").bind(run.fingerprint).first("hint") : null;
  // Шаги этой кампании: от её publish.spec до итога (у raw.run нет имени кампании, режем по времени).
  const spec = await env.DB.prepare("SELECT min(ts) AS ts FROM events WHERE job_id = ? AND type = 'publish.spec' AND campaign = ?")
    .bind(run.job_id, run.campaign).first("ts");
  const end = Date.parse(run.finished_at || "");
  const { results: steps } = await env.DB.prepare(
    "SELECT ts, payload FROM events WHERE job_id = ? AND type = 'raw.run' AND ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT 6",
  ).bind(run.job_id, spec || "", Number.isFinite(end) ? iso(end + 1000) : "9999").all();
  const same = run.fingerprint ? await env.DB.prepare(
    "SELECT count(*) AS n, count(DISTINCT machine_id) AS m, min(finished_at) AS first_at FROM runs WHERE fingerprint = ?",
  ).bind(run.fingerprint).first() : null;
  const lines = [`<b>Why it failed</b> — ${h(cut(run.campaign, 80))}`,
    `${h(run.manager || "—")} · ${h(run.account || "—")} · ${when(run.finished_at)}${run.stop_kind ? ` · stop: ${h(run.stop_kind)}` : ""}`,
    "", h(cut(run.error || "no error text", 600))];
  if (steps.length) {
    lines.push("", "<b>Last steps</b>");
    for (const s of steps.reverse()) {
      const p = parse(s.payload);
      const note = (p.diag && (p.diag.step || p.diag.error)) || p.note || p.step || "(unreadable step)";
      lines.push(`${p.ok === false ? "✗" : "·"} ${when(s.ts).split(", ").pop()} ${h(cut(note, 120))}`);
    }
  }
  if (run.created_any) lines.push("", "⚠️ Left in Meta: a paused campaign (and what was created before the stop). \"Publish again\" reuses it.");
  if (same && same.n > 1) lines.push("", `Seen ${same.n}× on ${same.m} machine(s) since ${when(same.first_at)}.`);
  lines.push("", hint ? `💡 Known fix: ${h(hint)}` : `No known fix yet. Add one: /hint ${run.fingerprint || "&lt;id&gt;"} &lt;what to do&gt;`);
  return lines.join("\n");
}

async function onButton(env, cq, now) {
  const chat = cq.message?.chat?.id;
  if (chat === undefined || !allowedChats(env).includes(String(chat))) return;
  const [kind, arg] = String(cq.data || "").split(":");
  if (kind === "m") {
    if (!FP_RE.test(arg || "")) return answerButton(env, cq.id, "Nothing to mute for this one.");
    await mute(env, arg, 24, now, true);
    return answerButton(env, cq.id, `Muted for 24 h (${arg}).`);
  }
  const rid = Number.parseInt(arg, 10);
  const run = Number.isFinite(rid) ? await env.DB.prepare("SELECT * FROM runs WHERE rowid = ?").bind(rid).first() : null;
  if (!run || run.outcome !== "failed") return answerButton(env, cq.id, "This failure is no longer in the hub.");
  if (kind === "e") {
    await answerButton(env, cq.id, "Looking…");
    let text;
    try { text = await explainText(env, run, now); } catch (err) {
      console.error(`explain: ${err?.message || err}`);
      text = "Could not explain this one. Open the run on the page instead.";
    }
    return sendTo(env, chat, text);
  }
  if (kind === "t") {
    const res = await env.DB.prepare("INSERT OR IGNORE INTO task_requests (fingerprint, run_id, created_at) VALUES (?, ?, ?)")
      .bind(run.fingerprint || null, run.run_id, iso(now)).run();
    return answerButton(env, cq.id, res.meta.changes ? "Task request saved. The tracker session will pick it up." : "Already requested for this failure.");
  }
  return answerButton(env, cq.id, "Unknown button.");
}

export async function handleWebhook(request, env) {
  const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
  if (!(await sameSecret(secret, env.TG_WEBHOOK_SECRET || ""))) return json({ error: "unauthorized" }, 401);
  let update = null;
  try { update = await request.json(); } catch (_) { return json({ ok: true }); }
  const now = Date.now();
  if (update?.callback_query) {
    try { await onButton(env, update.callback_query, now); } catch (err) { console.error(`button: ${err?.message || err}`); }
    return json({ ok: true });
  }
  const msg = update?.message;   // правка старого сообщения команду не повторяет
  const chat = msg?.chat?.id;
  if (chat === undefined || !allowedChats(env).includes(String(chat)) || typeof msg.text !== "string") return json({ ok: true });
  try {
    await sendTo(env, chat, await answer(env, msg.text, now));
  } catch (err) {
    console.error(`command: ${err?.message || err}`);
    await sendTo(env, chat, "Something went wrong, try again.");
  }
  return json({ ok: true });
}
