"""The last finished dashboard page survives a restart (ACL-2207)."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import time
from pathlib import Path

import httpx
import pytest

from src.web import app, cache, report_store

ROOT = Path(__file__).resolve().parents[1]

KEY = {"code": {"version": "abc", "templates": "t1"}, "source": ["bi", "rev1", ["/b.xlsx", 1.5, 10]],
       "settings": {"env": {"BI_CURRENCY": "USD"}, "hidden": "h", "account_map": [1, 2]}}
INPUTS = {"cm": "Tester", "date_from": "2026-10-01", "date_to": "2026-10-07", "span": "",
          "compare": "", "cmp_from": "", "cmp_to": ""}
PAGE = "<html><head></head><body class='x'><h1>Report 42</h1></body></html>"


def entry_file() -> Path:
    return report_store.cache_dir() / report_store.entry_name(INPUTS)


def rewrite(mutator) -> None:
    """Edit the stored document in place, keeping it a well-formed gzip."""
    path = entry_file()
    doc = json.loads(gzip.decompress(path.read_bytes()))
    mutator(doc)
    path.write_bytes(gzip.compress(json.dumps(doc).encode("utf-8")))


# ── key change detection ───────────────────────────────────────────────

def test_same_key_is_current_even_after_json_round_trip():
    assert report_store.compare(json.loads(json.dumps(KEY)), KEY) == []


@pytest.mark.parametrize("part,change", [
    ("code", {"version": "def", "templates": "t1"}),
    ("code", {"version": "abc", "templates": "t2"}),
    ("source", ["bi", "rev2", ["/b.xlsx", 1.5, 10]]),
    ("source", ["bi", "rev1", ["/b.xlsx", 2.5, 10]]),
    ("settings", {"env": {"BI_CURRENCY": "EUR"}, "hidden": "h", "account_map": [1, 2]}),
    ("settings", {"env": {"BI_CURRENCY": "USD"}, "hidden": "h2", "account_map": [1, 2]}),
    ("settings", {"env": {"BI_CURRENCY": "USD"}, "hidden": "h", "account_map": [9, 2]}),
])
def test_each_changed_part_is_named(part, change):
    assert report_store.compare(KEY, {**KEY, part: change}) == [part]


def test_an_unverifiable_part_never_vouches_for_the_page():
    assert report_store.compare(KEY, {**KEY, "source": None}) == ["source"]


def test_number_changing_settings_are_part_of_the_key(monkeypatch):
    monkeypatch.delenv("BI_CURRENCY", raising=False)
    monkeypatch.delenv("STATS_TIMEZONE", raising=False)
    base = app._store_settings_key()
    monkeypatch.setenv("BI_CURRENCY", "EUR")
    assert app._store_settings_key() != base
    monkeypatch.delenv("BI_CURRENCY")
    assert app._store_settings_key() == base
    monkeypatch.setenv("STATS_TIMEZONE", "Asia/Dubai")
    assert app._store_settings_key() != base


def test_account_map_is_part_of_the_key(monkeypatch, tmp_path):
    amap = tmp_path / "map.csv"
    amap.write_text("a,b\n", encoding="utf-8")
    monkeypatch.setenv("META_AD_ACCOUNT_MAP_PATH", str(amap))
    base = app._store_settings_key()
    amap.write_text("a,b\nc,d\n", encoding="utf-8")
    assert app._store_settings_key() != base


def test_hidden_accounts_are_part_of_the_key(monkeypatch):
    from src.web import hidden_accounts

    base = app._store_settings_key()
    monkeypatch.setattr(hidden_accounts, "load", lambda: {"doc19"})
    assert app._store_settings_key() != base


# ── atomic write, corruption fallback ──────────────────────────────────

def test_round_trip_current_and_outdated():
    assert report_store.save(INPUTS, KEY, PAGE)
    got = report_store.load(INPUTS, KEY)
    assert got.html == PAGE and not got.outdated
    stale = report_store.load(INPUTS, {**KEY, "code": {"version": "new", "templates": "t1"}})
    assert stale.html == PAGE and stale.outdated and stale.reasons == ["code"]


def test_other_views_do_not_see_the_page():
    report_store.save(INPUTS, KEY, PAGE)
    assert report_store.load({**INPUTS, "cm": "Other"}, KEY) is None
    assert report_store.load({**INPUTS, "date_to": "2026-10-08"}, KEY) is None


def test_write_leaves_no_temp_files():
    report_store.save(INPUTS, KEY, PAGE)
    assert [p.name for p in report_store.cache_dir().iterdir()] == [entry_file().name]


def test_failed_write_keeps_the_previous_page(monkeypatch):
    report_store.save(INPUTS, KEY, PAGE)

    def crash(path, data):
        Path(path).with_name("half.tmp").write_bytes(data[:5])
        raise OSError("disk full")

    with monkeypatch.context() as patched:
        patched.setattr(report_store, "_atomic_write", crash)
        assert report_store.save(INPUTS, KEY, "<body>newer</body>") is False
    assert report_store.load(INPUTS, KEY).html == PAGE


@pytest.mark.parametrize("damage", ["truncated", "garbage", "bad_checksum", "schema", "no_html", "wrong_shape"])
def test_damaged_file_falls_back_to_rebuild(damage):
    report_store.save(INPUTS, KEY, PAGE)
    path = entry_file()
    if damage == "truncated":
        path.write_bytes(path.read_bytes()[:20])
    elif damage == "garbage":
        path.write_bytes(os.urandom(300))
    elif damage == "bad_checksum":
        rewrite(lambda d: d.update(html=d["html"] + "<script>evil()</script>"))
    elif damage == "schema":
        rewrite(lambda d: d.update(schema=99))
    elif damage == "no_html":
        rewrite(lambda d: d.pop("html"))
    else:
        path.write_bytes(gzip.compress(b"[1, 2, 3]"))
    assert report_store.load(INPUTS, KEY) is None  # no exception
    assert not path.exists()                        # and the bad file is gone


def test_a_pickle_is_never_executed(tmp_path):
    import pickle

    class Boom:
        def __reduce__(self):
            return (os.system, ("echo pwned > " + str(tmp_path / "pwned"),))

    report_store.save(INPUTS, KEY, PAGE)
    entry_file().write_bytes(pickle.dumps(Boom()))
    assert report_store.load(INPUTS, KEY) is None
    assert not (tmp_path / "pwned").exists()


def test_a_file_made_for_another_view_is_not_used():
    report_store.save(INPUTS, KEY, PAGE)
    rewrite(lambda d: d["inputs"].update(cm="Somebody else"))
    assert report_store.load(INPUTS, KEY) is None


def test_disabled_by_setting(monkeypatch):
    monkeypatch.setenv("ACL_REPORT_DISK_CACHE", "0")
    assert report_store.save(INPUTS, KEY, PAGE) is False
    assert report_store.load(INPUTS, KEY) is None
    assert not report_store.cache_dir().exists()


def test_a_key_that_cannot_be_determined_is_not_stored():
    assert report_store.save(INPUTS, {**KEY, "source": None}, PAGE) is False


# ── caps ───────────────────────────────────────────────────────────────

def test_old_entries_are_not_served_and_are_removed(monkeypatch):
    report_store.save(INPUTS, KEY, PAGE)
    now = time.time()
    monkeypatch.setattr(report_store.time, "time", lambda: now + 15 * 86400)
    assert report_store.load(INPUTS, KEY) is None
    assert not entry_file().exists()


def test_age_cap_is_configurable(monkeypatch):
    report_store.save(INPUTS, KEY, PAGE)
    monkeypatch.setenv("ACL_REPORT_CACHE_DAYS", "30")
    now = time.time()
    monkeypatch.setattr(report_store.time, "time", lambda: now + 20 * 86400)
    assert report_store.load(INPUTS, KEY) is not None


def test_expired_neighbours_are_pruned_on_save():
    other = {**INPUTS, "cm": "Old"}
    report_store.save(other, KEY, PAGE)
    old = report_store.cache_dir() / report_store.entry_name(other)
    long_ago = time.time() - 30 * 86400
    os.utime(old, (long_ago, long_ago))
    report_store.save(INPUTS, KEY, PAGE)
    assert not old.exists() and entry_file().exists()


def test_entry_count_cap_keeps_the_newest(monkeypatch):
    monkeypatch.setattr(report_store, "MAX_ENTRIES", 3)
    names = []
    for i in range(6):
        view = {**INPUTS, "cm": f"cm{i}"}
        report_store.save(view, KEY, PAGE)
        path = report_store.cache_dir() / report_store.entry_name(view)
        stamp = time.time() - 600 + i
        os.utime(path, (stamp, stamp))
        names.append(path.name)
    assert {p.name for p in report_store.cache_dir().iterdir()} == set(names[3:])


def test_total_size_cap(monkeypatch):
    page = "<body>" + os.urandom(20000).hex() + "</body>"
    report_store.save(INPUTS, KEY, page)
    one = entry_file().stat().st_size
    monkeypatch.setattr(report_store, "MAX_TOTAL_BYTES", int(one * 2.5))
    for i in range(4):
        report_store.save({**INPUTS, "cm": f"c{i}"}, KEY, page)
    total = sum(p.stat().st_size for p in report_store.cache_dir().iterdir())
    assert total <= one * 2.5


def test_one_oversized_page_is_not_stored(monkeypatch):
    monkeypatch.setattr(report_store, "MAX_ENTRY_BYTES", 200)
    assert report_store.save(INPUTS, KEY, "<body>" + os.urandom(2000).hex() + "</body>") is False
    assert not entry_file().exists()


def test_an_oversized_file_on_disk_is_refused_unread(monkeypatch):
    report_store.save(INPUTS, KEY, PAGE)
    monkeypatch.setattr(report_store, "MAX_ENTRY_BYTES", 10)
    assert report_store.load(INPUTS, KEY) is None


def test_large_page_loads_fast():
    page = "<html><body>" + "".join(
        f"<tr><td>Country {i % 40}</td><td>Account {i}</td><td>{i * 1.37:.2f}</td></tr>" for i in range(250_000)
    ) + "</body></html>"
    assert len(page) > 10_000_000
    assert report_store.save(INPUTS, KEY, page)
    t0 = time.perf_counter()
    got = report_store.load(INPUTS, KEY)
    assert got is not None and len(got.html) == len(page)
    assert time.perf_counter() - t0 < 3.0


# ── privacy and placement ──────────────────────────────────────────────

def test_cache_lives_in_the_machine_folder_not_the_repo():
    report_store.save(INPUTS, KEY, PAGE)
    directory = report_store.cache_dir().resolve()
    assert Path(os.environ["ACL_CONFIG_DIR"]).resolve() in directory.parents
    assert ROOT not in directory.parents
    assert not list((ROOT / "src").rglob("*.json.gz")) and not list((ROOT / "config").rglob("*.json.gz"))


def test_the_installer_and_support_bundle_never_pick_the_cache_up():
    iss = (ROOT / "packaging/windows/installer.iss").read_text(encoding="utf-8")
    diagnostics = (ROOT / "packaging/windows/diagnostics.py").read_text(encoding="utf-8")
    assert "report_cache" not in iss and "report_cache" not in diagnostics
    # The installer copies the build, never the machine folder's files.
    assert "{localappdata}\ACL\report_cache" not in iss


# ── what the person sees ───────────────────────────────────────────────

def test_cached_badge():
    out = report_store.decorate(report_store.StoredPage(PAGE, time.time() - 300))
    assert 'data-state="cached"' in out and "From cache, refreshing" in out
    assert "OUTDATED" not in out
    assert out.index("acl-stored-bar") > out.index("<body")
    assert out.endswith("</body></html>") and "<h1>Report 42</h1>" in out


def test_outdated_marker_is_prominent_and_says_why():
    out = report_store.decorate(report_store.StoredPage(PAGE, time.time() - 7200, ["source", "code"]))
    assert 'data-state="outdated"' in out and "OUTDATED" in out
    assert "newer statistics may be available" in out and "dashboard has been updated" in out
    assert "Do not rely on these numbers" in out
    assert "inset 0 0 0 4px" in out  # red frame around the whole page


def test_error_is_shown_and_polling_stops():
    out = report_store.decorate(report_store.StoredPage(PAGE, time.time()), error="<b>boom</b>")
    assert 'data-polling="0"' in out and "Refresh failed: &lt;b&gt;boom&lt;/b&gt;" in out
    assert "<b>boom</b>" not in out


# ── the page route ─────────────────────────────────────────────────────

@pytest.fixture
def route(monkeypatch):
    state = {"warm": 0, "error": None, "source": ["bi", "rev1", ["/b.xlsx", 1.5, 10]], "rendered": 0}
    monkeypatch.setattr(app, "_page_ready", lambda *a: False)
    monkeypatch.setattr(app, "_start_warm", lambda *a: state.__setitem__("warm", state["warm"] + 1))
    monkeypatch.setattr(app, "forget_background_error", lambda: state["error"])
    monkeypatch.setattr(app, "verified_source_key", lambda: state["source"])
    monkeypatch.setattr(app, "_store_code_key", lambda: KEY["code"])
    monkeypatch.setattr(app, "_store_settings_key", lambda: KEY["settings"])
    monkeypatch.setattr("src.web.buyer.get_selected_cm", lambda: "Tester")

    def live(request, **kw):
        state["rendered"] += 1
        return app.HTMLResponse("<body>LIVE PAGE or error page</body>")

    monkeypatch.setattr(app, "_render_report", live)
    return state


def get_page(**params):
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                     base_url="http://127.0.0.1") as client:
            return await client.get("/", params=params)
    return asyncio.run(request())


QUERY = {"date_from": "2026-10-01", "date_to": "2026-10-07"}


def test_no_stored_page_still_shows_the_loading_screen(route):
    response = get_page(**QUERY)
    assert "Getting your dashboard ready" in response.text and route["warm"] == 1


def test_matching_stored_page_is_served_at_once_while_rebuilding(route):
    report_store.save(INPUTS, KEY, PAGE)
    response = get_page(**QUERY)
    assert "Report 42" in response.text and "From cache, refreshing" in response.text
    assert "OUTDATED" not in response.text
    assert route["warm"] == 1 and route["rendered"] == 0


def test_stored_page_is_outdated_after_new_data(route):
    report_store.save(INPUTS, KEY, PAGE)
    route["source"] = ["bi", "rev2", ["/b.xlsx", 1.5, 10]]
    response = get_page(**QUERY)
    assert "Report 42" in response.text and 'data-state="outdated"' in response.text
    assert route["warm"] == 1


def test_unverified_data_revision_counts_as_outdated(route):
    report_store.save(INPUTS, KEY, PAGE)
    route["source"] = None
    assert 'data-state="outdated"' in get_page(**QUERY).text


def test_stored_page_is_outdated_after_new_code(route, monkeypatch):
    report_store.save(INPUTS, KEY, PAGE)
    monkeypatch.setattr(app, "_store_code_key", lambda: {"version": "newer", "templates": "t1"})
    assert 'data-state="outdated"' in get_page(**QUERY).text


def test_rebuild_failure_keeps_the_stored_report_and_shows_the_error(route):
    report_store.save(INPUTS, KEY, PAGE)
    route["error"] = "Stats workbook not found"
    response = get_page(**QUERY)
    assert "Report 42" in response.text
    assert "Refresh failed: Stats workbook not found" in response.text
    assert route["rendered"] == 0 and route["warm"] == 0


def test_rebuild_failure_without_a_stored_page_renders_the_normal_error(route):
    route["error"] = "Stats workbook not found"
    assert "LIVE PAGE" in get_page(**QUERY).text and route["rendered"] == 1


def test_damaged_store_falls_back_to_the_loading_screen(route):
    report_store.save(INPUTS, KEY, PAGE)
    entry_file().write_bytes(b"not a cache")
    assert "Getting your dashboard ready" in get_page(**QUERY).text


def test_warm_report_is_rendered_live_so_templates_reload_without_restart(route, monkeypatch):
    report_store.save(INPUTS, KEY, PAGE)
    monkeypatch.setattr(app, "_page_ready", lambda *a: True)
    response = get_page(**QUERY)
    assert "LIVE PAGE" in response.text and "From cache" not in response.text and route["warm"] == 0


# ── remembering the live page ──────────────────────────────────────────

class FakeResponse:
    def __init__(self, name="report.html", status=200, body=b"<body>fresh 7</body>"):
        self.template = type("T", (), {"name": name})()
        self.status_code, self.body = status, body


def wait_for(path: Path, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def remember(monkeypatch):
    state = {"source": KEY["source"]}
    monkeypatch.setattr(app, "entry_source_key", lambda: state["source"])
    monkeypatch.setattr(app, "_store_code_key", lambda: KEY["code"])
    monkeypatch.setattr(app, "_store_settings_key", lambda: KEY["settings"])
    return state


def test_live_page_is_saved_with_the_key_it_was_built_under(remember, monkeypatch):
    monkeypatch.setattr(app, "_render_report", lambda request, **kw: FakeResponse())
    app._render_and_remember(None, INPUTS)
    assert wait_for(entry_file())
    got = report_store.load(INPUTS, KEY)
    assert got.html == "<body>fresh 7</body>" and not got.outdated


@pytest.mark.parametrize("response", [FakeResponse(name="error.html"), FakeResponse(status=503)])
def test_error_pages_are_not_saved(remember, monkeypatch, response):
    monkeypatch.setattr(app, "_render_report", lambda request, **kw: response)
    app._render_and_remember(None, INPUTS)
    time.sleep(0.3)
    assert not entry_file().exists()


def test_page_built_while_the_data_moved_is_not_saved(remember, monkeypatch):
    def render(request, **kw):
        remember["source"] = ["bi", "rev2", None]  # a rebuild landed mid-render
        return FakeResponse()

    monkeypatch.setattr(app, "_render_report", render)
    app._render_and_remember(None, INPUTS)
    time.sleep(0.3)
    assert not entry_file().exists()


def test_nothing_in_memory_nothing_saved(remember, monkeypatch):
    remember["source"] = None
    monkeypatch.setattr(app, "_render_report", lambda request, **kw: FakeResponse())
    app._render_and_remember(None, INPUTS)
    time.sleep(0.3)
    assert not entry_file().exists()


# ── source key verification after a restart ────────────────────────────

def test_source_key_is_unverified_when_bi_does_not_answer(monkeypatch, tmp_path):
    book = tmp_path / "b.xlsx"
    book.write_bytes(b"x")
    monkeypatch.setattr(cache, "_resolve_excel_path", lambda _p=None: str(book))
    monkeypatch.setattr(cache.bi_stats, "bi_enabled", lambda: True)
    monkeypatch.setattr(cache.bi_stats, "known_revision", lambda: None)
    monkeypatch.setattr(cache.bi_stats, "refresh_revision", lambda **kw: time.sleep(2))
    t0 = time.perf_counter()
    assert cache.verified_source_key(timeout=0.2) is None
    assert time.perf_counter() - t0 < 1.5


def test_source_key_of_unavailable_bi_is_unverified(monkeypatch, tmp_path):
    book = tmp_path / "b.xlsx"
    book.write_bytes(b"x")
    monkeypatch.setattr(cache, "_resolve_excel_path", lambda _p=None: str(book))
    monkeypatch.setattr(cache.bi_stats, "bi_enabled", lambda: True)
    monkeypatch.setattr(cache.bi_stats, "known_revision", lambda: "unavailable")
    assert cache.verified_source_key(timeout=0.1) is None


def test_source_key_without_bi_is_the_workbook_version(monkeypatch, tmp_path):
    book = tmp_path / "b.xlsx"
    book.write_bytes(b"x")
    monkeypatch.setattr(cache, "_resolve_excel_path", lambda _p=None: str(book))
    monkeypatch.setattr(cache.bi_stats, "bi_enabled", lambda: False)
    assert cache.verified_source_key()[0] == os.path.abspath(book)
