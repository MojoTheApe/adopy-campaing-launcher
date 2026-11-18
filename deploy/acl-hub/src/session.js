// Сессия владельца для страницы: cookie `acl_hub_s=<exp>.<hmac>`, подпись — HMAC-SHA256 ключом OWNER_KEY.
// Смена OWNER_KEY гасит все сессии. Сама cookie секрета не содержит.

export const COOKIE = "acl_hub_s";
export const TTL_S = 30 * 86400;

async function hmacHex(secret, text) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Сравнение одинаковых по длине hex-строк за постоянное время.
function sameHex(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function makeSession(env, nowSec) {
  const exp = nowSec + TTL_S;
  return `${exp}.${await hmacHex(env.OWNER_KEY, `acl-hub-session:${exp}`)}`;
}

export async function checkSession(env, value, nowSec) {
  if (!env.OWNER_KEY || typeof value !== "string") return false;
  const m = /^(\d{1,12})\.([0-9a-f]{64})$/.exec(value);
  if (!m || Number(m[1]) <= nowSec) return false;
  return sameHex(m[2], await hmacHex(env.OWNER_KEY, `acl-hub-session:${m[1]}`));
}

export function readCookie(request) {
  for (const part of (request.headers.get("cookie") || "").split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === COOKIE) return v.join("=");
  }
  return "";
}

export const setCookie = (value) =>
  `${COOKIE}=${value}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=${TTL_S}`;
export const clearCookie = () => `${COOKIE}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0`;
