// Авторизация хаба: машины — токеном библиотеки команды (проверка через GitHub),
// владелец — секретом OWNER_KEY. Сам токен нигде не хранится: в D1 только sha256.

const FRESH_MS = 60 * 60 * 1000;          // запись о токене свежа 1 час
const GRACE_MS = 24 * 60 * 60 * 1000;     // при падении GitHub верим записи до 24 часов
const DEFAULT_REPO = "MojoTheApe/acl-lib";

export function bearer(request) {
  const m = /^Bearer\s+(\S+)$/.exec(request.headers.get("Authorization") || "");
  return m ? m[1] : "";
}

export async function sha256hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Сравнение за постоянное время: сравниваются хэши одинаковой длины.
export async function sameSecret(a, b) {
  if (!a || !b) return false;
  const [x, y] = await Promise.all([sha256hex(a), sha256hex(b)]);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

export async function isOwner(request, env) {
  return sameSecret(bearer(request), env.OWNER_KEY || "");
}

// "ok" | "denied" | "unavailable".
export async function authorize(token, env) {
  if (!token) return "denied";
  const key = await sha256hex(token);
  const now = Date.now();
  let checkedAt = 0;
  try {
    checkedAt = Number(await env.DB.prepare("SELECT checked_at FROM tokens WHERE token_sha = ?").bind(key).first("checked_at")) || 0;
  } catch (_) { /* сбой чтения = нет записи */ }
  if (checkedAt && now - checkedAt < FRESH_MS) return "ok";
  let status = 0;
  try {
    const resp = await fetch(`https://api.github.com/repos/${env.LIBRARY_REPO || DEFAULT_REPO}`, {
      headers: { Authorization: `Bearer ${token}`, "User-Agent": "acl-hub", Accept: "application/vnd.github+json" },
    });
    status = resp.status;
  } catch (_) { status = 0; }
  // Сбой записи о токене не меняет решение: GitHub уже ответил.
  if (status === 200) {
    try {
      await env.DB.prepare("INSERT OR REPLACE INTO tokens (token_sha, checked_at) VALUES (?, ?)").bind(key, now).run();
    } catch (_) { /* следующая попытка спросит GitHub снова */ }
    return "ok";
  }
  if (status === 401 || status === 403 || status === 404) {
    try {
      await env.DB.prepare("DELETE FROM tokens WHERE token_sha = ?").bind(key).run();
    } catch (_) { /* запись истечёт сама: свежесть 1 час */ }
    return "denied";
  }
  // GitHub лежит (5xx или сеть): рабочая машина продолжает писать по записи не старше суток.
  return checkedAt && now - checkedAt < GRACE_MS ? "ok" : "unavailable";
}
