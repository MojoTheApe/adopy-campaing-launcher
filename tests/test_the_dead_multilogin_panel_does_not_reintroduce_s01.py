"""ACL-1342 S01 — независимое ревью PR #343 (2026-09-28), информационная
находка: в `report.html` есть неиспользуемый блок JS «Multilogin connect»
(`mlTogglePanel`/`mlSetInstance`/`mlTokenFirst`/`mlListProfiles`/`mlSignin`
и т.д., ~8400+) — заменён карт­ой аккаунтов (`amap*`), ни один элемент
разметки на него больше не ссылается (`onclick="mlTogglePanel(...)"` /
`onclick="mlSetInstance(...)"` нигде не встречаются), поэтому сейчас это не
действующая уязвимость. Но внутри блока тот же паттерн, что чинил S01:
`onclick="mlListProfiles(\\'' + cid + '\\')"` — cid конкатенировался прямо в
JS-строку onclick. Если блок когда-нибудь переподключат к разметке, дыра
вернётся ровно там, где её один раз уже закрывали.

Решение: не удалять (код большой, связей на удивление много — `_mlSaveIds`
пишет в `config/ad_account_map.csv`, и трогать логику в security-фиксе
рискованнее, чем оставить мёртвый, но безопасный код), а закрыть тем же
способом, что и живой S01-фикс — data-* атрибут вместо конкатенации,
чтение через `this.dataset` на клике. Три места правлены одинаково
(`mlTokenFirst`'s "alive token" branch, и по одному в `mlListProfiles` и
`mlSignin`'s "no profiles found" fallback).

Тест — не пересказ по памяти: `mlTokenFirst` вырезан из настоящего
`report.html` и исполнен в offline Chromium с подменённым `fetch`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPORT = ROOT / "src" / "web" / "templates" / "report.html"

# Тот же приём, что и в живом S01-тесте: кавычка + '); ... // — разрыв
# JS-строкового литерала внутри HTML-атрибута.
MALICIOUS_CID = 'x\');window["aclPwned"]=1;//'


def _extract_source() -> str:
    src = REPORT.read_text(encoding="utf-8")

    esc_start = src.index("function aclEscHtml(value) {")
    esc_end = src.index("\n    }\n", esc_start) + len("\n    }")
    esc = src[esc_start:esc_end]

    labels_start = src.index("var ACL_MLX_LABELS")
    token_first_start = src.index("function mlTokenFirst(cid) {")
    token_first_end = src.index("\n}\n", token_first_start) + len("\n}")
    block = src[labels_start:token_first_end]

    for needed in ("mlLabel", "_mlInstance", "_mlTokenChecked", "function mlTokenFirst",
                   "mlListProfiles(this.dataset.cid)", "data-cid="):
        assert needed in block, f"срез не захватил {needed!r}"

    return esc + "\n" + block


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    src = _extract_source().replace("{{ mlx | tojson }}", "{}")
    ctx = browser.new_context()
    errors = []
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.set_content("<div id='root'></div>")
    # mlTokenFirst's own logic (building banner.innerHTML) is what's under
    # test, not the network — stub fetch directly rather than routing, and
    # stub mlListProfiles(cid) (called on an alive token) to a no-op so it
    # doesn't need a second network round trip.
    pg.evaluate(
        "window.fetch = function() { return Promise.resolve({ "
        "json: function() { return Promise.resolve("
        "{alive: true, expires_at: null, has_token: true}); } }); };"
    )
    pg.add_script_tag(content=src)
    pg.evaluate("window.mlListProfiles = function() {};")
    yield pg, errors
    ctx.close()


def _run_token_first(pg, cid: str) -> str:
    """DOM-элементы созданы через свойство `.id` (не innerHTML) — их id
    гарантированно равен ровно тому, что просит `getElementById`, без
    зависимости от того, как cid парсился бы в HTML-разметке."""
    pg.evaluate(
        """(cid) => {
            var banner = document.createElement('div');
            banner.id = 'ml-token-banner-' + cid;
            var creds = document.createElement('div');
            creds.id = 'ml-cred-fields-' + cid;
            creds.style.display = 'block';
            document.getElementById('root').append(banner, creds);
            mlTokenFirst(cid);
        }""",
        cid,
    )
    pg.wait_for_function(
        "(cid) => { var b = document.getElementById('ml-token-banner-' + cid); "
        "return b && b.innerHTML.includes('Connected'); }",
        arg=cid,
    )
    return pg.evaluate(
        "(cid) => document.getElementById('ml-token-banner-' + cid).innerHTML", cid
    )


def test_a_malicious_cid_never_builds_a_concatenated_onclick(page):
    pg, errors = page
    banner_html = _run_token_first(pg, MALICIOUS_CID)

    assert "onclick=\"mlListProfiles('" not in banner_html, (
        "cid снова конкатенируется прямо в onclick — тот же класс бага, что и S01"
    )
    assert "data-cid=" in banner_html
    # Кавычка/скобки не встретились нигде в живом коде onclick — только
    # внутри значения data-* атрибута (инертные данные, а не JS-текст).
    assert 'window["aclPwned"]=1' not in banner_html.split('data-cid=')[0]

    # Сам атрибут при этом хранит значение как есть (браузер декодировал
    # HTML-сущности при разборе, как и положено обычному атрибуту) — round
    # trip через getAttribute, а не по сериализованному innerHTML, которое
    # само решает, что ре-экранировать при выводе.
    got = pg.evaluate(
        "(cid) => document.getElementById('ml-token-banner-' + cid)"
        ".querySelector('.ml-connect-btn').getAttribute('data-cid')",
        MALICIOUS_CID,
    )
    assert got == MALICIOUS_CID

    pwned = pg.evaluate("window.aclPwned || 0")
    assert pwned == 0, "просто отрисовка баннера не должна исполнять код"
    assert not errors, f"страница упала: {errors}"


def test_an_ordinary_cid_still_produces_a_working_button(page):
    pg, errors = page
    banner_html = _run_token_first(pg, "sa")

    assert 'data-cid="sa"' in banner_html
    assert not errors, f"страница упала: {errors}"
