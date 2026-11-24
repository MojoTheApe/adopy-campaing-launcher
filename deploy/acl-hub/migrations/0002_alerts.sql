-- Хаб телеметрии, часть 3: Telegram. Антиспам алертов, тишина по отпечатку, разовые отметки (дайджест).
CREATE TABLE alert_log (key TEXT PRIMARY KEY, last_sent_at TEXT NOT NULL, suppressed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE mutes (fingerprint TEXT PRIMARY KEY, until TEXT NOT NULL);
CREATE TABLE bot_state (k TEXT PRIMARY KEY, v TEXT);
