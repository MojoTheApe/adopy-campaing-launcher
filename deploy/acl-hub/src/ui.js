// Страницы владельца. Без внешних ресурсов: всё отдаёт Worker. Цвета — токены Adopy из дешборда ACL.
// Данные вставляются только через textContent (функция el), без innerHTML.
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const TOKENS = `
:root{--bg:#f4f5f7;--surface:#fff;--subtle:#f0f2f6;--border:#dfe3ea;--text:#202a39;--muted:#606c80;--primary:#2563eb;
--sel:#edf3ff;--ok:#216e4e;--ok-bg:#edf7f1;--bad:#b42318;--bad-bg:#fff0ed;--warn:#855812;--warn-bg:#fcf5e9;--r:8px;
--font:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#111720;--surface:#19222e;--subtle:#1d2836;--border:#354459;--text:#edf2fa;
--muted:#a4b3c8;--primary:#83aaff;--sel:#223957;--ok:#8ad1ad;--ok-bg:#1e3c32;--bad:#ffb4aa;--bad-bg:#462a2e;--warn:#e5bd78;
--warn-bg:#3c3223;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--font)}`;

export function LOGIN_HTML(error = "") {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ACL Control Center — sign in</title><style>${TOKENS}
body{display:grid;place-items:center;min-height:100vh}
form{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:28px;width:min(360px,92vw)}
h1{font-size:18px;margin:0 0 16px}input{width:100%;padding:10px;border:1px solid var(--border);border-radius:var(--r);font:inherit;background:var(--surface);color:var(--text)}
button{margin-top:12px;width:100%;padding:10px;border:0;border-radius:var(--r);background:var(--primary);color:#fff;font:600 15px var(--font);cursor:pointer}
.err{color:var(--bad);margin:0 0 10px}</style></head><body>
<form method="post" action="/login"><h1>ACL Control Center</h1>${error ? `<p class="err">${esc(error)}</p>` : ""}
<input type="password" name="key" placeholder="Owner key" autocomplete="current-password" autofocus required>
<button type="submit">Sign in</button></form></body></html>`;
}

export const PAGE_HTML = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ACL Control Center</title><style>${TOKENS}
header{position:sticky;top:0;z-index:5;background:var(--surface);border-bottom:1px solid var(--border);display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;padding:10px 16px}
h1{font-size:16px;margin:0 8px 0 0}nav{display:flex;gap:4px;flex-wrap:wrap}
nav button,.btn{border:1px solid var(--border);background:var(--surface);color:var(--text);border-radius:var(--r);padding:6px 12px;font:inherit;cursor:pointer}
nav button[aria-selected=true]{background:var(--sel);border-color:var(--primary);color:var(--primary);font-weight:600}
.spacer{flex:1}input[type=date],select{border:1px solid var(--border);border-radius:var(--r);padding:5px 8px;font:inherit;background:var(--surface);color:var(--text)}
main{padding:16px;max-width:1280px;margin:0 auto}section[hidden]{display:none}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px;color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow-x:auto}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;background:var(--subtle);white-space:nowrap}tr:last-child td{border-bottom:0}
tbody tr.click{cursor:pointer}tbody tr.click:hover{background:var(--subtle)}.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;border-radius:999px;padding:1px 8px;font-size:12px;font-weight:600;white-space:nowrap}
.ok{background:var(--ok-bg);color:var(--ok)}.bad{background:var(--bad-bg);color:var(--bad)}.warn{background:var(--warn-bg);color:var(--warn)}.mut{background:var(--subtle);color:var(--muted)}
.feed{list-style:none;margin:0;padding:0}.feed li{display:grid;grid-template-columns:64px 18px minmax(0,1fr);gap:8px;padding:9px 12px;border-bottom:1px solid var(--border)}
.feed li:last-child{border-bottom:0}.feed li.click{cursor:pointer}.feed li.click:hover{background:var(--subtle)}
.dot{width:10px;height:10px;border-radius:50%;margin-top:5px;background:var(--muted)}.dot.error{background:var(--bad)}.dot.warn{background:var(--warn)}.dot.info{background:var(--ok)}
.t{color:var(--muted);font-variant-numeric:tabular-nums}.who{color:var(--muted);font-size:12px}.txt{overflow-wrap:anywhere}
.empty{padding:24px;text-align:center;color:var(--muted)}.err-line{color:var(--bad);overflow-wrap:anywhere}
dialog{border:1px solid var(--border);border-radius:14px;padding:0;width:min(880px,94vw);max-height:88vh;background:var(--surface);color:var(--text)}
dialog::backdrop{background:rgba(16,24,40,.45)}.dhead{display:flex;gap:10px;align-items:flex-start;padding:16px;border-bottom:1px solid var(--border)}
.dhead h2{font-size:16px;margin:0;overflow-wrap:anywhere}.dbody{padding:16px;overflow:auto;max-height:calc(88vh - 70px)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px 16px;margin:12px 0}.grid div span{display:block;font-size:12px;color:var(--muted)}
.steps{list-style:none;margin:8px 0 0;padding:0;font-size:13px}.steps li{display:grid;grid-template-columns:64px minmax(0,1fr);gap:8px;padding:4px 0;border-bottom:1px dashed var(--border)}
.steps li.fail{color:var(--bad);font-weight:600}.note{background:var(--subtle);border-radius:var(--r);padding:10px 12px;margin-top:12px}
@media (max-width:640px){th.hide-sm,td.hide-sm{display:none}main{padding:10px}}
</style></head><body>
<header><h1>ACL Control Center</h1>
<nav role="tablist"><button role="tab" data-tab="live" aria-selected="true">Live</button><button role="tab" data-tab="buyers" aria-selected="false">Buyers</button><button role="tab" data-tab="failures" aria-selected="false">Failures</button><button role="tab" data-tab="machines" aria-selected="false">Machines</button></nav>
<span class="spacer"></span><input type="date" id="day" aria-label="Day">
<form method="post" action="/logout"><button class="btn" type="submit">Log out</button></form></header>
<main>
<section id="tab-live"><div class="bar"><label>Buyer <select id="live-buyer"><option value="">All</option></select></label>
<label><input type="checkbox" id="live-problems"> Problems only</label><span id="live-upd"></span></div>
<div class="card"><ul class="feed" id="live-list"></ul></div></section>
<section id="tab-buyers" hidden><div class="bar" id="buyers-title"></div><div class="card"><table><thead><tr>
<th>Buyer</th><th class="num">Active</th><th class="num">Jobs</th><th class="num">Campaigns</th><th class="num">OK</th><th class="num">Failed</th><th class="num">Not started</th><th class="num hide-sm">Median</th><th class="hide-sm">Version</th>
</tr></thead><tbody id="buyers-body"></tbody></table></div></section>
<section id="tab-failures" hidden><div class="bar">Failures grouped by the same error, last 7 days</div><div class="card"><table><thead><tr>
<th class="num">Times</th><th class="num">Machines</th><th>Error</th><th class="hide-sm">Buyers</th><th class="hide-sm">Last</th></tr></thead><tbody id="failures-body"></tbody></table></div></section>
<section id="tab-machines" hidden><div class="card"><table><thead><tr>
<th>Machine</th><th>Buyer</th><th>Version</th><th>Last signal</th><th class="num hide-sm">Outbox</th><th class="hide-sm">Passport</th></tr></thead><tbody id="machines-body"></tbody></table></div></section>
</main>
<dialog id="run"><div class="dhead"><div style="flex:1"><h2 id="run-title"></h2><div class="who" id="run-sub"></div></div><span id="run-pill"></span><button class="btn" id="run-close" type="button">Close</button></div><div class="dbody" id="run-body"></div></dialog>
<script>
(function () {
  "use strict";
  // Пояс — на выбранный день (переход на летнее время меняет смещение).
  function tzFor(day) { return -new Date(day + "T12:00:00").getTimezoneOffset(); }
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, attrs) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "class") n.className = attrs[k]; else if (k === "on") n.addEventListener("click", attrs[k]); else n.setAttribute(k, attrs[k]);
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c === null || c === undefined || c === false) continue;
      n.appendChild(typeof c === "object" ? c : document.createTextNode(String(c)));
    }
    return n;
  }
  function api(path) {
    return fetch(path, { credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (r.status === 401) { location.href = "/login"; throw new Error("signed out"); }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function hhmm(ts) { if (!ts) return "—"; var d = new Date(ts); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
  function stamp(ts) { var d = new Date(ts); return d.toDateString() === new Date().toDateString() ? hhmm(ts) : d.toLocaleDateString(undefined, { day: "2-digit", month: "short" }) + " " + hhmm(ts); }
  function when(ts) { if (!ts) return "—"; var d = new Date(ts); return d.toLocaleDateString(undefined, { day: "2-digit", month: "short" }) + " " + hhmm(ts); }
  function dur(s) { if (s === null || s === undefined) return "—"; s = Math.round(s); return s < 90 ? s + " s" : Math.round(s / 60) + " min"; }
  function today() { var d = new Date(); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  // ok + issues = «опубликовано с проблемами»: жёлтая, как в ACL.
  function outcomePill(o, issues) {
    if (o === "ok" && issues > 0) return el("span", { class: "pill warn" }, "ok · with problems");
    return el("span", { class: "pill " + (o === "ok" ? "ok" : o === "failed" ? "bad" : o === "not_started" ? "warn" : "mut") }, o === "not_started" ? "not started" : (o || "—"));
  }
  function empty(body, cols, text) { body.replaceChildren(el("tr", null, el("td", { class: "empty", colspan: String(cols) }, text))); }

  var tab = "live", liveTimer = null, liveData = [], known = {}, runToken = 0, dayAuto = today();
  $("day").value = dayAuto;
  document.querySelectorAll("nav button").forEach(function (b) { b.addEventListener("click", function () { show(b.dataset.tab); }); });
  function show(name) {
    tab = name;
    document.querySelectorAll("nav button").forEach(function (b) { b.setAttribute("aria-selected", String(b.dataset.tab === name)); });
    ["live", "buyers", "failures", "machines"].forEach(function (t) { $("tab-" + t).hidden = t !== name; });
    load();
  }
  function load() {
    // Вкладка открыта с вечера: день по умолчанию переходит на сегодня, если его не меняли руками.
    if ($("day").value === dayAuto && dayAuto !== today()) { dayAuto = today(); $("day").value = dayAuto; }
    if (tab === "live") loadLive(); else if (tab === "buyers") loadBuyers(); else if (tab === "failures") loadFailures(); else loadMachines();
  }
  $("day").addEventListener("change", function () { if (tab === "buyers") loadBuyers(); });
  $("live-buyer").addEventListener("change", loadLive);
  $("live-problems").addEventListener("change", renderLive);

  // ── Live ──
  function setBuyers(names) {
    // Список байеров только растёт и не пересобирается, пока он открыт.
    var sel = $("live-buyer"), added = false;
    names.forEach(function (m) { if (m && !known[m]) { known[m] = 1; added = true; } });
    if (!added || document.activeElement === sel) return;
    var cur = sel.value;
    sel.replaceChildren(el("option", { value: "" }, "All"));
    Object.keys(known).sort().forEach(function (m) { sel.appendChild(el("option", { value: m }, m)); });
    sel.value = cur;
  }
  function loadLive() {
    var who = $("live-buyer").value;
    api("/api/live?limit=200" + (who ? "&manager=" + encodeURIComponent(who) : "")).then(function (d) {
      liveData = d.events;
      setBuyers(liveData.map(function (e) { return e.manager; }));
      $("live-upd").textContent = "Updated " + hhmm(new Date().toISOString()) + " · last 48 h · refreshes every 15 s";
      renderLive();
    }).catch(function (e) { $("live-upd").textContent = "Could not load: " + e.message; });
  }
  function renderLive() {
    var problems = $("live-problems").checked, list = $("live-list");
    var rows = liveData.filter(function (e) { return !problems || e.level !== "info"; });
    if (!rows.length) { list.replaceChildren(el("li", { class: "empty" }, "No events in the last 48 hours")); return; }
    list.replaceChildren.apply(list, rows.map(function (e) {
      var meta = [e.manager, e.account, e.campaign].filter(Boolean).join(" · ");
      var li = el("li", e.run_id ? { class: "click", on: function () { openRun(e.run_id); } } : null,
        el("span", { class: "t", title: e.ts }, stamp(e.ts)), el("span", { class: "dot " + e.level }),
        el("div", null, el("div", { class: "txt" }, e.text), el("div", { class: "who" }, meta)));
      return li;
    }));
  }
  function schedule() {
    clearTimeout(liveTimer);
    liveTimer = setTimeout(function () { if (tab === "live" && !document.hidden) loadLive(); schedule(); }, 15000);
  }

  // ── Buyers ──
  function loadBuyers() {
    var day = $("day").value || today();
    $("buyers-title").textContent = "Buyers on " + day;
    api("/api/summary?day=" + encodeURIComponent(day) + "&tz=" + tzFor(day)).then(function (d) {
      var body = $("buyers-body");
      setBuyers(d.buyers.map(function (b) { return b.manager; }));
      if (!d.buyers.length) return empty(body, 9, "No activity on this day");
      body.replaceChildren.apply(body, d.buyers.map(function (b) {
        var active = b.first_at ? hhmm(b.first_at) + "–" + hhmm(b.last_at) + " (" + b.active_h + " h)" : "—";
        return el("tr", { class: "click", on: function () { setBuyers([b.manager]); $("live-buyer").value = b.manager; show("live"); } },
          el("td", null, el("strong", null, b.manager), el("div", { class: "who" }, b.machines.join(", "))),
          el("td", { class: "num" }, active), el("td", { class: "num" }, b.jobs), el("td", { class: "num" }, b.campaigns),
          el("td", { class: "num" }, b.ok ? el("span", { class: "pill ok" }, b.ok) : "0",
            b.issues ? el("div", null, el("span", { class: "pill warn", title: "published with problems" }, b.issues + " with problems")) : null),
          el("td", { class: "num" }, b.failed ? el("span", { class: "pill bad" }, b.failed) : "0"),
          el("td", { class: "num" }, b.not_started ? el("span", { class: "pill warn" }, b.not_started) : "0"),
          el("td", { class: "num hide-sm" }, dur(b.median_s)), el("td", { class: "hide-sm who" }, b.version || "—"));
      }));
    }).catch(function (e) { empty($("buyers-body"), 9, "Could not load: " + e.message); });
  }

  // ── Failures ──
  function loadFailures() {
    api("/api/failures?days=7").then(function (d) {
      var body = $("failures-body");
      if (!d.failures.length) return empty(body, 5, "No failures in the last 7 days");
      body.replaceChildren.apply(body, d.failures.map(function (f) {
        return el("tr", { class: "click", on: function () { openRun(f.last_run_id); } },
          el("td", { class: "num" }, el("span", { class: "pill bad" }, f.n)), el("td", { class: "num" }, f.machines),
          el("td", null, el("div", { class: "err-line" }, f.sample_error || "—"), el("div", { class: "who" }, (f.stop_kind || "") + " · " + f.fingerprint)),
          el("td", { class: "hide-sm" }, f.managers.join(", ")), el("td", { class: "hide-sm t" }, when(f.last_at)));
      }));
    }).catch(function (e) { empty($("failures-body"), 5, "Could not load: " + e.message); });
  }

  // ── Machines ──
  function loadMachines() {
    api("/api/machines").then(function (d) {
      var body = $("machines-body");
      if (!d.machines.length) return empty(body, 6, "No machines yet");
      var now = Date.now();
      body.replaceChildren.apply(body, d.machines.map(function (m) {
        var last = m.last_heartbeat_at || m.last_event_at, t = last ? Date.parse(last) : 0;
        // Молчит: последний сигнал старше 30 мин, а работала машина в последние сутки.
        var silent = now - t > 30 * 60000 && now - t < 24 * 3600000;
        var p = m.profile, passport = p ? [p.ram_gb && p.ram_gb + " GB", p.cores_logical && p.cores_logical + " cores", p.windows, p.screen_w && (p.screen_w + "×" + p.screen_h)].filter(Boolean).join(" · ") : "—";
        return el("tr", null, el("td", null, m.machine || m.machine_id), el("td", null, m.manager || "—"),
          el("td", { class: "who" }, m.flow_version || "—"),
          el("td", null, el("span", { class: "pill " + (silent ? "bad" : "mut") }, when(last))),
          el("td", { class: "num hide-sm" }, m.outbox_bytes ? Math.round(m.outbox_bytes / 1024) + " KB" : "0"),
          el("td", { class: "hide-sm who" }, passport));
      }));
    }).catch(function (e) { empty($("machines-body"), 6, "Could not load: " + e.message); });
  }

  // ── Run ──
  var dlg = $("run");
  $("run-close").addEventListener("click", function () { dlg.close(); });
  function openRun(id) {
    if (!id) return;
    var token = ++runToken;
    $("run-title").textContent = id.split("|").slice(1).join("|");
    $("run-sub").textContent = "Loading…"; $("run-pill").replaceChildren(); $("run-body").replaceChildren();
    if (!dlg.open) dlg.showModal();
    api("/api/run?id=" + encodeURIComponent(id)).then(function (d) {
      if (token !== runToken) return;   // открыли другой прогон, пока грузился этот
      var r = d.run, j = d.job;
      $("run-sub").textContent = [r.manager, r.account, when(r.finished_at)].filter(Boolean).join(" · ");
      $("run-pill").replaceChildren(outcomePill(r.outcome, r.issues));
      var parts = [];
      if (r.error) parts.push(el("div", { class: "note err-line" }, r.error));
      function f(label, v) { return el("div", null, el("span", null, label), v === null || v === undefined || v === "" ? "—" : String(v)); }
      parts.push(el("div", { class: "grid" }, f("Build time", dur(r.duration_s)), f("Meta calls", r.calls), f("Writes", r.writes),
        f("Stop", r.stop_kind), f("Campaign id", r.campaign_id), f("Created / reused", (r.created_any ? "created" : "—") + " / " + (r.reused_any ? "reused" : "—")),
        f("Ads", r.n_ads), f("Country", r.country), f("Version", r.flow_version),
        f("Job", j ? (j.n_ok + " ok · " + j.n_failed + " failed · " + j.n_not_started + " not started of " + (j.n_planned || "?")) : null),
        f("Machine load", r.load ? ("CPU " + r.load.cpu_pct + "% · RAM " + r.load.mem_pct + "% · " + r.load.chrome_procs + " Chrome") : null)));
      if (d.fingerprint) parts.push(el("div", { class: "note" }, "Same failure: " + d.fingerprint.n + " time(s) on " + d.fingerprint.machines + " machine(s) — " + d.fingerprint.managers.join(", ") + "; first " + when(d.fingerprint.first_at) + ", last " + when(d.fingerprint.last_at) + "."));
      parts.push(el("h3", { style: "font-size:14px;margin:16px 0 0" }, "Steps"));
      parts.push(d.steps.length ? el.apply(null, ["ul", { class: "steps" }].concat(d.steps.map(function (s) {
        return el("li", s.ok ? null : { class: "fail" }, el("span", { class: "t" }, hhmm(s.ts)), el("span", null, s.note || s.step));
      }))) : el("div", { class: "empty" }, "No step details from this machine"));
      $("run-body").replaceChildren.apply($("run-body"), parts);
    }).catch(function (e) { $("run-sub").textContent = "Could not load: " + e.message; });
  }

  document.addEventListener("visibilitychange", function () { if (!document.hidden && tab === "live") loadLive(); });
  // Ссылка из Telegram: /#run=<id> сразу открывает карточку прогона.
  function fromHash() {
    var m = /^#run=(.+)$/.exec(location.hash);
    if (m) { try { openRun(decodeURIComponent(m[1])); } catch (_) { /* битая ссылка */ } }
  }
  window.addEventListener("hashchange", fromHash);
  dlg.addEventListener("close", function () { if (/^#run=/.test(location.hash)) history.replaceState(null, "", location.pathname); });
  load(); schedule(); fromHash();
})();
</script></body></html>`;
