"""ACL-1756: помощник страницы (JS) в Node — токен остаётся во вкладке.

Отдельно от tests/test_acl1756_fb_api.py: здесь настоящий JS помощника
выполняется в процессе Node (пропуск, если Node нет), там — Python-сторона с
поддельным браузером. Проверяется захват токена из собственных запросов Ads
Manager (XHR, fetch, HTML), снятие обёрток и их сходство с родными функциями,
только GET, белый и запретный списки имён параметров, `redirect: 'error'`,
один запрос на сбой сети и то, что токен не возвращается из страницы.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import token_capture as tc  # noqa: E402

TOKEN = "EAAB" + "Zx9" * 20          # «токен» страницы
TOKEN_LIKE = re.compile(r"EAA[A-Za-z0-9]{10,}")


_NODE_HARNESS = r"""
const fetched = [];
let failNext = false;
const listeners = [];
class FakeFormData { constructor(){ this.m = new Map(); } append(k,v){ this.m.set(k,v); } get(k){ return this.m.has(k)?this.m.get(k):null; } }
class FakeXHR { open(m,u,a,b,c){ this._u=u; } send(){ this._b=arguments[0]; } }
const scripts = SCRIPTS;
const origSend = FakeXHR.prototype.send, origOpen = FakeXHR.prototype.open;
const win = {
  location: { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' },
  fetch: async function fetch(url) {
    const init = arguments[1];
    fetched.push({ url: String(url), init: init || {} });
    if (failNext) { failNext = false; throw new TypeError('Failed to fetch'); }
    const body = JSON.stringify({ data: [{ id: 'act_1' }],
      paging: { next: 'https://graph.facebook.com/v22.0/act_1/campaigns?access_token=' + TOKEN + '&after=x' },
      echo: TOKEN });
    return { status: 200, text: async () => body,
             headers: { get: (h) => h === 'x-ad-account-usage' ? '{"acc_id_util_pct":12}' : null } };
  },
  XMLHttpRequest: FakeXHR,
  performance: { getEntriesByType: () => [] },
  setTimeout: (f, ms) => setTimeout(f, 0),
};
win.window = win; win.top = win;
const nativeFetch = win.fetch;
const doc = { readyState: 'complete', querySelector: () => null, scripts: scripts };
const run = new Function('window','location','document','XMLHttpRequest','URL','URLSearchParams',
  'FormData','performance','setTimeout', HELPER);
run(win, win.location, doc, FakeXHR, URL, URLSearchParams, FakeFormData, win.performance, win.setTimeout);
win.URL = URL; win.URLSearchParams = URLSearchParams;
const h = win[NAME];
(async () => {
  const out = {};
  out.before = h.state();
  out.wrapped = win.fetch !== nativeFetch && FakeXHR.prototype.send !== origSend;
  out.lengths = [win.fetch.length, nativeFetch.length, FakeXHR.prototype.send.length,
                 origSend.length, FakeXHR.prototype.open.length, origOpen.length];
  out.wrap = {
    fetch_len: win.fetch.length === nativeFetch.length,
    fetch_name: win.fetch.name === nativeFetch.name,
    fetch_str: win.fetch.toString() === Function.prototype.toString.call(nativeFetch),
    send_len: FakeXHR.prototype.send.length === origSend.length,
    open_len: FakeXHR.prototype.open.length === origOpen.length,
    send_str: FakeXHR.prototype.send.toString() === Function.prototype.toString.call(origSend),
    tostring_enum: Object.keys(win.fetch).length === 0,
  };
  out.no_token_call = await h.call({ method: 'GET', path: '/me', params: {} });
  if (MODE === 'xhr') {
    const x = new win.XMLHttpRequest();
    x.open('POST', 'https://graph.facebook.com/v22.0/act_1/insights');
    x.send('fields=spend&access_token=' + TOKEN);
  } else if (MODE === 'fetch') {
    await win.fetch('https://graph.facebook.com/v21.0/me/adaccounts?access_token=' + TOKEN);
    fetched.length = 0;
  }
  out.after = await h.ready(MODE === 'html' ? 10 : 1000);
  out.unhooked = FakeXHR.prototype.send === origSend && FakeXHR.prototype.open === origOpen;
  out.post = await h.call({ method: 'POST', path: '/act_1/campaigns', params: {} });
  out.bad = await h.call({ method: 'GET', path: '/../oauth', params: {} });
  out.got = await h.call({ method: 'GET', path: '/act_1/campaigns', params: { fields: 'name', limit: 5 } });
  out.fetched_has_token = fetched.length === 1 && fetched[0].url.includes('access_token=' + TOKEN);
  out.fetched_url_head = fetched.length ? fetched[0].url.split('?')[0] : '';
  out.credentials = fetched.length ? fetched[0].init.credentials : '';
  out.redirect = fetched.length ? fetched[0].init.redirect : '';
  out.method = fetched.length ? fetched[0].init.method : '';
  out.forbidden = {};
  const before = fetched.length;
  for (const k of ['method', '_method', 'batch', 'suppress_http_code', 'access_token', 'Method',
                   ' method', 'method ', '\tmethod', 'method\u0000', 'method[]', '\uff4dethod', '']) {
    out.forbidden[k] = await h.call({ method: 'GET', path: '/me', params: { [k]: 'post' } });
  }
  out.forbidden_fetches = fetched.length - before;
  failNext = true;
  out.net = await h.call({ method: 'GET', path: '/me', params: {} });
  out.net_fetches = fetched.length - before;
  h.retire();
  out.after_retire = await h.call({ method: 'GET', path: '/me', params: {} });
  out.keys = Object.keys(win).filter(k => k.startsWith('__acl'));
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_node(mode: str, scripts: list[dict] | None = None) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_test"
    src = (_NODE_HARNESS
           .replace("SCRIPTS", json.dumps(scripts or []))
           .replace("TOKEN", json.dumps(TOKEN))
           .replace("NAME", json.dumps(name))
           .replace("MODE", json.dumps(mode))
           .replace("HELPER", json.dumps(tc.helper_source(name))))
    done = subprocess.run([node, "-e", src], capture_output=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


@pytest.mark.parametrize("mode,source,version", [("xhr", "network", "v22.0"), ("fetch", "network", "v21.0")])
def test_helper_takes_the_token_from_ads_manager_requests_and_keeps_it(mode, source, version):
    out = _run_node(mode)
    assert out["before"]["has_token"] is False
    assert out["no_token_call"]["error"] == "no_token"
    assert out["after"]["has_token"] is True and out["after"]["source"] == source
    assert out["after"]["version"] == version
    assert out["unhooked"] is True                 # обёртки сняты после захвата
    assert out["post"] == {"error": "read_only"}
    assert out["bad"] == {"error": "bad_path"}
    assert out["fetched_has_token"] is True        # токен приложила сама страница
    assert out["fetched_url_head"] == f"https://graph.facebook.com/{version}/act_1/campaigns"
    assert out["credentials"] == "omit"
    assert out["redirect"] == "error" and out["method"] == "GET"
    # Глагол не подменить параметрами Graph (ревью S1): отказ без запроса.
    assert all(v == {"error": "forbidden_param"} for v in out["forbidden"].values()), out["forbidden"]
    assert out["forbidden_fetches"] == 0
    # Сбой сети — ровно один запрос, без второго GET (ревью O4).
    assert out["net"]["error"] == "network" and out["net_fetches"] == 1
    # Обёртки выглядят как родные: toString, length, name (ревью O1).
    assert out["wrapped"] is True
    assert all(out["wrap"].values()), out["wrap"]
    # Длины родных функций нарочно не совпадают с числом параметров обёрток.
    assert out["lengths"] == [1, 1, 0, 0, 5, 5], out["lengths"]
    assert len(out["forbidden"]) == 13 and out["forbidden"][""] == {"error": "forbidden_param"}
    assert out["got"]["status"] == 200
    assert out["got"]["headers"] == {"x-ad-account-usage": '{"acc_id_util_pct":12}'}
    assert out["after_retire"] == {"error": "retired"}
    assert out["keys"] == []                       # свойство помощника не перечисляется
    # Ни одно значение, вернувшееся из страницы, не несёт токен.
    blob = json.dumps(out)
    assert TOKEN not in blob and not TOKEN_LIKE.search(blob)
    assert "access_token=<hidden>" in json.dumps(out["got"])


def test_helper_html_fallback_keeps_the_token_in_page():
    out = _run_node("html", [{"src": "", "textContent": 'x={"accessToken":"' + TOKEN + '"}'}])
    assert out["after"]["has_token"] is True and out["after"]["source"] == "html"
    assert out["fetched_url_head"] == "https://graph.facebook.com/act_1/campaigns"
    assert TOKEN not in json.dumps(out)
