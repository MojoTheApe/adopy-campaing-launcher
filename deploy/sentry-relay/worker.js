// ACL Sentry relay (ACL-1330): Sentry issue alert -> one cloud Claude Code routine run.
//
// Why a relay at all: Sentry's alert webhooks (Internal Integration "Alert Rule
// Action" and the legacy WebHooks plugin) cannot add an Authorization header,
// and the routine's /fire endpoint needs `Authorization: Bearer <routine token>`.
// This Worker sits between the two:
//
//   1. checks Sentry's signature (Sentry-Hook-Signature = HMAC-SHA256 of the raw
//      body with the Internal Integration's client secret) and the event time
//      inside the signed body (Sentry does not sign the Sentry-Hook-Timestamp
//      header, so only the body time stops a replay; no time = refused);
//   2. accepts only `event_alert` for our alert rule, our project and our
//      failure kinds (blocking / campaign_stop / job_exception);
//   3. fires the routine at most once per Sentry issue within DEDUPE_HOURS, at
//      most MAX_FIRES_PER_HOUR times per hour (below the routine's 30/hour and the
//      account's 100/hour limits) and at most MAX_FIRES_PER_DAY times a day;
//   4. sends only a short, scrubbed text: ids, e-mails, tokens, URLs and long
//      digit runs never leave Sentry through this path.
//
// Sentry expects an answer within 1 second, so the /fire call runs after the
// response (ctx.waitUntil).
//
// Configuration (Cloudflare -> Worker -> Settings -> Variables and Secrets):
//   ROUTINE_FIRE_URL      text    https://api.anthropic.com/v1/claude_code/routines/<id>/fire
//   ROUTINE_TOKEN         secret  the routine's API trigger token
//   SENTRY_CLIENT_SECRET  secret  the Internal Integration's client secret
//   SENTRY_PROJECT        text    project slug to accept (ACL: python); empty = any
//   ALERT_RULE            text    rule name to accept (default "ACL - new failure kind")
//   FAILURE_KINDS         text    default "blocking,campaign_stop,job_exception"
//   DEDUPE_HOURS          text    default 24
//   MAX_FIRES_PER_HOUR    text    default 20
//   MAX_FIRES_PER_DAY     text    default 30
// Binding: KV namespace as FIRED (dedupe + hourly counter). Without it nothing fires.
//
// Tests: `node --test deploy/sentry-relay/` (Node 20+, no packages).

const FIRE_PREFIX = "https://api.anthropic.com/v1/claude_code/routines/";
const BETA = "experimental-cc-routine-2026-04-01";
const DEFAULT_RULE = "ACL - new failure kind";
const DEFAULT_KINDS = "blocking,campaign_stop,job_exception";
const MAX_SKEW_SECONDS = 15 * 60;
// Возраст события по времени из подписанного тела. Окно не длиннее
// минимального DEDUPE_HOURS (1 ч): повтор старого тела внутри окна упирается в
// метку дедупа, а после окна отвергается как старый.
const MAX_EVENT_AGE_SECONDS = 60 * 60;
const MAX_BODY_BYTES = 512 * 1024;
const TAGS = ["failure_kind", "step", "reason", "release", "server_name", "machine", "account", "environment"];
// Версии («v96+8@22aea04», «acl-release-2026-09-29b») и имена машин — не
// телефоны; для них правило длинных номеров выключено, остальное действует.
const KEEP_NUMBERS = new Set(["release", "server_name", "machine", "environment"]);

function reply(status, text) {
  return new Response(text + "\n", { status, headers: { "content-type": "text/plain; charset=utf-8" } });
}

function intSetting(value, fallback, min, max) {
  const n = Number.parseInt(String(value ?? ""), 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function hex(buffer) {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function sameText(a, b) {
  // Сравнение за постоянное время: подпись не угадывается по времени ответа.
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function signatureOk(secret, rawBody, given) {
  if (!secret || !given) return false;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(rawBody));
  return sameText(hex(mac), String(given).trim().toLowerCase());
}

function timestampOk(value, nowMs) {
  // Заголовок Sentry не подписывает — это лишь дополнительная проверка; без
  // него принимаем, старый — нет. Настоящая защита от повтора — eventTimeOk.
  if (value === null || value === undefined || String(value).trim() === "") return true;
  let ts = Number(String(value).trim());
  if (!Number.isFinite(ts)) return false;
  if (ts > 1e12) ts /= 1000;
  return Math.abs(nowMs / 1000 - ts) <= MAX_SKEW_SECONDS;
}

function epochSeconds(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number" || /^\s*\d+(\.\d+)?\s*$/.test(String(value))) {
    let ts = Number(value);
    if (!Number.isFinite(ts) || ts <= 0) return null;
    if (ts > 1e12) ts /= 1000;
    return ts;
  }
  const ms = Date.parse(String(value));
  return Number.isFinite(ms) ? ms / 1000 : null;
}

// Время события из подписанного тела: received (время приёма Sentry, часы
// сервера), затем datetime / timestamp (часы машины байера). Нет времени —
// отказ: тело без времени нельзя отличить от повтора.
function eventTimeOk(payload, nowMs) {
  const event = (payload && payload.data && payload.data.event) || {};
  let ts = null;
  for (const name of ["received", "datetime", "timestamp"]) {
    ts = epochSeconds(event[name]);
    if (ts !== null) break;
  }
  if (ts === null) return false;
  const age = nowMs / 1000 - ts;
  return age <= MAX_EVENT_AGE_SECONDS && age >= -MAX_SKEW_SECONDS;
}

// Вычистка — тот же принцип, что run_telemetry.scrub: наружу не уходят
// кабинеты, контакты, токены, адреса и длинные номера.
function scrub(value, limit = 200, keepNumbers = false) {
  let text = String(value ?? "");
  text = text.replace(/[\u0000-\u001f\u007f]+/g, " ");
  text = text.replace(/-----BEGIN[^-]*-----[\s\S]*?(-----END[^-]*-----|$)/g, "[key]");
  text = text.replace(/\bBearer\s+\S+/gi, "Bearer [token]");
  text = text.replace(/\b(?:sk-[A-Za-z0-9_-]{8,}|sntr[a-z]{1,3}_[A-Za-z0-9_=-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|xox[abpr]-[A-Za-z0-9-]{8,}|EAA[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){0,2})/g, "[token]");
  text = text.replace(/\b[a-z][a-z0-9+.-]*:\/\/\S+/gi, "[url]");
  // Адреса без схемы (business.facebook.com/adsmanager?...) — тоже [url].
  text = text.replace(/\b(?:[a-z0-9-]+\.)+[a-z]{2,}\/\S*/gi, "[url]");
  text = text.replace(/\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b/g, "[email]");
  text = text.replace(/\bact[_=]\s*\d+/gi, "act=[hidden]");
  text = text.replace(/\b([a-z_]*_id|id|token|key|secret|password)\s*[=:]\s*[^\s&,;]+/gi, "$1=[hidden]");
  text = text.replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, "[uuid]");
  text = text.replace(/\b[0-9a-f]{32,}\b/gi, "[hex]");
  if (!keepNumbers) {
    text = text.replace(/\+?\d[\d\s().-]{8,}\d/g, "[number]");
    text = text.replace(/\d{6,}/g, "[number]");
  }
  // Угловые скобки убираем: текст приходит в routine внутри
  // <routine-fire-payload>, и закрыть этот блок изнутри нельзя.
  text = text.replace(/[<>]/g, (c) => (c === "<" ? "(" : ")"));
  text = text.replace(/\s+/g, " ").trim();
  return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
}

function tagMap(tags) {
  const out = {};
  if (Array.isArray(tags)) {
    for (const item of tags) {
      if (Array.isArray(item) && item.length >= 2) out[String(item[0])] = item[1];
      else if (item && typeof item === "object" && "key" in item) out[String(item.key)] = item.value;
    }
  } else if (tags && typeof tags === "object") {
    Object.assign(out, tags);
  }
  return out;
}

function projectSlug(event) {
  const m = /\/projects\/[^/]+\/([^/]+)\/events\//.exec(String(event.url || ""));
  if (m) return m[1];
  if (event.project && typeof event.project === "object" && event.project.slug) return String(event.project.slug);
  return "";
}

function issueLink(event, issueId) {
  try {
    const u = new URL(String(event.web_url || ""));
    if (u.protocol !== "https:" || !(u.hostname === "sentry.io" || u.hostname.endsWith(".sentry.io"))) return "";
    const m = /^(.*\/issues\/)(\d+)\//.exec(u.pathname + "/");
    if (!m || m[2] !== issueId) return "";
    return `${u.origin}${m[1]}${issueId}/`;
  } catch {
    return "";
  }
}

// Разбор тела алерта. Возвращает {skip: "<почему>"} или {issueId, text}.
function buildFire(payload, env, resource) {
  if (resource !== "event_alert") return { skip: "resource " + scrub(resource || "none", 40) };
  const data = (payload && payload.data) || {};
  const event = data.event || {};
  const rule = String(data.triggered_rule || (data.issue_alert && data.issue_alert.title) || "");
  const wantRule = (env.ALERT_RULE || DEFAULT_RULE).trim();
  if (rule.trim() !== wantRule) return { skip: "other rule" };
  const wantProject = String(env.SENTRY_PROJECT || "").trim();
  if (wantProject && projectSlug(event) !== wantProject) return { skip: "other project" };
  const issueId = String(event.issue_id ?? "").trim();
  if (!/^\d{1,20}$/.test(issueId)) return { skip: "no issue id" };
  const tags = tagMap(event.tags);
  const kinds = String(env.FAILURE_KINDS || DEFAULT_KINDS).split(",").map((s) => s.trim()).filter(Boolean);
  const kind = String(tags.failure_kind ?? "").trim();
  if (!kinds.includes(kind)) return { skip: "failure_kind not watched" };
  if (tags.release === undefined && event.release) tags.release = event.release;
  if (tags.environment === undefined && event.environment) tags.environment = event.environment;

  const lines = [
    "ACL Sentry alert: a new failure kind or a regression (ACL-1330 relay).",
    `sentry_issue_id: ${issueId}`,
    `title: ${scrub(event.title)}`,
    `culprit: ${scrub(event.culprit)}`,
  ];
  for (const name of TAGS) {
    if (tags[name] !== undefined && tags[name] !== null && String(tags[name]).trim() !== "") {
      lines.push(`${name}: ${scrub(tags[name], 120, KEEP_NUMBERS.has(name))}`);
    }
  }
  lines.push(`rule: ${scrub(rule, 80)}`);
  const link = issueLink(event, issueId);
  if (link) lines.push(`link: ${link}`);
  return { issueId, text: lines.join("\n").slice(0, 2000) };
}

async function fireRoutine(env, fire, kv, dedupeKey, dedupeTtl, fetchImpl) {
  let res;
  try {
    res = await fetchImpl(env.ROUTINE_FIRE_URL, {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.ROUTINE_TOKEN}`,
        "anthropic-beta": BETA,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
      },
      body: JSON.stringify({ text: fire.text }),
    });
  } catch (err) {
    await kv.delete(dedupeKey);
    console.log(`sentry-relay: fire failed for issue ${fire.issueId}: network`);
    return;
  }
  if (!res.ok) {
    // Ключ снимаем: следующий алерт по этой проблеме сможет запустить routine.
    await kv.delete(dedupeKey);
    console.log(`sentry-relay: fire failed for issue ${fire.issueId}: HTTP ${res.status}`);
    return;
  }
  let session = "";
  try {
    const body = await res.json();
    session = String(body.claude_code_session_url || "");
  } catch {
    session = "";
  }
  await kv.put(dedupeKey, JSON.stringify({ state: "fired", session, at: new Date().toISOString() }), { expirationTtl: dedupeTtl });
  console.log(`sentry-relay: fired for issue ${fire.issueId}: ${session || "session url not returned"}`);
}

async function handle(request, env, ctx, { now = () => Date.now(), fetchImpl = (...a) => fetch(...a) } = {}) {
  if (request.method === "GET") return reply(200, "sentry-relay ok");
  if (request.method !== "POST") return reply(405, "method not allowed");

  const declared = Number(request.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) return reply(413, "too large");
  // Предел — в байтах, а не в символах строки.
  const bytes = new Uint8Array(await request.arrayBuffer());
  if (bytes.length > MAX_BODY_BYTES) return reply(413, "too large");
  const raw = new TextDecoder().decode(bytes);
  const ok = await signatureOk(env.SENTRY_CLIENT_SECRET, raw, request.headers.get("sentry-hook-signature"));
  if (!ok) return reply(401, "bad signature");
  if (!timestampOk(request.headers.get("sentry-hook-timestamp"), now())) return reply(401, "stale request");

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    return reply(400, "bad json");
  }
  if (!eventTimeOk(payload, now())) return reply(401, "stale or undated event");
  const fire = buildFire(payload, env, request.headers.get("sentry-hook-resource"));
  if (fire.skip) return reply(200, "ignored: " + fire.skip);

  const kv = env.FIRED;
  if (!kv || typeof kv.get !== "function") return reply(500, "dedupe store FIRED is not bound; nothing fired");
  const url = String(env.ROUTINE_FIRE_URL || "");
  if (!url.startsWith(FIRE_PREFIX) || !url.endsWith("/fire") || !env.ROUTINE_TOKEN) {
    return reply(500, "routine is not configured; nothing fired");
  }

  const dedupeKey = `issue:${fire.issueId}`;
  if (await kv.get(dedupeKey)) return reply(200, "duplicate: already fired for this issue");

  const hourKey = `hour:${new Date(now()).toISOString().slice(0, 13)}`;
  const cap = intSetting(env.MAX_FIRES_PER_HOUR, 20, 1, 100);
  const used = intSetting(await kv.get(hourKey), 0, 0, 1e9);
  if (used >= cap) return reply(200, "hourly limit reached; not fired");
  // Суточный предел: DSN есть в каждой установке, и поток поддельных
  // «новых» проблем не должен съесть подписку владельца.
  const dayKey = `day:${new Date(now()).toISOString().slice(0, 10)}`;
  const dayCap = intSetting(env.MAX_FIRES_PER_DAY, 30, 1, 480);
  const usedToday = intSetting(await kv.get(dayKey), 0, 0, 1e9);
  if (usedToday >= dayCap) return reply(200, "daily limit reached; not fired");

  const dedupeTtl = intSetting(env.DEDUPE_HOURS, 24, 1, 24 * 30) * 3600;
  // Сначала метка «в работе» — второй алерт, пришедший через секунду, уже
  // видит её и не запускает вторую routine.
  await kv.put(dedupeKey, JSON.stringify({ state: "pending", at: new Date(now()).toISOString() }), { expirationTtl: dedupeTtl });
  await kv.put(hourKey, String(used + 1), { expirationTtl: 7200 });
  await kv.put(dayKey, String(usedToday + 1), { expirationTtl: 2 * 86400 });

  const work = fireRoutine(env, fire, kv, dedupeKey, dedupeTtl, fetchImpl);
  if (ctx && typeof ctx.waitUntil === "function") ctx.waitUntil(work);
  else await work;
  return reply(202, "accepted");
}

export default {
  fetch(request, env, ctx) {
    return handle(request, env, ctx);
  },
};
