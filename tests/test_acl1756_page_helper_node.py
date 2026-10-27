"""ACL-1756: помощник ОТДЕЛЬНОГО мира (JS) в Node — без обёрток на странице.

Отдельно от tests/test_acl1756_fb_api.py: здесь настоящий JS помощника
выполняется в процессе Node (пропуск, если Node нет). Ревью r8: страница не
оборачивается вовсе; токен берётся из Resource Timing и инлайнового HTML, а
`fetch()` к Graph идёт из отдельного мира. Проверяется: захват без запросов,
якорь хоста Graph, только GET, белый и запретный списки имён параметров,
`redirect: 'error'`, один запрос на сбой сети, что токен не возвращается, и
что помощник не создаёт ничего, кроме своего свойства в своём globalThis и
не трогает fetch/XHR.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_api import token_capture as tc  # noqa: E402

TOKEN = "EAAB" + "Zx9" * 20
TOKEN_LIKE = re.compile(r"EAA[A-Za-z0-9]{10,}")

# Помощник выполняется с нашим поддельным globalThis (параметр `globalThis`
# затеняет настоящий в теле функции), поэтому его свойство не попадает ни в
# какой общий объект — как в отдельном мире вкладки.
_HARNESS = r"""
const RESOURCES = __RESOURCES__;
const SCRIPTS = __SCRIPTS__;
const HTML = __HTML__;
const NET = __NET__;            // 'ok' | 'fail'
const calls = [];
const fakeFetch = async (url, init) => {
  calls.push({ url: String(url), init: init || {} });
  if (NET === 'fail') throw new TypeError('Failed to fetch');
  const body = JSON.stringify({ data: [{ id: 'act_1', leak: TOKENV }],
    paging: { next: 'https://graph.facebook.com/v22.0/act_1/campaigns?access_token=' + TOKENV } });
  return { status: 200, text: async () => body,
           headers: { get: (h) => h === 'x-ad-account-usage' ? '{"acc_id_util_pct":12}' : null } };
};
const loc = { href: 'https://adsmanager.facebook.com/adsmanager/manage/campaigns?act=1',
              hostname: 'adsmanager.facebook.com', pathname: '/adsmanager/manage/campaigns' };
const doc = { readyState: 'complete', querySelector: () => null, scripts: SCRIPTS,
              documentElement: { innerHTML: HTML } };
const perf = { getEntriesByType: (t) => t === 'resource' ? RESOURCES.map((n) => ({ name: n })) : [] };
const G = {};                   // отдельный «мир»: свой globalThis
const fn = new Function('globalThis','location','document','performance','fetch','URL','URLSearchParams',
                        HELPER);
fn(G, loc, doc, perf, fakeFetch, URL, URLSearchParams);
const h = G[NAME];
(async () => {
  const out = {};
  // Ничего, кроме помощника, в «мир» не добавлено; fetch/XHR не трогаются.
  out.world_keys = Object.getOwnPropertyNames(G);
  out.scan_made_no_fetch = (calls.length === 0);
  out.before = h.state();           // дешёвый опрос: только Resource Timing
  out.after_probe = h.probe();       // финальный: + HTML/скрипты
  out.scan_still_no_fetch = (calls.length === 0);
  out.post = await h.call({ method: 'POST', path: '/me', params: {} });
  out.bad = await h.call({ method: 'GET', path: '/../oauth', params: {} });
  out.forbidden = {};
  for (const k of ['method','_method','batch','suppress_http_code','access_token','Method',
                   ' method','method ','\tmethod','method\u0000','method[]','ｍethod','']) {
    out.forbidden[k] = await h.call({ method: 'GET', path: '/me', params: { [k]: 'post' } });
  }
  out.forbidden_fetches = calls.length;
  out.got = await h.call({ method: 'GET', path: '/act_1/campaigns', params: { fields: 'name', limit: 5 } });
  out.fetch_count = calls.length;
  out.fetched_head = calls.length ? calls[calls.length - 1].url.split('?')[0] : '';
  out.fetched_has_token = calls.length ? calls[calls.length - 1].url.includes('access_token=' + TOKENV) : false;
  out.fetched_cred = calls.length ? calls[calls.length - 1].init.credentials : '';
  out.fetched_method = calls.length ? calls[calls.length - 1].init.method : '';
  out.fetched_redirect = calls.length ? calls[calls.length - 1].init.redirect : '';
  out.opt_public = await h.call({ method: 'GET', path: '/me', params: {}, host: 'public', credentials: 'omit' });
  out.opt_public_head = calls.length ? calls[calls.length - 1].url.split('?')[0] : '';
  out.opt_public_cred = calls.length ? calls[calls.length - 1].init.credentials : '';
  out.opt_bad_host = await h.call({ method: 'GET', path: '/me', params: {}, host: 'evil.example.com' });
  out.opt_bad_cred = await h.call({ method: 'GET', path: '/me', params: {}, credentials: 'same-origin' });
  h.retire();
  out.after_retire = await h.call({ method: 'GET', path: '/me', params: {} });
  out.retired_state = h.state().retired;
  out.token_in_output = JSON.stringify(out).includes(TOKENV);
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run(resources=None, scripts=None, html="", net="ok") -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    name = "__acl_graph_iso"
    src = (_HARNESS
           .replace("__RESOURCES__", json.dumps(resources or []))
           .replace("__SCRIPTS__", json.dumps(scripts or []))
           .replace("__HTML__", json.dumps(html))
           .replace("__NET__", json.dumps(net))
           .replace("TOKENV", json.dumps(TOKEN))
           .replace("NAME", json.dumps(name))
           .replace("HELPER", json.dumps(tc.helper_source(name))))
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as fh:
        fh.write(src)
        script = fh.name
    try:
        done = subprocess.run([node, script], capture_output=True, encoding="utf-8", timeout=60)
    finally:
        try:
            Path(script).unlink()
        except OSError:
            pass
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


# Адрес запроса Ads Manager к Graph с токеном в строке запроса (Resource Timing).
GRAPH_WITH_TOKEN = f"https://graph.facebook.com/v22.0/act_1/campaigns?fields=name&access_token={TOKEN}"


def test_helper_takes_token_from_resource_timing_without_any_request():
    out = _run(resources=["https://cdn.example.com/x.js", GRAPH_WITH_TOKEN])
    assert out["world_keys"] == ["__acl_graph_iso"]          # один ключ, свой мир
    assert out["scan_made_no_fetch"] and out["scan_still_no_fetch"]
    st = out["before"]
    assert st["has_token"] is True and st["source"] == "resource_timing"
    assert st["graph_host"] == "graph.facebook.com" and st["version"] == "v22.0"
    assert st["am_request"] == {"via": "resource_timing", "method": "GET", "credentials": "include",
                                "path": "/v22.0/act_1/campaigns", "params": ["fields"]}
    # GET-only, пути и варианты закрыты; один запрос; токен приложен страницей.
    assert out["post"] == {"error": "read_only"} and out["bad"] == {"error": "bad_path"}
    assert all(v == {"error": "forbidden_param"} for v in out["forbidden"].values())
    assert out["forbidden_fetches"] == 0
    assert out["got"]["status"] == 200
    assert out["fetched_head"] == "https://graph.facebook.com/v22.0/act_1/campaigns"
    assert out["fetched_has_token"] and out["fetched_cred"] == "include"
    assert out["fetched_method"] == "GET" and out["fetched_redirect"] == "error"
    assert out["got"]["headers"] == {"x-ad-account-usage": '{"acc_id_util_pct":12}'}
    assert "access_token=<hidden>" in json.dumps(out["got"]) and "<token>" in json.dumps(out["got"])
    assert out["opt_public_head"] == "https://graph.facebook.com/v22.0/me"
    assert out["opt_public_cred"] == "omit"
    assert out["opt_bad_host"] == {"error": "bad_option"} and out["opt_bad_cred"] == {"error": "bad_option"}
    assert out["after_retire"] == {"error": "retired"} and out["retired_state"] is True
    assert out["token_in_output"] is False


def test_helper_takes_token_from_inline_html_only_in_probe_not_the_poll():
    out = _run(html='<script>window.__x={"accessToken":"' + TOKEN + '"}</script>')
    # Опрос state() не сериализует DOM: HTML читается только в финальном probe (ревью r9-2).
    assert out["before"]["has_token"] is False
    assert out["after_probe"]["has_token"] is True and out["after_probe"]["source"] == "html"
    assert out["got"]["status"] == 200
    assert out["token_in_output"] is False


def test_helper_takes_token_from_inline_script_text():
    out = _run(scripts=[{"src": "", "textContent": 'require({"accessToken":"' + TOKEN + '"})'}])
    assert out["after_probe"]["has_token"] is True and out["after_probe"]["source"] == "html"


def test_helper_ignores_a_bare_token_outside_the_accessToken_key():
    # Голый EAA… без ключа "accessToken" НЕ берём (ревью r9-2, O5): убрали /(EAA…)/.
    out = _run(scripts=[{"src": "", "textContent": 'var t = "' + TOKEN + '";'}],
               html='<div data-x="' + TOKEN + '"></div>')
    assert out["before"]["has_token"] is False and out["after_probe"]["has_token"] is False
    assert out["got"]["error"] == "no_token"


def test_helper_resource_timing_requires_a_token_shaped_value():
    # access_token с не-токеном в Resource Timing не захватываем (TOKEN_RE на пути).
    out = _run(resources=["https://graph.facebook.com/v22.0/me?access_token=not_a_valid_token"])
    assert out["before"]["has_token"] is False and out["after_probe"]["has_token"] is False
    assert out["got"]["error"] == "no_token"


def test_helper_reports_no_token_and_makes_no_request_when_none_present():
    out = _run(resources=["https://graph.facebook.com/v22.0/me?fields=id"])  # нет access_token
    assert out["before"]["has_token"] is False
    assert out["got"] == {"error": "no_token", "state": out["got"].get("state")} or out["got"]["error"] == "no_token"
    assert out["fetch_count"] == 0


def test_helper_network_failure_makes_one_request_and_no_retry():
    out = _run(resources=[GRAPH_WITH_TOKEN], net="fail")
    assert out["got"]["error"] == "network"
    assert out["fetch_count"] == 1


# --- якорь хоста Graph: токен только с graph.facebook.com / <слово>-graph.facebook.com ---

BAD_HOSTS = [
    "https://adsmanager.facebook.com/v22.0/me?access_token=" + TOKEN,
    "https://www.facebook.com/v22.0/me?access_token=" + TOKEN,
    "https://graph.facebook.com.evil.com/v22.0/me?access_token=" + TOKEN,
    "https://evil.com/graph.facebook.com/v22.0/me?access_token=" + TOKEN,
    "https://graph.facebook.com@evil.com/v22.0/me?access_token=" + TOKEN,
    "https://user:pw@graph.facebook.com/v22.0/me?access_token=" + TOKEN,
    "https://graph.facebook.com:8443/v22.0/me?access_token=" + TOKEN,
    "https://a.graph.facebook.com/v22.0/me?access_token=" + TOKEN,
    "http://graph.facebook.com/v22.0/me?access_token=" + TOKEN,
    "https://evilgraph.facebook.com/v22.0/me?access_token=" + TOKEN,
    "https://evil.com/r?u=https://graph.facebook.com/v22.0/me?access_token=" + TOKEN,
    # Адрес Graph внутри чужого адреса + НАСТОЯЩИЙ параметр access_token:
    # ловит потерю якоря ^ (без него regex нашёл бы вложенный graph-хост).
    "https://evil.com/x?next=https://graph.facebook.com/v22.0/me&access_token=" + TOKEN,
]
GOOD_HOSTS = {
    "https://graph.facebook.com/v22.0/me?access_token=" + TOKEN: ("graph.facebook.com", "v22.0"),
    "https://adsmanager-graph.facebook.com/v23.0/act_1/x?access_token=" + TOKEN:
        ("adsmanager-graph.facebook.com", "v23.0"),
    "HTTPS://Graph.Facebook.com/v21.0/me?access_token=" + TOKEN: ("graph.facebook.com", "v21.0"),
}


@pytest.mark.parametrize("url", BAD_HOSTS)
def test_helper_refuses_token_from_a_non_graph_host(url):
    out = _run(resources=[url])
    assert out["before"]["has_token"] is False, url
    assert out["fetch_count"] == 0, url


@pytest.mark.parametrize("url,expected", list(GOOD_HOSTS.items()))
def test_helper_takes_token_from_the_graph_hosts(url, expected):
    out = _run(resources=[url])
    host, version = expected
    assert out["before"]["has_token"] is True, url
    assert out["before"]["graph_host"] == host and out["before"]["version"] == version
