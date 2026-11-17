// Бот, этап 1: кнопки под алертом, NEW, справочник решений, недоделанные кампании, /buyer, /machine, /tasks.
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
const HOOK = "hook_" + "s".repeat(30);
const OWNER_CHAT = 555;

let calls, realFetch;
beforeEach(() => {
  calls = []; realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    if (String(url).startsWith("https://api.telegram.org/")) {
      calls.push({ method: String(url).split("/").pop(), body: JSON.parse(init.body) });
      return new Response('{"ok":true}', { status: 200 });
    }
    return new Response("{}", { status: 200 });
  };
});
afterEach(() => { globalThis.fetch = realFetch; });

const makeEnv = () => ({ DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: "owner_" + "k".repeat(30),
  TG_BOT_TOKEN: "123456:" + "B".repeat(35), TG_WEBHOOK_SECRET: HOOK, TG_ALLOWED_CHATS: String(OWNER_CHAT),
  HUB_URL: "https://hub.example" });
const messages = () => calls.filter((c) => c.method === "sendMessage").map((c) => c.body);
const isoAgo = (min) => new Date(Date.now() - min * 60000).toISOString().replace("Z", "+00:00");
const failed = () => YOTAM.find((e) => e.type === "publish.result" && e.campaign.endsWith("_45_16.11"));
const single = (i, minAgo, extra = {}) => ({ ...failed(), event_id: `f:${i}`, job_id: `j${i}`, ts: isoAgo(minAgo), ...extra });

let n = 0;
function post(env, events) {
  const headers = { "X-ACL-Machine": "m-yotam", "X-ACL-Batch": `s${++n}-1-1`, Authorization: `Bearer ${TOKEN}` };
  return worker.fetch(new Request("https://hub/v1/batch", { method: "POST", headers,
    body: gzipSync(events.map((e) => JSON.stringify(e)).join("\n") + "\n") }), env);
}
function update(env, body) {
  return worker.fetch(new Request("https://hub/tg/webhook", { method: "POST",
    headers: { "content-type": "application/json", "X-Telegram-Bot-Api-Secret-Token": HOOK },
    body: JSON.stringify({ update_id: 1, ...body }) }), env);
}
const say = (env, text, chat = OWNER_CHAT) => update(env, { message: { message_id: 1, chat: { id: chat }, text } });
const press = (env, data, chat = OWNER_CHAT) => update(env, { callback_query: { id: "cb1", data,
  from: { id: chat }, message: { message_id: 9, chat: { id: chat } } } });
const buttons = (msg) => (msg.reply_markup?.inline_keyboard || []).flat();

test("alert has buttons and NEW for a first-seen failure", async () => {
  const env = makeEnv();
  await post(env, [single(1, 2)]);
  const [m] = messages();
  assert.match(m.text, /🆕 NEW/);
  const b = buttons(m);
  assert.deepEqual(b.map((x) => x.text), ["Open run", "Mute 24h", "Explain", "Create task"]);
  assert.ok(b[0].url.startsWith("https://hub.example/#run="));
  for (const x of b.slice(1)) assert.ok(Buffer.byteLength(x.callback_data) <= 64, x.callback_data);
});

test("second time the same failure is not NEW and carries the known fix", async () => {
  const env = makeEnv();
  await post(env, [single(1, 3)]);
  const fp = /\/mute ([0-9a-f]{12})/.exec(messages()[0].text)[1];
  await say(env, `/hint ${fp} Open the profile, pass the check on facebook.com, then Publish again.`);
  await env.DB.prepare("DELETE FROM alert_log").run();
  await post(env, [single(2, 1)]);
  const last = messages().at(-1);
  assert.doesNotMatch(last.text, /NEW/);
  assert.match(last.text, /Known fix: Open the profile, pass the check/);
});

test("Mute 24h button mutes and answers the button", async () => {
  const env = makeEnv();
  await post(env, [single(1, 2)]);
  const mute = buttons(messages()[0]).find((b) => b.text === "Mute 24h");
  await press(env, mute.callback_data);
  const row = await env.DB.prepare("SELECT until FROM mutes").first();
  assert.ok(row && Date.parse(row.until) > Date.now() + 23 * 3600000);
  const ack = calls.find((c) => c.method === "answerCallbackQuery");
  assert.equal(ack.body.callback_query_id, "cb1");
  assert.match(ack.body.text, /Muted for 24 h/);
});

test("Explain shows error, steps, what is left in Meta and the known fix", async () => {
  const env = makeEnv();
  const fail = single(1, 2, { payload: { ...failed().payload, created: { campaign: 1, adsets: 1 } } });
  const step = { ...YOTAM[1], event_id: "st:1", type: "raw.run", ts: isoAgo(3), job_id: "j1",
    payload: { step: "api_create_step", ok: false, diag: { step: "ad 2: video upload timing" } } };
  const spec = { ...YOTAM.find((e) => e.type === "publish.spec"), event_id: "sp:1", job_id: "j1", ts: isoAgo(5) };
  await post(env, [spec, step, fail]);
  const explain = buttons(messages()[0]).find((b) => b.text === "Explain");
  await press(env, explain.callback_data);
  const m = messages().at(-1);
  assert.match(m.text, /checkpoint/);
  assert.match(m.text, /ad 2: video upload timing/);
  assert.match(m.text, /Left in Meta/);
  assert.match(m.text, /No known fix yet/);
  assert.match(m.text, /\/hint [0-9a-f]{12}/);
});

test("Create task saves one request per failure and /tasks lists it", async () => {
  const env = makeEnv();
  await post(env, [single(1, 2)]);
  const task = buttons(messages()[0]).find((b) => b.text === "Create task");
  await press(env, task.callback_data);
  await press(env, task.callback_data);
  const { results } = await env.DB.prepare("SELECT * FROM task_requests").all();
  assert.equal(results.length, 1);
  assert.equal(results[0].status, "open");
  const acks = calls.filter((c) => c.method === "answerCallbackQuery").map((c) => c.body.text);
  assert.match(acks[0], /Task request saved/);
  assert.match(acks[1], /Already requested/);
  await say(env, "/tasks");
  assert.match(messages().at(-1).text, /1 open task request/);
  const api = await worker.fetch(new Request("https://hub/api/task-requests", {
    headers: { Authorization: `Bearer ${env.OWNER_KEY}` } }), env);
  assert.equal((await api.json()).requests.length, 1);
});

test("buttons from other chats are ignored", async () => {
  const env = makeEnv();
  await post(env, [single(1, 2)]);
  const mute = buttons(messages()[0]).find((b) => b.text === "Mute 24h");
  await press(env, mute.callback_data, 777);
  assert.equal(await env.DB.prepare("SELECT * FROM mutes").first(), null);
});

test("/unfinished lists partly created campaigns until a later success", async () => {
  const env = makeEnv();
  const partial = single(1, 60, { payload: { ...failed().payload, created: { campaign: 1, adsets: 1, ads: 1 } } });
  const nothing = single(2, 50, { campaign: "Nothing made", payload: { ...failed().payload, campaign_name: "Nothing made", created: {} } });
  await ingestEvents(env, [partial, nothing], { batchId: "b" });
  await say(env, "/unfinished");
  let t = messages().at(-1).text;
  assert.match(t, /1 unfinished/);
  assert.match(t, /CL_ML_All_Video_CL_HV_TRD_45_16\.11/);
  assert.doesNotMatch(t, /Nothing made/);
  const ok = { ...partial, event_id: "ok:1", job_id: "j9", ts: isoAgo(5),
    payload: { campaign_name: partial.campaign, success: true, created: {}, reused: { campaign: 1 } } };
  await ingestEvents(env, [ok], { batchId: "b" });
  await say(env, "/unfinished");
  t = messages().at(-1).text;
  assert.match(t, /No unfinished campaigns/);
});

test("/buyer and /machine find by name prefix", async () => {
  const env = makeEnv();
  await ingestEvents(env, [single(1, 10)], { batchId: "b" });
  await say(env, "/buyer yot");
  assert.match(messages().at(-1).text, /Yotam Balteriski/);
  assert.match(messages().at(-1).text, /Last runs/);
  await say(env, "/machine yot");
  assert.match(messages().at(-1).text, /Version/);
  await say(env, "/buyer nobody");
  assert.match(messages().at(-1).text, /No buyer/);
});

test("/hint without text shows usage and hints are escaped", async () => {
  const env = makeEnv();
  await say(env, "/hint 0123456789ab");
  assert.match(messages().at(-1).text, /Usage/);
  await say(env, "/hint 0123456789ab <b>do</b> this");
  const h = await env.DB.prepare("SELECT hint FROM failure_hints").first("hint");
  assert.equal(h, "<b>do</b> this");
  await say(env, "/failures");
});
