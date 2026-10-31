// Тесты Worker общего журнала (ACL-2347). Запуск: cd deploy/event-log && node --test worker.test.js
// Worker проверяется как чёрный ящик через default.fetch: R2 и KV в памяти, GitHub подменён.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { gzipSync } from "node:zlib";
import worker from "./worker.js";

// Пустышки собираются в коде: в файле нет ничего похожего на настоящий ключ.
const TOKEN = ["lib", "token", "x".repeat(20)].join("_");
const READ_KEY = ["read", "key", "y".repeat(20)].join("_");
const BATCH = "a".repeat(32) + "-1-5";

class MemoryKV {
  constructor() { this.data = new Map(); this.puts = []; }
  async get(key) { return this.data.has(key) ? this.data.get(key) : null; }
  async put(key, value, options) { this.data.set(key, value); this.puts.push({ key, value, options }); }
  async delete(key) { this.data.delete(key); }
}

class MemoryR2 {
  constructor() { this.objects = new Map(); this.puts = 0; }
  async put(key, body, opts) {
    this.puts++;
    if (opts && opts.onlyIf && this.objects.has(key)) return null;
    this.objects.set(key, new Uint8Array(body));
    return { key };
  }
  async list({ prefix = "", cursor, limit = 1000 }) {
    const keys = [...this.objects.keys()].filter((k) => k.startsWith(prefix)).sort();
    const start = cursor ? Number(cursor) : 0;
    const slice = keys.slice(start, start + limit);
    const truncated = start + limit < keys.length;
    return { objects: slice.map((key) => ({ key })), truncated, cursor: truncated ? String(start + limit) : undefined };
  }
  async get(key) {
    if (!this.objects.has(key)) return null;
    const bytes = this.objects.get(key);
    return { arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) };
  }
}

let githubCalls;
let githubStatus;
let realFetch;

beforeEach(() => {
  githubCalls = [];
  githubStatus = 200;
  realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    githubCalls.push({ url, init });
    if (githubStatus === "network") throw new Error("network down");
    return new Response("{}", { status: githubStatus });
  };
});
afterEach(() => { globalThis.fetch = realFetch; });

function makeEnv() {
  return { EVENTS: new MemoryR2(), AUTH: new MemoryKV(), READ_KEY, LIBRARY_REPO: "Owner/lib" };
}

function post(env, { token = TOKEN, machine = "m1", batch = BATCH, body = gzipSync("{}\n"), extra = {} } = {}) {
  const headers = { "X-ACL-Machine": machine, "X-ACL-Batch": batch, ...extra };
  if (token) headers.Authorization = `Bearer ${token}`;
  return worker.fetch(new Request("https://log.example/v1/batch", { method: "POST", headers, body }), env);
}

function read(env, path, key = READ_KEY) {
  const headers = key ? { Authorization: `Bearer ${key}` } : {};
  return worker.fetch(new Request(`https://log.example${path}`, { headers }), env);
}

async function tokenKey(env) { return [...env.AUTH.data.keys()][0]; }

test("batch with a good token is stored under the dated key", async () => {
  const env = makeEnv();
  const resp = await post(env);
  assert.equal(resp.status, 200);
  const keys = [...env.EVENTS.objects.keys()];
  assert.equal(keys.length, 1);
  assert.match(keys[0], new RegExp(`^v1/dt=\\d{4}-\\d{2}-\\d{2}/machine=m1/${BATCH}\\.ndjson\\.gz$`));
});

test("the same batch again is accepted and the object is untouched", async () => {
  const env = makeEnv();
  await post(env, { body: gzipSync("first\n") });
  const key = [...env.EVENTS.objects.keys()][0];
  const before = Buffer.from(env.EVENTS.objects.get(key));
  const resp = await post(env, { body: gzipSync("second\n") });
  assert.equal(resp.status, 200);
  assert.equal((await resp.json()).duplicate, true);
  assert.deepEqual(Buffer.from(env.EVENTS.objects.get(key)), before);
});

test("a second request with the same token does not call GitHub again", async () => {
  const env = makeEnv();
  await post(env);
  await post(env, { batch: "b".repeat(32) + "-1-1" });
  assert.equal(githubCalls.length, 1);
  assert.match(githubCalls[0].url, /repos\/Owner\/lib$/);
});

test("GitHub 401 refuses the token and drops the cached record", async () => {
  const env = makeEnv();
  await post(env);
  const key = await tokenKey(env);
  env.AUTH.data.set(key, JSON.stringify({ checked_at: Date.now() - 2 * 3600 * 1000 }));
  githubStatus = 401;
  const resp = await post(env, { batch: "c".repeat(32) + "-1-1" });
  assert.equal(resp.status, 401);
  assert.equal(env.AUTH.data.has(key), false);
});

test("a record older than an hour with GitHub down still lets a working machine write", async () => {
  const env = makeEnv();
  await post(env);
  const key = await tokenKey(env);
  env.AUTH.data.set(key, JSON.stringify({ checked_at: Date.now() - 2 * 3600 * 1000 }));
  githubStatus = 500;
  assert.equal((await post(env, { batch: "d".repeat(32) + "-1-1" })).status, 200);
});

test("a record older than a day with GitHub down is 503", async () => {
  const env = makeEnv();
  await post(env);
  const key = await tokenKey(env);
  env.AUTH.data.set(key, JSON.stringify({ checked_at: Date.now() - 25 * 3600 * 1000 }));
  githubStatus = 500;
  assert.equal((await post(env, { batch: "e".repeat(32) + "-1-1" })).status, 503);
});

test("no record and GitHub down is 503, a network error too", async () => {
  const env = makeEnv();
  githubStatus = 500;
  assert.equal((await post(env)).status, 503);
  githubStatus = "network";
  assert.equal((await post(env)).status, 503);
});

test("the token itself is never stored", async () => {
  const env = makeEnv();
  await post(env);
  for (const [k, v] of env.AUTH.data) {
    assert.ok(!k.includes(TOKEN) && !String(v).includes(TOKEN));
  }
  assert.ok(env.AUTH.puts.every((p) => p.options && p.options.expirationTtl === 86400));
});

test("a missing token is 401 without calling GitHub", async () => {
  const env = makeEnv();
  assert.equal((await post(env, { token: "" })).status, 401);
  assert.equal(githubCalls.length, 0);
});

test("bad machine or batch headers are 400", async () => {
  const env = makeEnv();
  assert.equal((await post(env, { machine: "../x" })).status, 400);
  assert.equal((await post(env, { batch: "a/b" })).status, 400);
  assert.equal(env.EVENTS.objects.size, 0);
});

test("a body that is not gzip is 400, over a megabyte is 413", async () => {
  const env = makeEnv();
  assert.equal((await post(env, { body: Buffer.from("plain text") })).status, 400);
  const big = Buffer.concat([Buffer.from([0x1f, 0x8b]), Buffer.alloc(1024 * 1024 + 1)]);
  assert.equal((await post(env, { body: big })).status, 413);
  assert.equal(env.EVENTS.objects.size, 0);
});

test("listing needs the read key", async () => {
  const env = makeEnv();
  await post(env);
  assert.equal((await read(env, "/v1/list", "")).status, 401);
  assert.equal((await read(env, "/v1/list", "wrong")).status, 401);
  const resp = await read(env, "/v1/list");
  assert.equal(resp.status, 200);
  const body = await resp.json();
  assert.equal(body.keys.length, 1);
  assert.equal(body.cursor, null);
});

test("the machine token does not open the read endpoints", async () => {
  const env = makeEnv();
  assert.equal((await read(env, "/v1/list", TOKEN)).status, 401);
});

test("without READ_KEY configured reading is refused", async () => {
  const env = makeEnv();
  env.READ_KEY = undefined;
  assert.equal((await read(env, "/v1/list", "anything")).status, 401);
});

test("an object is read back and a foreign key is refused", async () => {
  const env = makeEnv();
  const payload = gzipSync("hello\n");
  await post(env, { body: payload });
  const key = [...env.EVENTS.objects.keys()][0];
  const resp = await read(env, `/v1/object?key=${encodeURIComponent(key)}`);
  assert.equal(resp.status, 200);
  assert.deepEqual(Buffer.from(await resp.arrayBuffer()), payload);
  assert.equal((await read(env, "/v1/object?key=other/x")).status, 400);
  assert.equal((await read(env, `/v1/object?key=${encodeURIComponent("v1/none")}`)).status, 404);
});

test("unknown paths and methods are 404", async () => {
  const env = makeEnv();
  assert.equal((await worker.fetch(new Request("https://log.example/"), env)).status, 404);
  assert.equal((await worker.fetch(new Request("https://log.example/v1/batch"), env)).status, 404);
});
