// Приём пачки POST /v1/batch: авторизация, заголовки, распаковка, повторы, битые строки, секреты.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { gzipSync } from "node:zlib";
import { readFileSync } from "node:fs";
import { FakeD1 } from "./fake-d1.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
export const YOTAM = JSON.parse(readFileSync(new URL("./fixtures-yotam-h23.json", import.meta.url), "utf8"));
const TOKEN = ["lib", "token", "x".repeat(20)].join("_");
const BATCH = "a".repeat(32) + "-1-7";

let githubStatus, realFetch;
beforeEach(() => {
  githubStatus = 200; realFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    if (githubStatus === "network") throw new Error("down");
    return new Response("{}", { status: githubStatus });
  };
});
afterEach(() => { globalThis.fetch = realFetch; });

const makeEnv = () => ({ DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: "owner_key_" + "z".repeat(20) });
const ndjson = (events) => gzipSync(events.map((e) => (typeof e === "string" ? e : JSON.stringify(e))).join("\n") + "\n");

function post(env, { events = YOTAM, body, token = TOKEN, machine = "m-yotam", batch = BATCH } = {}) {
  const headers = { "X-ACL-Machine": machine, "X-ACL-Batch": batch, "Content-Encoding": "gzip" };
  if (token) headers.Authorization = `Bearer ${token}`;
  return worker.fetch(new Request("https://hub/v1/batch", { method: "POST", headers, body: body ?? ndjson(events) }), env);
}
const count = async (env, table) => (await env.DB.prepare(`SELECT count(*) AS n FROM ${table}`).first()).n;

test("accepts batch and stores events", async () => {
  const env = makeEnv();
  const res = await post(env);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true, accepted: 7, duplicates: 0, rejected: 0 });
  assert.equal(await count(env, "events"), 7);
  const row = await env.DB.prepare("SELECT * FROM events WHERE type='publish.stats'").first();
  assert.equal(row.machine, "Yotam Balteriski");
  assert.equal(row.batch_id, BATCH);
  assert.equal(JSON.parse(row.payload).calls, 85);
});

test("duplicate batch adds nothing", async () => {
  const env = makeEnv();
  await post(env);
  const res = await post(env);
  assert.deepEqual(await res.json(), { ok: true, accepted: 0, duplicates: 7, rejected: 0 });
  assert.equal(await count(env, "events"), 7);
});

test("heartbeat updates machine but is not stored", async () => {
  const env = makeEnv();
  const beat = { ...YOTAM[0], event_id: "s:99", type: "heartbeat", ts: "2026-11-17T07:43:20.967+00:00",
    job_id: null, payload: { outbox_bytes: 14922, dropped: 2, streams: {} } };
  await post(env, { events: [beat] });
  assert.equal(await count(env, "events"), 0);
  const m = await env.DB.prepare("SELECT * FROM machines WHERE machine_id='m-yotam'").first();
  assert.equal(m.last_heartbeat_at, beat.ts);
  assert.equal(m.outbox_bytes, 14922);
  assert.equal(m.dropped_total, 2);
});

test("bad lines are skipped", async () => {
  const env = makeEnv();
  const noId = { ...YOTAM[1] }; delete noId.event_id;
  const res = await post(env, { events: ['{"event_id": "x", "type"', noId, YOTAM[1]] });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true, accepted: 1, duplicates: 0, rejected: 2 });
});

test("secret-looking payload is rejected", async () => {
  const env = makeEnv();
  const bad = { ...YOTAM[1], event_id: "s:98", payload: { url: "https://x/?access_token=abc" } };
  const res = await post(env, { events: [bad] });
  assert.equal((await res.json()).rejected, 1);
  assert.equal(await count(env, "events"), 0);
});

test("oversized body is 413", async () => {
  const env = makeEnv();
  assert.equal((await post(env, { body: new Uint8Array(1024 * 1024 + 1) })).status, 413);
});

test("not gzip is 400", async () => {
  assert.equal((await post(makeEnv(), { body: new TextEncoder().encode("{}\n") })).status, 400);
});

test("bad machine header is 400", async () => {
  assert.equal((await post(makeEnv(), { machine: "bad machine!" })).status, 400);
});

test("no token is 401", async () => {
  assert.equal((await post(makeEnv(), { token: "" })).status, 401);
});

test("github down without record is 503", async () => {
  githubStatus = 500;
  assert.equal((await post(makeEnv())).status, 503);
});

test("storage failure is 503", async () => {
  const env = makeEnv();
  await post(env, { events: [YOTAM[1]] });                 // запись о токене уже есть
  env.DB.batch = async () => { throw new Error("D1_ERROR"); };
  assert.equal((await post(env, { batch: "b".repeat(32) + "-1-1" })).status, 503);
});

// ── Task 6: перенос истории владельцем ──
const OWNER = "owner_key_" + "z".repeat(20);
function backfill(env, { key = OWNER, events = YOTAM, batch = "backfill-1-7" } = {}) {
  const headers = { "X-ACL-Batch": batch };
  if (key) headers.Authorization = `Bearer ${key}`;
  return worker.fetch(new Request("https://hub/v1/backfill", { method: "POST", headers, body: ndjson(events) }), env);
}

test("backfill requires owner key", async () => {
  const env = makeEnv();
  assert.equal((await backfill(env, { key: "" })).status, 401);
  assert.equal((await backfill(env, { key: TOKEN })).status, 401);
  assert.equal(await count(env, "events"), 0);
});

test("backfill keeps each event's machine", async () => {
  const env = makeEnv();
  const gil = { ...YOTAM[1], event_id: "g:1", machine_id: "m-gil", machine: "Gil Zangi", manager: "Gil Zangi", job_id: "jg" };
  const res = await backfill(env, { events: [YOTAM[1], gil] });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true, accepted: 2, duplicates: 0, rejected: 0 });
  const { results } = await env.DB.prepare("SELECT machine_id, machine FROM machines ORDER BY machine_id").all();
  assert.deepEqual(results.map((r) => r.machine), ["Gil Zangi", "Yotam Balteriski"]);
  assert.equal((await env.DB.prepare("SELECT batch_id FROM events WHERE event_id='g:1'").first()).batch_id, "backfill-1-7");
});
