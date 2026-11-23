// API владельца: ключ, постраничная выдача событий, фильтры прогонов и заданий, машины.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { FakeD1 } from "./fake-d1.js";
import { ingestEvents } from "../src/ingest.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const YOTAM = JSON.parse(readFileSync(new URL("./fixtures-yotam-h23.json", import.meta.url), "utf8"));
const OWNER = ["owner", "key", "y".repeat(20)].join("_");
const LIB = ["lib", "token", "x".repeat(20)].join("_");

async function makeEnv(events = YOTAM) {
  const env = { DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: OWNER };
  await ingestEvents(env, events, { batchId: "b" });
  return env;
}
const get = (env, path, key = OWNER) =>
  worker.fetch(new Request(`https://hub${path}`, { headers: key ? { Authorization: `Bearer ${key}` } : {} }), env);
const lines = async (res) => (await res.text()).split("\n").filter(Boolean).map((l) => JSON.parse(l));

test("requires owner key", async () => {
  const env = await makeEnv();
  for (const path of ["/api/events", "/api/runs", "/api/jobs", "/api/machines"]) {
    assert.equal((await get(env, path, "")).status, 401, path);
    assert.equal((await get(env, path, LIB)).status, 401, path);
  }
});

test("events paging", async () => {
  const env = await makeEnv(YOTAM.slice(0, 3));
  const first = await get(env, "/api/events?limit=2");
  assert.equal(first.status, 200);
  assert.match(first.headers.get("content-type"), /x-ndjson/);
  const page = await lines(first);
  assert.equal(page.length, 2);
  assert.equal(page[0].event_id, YOTAM[0].event_id);
  assert.deepEqual(page[0].payload, YOTAM[0].payload);
  assert.equal(page[0].machine, "Yotam Balteriski");
  const after = first.headers.get("X-Next-After");
  const second = await get(env, `/api/events?after=${after}&limit=2`);
  const rest = await lines(second);
  assert.equal(rest.length, 1);
  assert.equal(rest[0].event_id, YOTAM[2].event_id);
  const empty = await get(env, `/api/events?after=${second.headers.get("X-Next-After")}`);
  assert.equal((await lines(empty)).length, 0);
  assert.equal(empty.headers.get("X-Next-After"), second.headers.get("X-Next-After"));
});

test("runs filter by manager and outcome", async () => {
  const env = await makeEnv();
  const all = await (await get(env, "/api/runs")).json();
  assert.equal(all.runs.length, 2);
  assert.ok(all.runs[0].finished_at >= all.runs[1].finished_at);
  const failed = await (await get(env, "/api/runs?outcome=failed&manager=Yotam%20Balteriski")).json();
  assert.equal(failed.runs.length, 1);
  assert.equal(failed.runs[0].stop_kind, "checkpoint");
  const none = await (await get(env, "/api/runs?manager=Gil%20Zangi")).json();
  assert.equal(none.runs.length, 0);
  const window = await (await get(env, "/api/runs?from=2026-11-17T00:00:00Z")).json();
  assert.equal(window.runs.length, 0);
});

test("jobs by period", async () => {
  const env = await makeEnv();
  const { jobs } = await (await get(env, "/api/jobs?from=2026-11-16T00:00:00Z&to=2026-11-17T00:00:00Z")).json();
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0].n_failed, 1);
});

test("machines parse profile", async () => {
  const profile = { ...YOTAM[1], event_id: "p:1", type: "machine.profile", job_id: null, payload: { ram_gb: 16, cores_logical: 8 } };
  const env = await makeEnv([profile]);
  const { machines } = await (await get(env, "/api/machines")).json();
  assert.equal(machines.length, 1);
  assert.deepEqual(machines[0].profile, { ram_gb: 16, cores_logical: 8 });
});
