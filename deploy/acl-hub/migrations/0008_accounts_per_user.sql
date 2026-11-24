-- ACL-2898: аккаунты по юзерам. Номера 0006/0007 заняты другими ветками, пропуск намеренный.
-- Без прокси и без трекинг-ссылок: этих данных в хабе быть не должно.
CREATE TABLE users (
  email      TEXT PRIMARY KEY,
  name       TEXT,
  role       TEXT NOT NULL DEFAULT 'buyer',   -- admin | buyer
  created_at TEXT NOT NULL,
  disabled   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE account_assignments (
  user_email          TEXT NOT NULL,
  account_name        TEXT NOT NULL COLLATE NOCASE,  -- карта ACL сравнивает имена без регистра
  ad_account_id       TEXT,
  page_id             TEXT,
  page_name           TEXT,
  country             TEXT,
  multilogin_instance TEXT,
  folder_id           TEXT,
  profile_id          TEXT,
  assigned_by         TEXT,
  assigned_at         TEXT NOT NULL,
  removed_at          TEXT,                    -- снятие мягкое: строка остаётся
  PRIMARY KEY (user_email, account_name)
);

CREATE INDEX idx_assignments_user ON account_assignments (user_email, removed_at);
