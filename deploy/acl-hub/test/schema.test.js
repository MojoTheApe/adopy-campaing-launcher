// Схема D1, поддельная D1 и роутер. Запуск: cd deploy/acl-hub && node --test
import { test } from "node:test";
import assert from "node:assert/strict";
import { FakeD1 } from "./fake-d1.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);

test("migration creates all tables", async () => {
  const db = new FakeD1(); db.applyMigrations(MIGRATIONS);
  const { results } = await db.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all();
  const names = results.map((r) => r.name).filter((n) => !n.startsWith("sqlite_"));
  assert.deepEqual(names, ["events", "jobs", "machines", "runs", "tokens"]);
});

test("batch rolls back on error", async () => {
  const db = new FakeD1(); db.applyMigrations(MIGRATIONS);
  const ins = (id) => db.prepare("INSERT INTO events (event_id, type, ts, received_at, payload) VALUES (?, 't', 'x', 'x', '{}')").bind(id);
  await assert.rejects(db.batch([ins("a"), ins("a")]));
  const row = await db.prepare("SELECT count(*) AS n FROM events").first();
  assert.equal(row.n, 0);
});

test("run reports changes and first returns null when empty", async () => {
  const db = new FakeD1(); db.applyMigrations(MIGRATIONS);
  const r = await db.prepare("INSERT OR IGNORE INTO tokens VALUES (?, ?)").bind("s", 1).run();
  assert.equal(r.meta.changes, 1);
  const again = await db.prepare("INSERT OR IGNORE INTO tokens VALUES (?, ?)").bind("s", 1).run();
  assert.equal(again.meta.changes, 0);
  assert.equal(await db.prepare("SELECT * FROM jobs").first(), null);
});

test("unknown path is 404", async () => {
  const db = new FakeD1(); db.applyMigrations(MIGRATIONS);
  const res = await worker.fetch(new Request("https://h/nope"), { DB: db });
  assert.equal(res.status, 404);
  assert.deepEqual(await res.json(), { error: "not found" });
});
