// Отчёты Control Center: день по байерам, лента, прогон, сбои.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { FakeD1 } from "./fake-d1.js";
import { ingestEvents } from "../src/ingest.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const YOTAM = JSON.parse(readFileSync(new URL("./fixtures-yotam-h23.json", import.meta.url), "utf8"));
const OWNER = ["owner", "key", "y".repeat(20)].join("_");

// Гиль: одно задание, кампания ok в 21:30Z и кампания failed в 22:30Z (по Израилю 23:30 и 00:30).
function gil() {
  const base = { stream_id: "g", schema_version: 1, machine_id: "m-gil", machine: "Gil Zangi", manager: "Gil Zangi",
    flow_version: "v97@abc", channel: "prod", job_id: "job-gil", account: "FacebookDoc391" };
  let n = 0;
  const ev = (type, ts, campaign, payload) => ({ ...base, event_id: `g:${++n}`, seq: n, type, ts, campaign, payload });
  return [
    ev("job.created", "2026-11-16T21:00:00.000+00:00", null, { campaign_names: ["G_ok", "G_bad"], dry_run: false, reviewed: true }),
    ev("publish.spec", "2026-11-16T21:00:10.000+00:00", "G_ok", { campaign_name: "G_ok", ads: [{ name: "a" }], adsets: [{ name: "s" }] }),
    ev("raw.run", "2026-11-16T21:00:20.000+00:00", null, { step: "api_create_step", ok: true, diag: { step: "campaign: created PAUSED" } }),
    ev("publish.stats", "2026-11-16T21:29:00.000+00:00", "G_ok", { campaign_name: "G_ok", duration_s: 100, calls: 20, writes: 5 }),
    ev("publish.result", "2026-11-16T21:30:00.000+00:00", "G_ok", { campaign_name: "G_ok", success: true, created: { campaign: 1 }, reused: {} }),
    ev("publish.spec", "2026-11-16T22:00:00.000+00:00", "G_bad", { campaign_name: "G_bad", ads: [], adsets: [] }),
    ev("raw.run", "2026-11-16T22:10:00.000+00:00", null, { step: "api_create_step", ok: false, diag: { error: "NotSent", step: "ad 2: video upload timing" } }),
    ev("publish.result", "2026-11-16T22:30:00.000+00:00", "G_bad", { campaign_name: "G_bad", success: false,
      error: "Graph API stopped: the page refused the write (no_file: clean upload tab lost (PageFailed))" }),
    ev("job.finished", "2026-11-16T22:31:00.000+00:00", null, { status: "done", duration_s: 5460, cancelled: false }),
  ];
}

async function makeEnv(events = [...YOTAM, ...gil()]) {
  const env = { DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: OWNER };
  await ingestEvents(env, events, { batchId: "b" });
  return env;
}
const get = async (env, path) => {
  const res = await worker.fetch(new Request(`https://hub${path}`, { headers: { Authorization: `Bearer ${OWNER}` } }), env);
  return { status: res.status, body: res.status === 200 ? await res.json() : null };
};

test("summary per buyer", async () => {
  const env = await makeEnv();
  const { body } = await get(env, "/api/summary?day=2026-11-16&tz=120");
  const y = body.buyers.find((b) => b.manager === "Yotam Balteriski");
  assert.equal(y.campaigns, 2);
  assert.equal(y.ok, 0);
  assert.equal(y.failed, 1);
  assert.equal(y.not_started, 1);
  assert.equal(y.jobs, 1);
  assert.deepEqual(y.machines, ["Yotam Balteriski"]);
  assert.equal(y.median_s, 344.6);
  assert.ok(y.active_h > 0);
  assert.equal(y.version, YOTAM[0].flow_version);
});

test("summary respects tz", async () => {
  const env = await makeEnv();
  const d16 = (await get(env, "/api/summary?day=2026-11-16&tz=120")).body.buyers.find((b) => b.manager === "Gil Zangi");
  assert.equal(d16.campaigns, 1);
  assert.equal(d16.ok, 1);
  const d17 = (await get(env, "/api/summary?day=2026-11-17&tz=120")).body.buyers.find((b) => b.manager === "Gil Zangi");
  assert.equal(d17.campaigns, 1);
  assert.equal(d17.failed, 1);
  const utc = (await get(env, "/api/summary?day=2026-11-16&tz=0")).body.buyers.find((b) => b.manager === "Gil Zangi");
  assert.equal(utc.campaigns, 2);
});

test("summary empty day", async () => {
  const env = await makeEnv();
  const { status, body } = await get(env, "/api/summary?day=2026-10-01&tz=120");
  assert.equal(status, 200);
  assert.deepEqual(body.buyers, []);
  assert.equal((await get(env, "/api/summary?day=nope")).status, 400);
});

test("live levels and text", async () => {
  const env = await makeEnv();
  const { body } = await get(env, "/api/live?limit=50");
  assert.ok(body.events.every((e) => !e.type.startsWith("raw.")));
  assert.ok(body.events[0].ts >= body.events.at(-1).ts);
  const fail = body.events.find((e) => e.campaign === "CL_ML_All_Video_CL_HV_TRD_45_16.11" && e.type === "publish.result");
  assert.equal(fail.level, "error");
  assert.match(fail.text, /^Failed: /);
  assert.equal(fail.run_id, "job-yotam-h23|CL_ML_All_Video_CL_HV_TRD_45_16.11");
  const ns = body.events.find((e) => e.campaign === "CL_ML_All_Video_CL_HV_TRD_50_16.11");
  assert.equal(ns.level, "warn");
  const ok = body.events.find((e) => e.campaign === "G_ok" && e.type === "publish.result");
  assert.equal(ok.level, "info");
  assert.match(ok.text, /^Published/);
  const fin = body.events.find((e) => e.type === "job.finished" && e.manager === "Gil Zangi");
  assert.match(fin.text, /1 ok, 1 failed/);
});

test("run detail with steps and fingerprint", async () => {
  const env = await makeEnv();
  const { body } = await get(env, `/api/run?id=${encodeURIComponent("job-gil|G_bad")}`);
  assert.equal(body.run.outcome, "failed");
  assert.equal(body.job.n_planned, 2);
  assert.deepEqual(body.steps.map((s) => [s.ok, s.note]), [[false, "ad 2: video upload timing"]]);
  assert.equal(body.fingerprint.n, 1);
  assert.equal(body.fingerprint.machines, 1);
});

test("run without stats", async () => {
  const env = await makeEnv([YOTAM.find((e) => e.campaign?.endsWith("_50_16.11"))]);
  const { status, body } = await get(env, `/api/run?id=${encodeURIComponent("job-yotam-h23|CL_ML_All_Video_CL_HV_TRD_50_16.11")}`);
  assert.equal(status, 200);
  assert.equal(body.run.outcome, "not_started");
  assert.equal(body.job, null);
  assert.deepEqual(body.steps, []);
  assert.equal(body.fingerprint, null);
});

test("run 404", async () => {
  assert.equal((await get(await makeEnv(), "/api/run?id=nope")).status, 404);
});

test("failures grouped by fingerprint", async () => {
  const env = await makeEnv();
  const { body } = await get(env, "/api/failures?days=36500");
  assert.equal(body.failures.length, 2);
  const tab = body.failures.find((f) => /upload tab lost/.test(f.sample_error));
  assert.equal(tab.n, 1);
  assert.deepEqual(tab.managers, ["Gil Zangi"]);
  assert.equal(tab.last_run_id, "job-gil|G_bad");
});

// ── Ревью части 2 ──
test("run steps keep the tail and same-millisecond last step", async () => {
  const evs = gil();
  const res = evs.find((e) => e.campaign === "G_bad" && e.type === "publish.result");
  const steps = Array.from({ length: 600 }, (_, i) => ({ ...evs[2], event_id: `s:${i}`,
    ts: new Date(Date.parse("2026-11-16T22:00:01.000Z") + i * 1000).toISOString().replace("Z", "+00:00"),
    payload: { step: "api_create_step", ok: true, diag: { step: `step ${i}` } } }));
  const last = { ...evs[2], event_id: "s:last", ts: res.ts.replace(".000+", ".000777+"),
    payload: { step: "api_create_step", ok: false, diag: { step: "final failing step" } } };
  const env = await makeEnv([...evs, ...steps, last]);
  const { body } = await get(env, `/api/run?id=${encodeURIComponent("job-gil|G_bad")}`);
  assert.equal(body.steps.length, 500);
  assert.equal(body.steps.at(-1).note, "final failing step");
  assert.ok(body.steps[0].ts < body.steps.at(-1).ts);
});

test("live filters by manager on the server", async () => {
  const noisy = Array.from({ length: 30 }, (_, i) => ({ ...gil()[0], event_id: `n:${i}`, type: "plan.checked",
    ts: `2026-11-17T10:00:${String(i).padStart(2, "0")}.000+00:00`, job_id: null, payload: {} }));
  const env = await makeEnv([...YOTAM, ...gil(), ...noisy]);
  const { body } = await get(env, "/api/live?limit=10&manager=" + encodeURIComponent("Yotam Balteriski"));
  assert.ok(body.events.length > 0);
  assert.ok(body.events.every((e) => e.manager === "Yotam Balteriski"));
});

test("jobs counted from runs when job.created is missing", async () => {
  const env = await makeEnv(gil().filter((e) => e.type !== "job.created"));
  const g = (await get(env, "/api/summary?day=2026-11-16&tz=0")).body.buyers.find((b) => b.manager === "Gil Zangi");
  assert.equal(g.jobs, 1);
});

test("object errors are shown as text", async () => {
  const bad = { ...gil()[7], event_id: "o:1", campaign: "G_obj", payload: { campaign_name: "G_obj", success: false, error: { message: "boom" } } };
  const env = await makeEnv([bad]);
  const { body } = await get(env, "/api/live");
  assert.ok(!body.events[0].text.includes("[object Object]"));
  assert.match(body.events[0].text, /boom/);
});
