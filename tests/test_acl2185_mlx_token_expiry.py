"""ACL-2185: срок токена автоматизации Multilogin виден заранее.

Только подделки: ни Multilogin, ни сети. Токены — собранные в тесте JWT с
заведомо вымышленной подписью; часы — подставные. Проверяется: срок читается
из токена без сети; предупреждение за час и ошибка после срока; 401 на старте
профиля даёт «Multilogin token expired — sign in again»; «Sign in again»
открывает обычное подключение, а после обновления плашка гаснет; сам токен не
попадает ни в журналы, ни в телеметрию, ни в ответ сервера.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import mlx_session as mlx  # noqa: E402
from src.fb_api import profile as prof  # noqa: E402
from src.meta_campaign_agent.multilogin_launcher import MultiloginLauncherError  # noqa: E402
from tests.test_acl1826_mlx_session_lost import FakeLauncher, _clean, _open  # noqa: E402,F401

REPORT = ROOT / "src" / "web" / "templates" / "report.html"
NOW = 1_800_000_000.0
HOUR = 3600.0
EXPIRED_TEXT = "Multilogin token expired — sign in again"


def _fake_token(exp: float | None) -> str:
    """JWT из случайных байт: подпись и вид не похожи ни на один настоящий."""
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    claims = {"nonce": secrets.token_hex(8)}
    if exp is not None:
        claims["exp"] = exp
    return f"{part({'alg': 'none'})}.{part(claims)}.fake{secrets.token_hex(12)}"


def _env(exp, slot="A"):
    token = _fake_token(exp)
    return token, {f"MULTILOGIN_AUTOMATION_TOKEN_{slot}": token}


# ───────────────────────── срок и состояние ─────────────────────────

@pytest.mark.parametrize("left,state", [
    (2 * HOUR, "ok"),
    (HOUR + 1, "ok"),
    (HOUR, "warning"),
    (600, "warning"),
    (1, "warning"),
    (0, "expired"),
    (-5, "expired"),
])
def test_state_follows_the_fake_clock(left, state):
    _, env = _env(NOW + left)
    got = mlx.token_expiry("A", env=env, now=NOW)
    assert got["state"] == state and got["has_token"] is True
    assert got["expires_epoch"] == NOW + left
    assert got["expires_at"].endswith("+00:00")


def test_missing_and_undecodable_tokens_are_not_warnings():
    assert mlx.token_expiry("A", env={}, now=NOW)["state"] == "none"
    assert mlx.token_expiry("A", env={"MULTILOGIN_AUTOMATION_TOKEN_A": "not-a-jwt"}, now=NOW)["state"] == "unknown"
    _, no_exp = _env(None)
    assert mlx.token_expiry("A", env=no_exp, now=NOW)["state"] == "unknown"


def test_result_never_contains_the_token():
    token, env = _env(NOW + 100)
    assert token not in json.dumps(mlx.token_expiry("A", env=env, now=NOW))


def test_endpoint_reports_slots_with_tokens_only(monkeypatch):
    from src.web.app import app

    token_a = _fake_token(NOW + 600)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", token_a)
    monkeypatch.delenv("MULTILOGIN_AUTOMATION_TOKEN_B", raising=False)
    monkeypatch.setattr(mlx.time, "time", lambda: NOW)

    async def _get():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            return await c.get("/multilogin-token-expiry")

    r = asyncio.run(_get())
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["warn_seconds"] == 3600
    assert [s["instance"] for s in body["slots"]] == ["A"]
    assert body["slots"][0]["state"] == "warning"
    assert token_a not in r.text
    # После обновления токена ответ меняется сразу, без перезапуска.
    token_new = _fake_token(NOW + 30 * 24 * HOUR)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", token_new)
    again = asyncio.run(_get())
    assert again.json()["slots"][0]["state"] == "ok" and token_new not in again.text


# ───────────────────────── 401 на старте профиля ─────────────────────────

@pytest.mark.parametrize("slot", ["A", "B"])
def test_401_on_start_means_expired_token_on_any_slot(slot):
    err = MultiloginLauncherError("Start profile failed HTTP 401: unauthorized", status=401)
    assert mlx.classify(err, slot) == mlx.TOKEN_EXPIRED
    assert mlx.classify("Start profile failed HTTP 401: x", slot) == mlx.TOKEN_EXPIRED
    assert mlx.message_for(mlx.TOKEN_EXPIRED) == EXPIRED_TEXT


def test_other_statuses_are_still_generic():
    assert mlx.classify("Start profile failed HTTP 500: boom", "A") == ""
    assert mlx.classify(MultiloginLauncherError("x", status=404), "A") == ""


def test_profile_start_401_reports_the_token_not_a_generic_failure(_clean):
    launcher = FakeLauncher("A", MultiloginLauncherError("Start profile failed HTTP 401: x", status=401))
    with mlx.scope(account="Acct 1"):
        with pytest.raises(prof.ProfileError) as ei:
            _open("A", launcher)
    assert ei.value.code == mlx.TOKEN_EXPIRED and str(ei.value) == EXPIRED_TEXT
    assert _clean  # событие ушло в заглушку Sentry


def test_token_never_reaches_logs_or_telemetry(_clean, caplog, monkeypatch):
    from src.fb_ads_rpa import run_telemetry as rt
    from src.fb_api import telemetry as tm

    token = _fake_token(NOW - 10)
    monkeypatch.setenv("MULTILOGIN_AUTOMATION_TOKEN_A", token)
    caplog.set_level(logging.DEBUG)
    mlx.token_expiry("A", now=NOW)
    mlx.token_preflight("A", now=NOW)
    launcher = FakeLauncher("A", MultiloginLauncherError("Start profile failed HTTP 401: x", status=401))
    with mlx.scope(account="Acct 1"):
        with pytest.raises(prof.ProfileError) as ei:
            _open("A", launcher)
    written = ""
    for path in (rt.TELEMETRY_PATH, tm.journal_path()):
        if Path(path).exists():
            written += Path(path).read_text(encoding="utf-8")
    assert written  # телеметрия действительно писалась
    assert token not in written and token not in caplog.text
    assert token not in str(ei.value) and token not in json.dumps(_clean, default=str)


# ───────────────────────── страница ─────────────────────────

def test_page_has_banner_and_signin_button_without_any_token():
    src = REPORT.read_text(encoding="utf-8")
    assert 'id="mlx-expiry-banner"' in src
    assert 'onclick="aclMlxSignInAgain()"' in src and ">Sign in again<" in src
    assert "/multilogin-token-expiry" in src
    markup = src[src.index('id="mlx-expiry-banner"'):src.index('<div class="rh-pop"')]
    assert "MULTILOGIN_AUTOMATION_TOKEN" not in markup


def _node_driver(body: str) -> str:
    src = REPORT.read_text(encoding="utf-8")
    block = src[src.index("var ACL_MLX_EXPIRY = "):src.index("function aclMlxExpiryBoot()")]
    head = textwrap.dedent("""
        const els = {};
        const mk = id => (els[id] = {id, hidden: true, textContent: '', _c: new Set(),
          classList: {toggle(n, on) { on ? els[id]._c.add(n) : els[id]._c.delete(n); },
                      remove(n) { els[id]._c.delete(n); }}});
        mk('mlx-expiry-banner'); mk('mlx-expiry-text');
        global.document = {getElementById: id => els[id]};
        global.window = {ACL_MLX_LABELS: {A: 'Inhouse', B: 'KLD'}};
        let nowMs = 0; Date.now = () => nowMs;
        global.fetch = () => Promise.resolve({ok: true, json: () => Promise.resolve(global.__reply)});
        """)
    return head + block + "\n" + body


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_banner_follows_the_fake_clock_and_clears_after_renewal():
    exp = int(NOW)
    body = textwrap.dedent("""
        const banner = els['mlx-expiry-banner'], text = els['mlx-expiry-text'];
        const out = [];
        ACL_MLX_EXPIRY.slots = [{instance: 'A', expires_epoch: __T0__}];
        function at(sec) { nowMs = (__T0__ + sec) * 1000; aclMlxExpiryPaint();
          out.push([sec, banner.hidden, banner._c.has('mlx-expired'), text.textContent]); }
        at(-7200); at(-3601); at(-3600); at(-60); at(0); at(90);
        (async () => {
          global.__reply = {ok: true, warn_seconds: 3600, slots: [{instance: 'A', expires_epoch: __T0__ + 2592000}]};
          await aclMlxExpiryRefresh();
          out.push(['renewed', banner.hidden, banner._c.has('mlx-expired'), text.textContent]);
          console.log(JSON.stringify(out));
        })();
    """).replace("__T0__", str(exp))
    res = subprocess.run(["node", "-e", _node_driver(body)], capture_output=True, text=True,
                         timeout=60, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    rows = json.loads(res.stdout.strip().splitlines()[-1])
    by = {str(r[0]): r for r in rows}
    assert by["-7200"][1] is True and by["-3601"][1] is True      # раньше чем за час — тихо
    warn = by["-3600"]
    assert warn[1] is False and warn[2] is False and "60 min" in warn[3]
    assert re.search(r"expires at \d{4}-\d{2}-\d{2} \d{2}:\d{2}", warn[3])
    assert by["-60"][1] is False and "1 min" in by["-60"][3]
    for s in ("0", "90"):                                         # после срока — ошибка
        assert by[s][1] is False and by[s][2] is True
        assert by[s][3].startswith("Multilogin token expired at ") and "sign in again" in by[s][3]
    assert by["renewed"][1] is True and by["renewed"][2] is False  # плашка погасла


def test_signin_again_opens_the_existing_connect_flow_even_with_a_live_token():
    src = REPORT.read_text(encoding="utf-8")
    start = src.index("function aclMlxSignInAgain()")
    fn = src[start:src.index("if (document.readyState", start)]
    assert 'data-view="accounts"' in fn and "amapMlOpen(" in fn and ", true)" in fn
    opener = src[src.index("function amapMlOpen("):src.index("function amapInstanceChanged")]
    assert "!force &&" in opener                                  # живой токен форму не прячет
    submit = src[src.index("async function amapMlSubmit("):src.index("function amapMlSignin(")]
    assert "delete block.dataset.mlRenew" in submit and "aclMlxExpiryRefresh()" in submit
