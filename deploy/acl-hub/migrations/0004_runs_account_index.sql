-- Недоделанные кампании и поиск успеха той же кампании на аккаунте: без индекса — полный проход runs.
CREATE INDEX runs_acct_campaign ON runs(account, campaign, outcome, finished_at);
