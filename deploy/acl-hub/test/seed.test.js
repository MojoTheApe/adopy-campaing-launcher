// Первый вход переносит карту машины (ACL-2901): POST /v1/my-accounts/seed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { FakeD1 } from "./fake-d1.js";
import { makeUserKey } from "../src/users.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const OWNER = ["owner", "key", "y".repeat(20)].join("_");
const SECRET = ["user", "secret", "z".repeat(20)].join("_");
const EMAIL = "buyer.one@example.com";

const makeEnv = async () => {
  const env = { DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: OWNER, USER_KEY_SECRET: SECRET };
  await env.DB.prepare("INSERT INTO users (email, role, created_at) VALUES (?, 'buyer', 'x')").bind(EMAIL).run();
  return env;
};
const keyFor = async (env, email = EMAIL) => (await makeUserKey(env, email, Math.floor(Date.now() / 1000))).key;
const call = (env, method, path, key, body) => worker.fetch(new Request(`https://hub${path}`, { method,
  headers: key ? { Authorization: `Bearer ${key}` } : {},
  body: body === undefined ? undefined : JSON.stringify(body) }), env);
const ROW = (name, extra = {}) => ({ account_name: name, ad_account_id: `act_${name}`, profile_id: "pr1", ...extra });
const rowsOf = async (env) => (await env.DB.prepare(
  "SELECT * FROM account_assignments WHERE user_email = ? ORDER BY account_name").bind(EMAIL).all()).results;

test("seed: empty user gets rows marked assigned_by=seed, visible in my-accounts", async () => {
  const env = await makeEnv();
  const key = await keyFor(env);
  const res = await call(env, "POST", "/v1/my-accounts/seed", key, { rows: [ROW("UL921"), ROW("DQ204")] });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true, seeded: 2 });
  const rows = await rowsOf(env);
  assert.deepEqual(rows.map((r) => [r.account_name, r.assigned_by, r.removed_at]),
    [["DQ204", "seed", null], ["UL921", "seed", null]]);
  const mine = await (await call(env, "GET", "/v1/my-accounts", key)).json();
  assert.equal(mine.accounts.length, 2);
});

test("seed: refused (409) once the user has any assignment, even a removed one", async () => {
  const env = await makeEnv();
  const key = await keyFor(env);
  assert.equal((await call(env, "POST", "/v1/my-accounts/seed", key, { rows: [ROW("A1")] })).status, 200);
  const again = await call(env, "POST", "/v1/my-accounts/seed", key, { rows: [ROW("B2")] });
  assert.equal(again.status, 409);
  assert.deepEqual((await rowsOf(env)).map((r) => r.account_name), ["A1"]);
  // админ снял всё: строки остаются снятыми, сид снова не принимается
  await call(env, "POST", "/api/assignments/remove", OWNER, { user_email: EMAIL, account_names: ["A1"] });
  assert.equal((await call(env, "POST", "/v1/my-accounts/seed", key, { rows: [ROW("C3")] })).status, 409);
});

test("seed: refused when the admin assigned accounts first", async () => {
  const env = await makeEnv();
  await call(env, "POST", "/api/assignments", OWNER, { user_email: EMAIL, rows: [ROW("ADM1")] });
  const res = await call(env, "POST", "/v1/my-accounts/seed", await keyFor(env), { rows: [ROW("MINE")] });
  assert.equal(res.status, 409);
  assert.deepEqual((await rowsOf(env)).map((r) => [r.account_name, r.assigned_by]), [["ADM1", "owner"]]);
});

test("seed: proxy and tracking fields are rejected, nothing written", async () => {
  const env = await makeEnv();
  const key = await keyFor(env);
  for (const extra of [{ proxy_host: "h" }, { proxy_password: "x" }, { tracking_link: "https://t" }]) {
    const res = await call(env, "POST", "/v1/my-accounts/seed", key, { rows: [ROW("A1"), ROW("A2", extra)] });
    assert.equal(res.status, 400);
  }
  assert.equal((await rowsOf(env)).length, 0);
});

test("seed: needs a user key; bad bodies are 400", async () => {
  const env = await makeEnv();
  assert.equal((await call(env, "POST", "/v1/my-accounts/seed", null, { rows: [ROW("A1")] })).status, 401);
  assert.equal((await call(env, "POST", "/v1/my-accounts/seed", OWNER, { rows: [ROW("A1")] })).status, 401);
  const key = await keyFor(env);
  assert.equal((await call(env, "POST", "/v1/my-accounts/seed", key, { rows: [] })).status, 400);
  assert.equal((await call(env, "POST", "/v1/my-accounts/seed", key, { rows: [{ ad_account_id: "x" }] })).status, 400);
  const many = Array.from({ length: 2001 }, (_, i) => ({ account_name: `N${i}` }));
  assert.equal((await call(env, "POST", "/v1/my-accounts/seed", key, { rows: many })).status, 400);
});

test("seed: duplicate names in one seed collapse to one row (case-insensitive)", async () => {
  const env = await makeEnv();
  const res = await call(env, "POST", "/v1/my-accounts/seed", await keyFor(env),
    { rows: [ROW("UL921"), ROW("ul921", { page_id: "p2" })] });
  assert.equal(res.status, 200);
  assert.equal((await res.json()).seeded, 1);
  assert.equal((await rowsOf(env)).length, 1);
});

test("seed: one user's seed does not touch another user", async () => {
  const env = await makeEnv();
  const other = "buyer.two@example.com";
  await env.DB.prepare("INSERT INTO users (email, role, created_at) VALUES (?, 'buyer', 'x')").bind(other).run();
  await call(env, "POST", "/api/assignments", OWNER, { user_email: other, rows: [ROW("X1")] });
  assert.equal((await call(env, "POST", "/v1/my-accounts/seed", await keyFor(env), { rows: [ROW("A1")] })).status, 200);
  assert.deepEqual((await rowsOf(env)).map((r) => r.account_name), ["A1"]);
});
