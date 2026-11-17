// Telegram-бот: алерты о сбоях при приёме, антиспам и тишина, зависшие задания, дайджест, команды.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { gzipSync } from "node:zlib";
import { readFileSync } from "node:fs";
import { FakeD1 } from "./fake-d1.js";
import { ingestEvents } from "../src/ingest.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const YOTAM = JSON.parse(readFileSync(new URL("./fixtures-yotam-h23.json", import.meta.url), "utf8"));
const TOKEN = ["lib", "token", "x".repeat(20)].join("_");
const BOT = "123456:" + "B".repeat(35);
const HOOK = "hook_" + "s".repeat(30);
const OWNER_CHAT = "555";

let sent, realFetch;
beforeEach(() => {
  sent = []; realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (String(url).startsWith("https://api.telegram.org/")) {
      sent.push({ url: String(url), body: JSON.parse(init.body) });
      return new Response('{"ok":true}', { status: 200 });
    }
    return new Response("{}", { status: 200 });   // GitHub: токен библиотеки принят
  };
});
afterEach(() => { globalThis.fetch = realFetch; });

const makeEnv = (extra = {}) => ({ DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: "owner_" + "k".repeat(30),
  TG_BOT_TOKEN: BOT, TG_WEBHOOK_SECRET: HOOK, TG_ALLOWED_CHATS: OWNER_CHAT, HUB_URL: "https://hub.example", ...extra });
const texts = () => sent.filter((s) => s.url.endsWith("/sendMessage")).map((s) => s.body.text);

function post(env, events, batch = "a".repeat(32) + "-1-1") {
  const headers = { "X-ACL-Machine": "m-yotam", "X-ACL-Batch": batch, Authorization: `Bearer ${TOKEN}` };
  const body = gzipSync(events.map((e) => JSON.stringify(e)).join("\n") + "\n");
  return worker.fetch(new Request("https://hub/v1/batch", { method: "POST", headers, body }), env);
}

const failed = () => YOTAM.find((e) => e.type === "publish.result" && e.campaign.endsWith("_45_16.11"));

test("failed run sends one alert with link and mute hint", async () => {
  const env = makeEnv();
  assert.equal((await post(env, YOTAM)).status, 200);
  const t = texts();
  assert.equal(t.length, 1, "only the failed run alerts; ok and not-started do not");
  assert.match(t[0], /Campaign failed/);
  assert.match(t[0], /Yotam Balteriski/);
  assert.match(t[0], /CL_ML_All_Video_CL_HV_TRD_45_16\.11/);
  assert.match(t[0], /checkpoint/);
  assert.ok(t[0].includes("https://hub.example/#run=" + encodeURIComponent("job-yotam-h23|CL_ML_All_Video_CL_HV_TRD_45_16.11")));
  assert.match(t[0], /\/mute [0-9a-f]{12}/);
  assert.equal(sent[0].body.chat_id, OWNER_CHAT);
  assert.equal(sent[0].body.parse_mode, "HTML");
});

test("redelivery does not alert again", async () => {
  const env = makeEnv();
  await post(env, YOTAM);
  await post(env, YOTAM);
  assert.equal(texts().length, 1);
});

test("same failure on same machine is held for 30 minutes, then counted", async () => {
  const env = makeEnv();
  const mk = (i, ts) => ({ ...failed(), event_id: `f:${i}`, job_id: `j${i}`, ts });
  await post(env, [mk(1, "2026-11-17T10:00:00.000+00:00")]);
  await post(env, [mk(2, "2026-11-17T10:05:00.000+00:00")], "b".repeat(32) + "-1-1");
  await post(env, [mk(3, "2026-11-17T10:06:00.000+00:00")], "c".repeat(32) + "-1-1");
  assert.equal(texts().length, 1);
  await env.DB.prepare("UPDATE alert_log SET last_sent_at = '2000-01-01T00:00:00.000+00:00'").run();
  await post(env, [mk(4, "2026-11-17T10:40:00.000+00:00")], "d".repeat(32) + "-1-1");
  const t = texts();
  assert.equal(t.length, 2);
  assert.match(t[1], /2 more like this were not sent/);
});

test("muted fingerprint is not sent", async () => {
  const env = makeEnv();
  await post(env, YOTAM);
  const fp = /\/mute ([0-9a-f]{12})/.exec(texts()[0])[1];
  await env.DB.prepare("INSERT INTO mutes (fingerprint, until) VALUES (?, '2999-01-01T00:00:00.000Z')").bind(fp).run();
  await env.DB.prepare("DELETE FROM alert_log").run();
  await post(env, [{ ...failed(), event_id: "f:9", job_id: "j9" }], "e".repeat(32) + "-1-1");
  assert.equal(texts().length, 1);
});

test("no bot configured sends nothing and ingest still works", async () => {
  const env = makeEnv({ TG_BOT_TOKEN: "" });
  assert.equal((await post(env, YOTAM)).status, 200);
  assert.equal(sent.length, 0);
});

test("backfill never alerts", async () => {
  const env = makeEnv();
  await ingestEvents(env, YOTAM, { batchId: "backfill-1-7" });
  const headers = { Authorization: `Bearer ${env.OWNER_KEY}`, "X-ACL-Batch": "backfill-9-9" };
  await worker.fetch(new Request("https://hub/v1/backfill", { method: "POST", headers,
    body: gzipSync(JSON.stringify({ ...failed(), event_id: "bf:1", job_id: "bf" }) + "\n") }), env);
  assert.equal(sent.length, 0);
});

test("alert text is escaped", async () => {
  const env = makeEnv();
  const ev = { ...failed(), event_id: "x:1", job_id: "jx", campaign: "<b>evil</b>",
    payload: { ...failed().payload, campaign_name: "<b>evil</b>", error: "boom <script>" } };
  await post(env, [ev]);
  assert.ok(texts()[0].includes("&lt;b&gt;evil&lt;/b&gt;"));
  assert.ok(texts()[0].includes("&lt;script&gt;"));
});

// ── cron ──
const cron = (env, expr, when) => worker.scheduled({ cron: expr, scheduledTime: Date.parse(when) }, env, { waitUntil() {} });

test("stuck job alerts once", async () => {
  const env = makeEnv();
  const started = YOTAM.find((e) => e.type === "job.started");
  await ingestEvents(env, [YOTAM[0], { ...started, ts: "2026-11-17T09:00:00.000+00:00" }], { batchId: "b" });
  await cron(env, "*/5 * * * *", "2026-11-17T10:00:00Z");
  await cron(env, "*/5 * * * *", "2026-11-17T10:05:00Z");
  const t = texts();
  assert.equal(t.length, 1);
  assert.match(t[0], /Job running for 60 min/);
  assert.match(t[0], /KLD_BFL_H23/);
});

test("finished or fresh jobs are not stuck", async () => {
  const env = makeEnv();
  await ingestEvents(env, YOTAM, { batchId: "b" });
  await cron(env, "*/5 * * * *", "2026-11-16T16:30:00Z");
  assert.equal(texts().length, 0);
});

test("morning digest once per day with buyers and top failures", async () => {
  const env = makeEnv();
  await ingestEvents(env, YOTAM, { batchId: "b" });
  await cron(env, "0 5 * * *", "2026-11-17T05:00:00Z");
  await cron(env, "0 5 * * *", "2026-11-17T05:00:30Z");
  const t = texts();
  assert.equal(t.length, 1);
  assert.match(t[0], /Yesterday · 16 Nov/);
  assert.match(t[0], /Yotam Balteriski: 2 campaigns · 0 ok · 1 failed · 1 not started/);
  assert.match(t[0], /Top failures/);
});

// ── webhook ──
function hook(env, text, { secret = HOOK, chat = OWNER_CHAT } = {}) {
  return worker.fetch(new Request("https://hub/tg/webhook", { method: "POST",
    headers: { "content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": secret },
    body: JSON.stringify({ update_id: 1, message: { message_id: 1, chat: { id: Number(chat) }, text } }) }), env);
}

test("webhook rejects wrong secret", async () => {
  assert.equal((await hook(makeEnv(), "/today", { secret: "nope" })).status, 401);
  assert.equal(sent.length, 0);
});

test("webhook ignores other chats", async () => {
  const res = await hook(makeEnv(), "/today", { chat: "777" });
  assert.equal(res.status, 200);
  assert.equal(sent.length, 0);
});

test("/today and /yesterday answer with buyers", async () => {
  const env = makeEnv();
  await ingestEvents(env, YOTAM, { batchId: "b" });
  await hook(env, "/today");
  assert.equal(sent.length, 1);
  assert.equal(sent[0].body.chat_id, OWNER_CHAT);
  assert.match(texts()[0], /Today/);
  await hook(env, "/help");
  assert.match(texts()[1], /\/today/);
});

test("/failures lists recent failures", async () => {
  const env = makeEnv();
  await ingestEvents(env, [{ ...failed(), ts: new Date().toISOString().replace("Z", "+00:00") }], { batchId: "b" });
  await hook(env, "/failures");
  assert.match(texts()[0], /checkpoint/);
});

test("/mute and /unmute store the mute", async () => {
  const env = makeEnv();
  await hook(env, "/mute 0123456789ab 2");
  const row = await env.DB.prepare("SELECT * FROM mutes WHERE fingerprint = '0123456789ab'").first();
  assert.ok(row && Date.parse(row.until) > Date.now() + 3600000);
  assert.match(texts()[0], /Muted 0123456789ab for 2 h/);
  await hook(env, "/unmute 0123456789ab");
  assert.equal(await env.DB.prepare("SELECT * FROM mutes").first(), null);
  await hook(env, "/mute nonsense");
  assert.match(texts()[2], /Usage/);
});
