// Юзеры и аккаунты (ACL-2898): миграция, вход через Google, ключ юзера, my-accounts, назначения админом.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { FakeD1 } from "./fake-d1.js";
import { makeUserKey } from "../src/users.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const OWNER = ["owner", "key", "y".repeat(20)].join("_");
const SECRET = ["user", "secret", "z".repeat(20)].join("_");
const CLIENT = "client-123.apps.example";
const now = () => Math.floor(Date.now() / 1000);

const makeEnv = () => ({ DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: OWNER, USER_KEY_SECRET: SECRET,
  ALLOWED_EMAIL_DOMAINS: "Example.com, corp.test", GOOGLE_CLIENT_ID: CLIENT });

let tokeninfo, calls, realFetch;
beforeEach(() => {
  calls = []; realFetch = globalThis.fetch;
  tokeninfo = { email: "buyer.one@example.com", email_verified: "true", azp: CLIENT, aud: CLIENT, name: "Buyer One" };
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    if (tokeninfo === "down") throw new Error("down");
    if (typeof tokeninfo === "number") return new Response("{}", { status: tokeninfo });
    return new Response(JSON.stringify(tokeninfo), { status: 200 });
  };
});
afterEach(() => { globalThis.fetch = realFetch; });

const GTOKEN = ["ya29", "google", "q".repeat(20)].join(".");
const session = (env, token = GTOKEN) => worker.fetch(new Request("https://hub/v1/session", { method: "POST",
  body: JSON.stringify({ google_access_token: token }) }), env);
const call = (env, method, path, key, body, extra = {}) => worker.fetch(new Request(`https://hub${path}`, { method,
  headers: { ...(key ? { Authorization: `Bearer ${key}` } : {}), ...extra },
  body: body === undefined ? undefined : JSON.stringify(body) }), env);
const addUser = (env, email, role = "buyer", disabled = 0) => env.DB.prepare(
  "INSERT INTO users (email, role, created_at, disabled) VALUES (?, ?, 'x', ?)").bind(email, role, disabled).run();
const keyFor = async (env, email) => (await makeUserKey(env, email, now())).key;
const ROW = (name, extra = {}) => ({ account_name: name, ad_account_id: `act_${name}`, page_id: "p1", ...extra });

test("migration 0008 creates tables and index on top of 0001-0005", async () => {
  const db = new FakeD1();
  const dir = fileURLToPath(MIGRATIONS);
  for (const n of readdirSync(dir).filter((f) => /^000[1-5]_/.test(f)).sort()) db.raw.exec(readFileSync(dir + n, "utf8"));
  db.raw.exec(readFileSync(dir + "0008_accounts_per_user.sql", "utf8"));
  const { results } = await db.prepare("SELECT name, type FROM sqlite_master WHERE name IN ('users','account_assignments','idx_assignments_user')").all();
  assert.deepEqual(results.map((r) => r.name).sort(), ["account_assignments", "idx_assignments_user", "users"]);
  const cols = (await db.prepare("PRAGMA table_info(account_assignments)").all()).results.map((c) => c.name);
  assert.ok(!cols.some((c) => /proxy|tracking/.test(c)));
});

test("session: valid token gives 30-day key, creates buyer, keeps role, never stores google token", async () => {
  const env = makeEnv();
  const res = await session(env);
  assert.equal(res.status, 200);
  const out = await res.json();
  assert.equal(out.email, "buyer.one@example.com");
  assert.equal(out.role, "buyer");
  assert.ok(Math.abs(Date.parse(out.expires_at) / 1000 - (now() + 30 * 86400)) < 5);
  assert.match(out.key, /^buyer\.one@example\.com\.\d+\.[0-9a-f]{64}$/);
  assert.match(calls[0].url, /^https:\/\/oauth2\.googleapis\.com\/tokeninfo\?access_token=/);
  assert.equal(calls[0].init.headers["User-Agent"], "acl-hub");
  await env.DB.prepare("UPDATE users SET role = 'admin'").run();
  assert.equal((await (await session(env)).json()).role, "admin");
  const dump = JSON.stringify((await env.DB.prepare("SELECT * FROM users").all()).results);
  assert.ok(!dump.includes(GTOKEN));
});

test("session: refuses unverified email, foreign domain, other client, disabled user", async () => {
  const env = makeEnv();
  tokeninfo = { ...tokeninfo, email_verified: "false" };
  assert.equal((await session(env)).status, 403);
  tokeninfo = { ...tokeninfo, email_verified: true, email: "x@evil.test" };
  assert.equal((await session(env)).status, 403);
  tokeninfo = { email: "buyer.one@example.com", email_verified: true, azp: "other", aud: "other" };
  assert.equal((await session(env)).status, 403);
  tokeninfo = { email: "buyer.one@example.com", email_verified: true, azp: "other", aud: CLIENT };
  assert.equal((await session(env)).status, 200);          // достаточно совпадения aud
  await env.DB.prepare("UPDATE users SET disabled = 1").run();
  assert.equal((await session(env)).status, 403);
  assert.equal((await session(env, "")).status, 400);
});

test("session: google unreachable is 503, rejected token is 401", async () => {
  const env = makeEnv();
  tokeninfo = "down";
  assert.equal((await session(env)).status, 503);
  tokeninfo = 500;
  assert.equal((await session(env)).status, 503);
  tokeninfo = 400;
  assert.equal((await session(env)).status, 401);
});

test("user key: tampered, expired, unknown and disabled users are refused", async () => {
  const env = makeEnv();
  await addUser(env, "a.b@example.com");
  const good = await keyFor(env, "a.b@example.com");
  assert.equal((await call(env, "GET", "/v1/my-accounts", good)).status, 200);
  assert.equal((await call(env, "GET", "/v1/my-accounts", good.replace(/.$/, (c) => (c === "0" ? "1" : "0")))).status, 401);
  assert.equal((await call(env, "GET", "/v1/my-accounts", "")).status, 401);
  const expired = good.replace(/\.(\d+)\./, `.${now() - 10}.`);
  assert.equal((await call(env, "GET", "/v1/my-accounts", expired)).status, 401);
  assert.equal((await call(env, "GET", "/v1/my-accounts", await keyFor(env, "nobody@example.com"))).status, 401);
  await env.DB.prepare("UPDATE users SET disabled = 1").run();
  assert.equal((await call(env, "GET", "/v1/my-accounts", good)).status, 401);
});

test("my-accounts: own rows only, includes removed, 304 on same ETag", async () => {
  const env = makeEnv();
  await addUser(env, "a@example.com"); await addUser(env, "b@example.com");
  await call(env, "POST", "/api/assignments", OWNER, { user_email: "a@example.com", rows: [ROW("A1"), ROW("A2")] });
  await call(env, "POST", "/api/assignments", OWNER, { user_email: "b@example.com", rows: [ROW("B1")] });
  await call(env, "POST", "/api/assignments/remove", OWNER, { user_email: "a@example.com", account_names: ["A2"] });
  const key = await keyFor(env, "a@example.com");
  const res = await call(env, "GET", "/v1/my-accounts", key);
  const { accounts } = await res.json();
  assert.deepEqual(accounts.map((a) => a.account_name), ["A1", "A2"]);
  assert.equal(accounts[0].removed_at, null);
  assert.ok(accounts[1].removed_at);
  const etag = res.headers.get("etag");
  assert.ok(etag);
  const same = await call(env, "GET", "/v1/my-accounts", key, undefined, { "If-None-Match": etag });
  assert.equal(same.status, 304);
  assert.equal(await same.text(), "");
  await call(env, "POST", "/api/assignments", OWNER, { user_email: "a@example.com", rows: [ROW("A3")] });
  const changed = await call(env, "GET", "/v1/my-accounts", key, undefined, { "If-None-Match": etag });
  assert.equal(changed.status, 200);
});

test("admin endpoints: no auth 401, buyer 403, admin user and owner pass", async () => {
  const env = makeEnv();
  await addUser(env, "buyer@example.com"); await addUser(env, "boss@example.com", "admin");
  const body = { user_email: "t@example.com", rows: [ROW("T1")] };
  for (const [m, p, b] of [["GET", "/api/users"], ["POST", "/api/assignments", body],
    ["POST", "/api/assignments/remove", { user_email: "t@example.com", account_names: ["T1"] }]]) {
    assert.equal((await call(env, m, p, "", b)).status, 401, p);
    assert.equal((await call(env, m, p, await keyFor(env, "buyer@example.com"), b)).status, 403, p);
  }
  const boss = await keyFor(env, "boss@example.com");
  assert.equal((await call(env, "POST", "/api/assignments", boss, body)).status, 200);
  const row = await env.DB.prepare("SELECT * FROM account_assignments").first();
  assert.equal(row.assigned_by, "boss@example.com");
  assert.ok(row.assigned_at);
  await call(env, "POST", "/api/assignments", OWNER, body);
  assert.equal((await env.DB.prepare("SELECT assigned_by FROM account_assignments").first()).assigned_by, "owner");
  const users = await (await call(env, "GET", "/api/users", boss)).json();
  const t = users.users.find((u) => u.email === "t@example.com");
  assert.equal(t.role, "buyer");                       // неизвестный юзер создан как buyer
  assert.equal(t.active_accounts, 1);
});

test("assignments: re-assign clears removed_at; remove never deletes", async () => {
  const env = makeEnv();
  const base = { user_email: "a@example.com", rows: [ROW("A1")] };
  await call(env, "POST", "/api/assignments", OWNER, base);
  const rm = await call(env, "POST", "/api/assignments/remove", OWNER, { user_email: "a@example.com", account_names: ["A1"] });
  assert.equal((await rm.json()).removed, 1);
  const n = await env.DB.prepare("SELECT count(*) AS n, max(removed_at) AS r FROM account_assignments").first();
  assert.equal(n.n, 1);
  assert.ok(n.r);
  await call(env, "POST", "/api/assignments", OWNER, { user_email: "a@example.com", rows: [ROW("A1", { country: "DE" })] });
  const row = await env.DB.prepare("SELECT * FROM account_assignments").first();
  assert.equal(row.removed_at, null);
  assert.equal(row.country, "DE");
});

test("assignments: account name is case-insensitive (one row, remove by other case)", async () => {
  const env = makeEnv();
  await call(env, "POST", "/api/assignments", OWNER, { user_email: "a@example.com", rows: [ROW("UL921")] });
  await call(env, "POST", "/api/assignments", OWNER, { user_email: "a@example.com", rows: [ROW("ul921", { country: "CR" })] });
  const n = await env.DB.prepare("SELECT count(*) AS n, max(country) AS c FROM account_assignments").first();
  assert.equal(n.n, 1);
  assert.equal(n.c, "CR");
  const rm = await call(env, "POST", "/api/assignments/remove", OWNER, { user_email: "a@example.com", account_names: ["Ul921"] });
  assert.equal((await rm.json()).removed, 1);
});

test("assignments: unknown or proxy/tracking fields are 400 and nothing is written", async () => {
  const env = makeEnv();
  for (const bad of [{ proxy_host: "1.2.3.4" }, { proxy_password: "x" }, { tracking_link: "http://t" }, { whatever: 1 }]) {
    const res = await call(env, "POST", "/api/assignments", OWNER, { user_email: "a@example.com", rows: [ROW("A1"), ROW("A2", bad)] });
    assert.equal(res.status, 400, JSON.stringify(bad));
  }
  assert.equal((await env.DB.prepare("SELECT count(*) AS n FROM account_assignments").first()).n, 0);
  assert.equal((await env.DB.prepare("SELECT count(*) AS n FROM users").first()).n, 0);
});

test("assignments: a failing row rolls back the whole batch", async () => {
  const env = makeEnv();
  // Триггер имитирует сбой D1 на второй строке: всё, что записано до неё, должно откатиться.
  await env.DB.exec("CREATE TRIGGER boom BEFORE INSERT ON account_assignments WHEN NEW.account_name = 'BAD' "
    + "BEGIN SELECT RAISE(ABORT, 'boom'); END");
  const res = await call(env, "POST", "/api/assignments", OWNER, { user_email: "a@example.com", rows: [ROW("A1"), ROW("BAD")] });
  assert.equal(res.status, 400);
  assert.equal((await env.DB.prepare("SELECT count(*) AS n FROM account_assignments").first()).n, 0);
  assert.equal((await env.DB.prepare("SELECT count(*) AS n FROM users").first()).n, 0);
});

test("role: owner key only, sets first admin", async () => {
  const env = makeEnv();
  await addUser(env, "a@example.com"); await addUser(env, "boss@example.com", "admin");
  const body = { email: "a@example.com", role: "admin" };
  assert.equal((await call(env, "POST", "/api/users/role", "", body)).status, 401);
  assert.equal((await call(env, "POST", "/api/users/role", await keyFor(env, "boss@example.com"), body)).status, 403);
  assert.equal((await call(env, "POST", "/api/users/role", OWNER, { email: "a@example.com", role: "root" })).status, 400);
  assert.equal((await call(env, "POST", "/api/users/role", OWNER, { email: "nobody@example.com", role: "admin" })).status, 404);
  assert.equal((await call(env, "POST", "/api/users/role", OWNER, body)).status, 200);
  assert.equal((await env.DB.prepare("SELECT role FROM users WHERE email = 'a@example.com'").first()).role, "admin");
});
