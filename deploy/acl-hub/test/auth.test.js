// Проверка токена библиотеки (GitHub) и ключа владельца, записи о токенах в D1.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { FakeD1 } from "./fake-d1.js";
import { authorize, isOwner, sameSecret } from "../src/auth.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
// Пустышки собираются в коде: в файле нет ничего похожего на настоящий ключ.
const TOKEN = ["lib", "token", "x".repeat(20)].join("_");
const OWNER = ["owner", "key", "y".repeat(20)].join("_");
const sha = (t) => createHash("sha256").update(t).digest("hex");

let calls, status, realFetch;
beforeEach(() => {
  calls = []; status = 200; realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (status === "network") throw new Error("down");
    return new Response("{}", { status });
  };
});
afterEach(() => { globalThis.fetch = realFetch; });

const makeEnv = () => ({ DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: OWNER, LIBRARY_REPO: "Owner/lib" });
const setChecked = (env, ms) =>
  env.DB.prepare("INSERT OR REPLACE INTO tokens VALUES (?, ?)").bind(sha(TOKEN), ms).run();

test("fresh token record skips GitHub", async () => {
  const env = makeEnv();
  await setChecked(env, Date.now() - 10 * 60 * 1000);
  assert.equal(await authorize(TOKEN, env), "ok");
  assert.equal(calls.length, 0);
});

test("github 200 stores sha only", async () => {
  const env = makeEnv();
  assert.equal(await authorize(TOKEN, env), "ok");
  assert.match(calls[0].url, /repos\/Owner\/lib$/);
  assert.equal(calls[0].init.headers["User-Agent"], "acl-hub");
  const { results } = await env.DB.prepare("SELECT * FROM tokens").all();
  assert.equal(results.length, 1);
  assert.equal(results[0].token_sha, sha(TOKEN));
  assert.ok(!JSON.stringify(results).includes(TOKEN));
});

test("github 401 denies and drops the record", async () => {
  const env = makeEnv();
  await setChecked(env, Date.now() - 2 * 3600 * 1000);
  status = 401;
  assert.equal(await authorize(TOKEN, env), "denied");
  assert.equal(await env.DB.prepare("SELECT * FROM tokens").first(), null);
});

test("empty token is denied without GitHub", async () => {
  assert.equal(await authorize("", makeEnv()), "denied");
  assert.equal(calls.length, 0);
});

test("github down within 24h grace is ok, after 24h unavailable", async () => {
  const env = makeEnv();
  status = 500;
  await setChecked(env, Date.now() - 2 * 3600 * 1000);
  assert.equal(await authorize(TOKEN, env), "ok");
  await setChecked(env, Date.now() - 25 * 3600 * 1000);
  assert.equal(await authorize(TOKEN, env), "unavailable");
  status = "network";
  assert.equal(await authorize(TOKEN, makeEnv()), "unavailable");
});

test("owner key compare", async () => {
  const env = makeEnv();
  const req = (key) => new Request("https://h/api/x", { headers: key ? { Authorization: `Bearer ${key}` } : {} });
  assert.equal(await isOwner(req(OWNER), env), true);
  assert.equal(await isOwner(req(""), env), false);
  assert.equal(await isOwner(req(TOKEN), env), false);
  assert.equal(await isOwner(req(OWNER), { ...env, OWNER_KEY: "" }), false);
  assert.equal(await sameSecret("a", "a"), true);
});
