// Юзеры и их аккаунты (ACL-2898): вход через Google, ключ юзера, «мои аккаунты», назначения админом.
// Токен Google нигде не хранится и не логируется: он живёт только внутри handleSession.
import { json } from "./respond.js";
import { bearer, isOwner, sameSecret, sha256hex } from "./auth.js";

export const USER_TTL_S = 30 * 86400;
export const ROLES = ["admin", "buyer"];
// Поля строки назначения, которые можно писать. Всё остальное (в т.ч. proxy_*, tracking_link) — 400.
export const ROW_FIELDS = ["account_name", "ad_account_id", "page_id", "page_name", "country",
  "multilogin_instance", "folder_id", "profile_id"];

async function hmacHex(secret, text) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function makeUserKey(env, email, nowSec) {
  const exp = nowSec + USER_TTL_S;
  return { exp, key: `${email}.${exp}.${await hmacHex(env.USER_KEY_SECRET, `acl-hub-user:${email}.${exp}`)}` };
}

// Юзер по ключу из Bearer: подпись, срок, юзер есть и не отключён. Иначе null.
// Email может содержать точки, поэтому ключ разбирается справа: <email>.<exp>.<hmac>.
export async function userFromRequest(request, env) {
  const token = bearer(request);
  if (!token || !env.USER_KEY_SECRET) return null;
  const m = /^(.+)\.(\d{1,12})\.([0-9a-f]{64})$/.exec(token);
  if (!m || Number(m[2]) <= Math.floor(Date.now() / 1000)) return null;
  const expected = await hmacHex(env.USER_KEY_SECRET, `acl-hub-user:${m[1]}.${m[2]}`);
  if (!(await sameSecret(m[3], expected))) return null;
  const user = await env.DB.prepare("SELECT * FROM users WHERE email = ?").bind(m[1]).first();
  return user && !user.disabled ? user : null;
}

// Админ = владелец (OWNER_KEY / cookie) или юзер с ролью admin. { by } — кто назначает.
async function adminFrom(request, env) {
  if (await isOwner(request, env)) return { by: "owner" };
  const user = await userFromRequest(request, env);
  return user && user.role === "admin" ? { by: user.email } : null;
}

const domainOf = (email) => email.slice(email.lastIndexOf("@") + 1).toLowerCase();
const truthy = (v) => v === true || v === "true";

// POST /v1/session {google_access_token} -> {key, email, role, expires_at}
export async function handleSession(request, env) {
  let token = "";
  try { token = String((await request.json())?.google_access_token || ""); } catch (_) { /* пусто */ }
  if (!token) return json({ error: "google_access_token required" }, 400);
  if (!env.USER_KEY_SECRET) return json({ error: "not configured" }, 503);
  let info;
  try {
    const resp = await fetch(`https://oauth2.googleapis.com/tokeninfo?access_token=${encodeURIComponent(token)}`,
      { headers: { "User-Agent": "acl-hub" } });
    if (resp.status >= 500) return json({ error: "google unavailable" }, 503);
    if (!resp.ok) return json({ error: "invalid token" }, 401);
    info = await resp.json();
  } catch (_) {
    return json({ error: "google unavailable" }, 503);
  }
  const email = String(info?.email || "").toLowerCase();
  const domains = String(env.ALLOWED_EMAIL_DOMAINS || "").split(",").map((d) => d.trim().toLowerCase()).filter(Boolean);
  const client = env.GOOGLE_CLIENT_ID || "";
  if (!email.includes("@") || !truthy(info.email_verified)) return json({ error: "email not verified" }, 403);
  if (!domains.includes(domainOf(email))) return json({ error: "domain not allowed" }, 403);
  if (!client || (info.azp !== client && info.aud !== client)) return json({ error: "wrong client" }, 403);
  const now = Math.floor(Date.now() / 1000);
  await env.DB.prepare("INSERT OR IGNORE INTO users (email, name, created_at) VALUES (?, ?, ?)")
    .bind(email, info.name ? String(info.name) : null, new Date(now * 1000).toISOString()).run();
  const user = await env.DB.prepare("SELECT * FROM users WHERE email = ?").bind(email).first();
  if (user.disabled) return json({ error: "user disabled" }, 403);
  const { key, exp } = await makeUserKey(env, email, now);
  return json({ key, email, role: user.role, expires_at: new Date(exp * 1000).toISOString() });
}

// GET /v1/my-accounts: строки юзера, активные и снятые; ETag по содержимому.
export async function handleMyAccounts(request, env) {
  const user = await userFromRequest(request, env);
  if (!user) return json({ error: "unauthorized" }, 401);
  const { results } = await env.DB.prepare(
    "SELECT * FROM account_assignments WHERE user_email = ? ORDER BY account_name").bind(user.email).all();
  const body = JSON.stringify({ accounts: results });
  const etag = `"${(await sha256hex(body)).slice(0, 32)}"`;
  if (request.headers.get("If-None-Match") === etag) return new Response(null, { status: 304, headers: { etag } });
  return new Response(body, { headers: { "content-type": "application/json", etag } });
}

export const SEED_MAX_ROWS = 2000;

// POST /v1/my-accounts/seed {rows} (ACL-2901): первый вход юзера переносит его текущую карту с машины.
// Принимается, только пока у юзера нет ни одной строки назначений (в т.ч. снятых): после этого
// аккаунтами управляет админ. Поля — те же ROW_FIELDS, proxy_*/tracking_link дают 400.
export async function handleSeed(request, env) {
  const user = await userFromRequest(request, env);
  if (!user) return json({ error: "unauthorized" }, 401);
  let body = null;
  try { body = await request.json(); } catch (_) { return json({ error: "bad json" }, 400); }
  const v = validRows(body?.rows);
  if (v.error) return json({ error: v.error }, 400);
  if (v.rows.length > SEED_MAX_ROWS) return json({ error: "too many rows" }, 400);
  const has = await env.DB.prepare("SELECT 1 AS x FROM account_assignments WHERE user_email = ? LIMIT 1")
    .bind(user.email).first();
  if (has) return json({ error: "already has assignments" }, 409);
  const nowIso = new Date().toISOString();
  // Условие в каждой вставке: если админ успел назначить что-то между проверкой и вставкой,
  // сид не смешивается с его строками. Повтор того же сида (две машины разом) — DO NOTHING.
  const res = await env.DB.batch(v.rows.map((r) => env.DB.prepare(
    "INSERT INTO account_assignments (user_email, account_name, ad_account_id, page_id, page_name, country, "
    + "multilogin_instance, folder_id, profile_id, assigned_by, assigned_at, removed_at) "
    + "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, 'seed', ?, NULL "
    + "WHERE NOT EXISTS (SELECT 1 FROM account_assignments WHERE user_email = ? AND assigned_by <> 'seed') "
    + "ON CONFLICT (user_email, account_name) DO NOTHING",
  ).bind(user.email, r.account_name, r.ad_account_id, r.page_id, r.page_name, r.country, r.multilogin_instance,
    r.folder_id, r.profile_id, nowIso, user.email)));
  return json({ ok: true, seeded: res.reduce((s, r) => s + r.meta.changes, 0) });
}

// GET /api/users
export async function listUsers(env) {
  const { results } = await env.DB.prepare(
    "SELECT u.email, u.name, u.role, u.created_at, u.disabled, "
    + "(SELECT count(*) FROM account_assignments a WHERE a.user_email = u.email AND a.removed_at IS NULL) AS active_accounts "
    + "FROM users u ORDER BY u.email").all();
  return json({ users: results });
}

const str = (v) => (typeof v === "string" && v.trim() ? v.trim() : null);

// Проверка строк назначения: возвращает { rows } или { error }.
function validRows(rows) {
  if (!Array.isArray(rows) || !rows.length) return { error: "rows required" };
  const out = [];
  for (const r of rows) {
    if (!r || typeof r !== "object" || Array.isArray(r)) return { error: "bad row" };
    for (const k of Object.keys(r)) if (!ROW_FIELDS.includes(k)) return { error: `unknown field: ${k}` };
    if (!str(r.account_name)) return { error: "account_name required" };
    out.push(Object.fromEntries(ROW_FIELDS.map((k) => [k, str(r[k])])));
  }
  return { rows: out };
}

// POST /api/assignments {user_email, rows}. Неизвестного юзера создаём покупателем (buyer): админ может
// назначить аккаунты раньше, чем человек первый раз войдёт.
async function assign(env, admin, body) {
  const email = String(body?.user_email || "").trim().toLowerCase();
  if (!email.includes("@")) return json({ error: "user_email required" }, 400);
  const v = validRows(body.rows);
  if (v.error) return json({ error: v.error }, 400);
  const nowIso = new Date().toISOString();
  const stmts = [env.DB.prepare("INSERT OR IGNORE INTO users (email, created_at) VALUES (?, ?)").bind(email, nowIso)];
  for (const r of v.rows) {
    stmts.push(env.DB.prepare(
      "INSERT INTO account_assignments (user_email, account_name, ad_account_id, page_id, page_name, country, "
      + "multilogin_instance, folder_id, profile_id, assigned_by, assigned_at, removed_at) "
      + "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
      + "ON CONFLICT (user_email, account_name) DO UPDATE SET ad_account_id = excluded.ad_account_id, "
      + "page_id = excluded.page_id, page_name = excluded.page_name, country = excluded.country, "
      + "multilogin_instance = excluded.multilogin_instance, folder_id = excluded.folder_id, "
      + "profile_id = excluded.profile_id, assigned_by = excluded.assigned_by, "
      + "assigned_at = excluded.assigned_at, removed_at = NULL",
    ).bind(email, r.account_name, r.ad_account_id, r.page_id, r.page_name, r.country, r.multilogin_instance,
      r.folder_id, r.profile_id, admin.by, nowIso));
  }
  await env.DB.batch(stmts);
  return json({ ok: true, assigned: v.rows.length });
}

// POST /api/assignments/remove {user_email, account_names}: мягкое снятие, строки не удаляются.
async function removeAssignments(env, body) {
  const email = String(body?.user_email || "").trim().toLowerCase();
  const names = body?.account_names;
  if (!email || !Array.isArray(names) || !names.length || names.some((n) => !str(n))) {
    return json({ error: "user_email and account_names required" }, 400);
  }
  const nowIso = new Date().toISOString();
  const res = await env.DB.batch(names.map((n) => env.DB.prepare(
    "UPDATE account_assignments SET removed_at = ? WHERE user_email = ? AND account_name = ? AND removed_at IS NULL")
    .bind(nowIso, email, n.trim())));
  return json({ ok: true, removed: res.reduce((s, r) => s + r.meta.changes, 0) });
}

// POST /api/users/role {email, role}: только OWNER_KEY (так задаётся первый админ).
async function setRole(env, body) {
  const email = String(body?.email || "").trim().toLowerCase();
  if (!email.includes("@") || !ROLES.includes(body?.role)) return json({ error: "email and role (admin|buyer) required" }, 400);
  const r = await env.DB.prepare("UPDATE users SET role = ? WHERE email = ?").bind(body.role, email).run();
  if (!r.meta.changes) return json({ error: "unknown user" }, 404);
  return json({ ok: true });
}

// Маршруты /api/users*, /api/assignments*. null — путь не наш.
export async function handleUserApi(request, env, url) {
  const method = request.method;
  const need = { "GET /api/users": "admin", "POST /api/assignments": "admin",
    "POST /api/assignments/remove": "admin", "POST /api/users/role": "owner" }[`${method} ${url.pathname}`];
  if (!need) return null;
  let admin = null;
  const denied = async (hasKey) => (hasKey ? json({ error: "forbidden" }, 403) : json({ error: "unauthorized" }, 401));
  if (need === "owner") {
    const b = bearer(request);
    if (!(await sameSecret(b, env.OWNER_KEY || ""))) return denied(Boolean(await userFromRequest(request, env)));
  } else {
    admin = await adminFrom(request, env);
    if (!admin) return denied(Boolean(await userFromRequest(request, env)));
  }
  try {
    if (method === "GET") return await listUsers(env);
    let body = null;
    try { body = await request.json(); } catch (_) { return json({ error: "bad json" }, 400); }
    if (url.pathname === "/api/assignments") return await assign(env, admin, body);
    if (url.pathname === "/api/assignments/remove") return await removeAssignments(env, body);
    return await setRole(env, body);
  } catch (_) {
    return json({ error: "bad request" }, 400);
  }
}
