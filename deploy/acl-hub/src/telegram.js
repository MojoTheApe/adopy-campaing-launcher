// Отправка в Telegram владельцу. Секреты: TG_BOT_TOKEN, TG_WEBHOOK_SECRET; список чатов TG_ALLOWED_CHATS.
// Без токена или без чатов бот молчит, приём событий от этого не зависит.

export const escapeHtml = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
export const cut = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
export const hubUrl = (env) => (env.HUB_URL || "https://acl-hub.acl-hub.workers.dev").replace(/\/$/, "");
export const runLink = (env, runId) => `${hubUrl(env)}/#run=${encodeURIComponent(runId)}`;

export function allowedChats(env) {
  return String(env.TG_ALLOWED_CHATS || "").split(",").map((s) => s.trim()).filter((s) => /^-?\d+$/.test(s));
}

export const botReady = (env) => Boolean(env.TG_BOT_TOKEN) && allowedChats(env).length > 0;

// Одно сообщение одному чату. Ошибка Telegram дальше не бросается: телеметрия не падает из-за бота.
export async function sendTo(env, chatId, text) {
  if (!env.TG_BOT_TOKEN) return false;
  try {
    const res = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/sendMessage`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: String(chatId), text: cut(text, 4000), parse_mode: "HTML",
        disable_web_page_preview: true }),
    });
    return res.ok;
  } catch (_) {
    return false;
  }
}

export async function broadcast(env, text) {
  if (!botReady(env)) return 0;
  let n = 0;
  for (const chat of allowedChats(env)) if (await sendTo(env, chat, text)) n++;
  return n;
}
