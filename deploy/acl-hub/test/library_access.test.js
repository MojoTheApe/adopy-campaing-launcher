// Доступ к общей библиотеке для юзеров ACL (ACL-2914): GET /v1/library-access.
import { test } from "node:test";
import assert from "node:assert/strict";
import { FakeD1 } from "./fake-d1.js";
import { makeUserKey } from "../src/users.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const OWNER = ["owner", "key", "y".repeat(20)].join("_");
const SECRET = ["user", "secret", "z".repeat(20)].join("_");
const LIB_TOKEN = ["lib", "token", "q".repeat(24)].join("_");
const REPO = "Org/team-lib";
const EMAIL = "buyer.one@example.com";

const makeEnv = async (extra = {}) => {
  const env = { DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: OWNER, USER_KEY_SECRET: SECRET,
    ALLOWED_EMAIL_DOMAINS: "example.com", LIBRARY_TOKEN: LIB_TOKEN, LIBRARY_REPO: REPO, ...extra };
  await env.DB.prepare("INSERT INTO users (email, role, created_at) VALUES (?, 'buyer', 'x')").bind(EMAIL).run();
  return env;
};
const keyFor = async (env, email = EMAIL, at = Math.floor(Date.now() / 1000)) => (await makeUserKey(env, email, at)).key;
const get = (env, key) => worker.fetch(new Request("https://hub/v1/library-access",
  { headers: key ? { Authorization: `Bearer ${key}` } : {} }), env);

test("library-access: valid user key gets repo and token, no-store", async () => {
  const env = await makeEnv();
  const res = await get(env, await keyFor(env));
  assert.equal(res.status, 200);
  assert.match(res.headers.get("cache-control"), /no-store/);
  assert.deepEqual(await res.json(), { repo: REPO, token: LIB_TOKEN });
});

test("library-access: no key, bad key, expired key, owner key and unknown user get no token", async () => {
  const env = await makeEnv();
  const good = await keyFor(env);
  const cases = [
    null,
    "garbage",
    good.slice(0, -1) + (good.endsWith("0") ? "1" : "0"),                 // подпись испорчена
    await keyFor(env, EMAIL, Math.floor(Date.now() / 1000) - 40 * 86400),  // просрочен
    OWNER,                                                                  // ключ владельца — не ключ юзера
    await keyFor(env, "stranger@example.com"),                              // подписан, но юзера нет
  ];
  for (const k of cases) {
    const res = await get(env, k);
    assert.equal(res.status, 401);
    assert.ok(!(await res.text()).includes(LIB_TOKEN));
  }
});

test("library-access: disabled user is 403 and gets no token", async () => {
  const env = await makeEnv();
  const key = await keyFor(env);
  await env.DB.prepare("UPDATE users SET disabled = 1 WHERE email = ?").bind(EMAIL).run();
  const res = await get(env, key);
  assert.equal(res.status, 403);
  assert.ok(!(await res.text()).includes(LIB_TOKEN));
});

test("library-access: foreign domain is 403 (admin-created user outside ALLOWED_EMAIL_DOMAINS)", async () => {
  const env = await makeEnv();
  const other = "someone@other.org";
  await env.DB.prepare("INSERT INTO users (email, role, created_at) VALUES (?, 'buyer', 'x')").bind(other).run();
  const res = await get(env, await keyFor(env, other));
  assert.equal(res.status, 403);
  assert.ok(!(await res.text()).includes(LIB_TOKEN));
});

test("library-access: domain match ignores case and spaces in ALLOWED_EMAIL_DOMAINS", async () => {
  const env = await makeEnv({ ALLOWED_EMAIL_DOMAINS: " Other.org , Example.COM " });
  assert.equal((await get(env, await keyFor(env))).status, 200);
});

test("library-access: secret or repo not set is a plain 503 without details", async () => {
  for (const extra of [{ LIBRARY_TOKEN: "" }, { LIBRARY_TOKEN: undefined }, { LIBRARY_REPO: "" }]) {
    const env = await makeEnv(extra);
    const res = await get(env, await keyFor(env));
    assert.equal(res.status, 503);
    assert.deepEqual(await res.json(), { error: "not configured" });
  }
});

test("library-access: wrong method is not served; token never stored in D1 or logged", async () => {
  const env = await makeEnv();
  const key = await keyFor(env);
  const lines = [];
  const orig = { log: console.log, error: console.error, warn: console.warn, info: console.info };
  for (const k of Object.keys(orig)) console[k] = (...a) => lines.push(a.join(" "));
  try {
    const post = await worker.fetch(new Request("https://hub/v1/library-access",
      { method: "POST", headers: { Authorization: `Bearer ${key}` }, body: "{}" }), env);
    assert.equal(post.status, 404);
    assert.equal((await get(env, key)).status, 200);
    assert.equal((await get(env, "garbage")).status, 401);
  } finally { Object.assign(console, orig); }
  assert.ok(!lines.join("\n").includes(LIB_TOKEN));
  for (const table of ["users", "account_assignments", "events", "machines", "jobs", "runs"]) {
    const dump = JSON.stringify((await env.DB.prepare(`SELECT * FROM ${table}`).all()).results);
    assert.ok(!dump.includes(LIB_TOKEN), table);
  }
});
