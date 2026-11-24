// «Опубликовано с проблемами»: ACL вернул success, но сам записал blocking_failures (расхождения при перечитывании).
// В ACL такие кампании жёлтые — в хабе и боте тоже.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { FakeD1 } from "./fake-d1.js";
import { ingestEvents } from "../src/ingest.js";
import { dayText } from "../src/alerts.js";
import { PAGE_HTML } from "../src/ui.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const YOTAM = JSON.parse(readFileSync(new URL("./fixtures-yotam-h23.json", import.meta.url), "utf8"));
const OWNER = "owner_" + "k".repeat(30);
const result = YOTAM.find((e) => e.type === "publish.result" && e.campaign.endsWith("_45_16.11"));
const MISMATCH = "readback mismatch: ad 1 (2421 2 - ES Pro Meli) main_file — expected '<digits>', got '<digits>'";

function okWith(blocking, id = "w:1", campaign = "C_warn") {
  return { ...result, event_id: id, campaign, payload: { campaign_name: campaign, success: true, status: "PAUSED",
    created: { campaign: 1 }, reused: {}, blocking_failures: blocking } };
}
async function makeEnv(events) {
  const env = { DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: OWNER };
  await ingestEvents(env, events, { batchId: "b" });
  return env;
}
const get = async (env, path) => (await worker.fetch(new Request(`https://hub${path}`,
  { headers: { Authorization: `Bearer ${OWNER}` } }), env)).json();

test("success with blocking failures is ok with issues", async () => {
  const env = await makeEnv([okWith([MISMATCH, MISMATCH]), okWith([], "w:2", "C_clean")]);
  const warn = await env.DB.prepare("SELECT * FROM runs WHERE campaign = 'C_warn'").first();
  assert.equal(warn.outcome, "ok");
  assert.equal(warn.issues, 2);
  assert.match(warn.error, /readback mismatch/);
  assert.equal(warn.fingerprint, null);
  const clean = await env.DB.prepare("SELECT * FROM runs WHERE campaign = 'C_clean'").first();
  assert.equal(clean.issues, 0);
  assert.equal(clean.error, null);
});

test("live shows a yellow line for published with problems", async () => {
  const env = await makeEnv([{ ...okWith([MISMATCH]), ts: new Date().toISOString().replace("Z", "+00:00") }]);
  const { events } = await get(env, "/api/live");
  const e = events.find((x) => x.campaign === "C_warn");
  assert.equal(e.level, "warn");
  assert.match(e.text, /^Published with problems \(1\): readback mismatch/);
});

test("summary and /today count published with problems", async () => {
  const env = await makeEnv([okWith([MISMATCH]), okWith([], "w:2", "C_clean")]);
  const { buyers } = await get(env, "/api/summary?day=2026-11-16&tz=120");
  const y = buyers.find((b) => b.manager === "Yotam Balteriski");
  assert.equal(y.ok, 2);
  assert.equal(y.issues, 1);
  const lines = await dayText(env, "2026-11-16", "Today");
  assert.ok(lines.some((l) => /2 ok \(1 with problems\)/.test(l)), lines.join("\n"));
});

test("history is re-read: existing ok runs get their issues from stored events", async () => {
  // Миграция 0005 заполняет issues у старых прогонов из уже лежащих событий publish.result.
  const env = await makeEnv([okWith([MISMATCH, MISMATCH, MISMATCH])]);
  await env.DB.prepare("UPDATE runs SET issues = 0, error = NULL").run();
  const sql = readFileSync(new URL("../migrations/0005_run_issues.sql", import.meta.url), "utf8")
    .split(";").filter((s) => /UPDATE/i.test(s)).join(";");
  env.DB.raw.exec(sql);
  const r = await env.DB.prepare("SELECT issues, error FROM runs").first();
  assert.equal(r.issues, 3);
  assert.match(r.error, /readback mismatch/);
});

test("page paints ok-with-issues yellow", () => {
  assert.match(PAGE_HTML, /with problems/);
  assert.match(PAGE_HTML, /r\.issues/);
});
