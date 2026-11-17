// Выводы: нормализация причины, отпечаток, исход, прогоны и задания из реальной последовательности Йотама.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { FakeD1 } from "./fake-d1.js";
import { ingestEvents } from "../src/ingest.js";
import { classify, fingerprint, normalizeReason } from "../src/derive.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const YOTAM = JSON.parse(readFileSync(new URL("./fixtures-yotam-h23.json", import.meta.url), "utf8"));
const makeEnv = () => ({ DB: new FakeD1().applyMigrations(MIGRATIONS) });
const all = async (env, sql) => (await env.DB.prepare(sql).all()).results;

test("normalizeReason matches python", () => {
  // Ожидания посчитаны src/failure_signatures.normalise_reason 2026-11-17.
  assert.equal(normalizeReason("Graph API stopped: The account needs to fix an issue on facebook.com (checkpoint). "
    + "Open the account in its browser profile, resolve the prompt on facebook.com, then retry."),
  "graph api stopped: the account needs to fix an issue on facebook.com (checkpoint). open the account in its "
    + "browser profile, resolve the prompt on facebook.com,…");
  assert.equal(normalizeReason("ad 1: video upload: the fast upload was refused and uploading in 5 MB pieces needs "
    + "about 494 Meta calls; this run can spare 88."),
  "ad #: video upload: the fast upload was refused and uploading in # mb pieces needs about # meta calls; this run can spare #.");
  assert.equal(normalizeReason("Job 3f2a1b4c-1111-2222-3333-444455556666 failed at step 12 after 30.5 s, id abcdef0123456789"),
    "job <uuid> failed at step # after # s, id <id>");
  assert.equal(normalizeReason("   "), "(no reason)");
});

test("classify", () => {
  assert.equal(classify({ success: true }), "ok");
  assert.equal(classify({ success: false, error: "Not started: Graph API stopped earlier (checkpoint)." }), "not_started");
  assert.equal(classify({ success: false, error: "Graph API stopped: ... (checkpoint)" }), "failed");
  assert.equal(classify({}), "failed");
});

test("same failure same fingerprint", async () => {
  const a = await fingerprint("ad 1: video upload: needs about 494 Meta calls; this run can spare 88.");
  const b = await fingerprint("ad 2: video upload: needs about 24 Meta calls; this run can spare 4.");
  const c = await fingerprint("Graph API stopped: The account needs to fix an issue (checkpoint).");
  assert.match(a, /^[0-9a-f]{12}$/);
  assert.equal(a, b);
  assert.notEqual(a, c);
});

test("yotam H23 sequence", async () => {
  const env = makeEnv();
  await ingestEvents(env, YOTAM, { batchId: "b1" });
  const runs = await all(env, "SELECT * FROM runs ORDER BY run_id");
  assert.equal(runs.length, 2);
  const [r45, r50] = runs;
  assert.equal(r45.run_id, "job-yotam-h23|CL_ML_All_Video_CL_HV_TRD_45_16.11");
  assert.equal(r45.outcome, "failed");
  assert.equal(r45.stop_kind, "checkpoint");
  assert.equal(r45.calls, 85);
  assert.equal(r45.writes, 257);
  assert.equal(r45.duration_s, 344.6);
  assert.match(r45.fingerprint, /^[0-9a-f]{12}$/);
  assert.match(r45.error, /checkpoint/);
  assert.equal(r45.manager, "Yotam Balteriski");
  assert.equal(r45.account, "KLD_BFL_H23");
  assert.equal(r45.country, "CL");
  assert.equal(r45.n_ads, JSON.parse(r45.ad_names).length);
  assert.deepEqual(JSON.parse(r45.adset_names), ["CL_ML_All_Video_CL_HV_TRD_45_16.11"]);
  assert.equal(r50.outcome, "not_started");
  assert.equal(r50.fingerprint, null);
  const [job] = await all(env, "SELECT * FROM jobs");
  assert.equal(job.job_id, "job-yotam-h23");
  assert.equal(job.n_planned, 4);
  assert.equal(job.n_ok, 0);
  assert.equal(job.n_failed, 1);
  assert.equal(job.n_not_started, 1);
  assert.equal(job.duration_s, 1476.9);
  assert.equal(job.status, "done");
  assert.equal(job.reviewed, 1);
  assert.equal(job.dry_run, 0);
  assert.equal(job.account, "KLD_BFL_H23");
  const [m] = await all(env, "SELECT * FROM machines");
  assert.equal(m.machine, "Yotam Balteriski");
  assert.equal(m.last_event_at, YOTAM.at(-1).ts);
  assert.equal(m.first_seen_at, YOTAM[0].ts);
});

test("duplicate batch does not double counters", async () => {
  const env = makeEnv();
  await ingestEvents(env, YOTAM, { batchId: "b1" });
  await ingestEvents(env, YOTAM, { batchId: "b1" });
  const [job] = await all(env, "SELECT * FROM jobs");
  assert.equal(job.n_failed, 1);
  assert.equal(job.n_not_started, 1);
});

test("derive is order-independent", async () => {
  const fwd = makeEnv(); const rev = makeEnv();
  await ingestEvents(fwd, YOTAM, { batchId: "b" });
  for (const ev of [...YOTAM].reverse()) await ingestEvents(rev, [ev], { batchId: "b" });
  for (const sql of ["SELECT * FROM runs ORDER BY run_id", "SELECT * FROM jobs", "SELECT * FROM machines"]) {
    assert.deepEqual(await all(rev, sql), await all(fwd, sql), sql);
  }
});

test("stats without load", async () => {
  const env = makeEnv();
  const stats = YOTAM.find((e) => e.type === "publish.stats");
  assert.equal(stats.payload.load, undefined);
  await ingestEvents(env, [stats], { batchId: "b" });
  const [r] = await all(env, "SELECT * FROM runs");
  assert.equal(r.load, null);
  assert.equal(r.calls, 85);
  const withLoad = { ...stats, event_id: "x:1", job_id: "j2", payload: { ...stats.payload, load: { cpu_pct: 71 } } };
  await ingestEvents(env, [withLoad], { batchId: "b" });
  const r2 = await env.DB.prepare("SELECT load FROM runs WHERE job_id = 'j2'").first();
  assert.deepEqual(JSON.parse(r2.load), { cpu_pct: 71 });
});

test("success result is ok and counted", async () => {
  const env = makeEnv();
  const ok = { ...YOTAM[4], event_id: "x:2", campaign: "C_ok",
    payload: { campaign_name: "C_ok", success: true, campaign_id: "1000000000000001", created: { campaign: 1 }, reused: {} } };
  await ingestEvents(env, [YOTAM[0], ok], { batchId: "b" });
  const r = await env.DB.prepare("SELECT * FROM runs WHERE campaign = 'C_ok'").first();
  assert.equal(r.outcome, "ok");
  assert.equal(r.created_any, 1);
  assert.equal(r.reused_any, 0);
  assert.equal(r.fingerprint, null);
  assert.equal((await env.DB.prepare("SELECT n_ok FROM jobs").first()).n_ok, 1);
});

test("publish event without job id derives nothing", async () => {
  const env = makeEnv();
  await ingestEvents(env, [{ ...YOTAM[3], event_id: "x:3", job_id: null }], { batchId: "b" });
  assert.equal((await all(env, "SELECT * FROM runs")).length, 0);
});
