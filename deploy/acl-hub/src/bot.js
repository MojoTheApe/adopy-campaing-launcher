// Webhook Telegram: команды владельца. Секрет webhook + список чатов; чужие чаты молча игнорируются.
import { json } from "./respond.js";
import { sameSecret } from "./auth.js";
import { allowedChats, sendTo } from "./telegram.js";
import { dayText, failuresText, ownerDay } from "./alerts.js";

const HELP = [
  "<b>ACL Control bot</b>",
  "/today — buyers today",
  "/yesterday — buyers yesterday",
  "/failures — top failures, last 24 h",
  "/mute &lt;id&gt; [hours] — silence one failure (default 24 h)",
  "/unmute &lt;id&gt;",
].join("\n");

async function answer(env, text, now) {
  const [cmd, ...args] = String(text || "").trim().split(/\s+/);
  const name = (cmd || "").split("@")[0].toLowerCase();
  if (name === "/today") return (await dayText(env, ownerDay(now), "Today")).join("\n");
  if (name === "/yesterday") return (await dayText(env, ownerDay(now, -1), "Yesterday")).join("\n");
  if (name === "/failures") return ["<b>Failures, last 24 h</b>", ...(await failuresText(env, now))].join("\n");
  if (name === "/mute" || name === "/unmute") {
    const fp = (args[0] || "").toLowerCase();
    if (!/^[0-9a-f]{12}$/.test(fp)) return `Usage: ${name} &lt;12-character id from an alert&gt;${name === "/mute" ? " [hours]" : ""}`;
    if (name === "/unmute") {
      await env.DB.prepare("DELETE FROM mutes WHERE fingerprint = ?").bind(fp).run();
      return `Unmuted ${fp}.`;
    }
    const hours = Math.min(Math.max(Number.parseFloat(args[1] || "24") || 24, 0.5), 24 * 30);
    await env.DB.prepare("INSERT INTO mutes (fingerprint, until) VALUES (?, ?) "
      + "ON CONFLICT(fingerprint) DO UPDATE SET until = excluded.until")
      .bind(fp, new Date(now + hours * 3600000).toISOString()).run();
    return `Muted ${fp} for ${hours} h.`;
  }
  return HELP;
}

export async function handleWebhook(request, env) {
  const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
  if (!(await sameSecret(secret, env.TG_WEBHOOK_SECRET || ""))) return json({ error: "unauthorized" }, 401);
  let update = null;
  try { update = await request.json(); } catch (_) { return json({ ok: true }); }
  const msg = update?.message || update?.edited_message;
  const chat = msg?.chat?.id;
  if (chat === undefined || !allowedChats(env).includes(String(chat)) || typeof msg.text !== "string") return json({ ok: true });
  try {
    await sendTo(env, chat, await answer(env, msg.text, Date.now()));
  } catch (_) {
    await sendTo(env, chat, "Something went wrong, try again.");
  }
  return json({ ok: true });
}
