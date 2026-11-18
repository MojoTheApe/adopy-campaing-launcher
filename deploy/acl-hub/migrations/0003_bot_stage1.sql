-- Бот, этап 1: справочник известных решений по отпечатку сбоя и запросы владельца «завести задачу».
CREATE TABLE failure_hints (fingerprint TEXT PRIMARY KEY, hint TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE task_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT, run_id TEXT NOT NULL, created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', note TEXT
);
CREATE UNIQUE INDEX task_requests_open_fp ON task_requests(fingerprint) WHERE status = 'open' AND fingerprint IS NOT NULL;
