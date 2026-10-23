# -*- coding: utf-8 -*-
"""Проблемное состояние кабинета приходит со снимком и кнопкой Copy screenshot (ACL-1430).

01.10 владелец попросил доказательство для поддержки: Doc20 disabled, 6540
restricted. До этой правки снимок страницы оставался только при неудачном
чтении, путём к файлу в подсказке. Теперь чтение, нашедшее что угодно, кроме
`clear`, снимает видимую часть Ads Manager — внутри того же профиля Multilogin,
другой страницы у чтения нет, — а карта даёт кнопку, которая кладёт картинку
в буфер обмена.

Живой кабинет здесь не открывается: страница подставная.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis_agent import account_spend as sp  # noqa: E402
from tests.profile_lease_fakes import fake_profile

DAY = "2026-09-30"
ACC = "FacebookDoc20 - Example"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\xa5\xf6E@\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def local(tmp_path, monkeypatch):
    """Хранилище и папка снимков — во временной папке, не в %LOCALAPPDATA%."""
    monkeypatch.setattr(sp.paths, "get_local_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "account_spend.json")
    return tmp_path


def _read(monkeypatch, status, *, grid=True, shots_on=True):
    # ACL-1535: снимок состояния выключен по умолчанию; эти тесты проверяют
    # включённый (`ACL_STATUS_SHOT=1`) путь — прежнее поведение.
    if shots_on:
        monkeypatch.setenv("ACL_STATUS_SHOT", "1")
    else:
        monkeypatch.delenv("ACL_STATUS_SHOT", raising=False)
    shots = []

    def screenshot(path, full_page=False):
        assert full_page is False, "снимок — видимая часть, баннер и колонка Delivery"
        shots.append(path)
        Path(path).write_bytes(PNG)

    page = NS(url="https://adsmanager.facebook.com/?act=111", goto=lambda *a, **k: None,
              close=lambda: None, screenshot=screenshot)
    monkeypatch.setattr(sp, "_entry_for", lambda _: NS(ad_account_id="111", profile_id=fake_profile(ACC)))
    monkeypatch.setattr(sp, "_attach_only", lambda *a: NS(page=page, close=lambda **k: None))
    monkeypatch.setattr(sp, "_wait_for_grid", lambda _: grid)
    monkeypatch.setattr(sp, "_read_status", lambda _: status)
    monkeypatch.setattr(sp, "_shown_period", lambda _: DAY)
    monkeypatch.setattr(sp, "_read_total", lambda _: (7.0, "test"))
    monkeypatch.setattr(sp, "_read_tree", lambda *a, **k: ([], "", ""))
    return sp.read_via_rpa(ACC, DAY, DAY), shots


# ── снимок ───────────────────────────────────────────────────────────────

def test_a_disabled_read_saves_a_screenshot(local, monkeypatch):
    out, shots = _read(monkeypatch, ("disabled", "Account disabled"))
    assert out.state == "ok" and out.status == "disabled"
    assert len(shots) == 1
    assert out.status_shot and out.status_shot_at
    assert sp.status_shot_path(out.status_shot) == Path(shots[0]).resolve()
    assert Path(shots[0]).parent == local / "spend_shots"
    assert out.shot == "", "снимок неудачи — другое поле, у удачного чтения его нет"


def test_the_status_entry_keeps_the_shot(local, monkeypatch):
    out, _ = _read(monkeypatch, ("restricted", "Verification required to advertise"))
    sp._put(out)
    got = sp.get_status(ACC)
    assert got["status"] == "restricted"
    assert got["shot_id"] == out.status_shot
    assert got["shot_at"] == out.status_shot_at


def test_a_clear_read_takes_no_screenshot(local, monkeypatch):
    out, shots = _read(monkeypatch, ("clear", ""))
    assert out.state == "ok" and shots == []
    assert out.status_shot == ""
    sp._put(out)
    assert sp.get_status(ACC)["shot_id"] == ""


def test_a_clear_read_replaces_the_old_problem_shot_on_the_map(local, monkeypatch):
    bad, _ = _read(monkeypatch, ("disabled", "Account disabled"))
    sp._put(bad)
    good, _ = _read(monkeypatch, ("clear", ""))
    sp._put(good)
    assert sp.get_status(ACC)["shot_id"] == "", "кнопка у «no warning» доказывала бы прошлое"


def test_a_problem_seen_on_a_stuck_page_is_shot_too(local, monkeypatch):
    out, shots = _read(monkeypatch, ("disabled", "Account disabled"), grid=False)
    assert out.state == "failed"
    assert out.status_shot, "у отключённого кабинета таблицы может не быть — снимок тогда и нужен"
    assert out.shot, "снимок неудачи остаётся как был"


def test_a_failed_screenshot_does_not_break_the_read(local, monkeypatch):
    monkeypatch.setenv("ACL_STATUS_SHOT", "1")

    def boom(**_k):
        raise RuntimeError("page closed")
    page = NS(url="https://adsmanager.facebook.com/?act=111", goto=lambda *a, **k: None,
              close=lambda: None, screenshot=boom)
    monkeypatch.setattr(sp, "_entry_for", lambda _: NS(ad_account_id="111", profile_id=fake_profile(ACC)))
    monkeypatch.setattr(sp, "_attach_only", lambda *a: NS(page=page, close=lambda **k: None))
    monkeypatch.setattr(sp, "_wait_for_grid", lambda _: True)
    monkeypatch.setattr(sp, "_read_status", lambda _: ("disabled", "Account disabled"))
    monkeypatch.setattr(sp, "_shown_period", lambda _: DAY)
    monkeypatch.setattr(sp, "_read_total", lambda _: (7.0, "test"))
    monkeypatch.setattr(sp, "_read_tree", lambda *a, **k: ([], "", ""))
    out = sp.read_via_rpa(ACC, DAY, DAY)
    assert out.state == "ok" and out.status == "disabled"
    assert out.status_shot == ""


# ── ACL-1535: по умолчанию снимка нет ────────────────────────────────────
#
# ACL-1532: `page.screenshot` сбрасывал override вьюпорта профиля Multilogin
# (1919×1079 → 3837×2157), а размер окна — часть отпечатка. Без
# `ACL_STATUS_SHOT=1` чтение страницу не снимает ни Playwright, ни CDP.

def _spy_read(monkeypatch, status, *, env=None, **fakes):
    if env is None:
        monkeypatch.delenv("ACL_STATUS_SHOT", raising=False)
    else:
        monkeypatch.setenv("ACL_STATUS_SHOT", env)
    calls = []

    def screenshot(path=None, **_k):
        calls.append("screenshot")
        if path:
            Path(path).write_bytes(PNG)

    def new_cdp_session(*_a, **_k):
        calls.append("new_cdp_session")
        raise AssertionError("CDP-сессия во время чтения состояния")

    ctx = NS(new_cdp_session=new_cdp_session)
    page = NS(url="https://adsmanager.facebook.com/?act=111", goto=lambda *a, **k: None,
              close=lambda: None, screenshot=screenshot, context=ctx,
              new_cdp_session=new_cdp_session)
    monkeypatch.setattr(sp, "_entry_for", lambda _: NS(ad_account_id="111", profile_id=fake_profile(ACC)))
    monkeypatch.setattr(sp, "_attach_only", lambda *a: NS(page=page, close=lambda **k: None))
    monkeypatch.setattr(sp, "_wait_for_grid", lambda _: True)
    monkeypatch.setattr(sp, "_read_status", lambda _: status)
    monkeypatch.setattr(sp, "_shown_period", lambda _: DAY)
    monkeypatch.setattr(sp, "_read_total", lambda _: (7.0, "test"))
    monkeypatch.setattr(sp, "_read_tree", lambda *a, **k: ([], "", ""))
    for name, value in fakes.items():
        monkeypatch.setattr(sp, name, value)
    return sp.read_via_rpa(ACC, DAY, DAY), calls


@pytest.mark.parametrize("status", [("disabled", "Account disabled"), ("restricted", "Verification"),
                                    ("unknown", ""), ("clear", "")])
@pytest.mark.parametrize("env", [None, "", "0", "true"])
def test_by_default_a_status_read_takes_no_screenshot(local, monkeypatch, status, env):
    out, calls = _spy_read(monkeypatch, status, env=env)
    assert out.state == "ok" and out.status == status[0]
    assert calls == [], "снимок или CDP во время чтения состояния"
    assert out.status_shot == "" and out.status_shot_at == ""
    assert not (local / "spend_shots").exists() or not list((local / "spend_shots").glob("*.png"))


def test_with_the_switch_on_the_old_behaviour_returns(local, monkeypatch):
    out, calls = _spy_read(monkeypatch, ("disabled", "Account disabled"), env="1")
    assert calls == ["screenshot"]
    assert out.status_shot and sp.status_shot_path(out.status_shot) is not None


def test_a_read_without_a_shot_keeps_the_last_stored_shot(local, monkeypatch):
    old, _ = _spy_read(monkeypatch, ("disabled", "Account disabled"), env="1")
    sp._put(old)
    new, calls = _spy_read(monkeypatch, ("restricted", "Verification required"))
    assert calls == []
    assert (new.status_shot, new.status_shot_at) == (old.status_shot, old.status_shot_at), \
        "живой Read отдаёт экрану прежний снимок"
    sp._put(new)
    got = sp.get_status(ACC)
    assert got["status"] == "restricted"
    assert (got["shot_id"], got["shot_at"]) == (old.status_shot, old.status_shot_at)


def test_a_read_without_any_stored_shot_has_none(local, monkeypatch):
    out, calls = _spy_read(monkeypatch, ("disabled", "Account disabled"))
    assert calls == [] and out.status_shot == ""
    sp._put(out)
    assert sp.get_status(ACC)["shot_id"] == ""


@pytest.mark.parametrize("failure", ["grid", "period", "total"])
def test_by_default_a_failed_read_takes_no_screenshot(local, monkeypatch, failure):
    """Снимок неудачи (`_shoot`) — тот же `page.screenshot`; по умолчанию его нет."""
    fakes = {
        "grid": {"_wait_for_grid": lambda _: False},
        "period": {"_shown_period": lambda _: "2026-01-01"},
        "total": {"_read_total": lambda _: (None, "no totals row")},
    }[failure]
    out, calls = _spy_read(monkeypatch, ("disabled", "Account disabled"), **fakes)
    assert out.state == "failed" and out.error
    assert calls == [], "снимок или CDP при неудачном чтении"
    assert out.shot == "" and out.status_shot == ""


def test_with_the_switch_on_a_failed_read_is_shot_as_before(local, monkeypatch):
    monkeypatch.setenv("ACL_STATUS_SHOT", "1")
    shots = []
    page = NS(screenshot=lambda path, full_page=False: (shots.append(path), Path(path).write_bytes(PNG)))
    assert sp._shoot(page, ACC, DAY) and len(shots) == 1


def test_country_split_failure_shot_is_gated_too(local, monkeypatch):
    """`country_split` берёт `_shoot` из account_spend — выключатель общий."""
    from src.analysis_agent import country_split as cs

    monkeypatch.delenv("ACL_STATUS_SHOT", raising=False)
    calls = []
    page = NS(screenshot=lambda *a, **k: calls.append("screenshot"),
              new_cdp_session=lambda *a, **k: calls.append("new_cdp_session"))
    assert cs._shoot(page, ACC, DAY) == "" and calls == []


def test_only_the_last_few_shots_per_account_are_kept(local, monkeypatch):
    clock = iter(range(100))
    monkeypatch.setattr(sp, "_shot_stamp", lambda: f"20261001-1200{next(clock):02d}")
    page = NS(screenshot=lambda path, full_page=False: Path(path).write_bytes(PNG))
    ids = [sp._status_shot(page, ACC)[0] for _ in range(sp.STATUS_SHOTS_KEEP + 3)]
    other = sp._status_shot(page, "FacebookDoc2")[0]
    left = sorted(p.stem for p in (local / "spend_shots").glob("*.png"))
    assert left == sorted(ids[-sp.STATUS_SHOTS_KEEP:] + [other]), \
        "чужой аккаунт с похожим именем не чистится вместе с этим"


# ── отдача снимка: только по id и только из папки ────────────────────────

@pytest.mark.parametrize("bad", [
    "", "..", "../account_spend", "..\\account_spend", "status_x/../../account_spend",  # linux-ok: a hostile id as data, never opened as a path on Linux
    "account_spend", "status_..", "status_a.png", "C:\\Windows\\win", "status_" + "a" * 300,  # linux-ok: a hostile id as data, never opened as a path on Linux
])
def test_a_shot_id_cannot_leave_the_folder(local, bad):
    (local / "account_spend.json").write_text("{}", encoding="utf-8")
    assert sp.status_shot_path(bad) is None


def test_an_unknown_shot_id_is_none(local):
    assert sp.status_shot_path("status_Nobody__20261001-120000") is None


class _Client:
    """GET через httpx.ASGITransport — как остальные тесты дешборда."""

    def __init__(self, base_url="http://127.0.0.1"):
        self.base_url = base_url

    def get(self, url):
        import asyncio

        import httpx

        from src.web.app import app

        async def go():
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url=self.base_url) as c:
                return await c.get(url)

        return asyncio.run(go())


@pytest.fixture
def client(local):
    return _Client()


def test_the_endpoint_serves_a_shot_by_its_id(local, client):
    page = NS(screenshot=lambda path, full_page=False: Path(path).write_bytes(PNG))
    shot_id, _at = sp._status_shot(page, ACC)
    r = client.get(f"/account-spend/status-shot/{shot_id}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG


@pytest.mark.parametrize("bad", [
    "status_Nobody__20261001-120000",
    "..%2Faccount_spend.json",
    "..%5Caccount_spend.json",
    "%2E%2E",
    "status_x%2F..%2F..%2Faccount_spend",
])
def test_the_endpoint_refuses_unknown_ids_and_paths(local, client, bad):
    (local / "account_spend.json").write_text('{"secret": 1}', encoding="utf-8")
    r = client.get(f"/account-spend/status-shot/{bad}")
    assert r.status_code == 404
    assert b"secret" not in r.content


# ── настоящий PNG за пределами папки (ACL-1486) ──────────────────────────
#
# Тесты выше прошли бы и без защит: за пределами `spend_shots` в них не было
# ни одного .png, так что «наивная» отдача тоже вернула бы 404. Здесь рядом с
# папкой лежат настоящие снимки, и каждый маршрут сначала проверяет, что без
# защит файл БЫЛ БЫ найден (`_naive`), — иначе тест ничего не доказывает.

OTHER_PNG = PNG + b"outside"  # отличимые байты: в ответе их быть не должно


def _outside(local: Path) -> dict[str, str]:
    """Разложить настоящие PNG вне папки снимков; вернуть id-маршрут → id.

    Маршруты с обратной косой («*-backslash») живут в
    tests/test_status_screenshot_backslash_windows.py: `\` — разделитель
    только на Windows (ACL-1582)."""
    (local / "spend_shots").mkdir(exist_ok=True)
    (local / "escape.png").write_bytes(OTHER_PNG)                       # родитель
    (local / "spend_shots_evil").mkdir(exist_ok=True)                   # сосед с похожим именем
    (local / "spend_shots_evil" / "status_x.png").write_bytes(OTHER_PNG)
    far = local.parent / f"{local.name}-elsewhere"                      # совсем другая папка
    far.mkdir(exist_ok=True)
    (far / "escape.png").write_bytes(OTHER_PNG)
    far_id = str(far / "escape")
    return {
        "parent": "../escape",
        "parent-through-subdir": "status_x/../../escape",
        "sibling": "../spend_shots_evil/status_x",
        "absolute": far_id,
    }


ROUTES = ["parent", "parent-through-subdir", "sibling", "absolute"]


def _naive(local: Path, shot_id: str) -> Path:
    """Что отдал бы `status_shot_path` без единой защиты."""
    return (local / "spend_shots" / f"{shot_id}.png").resolve()


def _loose_names(monkeypatch):
    """Ослабить проверку имени: так видно, держит ли вторая защита (папка) сама."""
    monkeypatch.setattr(sp, "_STATUS_SHOT_ID", re.compile(r".+", re.S))


@pytest.mark.parametrize("route", ROUTES)
def test_a_real_png_outside_the_folder_is_not_reachable_by_id(local, route):
    shot_id = _outside(local)[route]
    assert _naive(local, shot_id).read_bytes() == OTHER_PNG, "без защит файл нашёлся бы"
    assert sp.status_shot_path(shot_id) is None


@pytest.mark.parametrize("route", ROUTES)
def test_the_folder_check_holds_even_if_the_name_check_lets_a_path_through(local, monkeypatch, route):
    shot_id = _outside(local)[route]
    assert _naive(local, shot_id).read_bytes() == OTHER_PNG, "без защит файл нашёлся бы"
    _loose_names(monkeypatch)
    assert sp.status_shot_path(shot_id) is None
    # контроль: ослабленная проверка имени не ломает законный снимок
    (local / "spend_shots" / "status_ok__20261001-120000.png").write_bytes(PNG)
    assert sp.status_shot_path("status_ok__20261001-120000") is not None


def _encodings(shot_id: str) -> list[str]:
    from urllib.parse import quote

    once = quote(shot_id, safe="")
    return [shot_id, once, quote(once, safe="")]


@pytest.mark.parametrize("loose", [False, True], ids=["strict-names", "loose-names"])
@pytest.mark.parametrize("tail", ["", "/view"], ids=["image", "view"])
@pytest.mark.parametrize("route", ROUTES)
def test_the_endpoint_refuses_a_real_png_outside_the_folder(local, client, monkeypatch, route, tail, loose):
    shot_id = _outside(local)[route]
    assert _naive(local, shot_id).read_bytes() == OTHER_PNG, "без защит файл нашёлся бы"
    if loose:
        _loose_names(monkeypatch)
    # как есть, URL-кодированный (%2F, %5C, %3A) и кодированный дважды
    for sent in _encodings(shot_id):
        r = client.get(f"/account-spend/status-shot/{sent}{tail}")
        assert r.status_code == 404, (sent, r.status_code)
        assert b"outside" not in r.content, sent


@pytest.mark.parametrize("name", ["escape", "FacebookDoc20_2026-09-30"])
def test_a_png_in_the_folder_that_is_not_a_status_shot_is_refused(local, client, name):
    # Папка общая со снимками неудачного чтения (`_shoot`): отдаём только status_*.
    folder = local / "spend_shots"
    folder.mkdir()
    (folder / f"{name}.png").write_bytes(OTHER_PNG)
    assert _naive(local, name).is_file()
    assert sp.status_shot_path(name) is None
    r = client.get(f"/account-spend/status-shot/{name}")
    assert r.status_code == 404 and b"outside" not in r.content


def test_a_folder_named_like_a_shot_is_not_a_shot(local, client):
    (local / "spend_shots" / "status_dir__20261001-120000.png").mkdir(parents=True)
    assert sp.status_shot_path("status_dir__20261001-120000") is None
    assert client.get("/account-spend/status-shot/status_dir__20261001-120000").status_code == 404


def test_the_fallback_page_shows_the_image_and_how_to_copy_it(local, client):
    page = NS(screenshot=lambda path, full_page=False: Path(path).write_bytes(PNG))
    shot_id, _at = sp._status_shot(page, ACC)
    r = client.get(f"/account-spend/status-shot/{shot_id}/view")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Right-click → Copy image" in r.text
    assert f'src="/account-spend/status-shot/{shot_id}"' in r.text
    assert client.get("/account-spend/status-shot/status_Nobody__20261001-120000/view").status_code == 404


def test_the_endpoint_is_local_only(local):
    r = _Client("http://evil.example").get("/account-spend/status-shot/status_x__20261001-120000")
    assert r.status_code == 400


# ── карта ────────────────────────────────────────────────────────────────

def _render(status: dict, show: bool = True) -> str:
    from jinja2 import Environment, FileSystemLoader

    from src.analysis_agent.money_format import fmt_currency

    env = Environment(loader=FileSystemLoader(str(ROOT / "src" / "web" / "templates")), autoescape=True)
    env.filters["fmt_currency"] = fmt_currency
    env.filters["fmt_usd"] = str
    country = {"country": "Kuwait", "key": "KW", "virtual": False,
               "rows": [{"account": ACC, "has_data": False, "ready": True,
                         "in_workbook": True, "status": status, "to_do": ""}]}
    # ACL-1652 (owner, 07.10): the button is hidden by default; these tests check the
    # markup it has when the flag is on, so they switch it on explicitly.
    html = env.get_template("_account_map_country.html").render(
        SHOW_COPY_FOR_GROUP=show, mc=country, mlx={},
        amap={"countries": [country], "available": [], "all_accounts": [], "live": {},
              "splits": {}, "fallback": {}, "dates": [], "date_from": "2026-09-01",
              "date_to": "2026-09-28", "span": ""})
    cell = html[html.index('<td class="amap-status">'):]
    return cell[: cell.index("</td>")]


def test_a_problem_status_with_a_shot_has_the_copy_button():
    """С ACL-1520 снимок проблемного статуса копирует Copy for group."""
    cell = _render({"status": "disabled", "status_text": "Account disabled", "at": "2026-09-30 10:00:00",
                    "at_short": "30.09.26", "stale": "", "shot_id": "status_Doc20__20260930-100001",
                    "shot_at": "2026-09-30 10:00:01"})
    assert "Copy for group" in cell
    assert 'data-shot="status_Doc20__20260930-100001"' in cell
    assert "the screenshot taken 2026-09-30 10:00:01" in cell


def test_an_unreadable_status_with_a_shot_keeps_copy_screenshot():
    cell = _render({"status": "unknown", "at": "2026-09-30 10:00:00", "at_short": "30.09.26", "stale": "",
                    "shot_id": "status_Doc20__20260930-100001", "shot_at": "2026-09-30 10:00:01"})
    assert "Copy screenshot" in cell and "Screenshot taken 2026-09-30 10:00:01" in cell
    assert "Copy for group" not in cell


def test_no_shot_no_button():
    """Нет снимка — нет рабочей кнопки: она есть, но выключена и говорит
    почему (Issue #31, ACL-1520 п.3)."""
    cell = _render({"status": "disabled", "status_text": "Account disabled", "at": "2026-09-30 10:00:00",
                    "at_short": "30.09.26", "stale": "", "shot_id": "", "shot_at": ""})
    buttons = re.findall(r'<button[^>]*class="amap-(?:shot|btn amap-copy-group)"[^>]*>', cell)
    assert buttons, "кнопки нет вовсе"
    assert all(" disabled" in b for b in buttons), "кнопка без снимка нажимается"
    assert all("data-shot" not in b and "onclick" not in b for b in buttons)


def test_a_clear_status_never_has_the_button():
    cell = _render({"status": "clear", "at": "2026-09-30 10:00:00", "at_short": "30.09.26", "stale": "",
                    "shot_id": "status_Doc20__20260930-100001", "shot_at": "2026-09-30 10:00:01"})
    assert "Copy screenshot" not in cell and "Copy for group" not in cell


# ── кнопка в браузере: буфер обмена и запасной путь ─────────────────────

def _copy_js() -> str:
    src = (ROOT / "src" / "web" / "templates" / "report.html").read_text(encoding="utf-8")
    m = re.search(r"/\* ACL-1430 copy-shot \{ \*/(.*?)/\* \} ACL-1430 copy-shot \*/", src, re.S)
    assert m, "блок кнопки снимка в report.html не найден"
    return m.group(1)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get("ACL_BROWSER_EXECUTABLE") or None)
        try:
            yield b
        finally:
            b.close()


_SHOT = "status_Doc20__20260930-100001"


def _page(browser, *, clipboard: str):
    """Карта с одной кнопкой; `clipboard`: ok | refuse | missing."""
    ctx = browser.new_context()

    def serve(route):
        url = route.request.url
        if url.endswith("/map"):
            route.fulfill(content_type="text/html", body=(
                '<!doctype html><html><body><td class="amap-status"><span class="amap-st bad">disabled</span>'
                f'<button type="button" class="amap-shot" data-shot="{_SHOT}" '
                'title="Screenshot taken 2026-09-30 10:00:01" onclick="amapCopyShot(this)">Copy screenshot</button>'
                f'</td><script>{_copy_js()}</script></body></html>'))
        elif url.endswith(f"/account-spend/status-shot/{_SHOT}"):
            route.fulfill(content_type="image/png", body=PNG)
        else:
            route.fulfill(status=404, body="")

    ctx.route("http://acl.test/**", serve)
    ctx.route(re.compile(r"^(?!http://acl\.test/).*"), lambda r: r.abort())
    stub = {
        "ok": """
          window.__copied = [];
          window.ClipboardItem = function (items) { this.items = items; this.types = Object.keys(items); };
          Object.defineProperty(navigator, 'clipboard', {value: {write: async function (list) {
            for (const item of list) for (const t of item.types) {
              const blob = await item.items[t];
              window.__copied.push({type: t, blobType: blob.type, size: blob.size});
            }
          }}});""",
        "refuse": """
          window.ClipboardItem = function (items) { this.items = items; this.types = Object.keys(items); };
          Object.defineProperty(navigator, 'clipboard', {value: {write: async function () {
            throw new DOMException('Write permission denied.', 'NotAllowedError');
          }}});""",
        "missing": "delete window.ClipboardItem;",
    }[clipboard]
    ctx.add_init_script("window.__opened = []; window.open = function (u, t) { window.__opened.push([u, t]); return null; };" + stub)
    page = ctx.new_page()
    page.goto("http://acl.test/map")
    return ctx, page


def test_the_button_copies_the_png(browser):
    ctx, page = _page(browser, clipboard="ok")
    try:
        page.click(".amap-shot")
        page.wait_for_function("window.__copied.length > 0")
        copied = page.evaluate("window.__copied")
        assert copied == [{"type": "image/png", "blobType": "image/png", "size": len(PNG)}]
        assert page.evaluate("window.__opened") == []
        assert page.text_content(".amap-shot") == "Copied"
    finally:
        ctx.close()


@pytest.mark.parametrize("clipboard", ["refuse", "missing"])
def test_a_refused_clipboard_opens_the_image_in_a_new_tab(browser, clipboard):
    ctx, page = _page(browser, clipboard=clipboard)
    try:
        page.click(".amap-shot")
        page.wait_for_function("window.__opened.length > 0")
        assert page.evaluate("window.__opened") == [[f"/account-spend/status-shot/{_SHOT}/view", "_blank"]]
    finally:
        ctx.close()
