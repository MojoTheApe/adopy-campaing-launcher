// Вход владельца по cookie: подпись, срок, смена ключа, страница входа, доступ к API и к странице.
import { test } from "node:test";
import assert from "node:assert/strict";
import { FakeD1 } from "./fake-d1.js";
import { checkSession, makeSession } from "../src/session.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const OWNER = ["owner", "key", "y".repeat(20)].join("_");
const makeEnv = (key = OWNER) => ({ DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: key });
const now = () => Math.floor(Date.now() / 1000);

const cookieOf = (res) => (res.headers.get("set-cookie") || "").split(";")[0];

async function login(env, key = OWNER) {
  const body = new URLSearchParams({ key });
  return worker.fetch(new Request("https://hub/login", { method: "POST", body,
    headers: { "content-type": "application/x-www-form-urlencoded" } }), env);
}

test("session round trip", async () => {
  const env = makeEnv();
  const v = await makeSession(env, now());
  assert.equal(await checkSession(env, v, now()), true);
});

test("tampered session rejected", async () => {
  const env = makeEnv();
  const v = await makeSession(env, now());
  const [exp, sig] = v.split(".");
  assert.equal(await checkSession(env, `${Number(exp) + 999}.${sig}`, now()), false);
  assert.equal(await checkSession(env, `${exp}.${sig.slice(0, -1)}0`, now()), false);
  assert.equal(await checkSession(env, "garbage", now()), false);
  assert.equal(await checkSession(env, "", now()), false);
});

test("expired session rejected", async () => {
  const env = makeEnv();
  const v = await makeSession(env, now() - 31 * 86400);
  assert.equal(await checkSession(env, v, now()), false);
});

test("session dies when OWNER_KEY changes", async () => {
  const v = await makeSession(makeEnv(), now());
  assert.equal(await checkSession(makeEnv("other_" + "k".repeat(30)), v, now()), false);
});

test("login sets secure cookie", async () => {
  const res = await login(makeEnv());
  assert.equal(res.status, 303);
  assert.equal(res.headers.get("location"), "/");
  const sc = res.headers.get("set-cookie");
  for (const flag of ["acl_hub_s=", "HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=2592000"]) {
    assert.ok(sc.includes(flag), flag);
  }
});

test("wrong key is 401", async () => {
  const res = await login(makeEnv(), "nope");
  assert.equal(res.status, 401);
  assert.match(await res.text(), /Wrong key/);
});

test("api accepts cookie", async () => {
  const env = makeEnv();
  const cookie = cookieOf(await login(env));
  const res = await worker.fetch(new Request("https://hub/api/machines", { headers: { cookie } }), env);
  assert.equal(res.status, 200);
  const bad = await worker.fetch(new Request("https://hub/api/machines", { headers: { cookie: "acl_hub_s=1.2" } }), env);
  assert.equal(bad.status, 401);
});

test("root without session redirects to login", async () => {
  const env = makeEnv();
  const res = await worker.fetch(new Request("https://hub/"), env);
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("location"), "/login");
  const page = await worker.fetch(new Request("https://hub/login"), env);
  assert.equal(page.status, 200);
  assert.match(await page.text(), /<form/);
});

test("logout clears cookie", async () => {
  const res = await worker.fetch(new Request("https://hub/logout", { method: "POST" }), makeEnv());
  assert.equal(res.status, 303);
  assert.match(res.headers.get("set-cookie"), /acl_hub_s=; .*Max-Age=0/);
});
