// Админ смотрит назначения любого юзера, ищет юзера по имени и задаёт имя при назначении (ACL-2919).
import { test } from "node:test";
import assert from "node:assert/strict";
import { FakeD1 } from "./fake-d1.js";
import { makeUserKey } from "../src/users.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const OWNER = ["owner", "key", "y".repeat(20)].join("_");
const SECRET = ["user", "secret", "z".repeat(20)].join("_");
const BUYER = "dan.dicker@example.com";
const ADMIN = "boss@example.com";

const makeEnv = async () => {
  const env = { DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: OWNER, USER_KEY_SECRET: SECRET,
    ALLOWED_EMAIL_DOMAINS: "example.com" };
  await env.DB.prepare("INSERT INTO users (email, name, role, created_at) VALUES (?, ?, 'buyer', 'x')").bind(BUYER, "Dan Dicker").run();
  await env.DB.prepare("INSERT INTO users (email, name, role, created_at) VALUES (?, ?, 'admin', 'x')").bind(ADMIN, "The Boss").run();
  return env;
};
const keyFor = async (env, email) => (await makeUserKey(env, email, Math.floor(Date.now() / 1000))).key;
const call = (env, method, path, key, body) => worker.fetch(new Request(`https://hub${path}`, { method,
  headers: key ? { Authorization: `Bearer ${key}` } : {},
  body: body === undefined ? undefined : JSON.stringify(body) }), env);
const ROW = (name, extra = {}) => ({ account_name: name, ad_account_id: `act_${name}`, ...extra });
const nameOf = async (env, email) => (await env.DB.prepare("SELECT name FROM users WHERE email = ?").bind(email).first())?.name;

test("assignments list: admin gets active and removed rows of one user, no proxy or tracking fields", async () => {
  const env = await makeEnv();
  await call(env, "POST", "/api/assignments", OWNER, { user_email: BUYER, rows: [ROW("A1"), ROW("B2", { profile_id: "p1" })] });
  await call(env, "POST", "/api/assignments/remove", OWNER, { user_email: BUYER, account_names: ["B2"] });
  const res = await call(env, "GET", `/api/assignments?user_email=${encodeURIComponent(BUYER)}`, OWNER);
  assert.equal(res.status, 200);
  const out = await res.json();
  assert.equal(out.user_email, BUYER);
  assert.equal(out.name, "Dan Dicker");
  assert.deepEqual(out.accounts.map((a) => [a.account_name, a.removed_at === null]), [["A1", true], ["B2", false]]);
  for (const a of out.accounts) assert.ok(!Object.keys(a).some((k) => /proxy|tracking/.test(k)));
  assert.equal(out.accounts[1].profile_id, "p1");
  // тот же состав полей, что у /v1/my-accounts
  const mine = await (await call(env, "GET", "/v1/my-accounts", await keyFor(env, BUYER))).json();
  assert.deepEqual(Object.keys(out.accounts[0]).sort(), Object.keys(mine.accounts[0]).sort());
});

test("assignments list: admin user key works; unknown user is 404; missing email is 400", async () => {
  const env = await makeEnv();
  const adminKey = await keyFor(env, ADMIN);
  assert.equal((await call(env, "GET", `/api/assignments?user_email=${BUYER}`, adminKey)).status, 200);
  assert.equal((await call(env, "GET", "/api/assignments?user_email=nobody@example.com", adminKey)).status, 404);
  assert.equal((await call(env, "GET", "/api/assignments", adminKey)).status, 400);
  // у юзера без назначений — пустой список, не ошибка
  const empty = await (await call(env, "GET", `/api/assignments?user_email=${ADMIN}`, adminKey)).json();
  assert.deepEqual(empty.accounts, []);
});

test("assignments list: buyers and anonymous get nothing", async () => {
  const env = await makeEnv();
  await call(env, "POST", "/api/assignments", OWNER, { user_email: BUYER, rows: [ROW("A1")] });
  const url = `/api/assignments?user_email=${BUYER}`;
  assert.equal((await call(env, "GET", url, null)).status, 401);
  assert.equal((await call(env, "GET", url, "garbage")).status, 401);
  const buyerRes = await call(env, "GET", url, await keyFor(env, BUYER));   // даже свой список через админский путь нельзя
  assert.equal(buyerRes.status, 403);
  assert.ok(!(await buyerRes.text()).includes("A1"));
});

test("users: name filter is exact and case-insensitive", async () => {
  const env = await makeEnv();
  const find = async (q) => (await (await call(env, "GET", `/api/users${q}`, OWNER)).json()).users.map((u) => u.email);
  assert.deepEqual(await find("?name=dan%20dicker"), [BUYER]);
  assert.deepEqual(await find("?name=DAN%20DICKER"), [BUYER]);
  assert.deepEqual(await find("?name=Dan"), []);              // не подстрока
  assert.deepEqual(await find("?name=Nobody"), []);
  assert.deepEqual((await find("")).sort(), [ADMIN, BUYER].sort());   // без фильтра — все
  assert.equal((await call(env, "GET", "/api/users?name=Dan", await keyFor(env, BUYER))).status, 403);
});

test("assign: user_name is stored for a new user, created from email + name", async () => {
  const env = await makeEnv();
  const res = await call(env, "POST", "/api/assignments", OWNER,
    { user_email: "Yinon.Avraham@example.com", user_name: "  Yinon Avraham ", rows: [ROW("E75")] });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true, assigned: 1, created: true });
  assert.equal(await nameOf(env, "yinon.avraham@example.com"), "Yinon Avraham");
  const found = await (await call(env, "GET", "/api/users?name=yinon%20avraham", OWNER)).json();
  assert.deepEqual(found.users.map((u) => u.email), ["yinon.avraham@example.com"]);
});

test("assign: user_name never overwrites an existing name; fills an empty one", async () => {
  const env = await makeEnv();
  await call(env, "POST", "/api/assignments", OWNER, { user_email: BUYER, user_name: "Other Name", rows: [ROW("A1")] });
  assert.equal(await nameOf(env, BUYER), "Dan Dicker");       // имя из Google осталось
  await env.DB.prepare("UPDATE users SET name = NULL WHERE email = ?").bind(BUYER).run();
  const res = await call(env, "POST", "/api/assignments", OWNER, { user_email: BUYER, user_name: "Dan D", rows: [ROW("A2")] });
  assert.deepEqual(await res.json(), { ok: true, assigned: 1, created: false });
  assert.equal(await nameOf(env, BUYER), "Dan D");
  await call(env, "POST", "/api/assignments", OWNER, { user_email: BUYER, rows: [ROW("A3")] });   // без user_name имя не трогаем
  assert.equal(await nameOf(env, BUYER), "Dan D");
});

test("assign: a new user on a foreign domain is refused and nothing is written", async () => {
  const env = await makeEnv();
  const res = await call(env, "POST", "/api/assignments", OWNER,
    { user_email: "someone@other.org", user_name: "Someone", rows: [ROW("A1")] });
  assert.equal(res.status, 400);
  assert.deepEqual(await res.json(), { error: "domain not allowed" });
  assert.equal((await env.DB.prepare("SELECT count(*) AS n FROM users WHERE email = 'someone@other.org'").first()).n, 0);
  assert.equal((await env.DB.prepare("SELECT count(*) AS n FROM account_assignments").first()).n, 0);
  // уже существующий юзер чужого домена (создан раньше) по-прежнему получает назначения
  await env.DB.prepare("INSERT INTO users (email, role, created_at) VALUES ('old@other.org', 'buyer', 'x')").run();
  assert.equal((await call(env, "POST", "/api/assignments", OWNER, { user_email: "old@other.org", rows: [ROW("A9")] })).status, 200);
});

test("assign: a bad user_name is 400", async () => {
  const env = await makeEnv();
  for (const bad of ["", "   ", 5, "x".repeat(121)]) {
    const res = await call(env, "POST", "/api/assignments", OWNER, { user_email: BUYER, user_name: bad, rows: [ROW("A1")] });
    assert.equal(res.status, 400, JSON.stringify(bad));
  }
});

test("assign and list: non-admins cannot assign", async () => {
  const env = await makeEnv();
  const res = await call(env, "POST", "/api/assignments", await keyFor(env, BUYER), { user_email: BUYER, user_name: "X", rows: [ROW("A1")] });
  assert.equal(res.status, 403);
  assert.equal((await call(env, "POST", "/api/assignments", null, { user_email: BUYER, rows: [ROW("A1")] })).status, 401);
});
