// Кнопка Refresh: хаб забирает новые пачки из старого журнала acl-event-log и не берёт одну пачку дважды.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { gzipSync } from "node:zlib";
import { readFileSync } from "node:fs";
import { FakeD1 } from "./fake-d1.js";
import { MAX_FETCH, pullLegacy } from "../src/legacy.js";
import { PAGE_HTML } from "../src/ui.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const YOTAM = JSON.parse(readFileSync(new URL("./fixtures-yotam-h23.json", import.meta.url), "utf8"));
const OWNER = "owner_" + "k".repeat(30);
const READ = "read_" + "r".repeat(30);
const BASE = "https://old-log.example";

let store, calls, realFetch, listStatus;
beforeEach(() => {
  store = new Map(); calls = []; listStatus = 200; realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const u = new URL(url);
    calls.push({ path: u.pathname, auth: init?.headers?.Authorization, ua: init?.headers?.["User-Agent"] });
    if (u.pathname === "/v1/list") return new Response(JSON.stringify({ keys: [...store.keys()], cursor: null }), { status: listStatus });
    if (u.pathname === "/v1/object") {
      const body = store.get(u.searchParams.get("key"));
      return body ? new Response(body) : new Response("{}", { status: 404 });
    }
    return new Response("{}", { status: 404 });
  };
});
afterEach(() => { globalThis.fetch = realFetch; });

const makeEnv = () => ({ DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: OWNER,
  LEGACY_LOG_URL: BASE, LEGACY_READ_KEY: READ });
const pack = (events) => gzipSync(events.map((e) => JSON.stringify(e)).join("\n"));
const refresh = (env, key = OWNER) => worker.fetch(new Request("https://hub/api/refresh",
  { method: "POST", headers: { Authorization: `Bearer ${key}` } }), env);

test("new batches are ingested once, with the read key and own User-Agent", async () => {
  const env = makeEnv();
  store.set("v1/m1/a", pack(YOTAM.slice(0, 40)));
  store.set("v1/m1/b", pack(YOTAM.slice(40)));
  const first = await pullLegacy(env);
  assert.equal(first.segments, 2);
  assert.equal(first.accepted, YOTAM.length);
  assert.ok(calls.every((c) => c.auth === `Bearer ${READ}` && c.ua));
  const runs = await env.DB.prepare("SELECT count(*) AS n FROM runs").first();
  assert.ok(runs.n > 0);
  calls = [];
  const again = await pullLegacy(env);
  assert.equal(again.segments, 0);
  assert.deepEqual(calls.map((c) => c.path), ["/v1/list"]);
});

test("one press fetches at most MAX_FETCH batches and reports the rest", async () => {
  const env = makeEnv();
  for (let i = 0; i < MAX_FETCH + 5; i++) store.set(`v1/m/${String(i).padStart(3, "0")}`, pack([YOTAM[i]]));
  const r = await pullLegacy(env);
  assert.equal(r.segments, MAX_FETCH);
  assert.equal(r.remaining, 5);
  const r2 = await pullLegacy(env);
  assert.equal(r2.segments, 5);
  assert.equal(r2.remaining, 0);
});

test("an unreadable batch is retried on the next press", async () => {
  const env = makeEnv();
  store.set("v1/m/bad", new TextEncoder().encode("not gzip"));
  const r = await pullLegacy(env);
  assert.equal(r.failed_segments, 1);
  store.set("v1/m/bad", pack([YOTAM[0]]));
  const r2 = await pullLegacy(env);
  assert.equal(r2.segments, 1);
});

test("refresh route: owner only, configured, old log down is 502", async () => {
  const env = makeEnv();
  assert.equal((await refresh(env, "wrong")).status, 401);
  assert.equal((await refresh({ ...env, LEGACY_READ_KEY: "" })).status, 503);
  listStatus = 500;
  assert.equal((await refresh(env)).status, 502);
  listStatus = 200;
  store.set("v1/m/a", pack(YOTAM.slice(0, 3)));
  const body = await (await refresh(env)).json();
  assert.equal(body.ok, true);
  assert.equal(body.accepted, 3);
});

test("page has the Refresh button", () => {
  assert.match(PAGE_HTML, /id="refresh"/);
  assert.match(PAGE_HTML, /\/api\/refresh/);
});

// --- обход входов: пустое, отсутствующее, странное ---

test("route: each missing secret alone is 503", async () => {
  const env = makeEnv();
  assert.equal((await refresh({ ...env, LEGACY_LOG_URL: "" })).status, 503);
  assert.equal((await refresh({ ...env, LEGACY_LOG_URL: undefined })).status, 503);
  assert.equal((await refresh({ ...env, LEGACY_READ_KEY: undefined })).status, 503);
});

test("route: missing or empty key is 401, before any call to the old log", async () => {
  const env = makeEnv();
  const noAuth = await worker.fetch(new Request("https://hub/api/refresh", { method: "POST" }), env);
  assert.equal(noAuth.status, 401);
  assert.equal((await refresh(env, "")).status, 401);
  assert.equal(calls.length, 0);
});

test("route: more than 30 new batches, first press takes 30 and reports the rest", async () => {
  const env = makeEnv();
  for (let i = 0; i < MAX_FETCH + 4; i++) store.set(`v1/m/${String(i).padStart(3, "0")}`, pack([YOTAM[i]]));
  const body = await (await refresh(env)).json();
  assert.equal(body.segments, MAX_FETCH);
  assert.equal(body.remaining, 4);
});

test("route: batches already taken are not ingested again", async () => {
  const env = makeEnv();
  store.set("v1/m/a", pack(YOTAM.slice(0, 5)));
  await refresh(env);
  const before = await env.DB.prepare("SELECT count(*) AS n FROM events").first();
  const body = await (await refresh(env)).json();
  assert.equal(body.segments, 0);
  assert.equal(body.accepted, 0);
  assert.equal((await env.DB.prepare("SELECT count(*) AS n FROM events").first()).n, before.n);
});

test("empty old log: nothing to do, ok", async () => {
  const body = await (await refresh(makeEnv())).json();
  assert.equal(body.ok, true);
  assert.equal(body.segments, 0);
  assert.equal(body.remaining, 0);
});

test("list answer without keys field is treated as empty", async () => {
  const env = makeEnv();
  globalThis.fetch = async () => new Response("{}", { status: 200 });
  const r = await pullLegacy(env);
  assert.equal(r.segments, 0);
});

test("list answer that is not JSON is 502", async () => {
  const env = makeEnv();
  globalThis.fetch = async () => new Response("<html>", { status: 200 });
  assert.equal((await refresh(env)).status, 502);
});

test("old log rejecting the read key (401) is 502", async () => {
  listStatus = 401;
  assert.equal((await refresh(makeEnv())).status, 502);
});

test("network error talking to the old log is 502", async () => {
  const env = makeEnv();
  globalThis.fetch = async () => { throw new TypeError("fetch failed"); };
  assert.equal((await refresh(env)).status, 502);
});

test("a batch with no events is marked taken and not fetched again", async () => {
  const env = makeEnv();
  store.set("v1/m/empty", pack([]));
  const r = await pullLegacy(env);
  assert.equal(r.segments, 1);
  assert.equal(r.accepted, 0);
  calls = [];
  assert.equal((await pullLegacy(env)).segments, 0);
  assert.deepEqual(calls.map((c) => c.path), ["/v1/list"]);
});

test("a batch with garbage lines is taken, garbage counted as rejected not crashing", async () => {
  const env = makeEnv();
  store.set("v1/m/odd", gzipSync("not json\n{}\n" + JSON.stringify(YOTAM[0])));
  const r = await pullLegacy(env);
  assert.equal(r.segments, 1);
  assert.ok(r.accepted >= 1);
});

test("one object missing (404) does not stop the other batches", async () => {
  const env = makeEnv();
  store.set("v1/m/a", pack([YOTAM[0]]));
  store.set("v1/m/b", pack([YOTAM[1]]));
  const realList = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const u = new URL(url);
    if (u.pathname === "/v1/list") return new Response(JSON.stringify({ keys: ["v1/m/a", "v1/m/ghost", "v1/m/b"] }));
    return realList(url, init);
  };
  const r = await pullLegacy(env);
  assert.equal(r.segments, 2);
  assert.equal(r.failed_segments, 1);
});

test("list pages are followed via cursor, trailing slash in the address is ignored", async () => {
  const env = { ...makeEnv(), LEGACY_LOG_URL: BASE + "//" };
  globalThis.fetch = async (url) => {
    const u = new URL(url);
    if (u.pathname === "/v1/list") {
      return u.searchParams.get("cursor")
        ? new Response(JSON.stringify({ keys: ["v1/m/b"], cursor: null }))
        : new Response(JSON.stringify({ keys: ["v1/m/a"], cursor: "c1" }));
    }
    return new Response(pack([YOTAM[0]]));
  };
  const r = await pullLegacy(env);
  assert.equal(r.segments, 2);
});

test("key with special characters is URL-encoded when fetched", async () => {
  const env = makeEnv();
  store.set("v1/m x/a&b", pack([YOTAM[0]]));
  assert.equal((await pullLegacy(env)).segments, 1);
});
