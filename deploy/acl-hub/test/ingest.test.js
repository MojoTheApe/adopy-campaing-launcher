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
  const bad = { ...YOTAM[1], event_id: "s:98", payload: { url: "https://x/?access_token=abcdef0123456789" } };
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

// ── Финальное ревью: исправления ──
test("cleaned token forms are accepted, raw tokens rejected", async () => {
  const env = makeEnv();
  const mk = (id, url) => ({ ...YOTAM[1], event_id: id, payload: { next: url } });
  const res = await post(env, { events: [
    mk("c:1", "https://graph.facebook.com/v21.0/act_1/ads?access_token=<hidden>&after=x"),
    mk("c:2", "https://graph.facebook.com/v21.0/act_1/ads?access_token=<v>&limit=25"),
    mk("c:3", "https://x/?access_token=<token>"),
    mk("c:4", "https://x/?access_token=abcdefghijklmnop"),
    mk("c:5", "token EAA" + "B".repeat(30)),
  ] });
  assert.deepEqual(await res.json(), { ok: true, accepted: 3, duplicates: 0, rejected: 2 });
});

test("secret outside payload is rejected too", async () => {
  const env = makeEnv();
  const res = await post(env, { events: [{ ...YOTAM[1], event_id: "c:6", campaign: "EAA" + "C".repeat(30) }] });
  assert.equal((await res.json()).rejected, 1);
});

test("derive failure is repaired on redelivery", async () => {
  const env = makeEnv();
  await post(env, { events: [YOTAM[1]] });               // запись о токене уже есть
  const realBatch = env.DB.batch.bind(env.DB);
  let calls = 0;
  env.DB.batch = async (stmts) => {                         // первый batch (вставка) проходит, второй (выводы) падает
    calls += 1;
    if (calls === 2) throw new Error("D1_ERROR transient");
    return realBatch(stmts);
  };
  assert.equal((await post(env, { batch: "c".repeat(32) + "-1-7" })).status, 503);
  env.DB.batch = realBatch;
  const res = await post(env, { batch: "c".repeat(32) + "-1-7" });
  assert.equal(res.status, 200);
  const job = await env.DB.prepare("SELECT * FROM jobs").first();
  assert.equal(job.n_failed, 1);
  assert.equal(job.n_not_started, 1);
  assert.equal((await env.DB.prepare("SELECT count(*) AS n FROM runs").first()).n, 2);
});

test("heartbeat redelivery does not double dropped, older beat does not win", async () => {
  const env = makeEnv();
  const beat = (id, ts, dropped, bytes) => ({ ...YOTAM[0], event_id: id, type: "heartbeat", ts, job_id: null,
    payload: { outbox_bytes: bytes, dropped } });
  const newer = beat("h:2", "2026-11-17T08:00:00.000+00:00", 3, 100);
  await post(env, { events: [newer] });
  const again = await post(env, { events: [newer] });
  assert.deepEqual(await again.json(), { ok: true, accepted: 0, duplicates: 1, rejected: 0 });
  await post(env, { events: [beat("h:1", "2026-11-17T07:00:00.000+00:00", 5, 999)] });
  const m = await env.DB.prepare("SELECT * FROM machines").first();
  assert.equal(m.dropped_total, 3);
  assert.equal(m.outbox_bytes, 100);
  assert.equal(m.last_heartbeat_at, newer.ts);
});

test("big raw batch fits D1 limits", async () => {
  const env = makeEnv();
  const raws = Array.from({ length: 2500 }, (_, i) => ({ ...YOTAM[1], event_id: `r:${i}`, type: "raw.run",
    job_id: null, payload: { step: "api_read", ok: true } }));
  let statements = 0;
  const realBatch = env.DB.batch.bind(env.DB);
  env.DB.batch = async (stmts) => {
    for (const s of stmts) assert.ok(s.args.length <= 100, "D1: не больше 100 параметров в запросе");
    statements += stmts.length;
    return realBatch(stmts);
  };
  const res = await post(env, { events: raws });
  assert.deepEqual(await res.json(), { ok: true, accepted: 2500, duplicates: 0, rejected: 0 });
  assert.ok(statements < 900, `statements per invocation: ${statements}`);
});
