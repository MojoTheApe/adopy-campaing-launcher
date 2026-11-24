// Telegram-бот: алерты о сбоях при приёме, антиспам и тишина, зависшие задания, дайджест, команды.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { gzipSync } from "node:zlib";
import { readFileSync } from "node:fs";
import { FakeD1 } from "./fake-d1.js";
import { ingestEvents } from "../src/ingest.js";
import { splitMessage } from "../src/telegram.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const YOTAM = JSON.parse(readFileSync(new URL("./fixtures-yotam-h23.json", import.meta.url), "utf8"));
const TOKEN = ["lib", "token", "x".repeat(20)].join("_");
const BOT = "123456:" + "B".repeat(35);
const HOOK = "hook_" + "s".repeat(30);
const OWNER_CHAT = "555";

let sent, tgStatus, realFetch;
beforeEach(() => {
  sent = []; tgStatus = []; realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (String(url).startsWith("https://api.telegram.org/")) {
      sent.push({ url: String(url), body: JSON.parse(init.body) });
      const status = tgStatus.length ? tgStatus.shift() : 200;
      return new Response(JSON.stringify({ ok: status === 200, description: "test" }), { status });
    }
    return new Response("{}", { status: 200 });   // GitHub: токен библиотеки принят
  };
});
afterEach(() => { globalThis.fetch = realFetch; });

const makeEnv = (extra = {}) => ({ DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: "owner_" + "k".repeat(30),
  TG_BOT_TOKEN: BOT, TG_WEBHOOK_SECRET: HOOK, TG_ALLOWED_CHATS: OWNER_CHAT, HUB_URL: "https://hub.example", ...extra });
const texts = () => sent.filter((s) => s.url.endsWith("/sendMessage")).map((s) => s.body.text);
const isoAgo = (min) => new Date(Date.now() - min * 60000).toISOString().replace("Z", "+00:00");

// Те же события, сдвинутые так, что последнее было минуту назад (порядок и интервалы сохранены).
function recent(events, endMinAgo = 1) {
  const last = Math.max(...events.map((e) => Date.parse(e.ts)));
  const shift = Date.now() - endMinAgo * 60000 - last;
  return events.map((e) => ({ ...e, ts: new Date(Date.parse(e.ts) + shift).toISOString().replace("Z", "+00:00") }));
}

let batchN = 0;
function post(env, events) {
  const headers = { "X-ACL-Machine": "m-yotam", "X-ACL-Batch": `b${++batchN}-1-1`, Authorization: `Bearer ${TOKEN}` };
  const body = gzipSync(events.map((e) => JSON.stringify(e)).join("\n") + "\n");
  return worker.fetch(new Request("https://hub/v1/batch", { method: "POST", headers, body }), env);
}

const failed = () => YOTAM.find((e) => e.type === "publish.result" && e.campaign.endsWith("_45_16.11"));
const single = (i, minAgo, extra = {}) => ({ ...failed(), event_id: `f:${i}`, job_id: `j${i}`, ts: isoAgo(minAgo), ...extra });

test("failed run sends one alert with time, link and mute hint", async () => {
  const env = makeEnv();
  assert.equal((await post(env, recent(YOTAM))).status, 200);
  const t = texts();
  assert.equal(t.length, 1, "only the failed run alerts; ok and not-started do not");
  assert.match(t[0], /Campaign failed/);
  assert.match(t[0], /Yotam Balteriski/);
  assert.match(t[0], /CL_ML_All_Video_CL_HV_TRD_45_16\.11/);
  assert.match(t[0], /checkpoint/);
  assert.match(t[0], /at \d{2}:\d{2}/);
  assert.ok(t[0].includes("https://hub.example/#run=" + encodeURIComponent("job-yotam-h23|CL_ML_All_Video_CL_HV_TRD_45_16.11")));
  assert.match(t[0], /\/mute [0-9a-f]{12}/);
  assert.equal(sent[0].body.chat_id, OWNER_CHAT);
  assert.equal(sent[0].body.parse_mode, "HTML");
});

test("redelivery does not alert again", async () => {
  const env = makeEnv();
  const evs = recent(YOTAM);
  await post(env, evs);
  await post(env, evs);
  assert.equal(texts().length, 1);
});

test("same failure is held for 30 minutes, then counted", async () => {
  const env = makeEnv();
  await post(env, [single(1, 9)]);
  await post(env, [single(2, 8)]);
  await post(env, [single(3, 7, { machine_id: "m-gil", machine: "Gil Zangi", manager: "Gil Zangi" })]);
  assert.equal(texts().length, 1, "same error on another machine is also held");
  await env.DB.prepare("UPDATE alert_log SET last_sent_at = '2000-01-01T00:00:00.000+00:00'").run();
  await post(env, [single(4, 1)]);
  const t = texts();
  assert.equal(t.length, 2);
  assert.match(t[1], /2 more like this were not sent/);
});

test("many failures of one job come as one message", async () => {
  const env = makeEnv();
  const evs = Array.from({ length: 12 }, (_, i) => ({ ...failed(), event_id: `m:${i}`, job_id: "jbig", ts: isoAgo(2),
    campaign: `Camp ${i}`, payload: { ...failed().payload, campaign_name: `Camp ${i}`, error: `Campaign 'Camp ${i}' refused (${i})` } }));
  await post(env, evs);
  const t = texts();
  assert.equal(t.length, 1);
  assert.match(t[0], /12 campaigns failed/);
  assert.match(t[0], /and 7 more/);
});

test("old failures delivered late do not alert", async () => {
  const env = makeEnv();
  await post(env, [single(1, 5 * 60)]);
  assert.equal(texts().length, 0);
});

test("dry-run job failures do not alert", async () => {
  const env = makeEnv();
  const created = { ...YOTAM[0], event_id: "d:0", job_id: "jdry", ts: isoAgo(3), payload: { ...YOTAM[0].payload, dry_run: true } };
  await post(env, [created, single(5, 2, { job_id: "jdry" })]);
  assert.equal(texts().length, 0);
});

test("muted fingerprint is not sent", async () => {
  const env = makeEnv();
  await post(env, [single(1, 2)]);
  const fp = /\/mute ([0-9a-f]{12})/.exec(texts()[0])[1];
  await env.DB.prepare("INSERT INTO mutes (fingerprint, until) VALUES (?, '2999-01-01T00:00:00.000Z')").bind(fp).run();
  await env.DB.prepare("DELETE FROM alert_log").run();
  await post(env, [single(9, 1)]);
  assert.equal(texts().length, 1);
});

test("no bot configured sends nothing and ingest still works", async () => {
  const env = makeEnv({ TG_BOT_TOKEN: "" });
  assert.equal((await post(env, recent(YOTAM))).status, 200);
  assert.equal(sent.length, 0);
});

test("backfill never alerts", async () => {
  const env = makeEnv();
  const headers = { Authorization: `Bearer ${env.OWNER_KEY}`, "X-ACL-Batch": "backfill-9-9" };
  await worker.fetch(new Request("https://hub/v1/backfill", { method: "POST", headers,
    body: gzipSync(JSON.stringify(single(1, 1)) + "\n") }), env);
  assert.equal(sent.length, 0);
});

test("alert text is escaped", async () => {
  const env = makeEnv();
  await post(env, [single(1, 1, { campaign: "<b>evil</b>", payload: { ...failed().payload, campaign_name: "<b>evil</b>", error: "boom <script>" } })]);
  assert.ok(texts()[0].includes("&lt;b&gt;evil&lt;/b&gt;"));
  assert.ok(texts()[0].includes("&lt;script&gt;"));
});

test("alerts already derived before a D1 error are still sent", async () => {
  const env = makeEnv();
  await post(env, [single(0, 3)]);   // запись о токене уже есть
  sent.length = 0;
  await env.DB.prepare("DELETE FROM alert_log").run();
  const filler = Array.from({ length: 40 }, (_, i) => ({ ...YOTAM[1], event_id: `fl:${i}`, job_id: `jf${i}`, ts: isoAgo(2) }));
  const real = env.DB.batch.bind(env.DB);
  let calls = 0;
  env.DB.batch = async (stmts) => { calls += 1; if (calls === 3) throw new Error("D1_ERROR"); return real(stmts); };
  const res = await post(env, [single(7, 1, { campaign: "Other", payload: { ...failed().payload, campaign_name: "Other", error: "other error" } }), ...filler]);
  assert.equal(res.status, 503);
  assert.equal(texts().length, 1, "the failure committed in the first derive chunk is alerted");
});

test("telegram 400 is retried without buttons, then as plain text", async () => {
  const env = makeEnv();
  tgStatus = [400, 400, 200];
  await post(env, [single(1, 1)]);
  assert.equal(sent.length, 3);
  assert.ok(sent[0].body.reply_markup);
  assert.equal(sent[1].body.reply_markup, undefined);
  assert.equal(sent[1].body.parse_mode, "HTML");
  assert.equal(sent[2].body.parse_mode, undefined);
  assert.ok(!/<b>|<a /.test(sent[2].body.text));
});

test("long text is split at line boundaries", () => {
  const lines = Array.from({ length: 300 }, (_, i) => `<b>Buyer ${i}</b>: ${"x".repeat(40)}`);
  const parts = splitMessage(lines.join("\n"), 4000);
  assert.ok(parts.length > 1);
  for (const p of parts) {
    assert.ok(p.length <= 4000);
    assert.equal((p.match(/<b>/g) || []).length, (p.match(/<\/b>/g) || []).length);
  }
  assert.equal(parts.join("\n"), lines.join("\n"));
});

// ── cron ──
const tick = (env, when) => worker.scheduled({ cron: "*/5 * * * *", scheduledTime: Date.parse(when) }, env, { waitUntil() {} });

test("stuck job alerts once", async () => {
  const env = makeEnv();
  const started = YOTAM.find((e) => e.type === "job.started");
  await ingestEvents(env, [{ ...YOTAM[0], ts: "2026-11-17T08:59:00.000+00:00" }, { ...started, ts: "2026-11-17T09:00:00.000+00:00" }], { batchId: "b" });
  await tick(env, "2026-11-17T09:50:00Z");
  await tick(env, "2026-11-17T09:55:00Z");
  const t = texts().filter((x) => /Job running/.test(x));
  assert.equal(t.length, 1);
  assert.match(t[0], /Job running for 50 min/);
  assert.match(t[0], /KLD_BFL_H23/);
});

test("job with every result in, or older than 3 h, is not stuck", async () => {
  const env = makeEnv();
  const noFinish = YOTAM.filter((e) => e.type !== "job.finished");
  await ingestEvents(env, noFinish, { batchId: "b" });
  const ok = (c) => ({ ...failed(), event_id: `ok:${c}`, campaign: c, payload: { campaign_name: c, success: true, created: {}, reused: {} } });
  await ingestEvents(env, [ok("CL_ML_All_Video_CL_HV_TRD_35_16.11"), ok("CL_ML_All_Video_CL_HV_TRD_40_16.11")], { batchId: "b" });
  await tick(env, "2026-11-16T16:30:00Z");
  const started = YOTAM.find((e) => e.type === "job.started");
  const env2 = makeEnv();
  await ingestEvents(env2, [YOTAM[0], started], { batchId: "b" });
  await tick(env2, "2026-11-16T19:40:00Z");
  assert.equal(texts().filter((x) => /Job running/.test(x)).length, 0);
});

test("morning digest after 07:00 Israel, once per day, retried if Telegram failed", async () => {
  const env = makeEnv();
  await ingestEvents(env, YOTAM, { batchId: "b" });
  await tick(env, "2026-11-17T04:55:00Z");          // 06:55 по Израилю — ещё рано
  assert.equal(texts().length, 0);
  tgStatus = [500, 500];
  await tick(env, "2026-11-17T05:00:00Z");          // Telegram не принял (и HTML, и простой текст)
  await tick(env, "2026-11-17T05:05:00Z");          // повтор
  await tick(env, "2026-11-17T05:10:00Z");          // уже отправлен
  const t = texts().filter((x) => /Yesterday/.test(x));
  assert.equal(t.length, 3);
  assert.match(t[2], /Yesterday · 16 Nov/);
  assert.match(t[2], /Yotam Balteriski: 2 campaigns · 0 ok · 1 failed · 1 not started/);
  assert.match(t[2], /Top failures/);
});

test("digest follows Israel summer time", async () => {
  const env = makeEnv();
  await tick(env, "2026-07-01T03:55:00Z");          // 06:55 летом (UTC+3)
  assert.equal(texts().length, 0);
  await tick(env, "2026-07-01T04:00:00Z");          // 07:00
  assert.equal(texts().filter((x) => /Yesterday · 30 Jun/.test(x)).length, 1);
});

// ── webhook ──
function hook(env, text, { secret = HOOK, chat = OWNER_CHAT, edited = false } = {}) {
  const message = { message_id: 1, chat: { id: Number(chat) }, text };
  return worker.fetch(new Request("https://hub/tg/webhook", { method: "POST",
    headers: { "content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": secret },
    body: JSON.stringify({ update_id: 1, [edited ? "edited_message" : "message"]: message }) }), env);
}

test("webhook rejects wrong secret", async () => {
  assert.equal((await hook(makeEnv(), "/today", { secret: "nope" })).status, 401);
  assert.equal(sent.length, 0);
});

test("webhook ignores other chats and edited messages", async () => {
  const env = makeEnv();
  assert.equal((await hook(env, "/today", { chat: "777" })).status, 200);
  assert.equal((await hook(env, "/today", { edited: true })).status, 200);
  assert.equal(sent.length, 0);
});

test("/today and /help answer", async () => {
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
  await ingestEvents(env, [single(1, 1)], { batchId: "b" });
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
