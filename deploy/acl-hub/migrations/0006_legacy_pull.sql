-- Забор из старого журнала acl-event-log (KV) по кнопке Refresh: какие пачки уже перенесены.
-- Нужен, пока не все машины обновились до выпуска, который пишет прямо в хаб.
CREATE TABLE IF NOT EXISTS legacy_segments (
  key TEXT PRIMARY KEY,
  pulled_at TEXT NOT NULL
);
