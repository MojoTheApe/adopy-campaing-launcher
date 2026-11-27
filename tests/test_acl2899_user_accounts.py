"""ACL-2899: аккаунты юзера с хаба сливаются в локальную карту."""
import csv
import json
import logging

import pytest

from src.web import hidden_accounts, user_accounts as ua

KEY = "buyer@adopy.test.1999999999." + "a" * 64
GOOGLE = "ya29.secret-google-token"


@pytest.fixture
def env(tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    from config import paths, token_vault

    monkeypatch.setattr(paths, "get_local_config_dir", lambda: str(local))
    from src.web import account_links

    monkeypatch.setattr(account_links, "get_local_config_dir", lambda: str(local))  # ACL-2950: не трогать настоящий файл связей
    monkeypatch.setattr(account_links, "get_ad_account_map_path", lambda: str(local / "map.csv"))
    monkeypatch.setattr(paths, "get_ad_account_map_path", lambda: str(local / "map.csv"))
    monkeypatch.setattr(paths, "get_hidden_accounts_path", lambda: str(local / "hidden.json"))
    monkeypatch.setattr(paths, "get_google_oauth_token_path", lambda: str(local / "token.json"))
    monkeypatch.setattr(hidden_accounts, "get_hidden_accounts_path", lambda: str(local / "hidden.json"))
    monkeypatch.setattr(ua, "token_status", lambda: "ok")
    # Хранилище секретов без DPAPI: обычный текст
    monkeypatch.setattr(token_vault, "write_secret_text", lambda p, t: open(p, "w", encoding="utf-8").write(t))
    monkeypatch.setattr(token_vault, "read_secret_text", lambda p: open(p, encoding="utf-8").read())
    return local


def write_map(env, rows, cols=("account_name", "ad_account_id", "profile_id", "proxy_host", "tracking_link", "country")):
    with open(env / "map.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(cols))
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def read_map(env):
    with open(env / "map.csv", encoding="utf-8-sig", newline="") as f:
        return {r["account_name"]: r for r in csv.DictReader(f)}


def acc(name, **kw):
    base = {"account_name": name, "ad_account_id": "", "page_id": "", "page_name": "", "country": "",
            "multilogin_instance": "", "folder_id": "", "profile_id": "", "removed_at": None}
    base.update(kw)
    return base


class Hub:
    def __init__(self, accounts=None, etag='"e1"', status=200):
        self.accounts, self.etag, self.status, self.calls = accounts or [], etag, status, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers)))
        if url.endswith("/v1/session"):
            return 200, {}, {"key": KEY, "email": "buyer@adopy.test", "role": "buyer"}
        if self.status != 200:
            return self.status, {}, None
        if headers.get("If-None-Match") == self.etag:
            return 304, {"etag": self.etag}, None
        return 200, {"etag": self.etag}, {"accounts": self.accounts}


def run(hub):
    return ua.sync(http=hub, token_provider=lambda: GOOGLE)


def test_server_fields_win_and_local_fields_kept(env):
    write_map(env, [{"account_name": "Acc One", "ad_account_id": "111", "profile_id": "old",
                     "proxy_host": "1.2.3.4", "tracking_link": "https://t/x", "country": "US"}])
    r = run(Hub([acc("ACC ONE", ad_account_id="222", profile_id="new", country="")]))
    assert r["status"] == "updated" and r["changed"]
    row = read_map(env)["Acc One"]
    assert row["ad_account_id"] == "222" and row["profile_id"] == "new"
    assert row["proxy_host"] == "1.2.3.4" and row["tracking_link"] == "https://t/x"
    assert row["country"] == "US"  # пустое серверное поле не стирает


def test_empty_server_profile_id_keeps_local(env):
    write_map(env, [{"account_name": "A", "profile_id": "local-p"}])
    run(Hub([acc("A", ad_account_id="9")]))
    assert read_map(env)["A"]["profile_id"] == "local-p"


def test_new_account_appended_and_local_only_untouched(env):
    write_map(env, [{"account_name": "Mine", "ad_account_id": "5", "proxy_host": "h"}])
    run(Hub([acc("Server One", ad_account_id="7", profile_id="p7")]))
    rows = read_map(env)
    assert rows["Server One"]["profile_id"] == "p7"
    assert rows["Mine"]["ad_account_id"] == "5" and rows["Mine"]["proxy_host"] == "h"
    assert ua.load_state()["names"] == ["Server One"]


def test_removed_hides_and_keeps_row_then_reassign_unhides(env):
    write_map(env, [{"account_name": "Acc", "proxy_host": "h"}])
    run(Hub([acc("Acc", ad_account_id="1")], etag='"e1"'))
    run(Hub([acc("Acc", ad_account_id="1", removed_at="2026-11-24T10:00:00Z")], etag='"e2"'))
    assert hidden_accounts.is_hidden("acc")
    assert "Acc" in read_map(env) and read_map(env)["Acc"]["proxy_host"] == "h"
    run(Hub([acc("Acc", ad_account_id="1")], etag='"e3"'))
    assert not hidden_accounts.is_hidden("acc")


def test_manual_hide_survives_sync(env):
    # Ревью: юзер сам скрыл назначенный аккаунт — синхронизация его не возвращает
    write_map(env, [{"account_name": "Acc"}])
    run(Hub([acc("Acc", ad_account_id="1")], etag='"e1"'))
    hidden_accounts.set_hidden("Acc", True)
    run(Hub([acc("Acc", ad_account_id="2")], etag='"e2"'))
    assert hidden_accounts.is_hidden("acc")


def test_removed_local_only_account_is_not_hidden(env):
    write_map(env, [{"account_name": "Mine"}])
    run(Hub([acc("Mine", removed_at="2026-11-24T10:00:00Z")]))
    assert not hidden_accounts.is_hidden("Mine")


def test_304_does_nothing(env):
    write_map(env, [{"account_name": "A"}])
    hub = Hub([acc("A", ad_account_id="1")])
    run(hub)
    raw = (env / "map.csv").read_bytes()
    state = (env / "server_accounts.json").read_bytes()
    r = run(hub)
    assert r["status"] == "not_modified" and not r["changed"]
    assert (env / "map.csv").read_bytes() == raw
    assert (env / "server_accounts.json").read_bytes() == state
    # ACL-2915: после my-accounts дашборд может спросить библиотеку; ETag смотрим у последнего my-accounts
    last = [c for c in hub.calls if c[1].endswith("/v1/my-accounts")][-1]
    assert last[2]["If-None-Match"] == '"e1"'


@pytest.mark.parametrize("status", [500, 503, 404])
def test_hub_error_leaves_map(env, status):
    write_map(env, [{"account_name": "A", "ad_account_id": "1"}])
    raw = (env / "map.csv").read_bytes()
    r = run(Hub(status=status))
    assert r["ok"] and not r["changed"]
    assert (env / "map.csv").read_bytes() == raw


def test_offline_never_raises(env):
    write_map(env, [{"account_name": "A"}])
    raw = (env / "map.csv").read_bytes()

    def boom(*a, **k):
        raise OSError("no network")

    r = ua.sync(http=boom, token_provider=lambda: GOOGLE)
    assert r["status"] in ("offline", "no_session") and not r["changed"]
    assert (env / "map.csv").read_bytes() == raw


def test_no_google_token_is_quiet(env):
    r = ua.sync(http=Hub(), token_provider=lambda: "")
    assert r["status"] == "no_session"


def test_key_and_token_not_logged_or_in_state(env, caplog):
    caplog.set_level(logging.DEBUG)
    write_map(env, [{"account_name": "A"}])
    run(Hub([acc("A", ad_account_id="1")]))
    run(Hub(status=500, etag='"zz"'))
    assert KEY not in caplog.text and GOOGLE not in caplog.text
    assert KEY not in (env / "server_accounts.json").read_text(encoding="utf-8")
    assert ua.load_key() == KEY


def test_401_reopens_session_once(env):
    write_map(env, [{"account_name": "A"}])
    ua._save_key("stale")

    def http(method, url, headers, body):
        if url.endswith("/v1/session"):
            return 200, {}, {"key": KEY}
        if headers["Authorization"] == "Bearer stale":
            return 401, {}, None
        return 200, {"etag": '"n"'}, {"accounts": [acc("A", ad_account_id="3")]}

    r = ua.sync(http=http, token_provider=lambda: GOOGLE)
    assert r["status"] == "updated" and read_map(env)["A"]["ad_account_id"] == "3"


def test_old_scopes_ask_for_sign_in_without_calling_hub(env, monkeypatch):
    monkeypatch.setattr(ua, "token_status", lambda: "old_scopes")
    hub = Hub()
    r = ua.sync(http=hub, token_provider=lambda: GOOGLE)
    assert r["status"] == "signin_required" and not hub.calls


def test_sign_in_prompt_only_once_per_machine(env, monkeypatch):
    # Ревью: предложение войти заново — один раз на машину, не на каждую вкладку
    monkeypatch.setattr(ua, "token_status", lambda: "old_scopes")
    first = ua.sync(http=Hub(), token_provider=lambda: GOOGLE)
    second = ua.sync(http=Hub(), token_provider=lambda: GOOGLE)
    assert first["prompt"] is True and second["prompt"] is False
    assert second["status"] == "signin_required"


def test_token_status_detects_old_token(tmp_path, monkeypatch):
    from config import paths, token_vault

    sheets = "https://www.googleapis.com/auth/spreadsheets.readonly"
    old = {"refresh_token": "r", "scopes": [sheets]}
    new = {"refresh_token": "r", "scopes": [sheets, "openid", "https://www.googleapis.com/auth/userinfo.email"]}
    p = tmp_path / "tok.json"
    monkeypatch.setattr(paths, "get_google_oauth_token_path", lambda: str(p))
    monkeypatch.setattr(token_vault, "read_secret_text", lambda path: open(path, encoding="utf-8").read())
    p.write_text(json.dumps(old), encoding="utf-8")
    assert ua.token_status() == "old_scopes"
    p.write_text(json.dumps(new), encoding="utf-8")
    assert ua.token_status() == "ok"
    p.unlink()
    assert ua.token_status() == "none"


def test_sign_in_scopes_include_identity():
    from src.analysis_agent import connection_settings as cs, gsheet_accounts as ga

    for scopes in (ga._SCOPES, cs._READONLY):
        assert "openid" in scopes and "https://www.googleapis.com/auth/userinfo.email" in scopes
    old = {"scopes": ["https://www.googleapis.com/auth/spreadsheets.readonly"]}
    assert ga.saved_token_scopes(old) == old["scopes"]  # старый токен читает таблицу под своими правами
    assert ga.saved_token_scopes({}) == ga._SHEETS_SCOPES
    assert not ga.token_has_identity_scopes(old)


def test_hub_url_env_override(monkeypatch):
    monkeypatch.setenv("ACL_HUB_URL", "https://hub.example/")
    assert ua.hub_url() == "https://hub.example"
    monkeypatch.delenv("ACL_HUB_URL")
    assert ua.hub_url().startswith("https://")
