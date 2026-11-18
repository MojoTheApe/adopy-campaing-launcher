// Отправка в Telegram владельцу. Секреты: TG_BOT_TOKEN, TG_WEBHOOK_SECRET, TG_ALLOWED_CHATS (id через запятую).
// Без токена или без чатов бот молчит, приём событий от этого не зависит.

export const escapeHtml = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
export const cut = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
export const hubUrl = (env) => (env.HUB_URL || "https://acl-hub.acl-hub.workers.dev").replace(/\/$/, "");
export const runLink = (env, runId) => `${hubUrl(env)}/#run=${encodeURIComponent(runId)}`;
const LIMIT = 4000;   // у Telegram 4096; запас на разметку

export function allowedChats(env) {
  return String(env.TG_ALLOWED_CHATS || "").split(",").map((s) => s.trim()).filter((s) => /^-?\d+$/.test(s));
}

export const botReady = (env) => Boolean(env.TG_BOT_TOKEN) && allowedChats(env).length > 0;

// Делить по строкам: тег или сущность HTML никогда не рвутся посередине (строки собираются целыми).
export function splitMessage(text, limit = LIMIT) {
  const parts = [];
  let cur = "";
  for (const line of String(text).split("\n")) {
    const piece = line.length > limit ? plain(line).slice(0, limit - 1) + "…" : line;
    if (cur && cur.length + 1 + piece.length > limit) { parts.push(cur); cur = piece; } else cur = cur ? `${cur}\n${piece}` : piece;
  }
  if (cur) parts.push(cur);
  return parts;
}

// Текст без разметки: на случай, если Telegram не разобрал HTML.
export function plain(html) {
  return String(html).replace(/<[^>]*>/g, "")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&amp;/g, "&");
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function call(env, payload, method = "sendMessage") {
  const res = await fetch(`https://api.telegram.org/bot${env.TG_BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload),
  });
  let info = {};
  try { info = await res.json(); } catch (_) { /* пусто */ }
  return { status: res.status, retryAfter: Number(info?.parameters?.retry_after) || 0, description: info?.description || "" };
}

// Одно сообщение (или несколько частей) одному чату. extra (кнопки) — на последней части.
// Ошибки не бросаются: телеметрия не падает из-за бота.
export async function sendTo(env, chatId, text, extra = {}) {
  if (!env.TG_BOT_TOKEN) return false;
  let allOk = true;
  const parts = splitMessage(text);
  for (const [i, part] of parts.entries()) {
    const base = { chat_id: String(chatId), disable_web_page_preview: true, ...(i === parts.length - 1 ? extra : {}) };
    try {
      let r = await call(env, { ...base, text: part, parse_mode: "HTML" });
      if (r.status === 429 && r.retryAfter && r.retryAfter <= 5) {
        await sleep(r.retryAfter * 1000);
        r = await call(env, { ...base, text: part, parse_mode: "HTML" });
      }
      if (r.status !== 200 && r.status !== 429 && base.reply_markup) {
        // Кнопки могли не понравиться Telegram (BUTTON_URL_INVALID и т. п.): тот же текст без кнопок.
        console.error(`telegram ${r.status}: ${r.description}; resending without buttons`);
        const { reply_markup, ...noButtons } = base;
        r = await call(env, { ...noButtons, text: part, parse_mode: "HTML" });
      }
      if (r.status !== 200 && r.status !== 429) {
        console.error(`telegram ${r.status}: ${r.description}; resending as plain text`);
        const { reply_markup, ...noButtons } = base;
        r = await call(env, { ...noButtons, text: plain(part) });
      }
      if (r.status !== 200) { console.error(`telegram send failed ${r.status}: ${r.description}`); allOk = false; }
    } catch (err) {
      console.error(`telegram send error: ${err?.message || err}`);
      allOk = false;
    }
  }
  return allOk;
}

export async function broadcast(env, text, extra = {}) {
  if (!botReady(env)) return 0;
  let n = 0;
  for (const chat of allowedChats(env)) if (await sendTo(env, chat, text, extra)) n++;
  return n;
}

// Ответ на нажатие кнопки (короткая всплывающая надпись).
export async function answerButton(env, callbackId, text) {
  if (!env.TG_BOT_TOKEN) return;
  try { await call(env, { callback_query_id: callbackId, text: cut(text, 190) }, "answerCallbackQuery"); } catch (_) { /* не важно */ }
}
