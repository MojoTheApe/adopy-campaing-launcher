"""ACL-2901: первый вход юзера переносит карту машины на хаб (один раз, без кликов)."""
import csv
import json

import pytest

from src.web import hidden_accounts, user_accounts as ua

KEY = "buyer@adopy.test.1999999999." + "a" * 64
GOOGLE = "ya29.secret-google-token"
COLS = ("account_name", "ad_account_id", "page_id", "profile_id", "proxy_host", "proxy_password",
        "tracking_link", "country", "buyer")


@pytest.fixture
def env(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    from config import paths, token_vault

    monkeypatch.setattr(paths, "get_local_config_dir", lambda: str(local))
    monkeypatch.setattr(paths, "get_ad_account_map_path", lambda: str(local / "map.csv"))
    monkeypatch.setattr(paths, "get_hidden_accounts_path", lambda: str(local / "hidden.json"))
    monkeypatch.setattr(hidden_accounts, "get_hidden_accounts_path", lambda: str(local / "hidden.json"))
    monkeypatch.setattr(ua, "token_status", lambda: "ok")
    monkeypatch.setattr(token_vault, "write_secret_text", lambda p, t: open(p, "w", encoding="utf-8").write(t))
    monkeypatch.setattr(token_vault, "read_secret_text", lambda p: open(p, encoding="utf-8").read())
    return local


def write_map(env, rows):
    with open(env / "map.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(COLS))
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLS})


class Hub:
    """Хаб в памяти: my-accounts отдаёт назначения, seed принимается только при пустом списке."""

    def __init__(self, assigned=None, seed_status=None):
        self.assigned = list(assigned or [])
        self.seed_status = seed_status
        self.seeds = []
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        if url.endswith("/v1/session"):
            return 200, {}, {"key": KEY}
        if url.endswith("/v1/my-accounts/seed"):
            payload = json.loads(body.decode("utf-8"))
            self.seeds.append(payload)
            if self.seed_status == "down":
                raise OSError("no network")
            if self.seed_status:
                return self.seed_status, {}, None
            if self.assigned:
                return 409, {}, {"error": "already has assignments"}
            self.assigned = [dict(r, removed_at=None) for r in payload["rows"]]
            return 200, {}, {"ok": True, "seeded": len(payload["rows"])}
        etag = '"e%d"' % len(self.assigned)
        if headers.get("If-None-Match") == etag:
            return 304, {"etag": etag}, None
        return 200, {"etag": etag}, {"accounts": self.assigned}


def run(hub):
    return ua.sync(http=hub, token_provider=lambda: GOOGLE)


def seed_calls(hub):
    return [c for c in hub.calls if c[1].endswith("/seed")]


def test_empty_user_gets_map_seeded_without_proxy_or_tracking(env):
    write_map(env, [
        {"account_name": "UL921", "ad_account_id": "act_1", "page_id": "p1", "profile_id": "pr1",
         "proxy_host": "1.2.3.4", "proxy_password": "pw", "tracking_link": "https://t/x", "country": "US",
         "buyer": "Dana"},
        {"account_name": "DQ204", "ad_account_id": "act_2"},
    ])
    hub = Hub()
    r = run(hub)
    assert r["status"] == "updated" and r["seeded"] == 2
    rows = hub.seeds[0]["rows"]
    assert {row["account_name"] for row in rows} == {"UL921", "DQ204"}
    for row in rows:
        assert set(row) <= set(ua.SEED_FIELDS)
    raw = json.dumps(hub.seeds[0])
    assert "proxy" not in raw and "tracking" not in raw and "1.2.3.4" not in raw and "pw" not in raw
    assert "Dana" not in raw  # локальные колонки (buyer…) не уходят
    # после сида аккаунты числятся серверными: снятие админом потом скроет их
    assert sorted(ua.load_state()["names"]) == ["DQ204", "UL921"]


def test_seed_happens_once_per_user(env):
    write_map(env, [{"account_name": "A1"}])
    hub = Hub()
    run(hub)
    hub.assigned = []  # админ всё удалил бы — повторного сида всё равно нет
    run(hub)
    run(hub)
    assert len(seed_calls(hub)) == 1
    assert ua.load_state()["seeded_for"] == ["buyer@adopy.test"]


def test_user_with_assignments_is_not_seeded(env):
    write_map(env, [{"account_name": "Mine"}])
    hub = Hub(assigned=[{"account_name": "Server1", "ad_account_id": "act_9", "removed_at": None}])
    run(hub)
    assert seed_calls(hub) == []


def test_hub_refusal_409_is_final(env):
    write_map(env, [{"account_name": "Mine"}])
    hub = Hub(seed_status=409)
    run(hub)
    run(hub)
    assert len(seed_calls(hub)) == 1
    assert "buyer@adopy.test" in ua.load_state()["seeded_for"]


@pytest.mark.parametrize("failure", ["down", 500])
def test_failed_seed_is_retried_on_next_sync(env, failure):
    write_map(env, [{"account_name": "A1"}])
    hub = Hub(seed_status=failure)
    r = run(hub)
    assert r["ok"] and "seeded" not in r
    assert ua.load_state()["seeded_for"] == []
    hub.seed_status = None
    r = run(hub)  # без ETag прошлого ответа: не 304, перенос повторяется
    assert r["seeded"] == 1 and len(seed_calls(hub)) == 2


def test_hidden_and_duplicate_rows_are_not_seeded(env):
    write_map(env, [{"account_name": "Keep"}, {"account_name": "Gone"}, {"account_name": "keep "}])
    hidden_accounts.set_hidden("Gone", True)
    hub = Hub()
    run(hub)
    assert [r["account_name"] for r in hub.seeds[0]["rows"]] == ["Keep"]


def test_empty_map_sends_nothing_and_is_done(env):
    hub = Hub()
    r = run(hub)
    assert r["status"] == "updated" and seed_calls(hub) == []
    assert ua.load_state()["seeded_for"] == ["buyer@adopy.test"]


def test_seed_does_not_touch_local_map(env):
    write_map(env, [{"account_name": "UL921", "ad_account_id": "act_1", "proxy_host": "h",
                     "tracking_link": "https://t"}])
    before = (env / "map.csv").read_bytes()
    run(Hub())
    assert (env / "map.csv").read_bytes() == before


def test_email_of_key():
    assert ua._email_of(KEY) == "buyer@adopy.test"
    assert ua._email_of("first.last@corp.test.1999999999." + "b" * 64) == "first.last@corp.test"
    assert ua._email_of("stale") == ""
