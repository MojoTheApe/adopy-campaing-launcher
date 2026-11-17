-- Схема хаба телеметрии ACL (часть 1). Контракт для частей 2–5: менять только новой миграцией.
CREATE TABLE events (
  seq_id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE, type TEXT NOT NULL, ts TEXT NOT NULL,
  machine_id TEXT, machine TEXT, manager TEXT, flow_version TEXT, channel TEXT,
  job_id TEXT, account TEXT, campaign TEXT, stream_id TEXT, seq INTEGER,
  batch_id TEXT, received_at TEXT NOT NULL, payload TEXT NOT NULL,
  derived INTEGER NOT NULL DEFAULT 1   -- 0: выводы ещё не построены (повторная доставка достроит)
);
CREATE INDEX events_pending ON events(derived) WHERE derived = 0;
CREATE INDEX events_ts ON events(ts);
CREATE INDEX events_job ON events(job_id);
CREATE INDEX events_machine_ts ON events(machine_id, ts);
CREATE INDEX events_type_ts ON events(type, ts);

CREATE TABLE machines (
  machine_id TEXT PRIMARY KEY, machine TEXT, manager TEXT, flow_version TEXT,
  first_seen_at TEXT, last_event_at TEXT, last_heartbeat_at TEXT,
  outbox_bytes INTEGER, dropped_total INTEGER DEFAULT 0,
  profile TEXT, profile_at TEXT
);

CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY, machine_id TEXT, manager TEXT, account TEXT,
  created_at TEXT, started_at TEXT, finished_at TEXT, duration_s REAL,
  slot_wait_s REAL, status TEXT, cancelled INTEGER, dry_run INTEGER, reviewed INTEGER,
  n_planned INTEGER, n_ok INTEGER DEFAULT 0, n_failed INTEGER DEFAULT 0,
  n_not_started INTEGER DEFAULT 0
);
CREATE INDEX jobs_created ON jobs(created_at);
CREATE INDEX jobs_manager ON jobs(manager, created_at);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  job_id TEXT, campaign TEXT, machine_id TEXT, manager TEXT, account TEXT,
  finished_at TEXT, outcome TEXT,
  stop_kind TEXT, error TEXT, fingerprint TEXT,
  duration_s REAL, calls INTEGER, writes INTEGER,
  campaign_id TEXT, published_as TEXT, created_any INTEGER, reused_any INTEGER,
  spec_hash TEXT, country TEXT, daily_budget REAL, adset_names TEXT, ad_names TEXT,
  n_ads INTEGER, load TEXT, flow_version TEXT,
  stats_at TEXT, spec_at TEXT          -- время событий stats/spec: побеждает более позднее
);
CREATE INDEX runs_finished ON runs(finished_at);
CREATE INDEX runs_manager ON runs(manager, finished_at);
CREATE INDEX runs_fingerprint ON runs(fingerprint);

CREATE TABLE tokens (token_sha TEXT PRIMARY KEY, checked_at INTEGER NOT NULL);
