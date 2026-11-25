// ACL-2924: проверка поведения /static/user_assign.js в минимальном DOM-заменителе (без браузера).
// Запускается из tests/test_acl2924_assign_email_fixes.py: `node tests/ua_block_check.js <путь к user_assign.js>`.
// Печатает JSON { ok, checks: {...} }; любое нарушение — ok:false с описанием.
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync(process.argv[2], "utf8");
const handlers = {};            // событие → [fn]
const els = {};
const mk = (id, extra = {}) => (els[id] = { id, value: "", textContent: "", innerHTML: "", dataset: {}, className: "",
  focused: 0, focus() { this.focused += 1; }, closest(sel) { return sel.split(",").map((s) => s.trim()).some((s) => s === "#" + this.id) ? this : null; },
  getAttribute(n) { return this.attrs ? this.attrs[n] : null; }, ...extra });
mk("ua-msg"); mk("ua-list"); mk("ua-who"); mk("ua-results"); mk("ua-text", { value: "Beta 2" });
mk("ua-email"); mk("ua-check"); mk("ua-send"); mk("buyer-select", { value: "Dan Dicker" });

const document = {
  getElementById: (id) => els[id] || null,
  addEventListener: (ev, fn) => { (handlers[ev] = handlers[ev] || []).push(fn); },
};
let queue = [];                 // очередь заготовленных ответов fetch: [url-подстрока, ответ]
const calls = [];
const fetch = async (url, opts) => {
  calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  const i = queue.findIndex(([part]) => url.includes(part));
  if (i < 0) throw new Error("no stub for " + url);
  const [, answer] = queue.splice(i, 1)[0];
  return { status: 200, json: async () => answer };
};
vm.runInNewContext(src, { document, fetch, console, Promise, JSON, encodeURIComponent, String, Array, RegExp });

const fire = async (ev, event) => { for (const fn of handlers[ev] || []) await fn(event); };
const tick = () => new Promise((r) => setImmediate(r));
const flush = async () => { for (let i = 0; i < 8; i++) await tick(); };
const checks = {};
const bad = [];
const expect = (name, cond, detail) => { checks[name] = !!cond; if (!cond) bad.push(name + (detail ? ": " + detail : "")); };

const listing = (accounts, extra = {}) => ["/api/user-assign/accounts", { ok: true, user_email: "dan@adopy.test", accounts, ...extra }];

(async () => {
  // 1. Открыли блок: список загружен, почта байера показана в поле и не помечена как набранная вручную
  queue = [listing([{ account_name: "Alpha 1", removed: false }])];
  await fire("toggle", { target: { id: "ua-root", open: true } });
  await flush();
  expect("email shown in place", els["ua-email"].value === "dan@adopy.test" && els["ua-email"].dataset.dirty === "");
  expect("list rendered", els["ua-list"].innerHTML.includes("Alpha 1") && els["ua-list"].innerHTML.includes("data-ua-remove"));

  // 2. Assign: строка результата остаётся после перечитывания списка (ревью п.2)
  queue = [["/api/user-assign/send", { ok: true, assigned: ["Beta 2"], results: [], user_email: "dan@adopy.test" }],
           listing([{ account_name: "Alpha 1", removed: false }, { account_name: "Beta 2", removed: false }])];
  await fire("click", { target: els["ua-send"] });
  await flush();
  expect("assign result line stays", els["ua-msg"].textContent === "Assigned 1 to Dan Dicker.", els["ua-msg"].textContent);
  expect("assign did not send an untouched email", calls.find((c) => c.url.includes("/send")).body.user_email === "");
  expect("list reloaded after assign", els["ua-list"].innerHTML.includes("Beta 2"));

  // 3. Remove: то же самое
  const rmBtn = { attrs: { "data-ua-remove": "Beta 2" }, disabled: false, closest: (s) => (s.includes("[data-ua-remove]") ? rmBtn : null),
    getAttribute: (n) => rmBtn.attrs[n] };
  queue = [["/api/user-assign/remove", { ok: true, removed: ["Beta 2"] }],
           listing([{ account_name: "Alpha 1", removed: false }, { account_name: "Beta 2", removed: true }])];
  await fire("click", { target: rmBtn });
  await flush();
  expect("remove result line stays", els["ua-msg"].textContent === "Removed Beta 2 from Dan Dicker.", els["ua-msg"].textContent);
  expect("removed row struck through", els["ua-list"].innerHTML.includes("<s>Beta 2</s>"));

  // 4. Правка почты на месте: ввели новую — уходит на сервер, список перечитывается уже по ней (ревью п.3)
  els["ua-email"].value = "dan.new@adopy.test";
  await fire("input", { target: els["ua-email"] });
  queue = [["/api/user-assign/buyer-email", { ok: true, buyer: "Dan Dicker", user_email: "dan.new@adopy.test" }],
           listing([], { user_email: "dan.new@adopy.test", new_user: true })];
  await fire("change", { target: els["ua-email"] });
  await flush();
  const saved = calls.find((c) => c.url.includes("/buyer-email"));
  expect("new email posted", saved && saved.body.buyer === "Dan Dicker" && saved.body.user_email === "dan.new@adopy.test");
  expect("email line shows the saved one", els["ua-email"].value === "dan.new@adopy.test" && els["ua-email"].dataset.dirty === "");
  expect("new user hint shown", els["ua-list"].innerHTML.includes("created on the first Assign"));

  // 5. Некорректная почта не уходит на сервер
  const before = calls.length;
  els["ua-email"].value = "not an email";
  await fire("input", { target: els["ua-email"] });
  await fire("change", { target: els["ua-email"] });
  await flush();
  expect("invalid email is not posted", calls.length === before && els["ua-msg"].textContent.includes("valid email"));

  // 6. Два юзера с одним именем: поле очищено и в фокусе, подсказка видна; набранная почта идёт в Assign (ревью п.1)
  els["ua-email"].focused = 0;
  queue = [["/api/user-assign/accounts", { ok: false, need_email: true, error: "Several hub users are named Dan Dicker. Type the right email below.", accounts: [] }]];
  await fire("toggle", { target: { id: "ua-root", open: true } });
  await flush();
  expect("same-name: email field emptied and focused", els["ua-email"].value === "" && els["ua-email"].focused > 0);
  expect("same-name: message shown", els["ua-msg"].textContent.includes("Several hub users"));
  els["ua-email"].value = "dan.right@adopy.test";
  await fire("input", { target: els["ua-email"] });
  queue = [["/api/user-assign/send", { ok: true, assigned: ["Beta 2"], results: [], user_email: "dan.right@adopy.test" }],
           listing([{ account_name: "Beta 2", removed: false }], { user_email: "dan.right@adopy.test" })];
  await fire("click", { target: els["ua-send"] });
  await flush();
  const sent = calls.filter((c) => c.url.includes("/send")).pop();
  expect("typed email is used for Assign", sent.body.user_email === "dan.right@adopy.test");

  console.log(JSON.stringify({ ok: bad.length === 0, bad, checks }));
})().catch((e) => { console.log(JSON.stringify({ ok: false, bad: ["exception: " + e.message], checks })); });
