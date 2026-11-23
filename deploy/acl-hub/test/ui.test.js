// Страница Control Center: доступ, отсутствие внешних ресурсов, безопасная вставка данных.
import { test } from "node:test";
import assert from "node:assert/strict";
import { FakeD1 } from "./fake-d1.js";
import { LOGIN_HTML, PAGE_HTML } from "../src/ui.js";
import worker from "../src/index.js";

const MIGRATIONS = new URL("../migrations/", import.meta.url);
const OWNER = ["owner", "key", "y".repeat(20)].join("_");
const env = () => ({ DB: new FakeD1().applyMigrations(MIGRATIONS), OWNER_KEY: OWNER });

test("root serves page for owner", async () => {
  const res = await worker.fetch(new Request("https://hub/", { headers: { Authorization: `Bearer ${OWNER}` } }), env());
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type"), /text\/html/);
  const body = await res.text();
  for (const tab of ["Live", "Buyers", "Failures", "Machines"]) assert.ok(body.includes(`>${tab}<`), tab);
});

test("page has no external resources", () => {
  for (const page of [PAGE_HTML, LOGIN_HTML("x")]) {
    assert.doesNotMatch(page, /(src|href)\s*=\s*["']?https?:/i);
    assert.doesNotMatch(page, /@import|url\(\s*["']?https?:/i);
  }
});

test("ui escapes data", () => {
  assert.ok(!PAGE_HTML.includes("innerHTML"), "data must go through textContent");
  assert.ok(!PAGE_HTML.includes("insertAdjacentHTML"));
  assert.ok(!PAGE_HTML.includes("document.write"));
  assert.ok(LOGIN_HTML('<script>alert(1)</script>').includes("&lt;script&gt;"));
});

test("login page has form", () => {
  assert.match(LOGIN_HTML(), /<form method="post" action="\/login">/);
  assert.match(LOGIN_HTML(), /type="password" name="key"/);
});

test("page script parses", () => {
  const script = PAGE_HTML.split("<script>")[1].split("</script>")[0];
  assert.doesNotThrow(() => new Function(script));
});

test("page sends anti-framing and referrer headers", async () => {
  const res = await worker.fetch(new Request("https://hub/", { headers: { Authorization: `Bearer ${OWNER}` } }), env());
  assert.equal(res.headers.get("x-frame-options"), "DENY");
  assert.equal(res.headers.get("referrer-policy"), "no-referrer");
  assert.match(res.headers.get("content-security-policy"), /frame-ancestors 'none'/);
  const login = await worker.fetch(new Request("https://hub/login"), env());
  assert.equal(login.headers.get("x-frame-options"), "DENY");
});

test("page opens a run from the #run= link", () => {
  assert.match(PAGE_HTML, /location\.hash/);
  assert.match(PAGE_HTML, /#run=/);
});
