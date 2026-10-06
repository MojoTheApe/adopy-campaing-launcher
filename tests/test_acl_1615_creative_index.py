"""ACL-1615: поиск креатива идёт по индексу, а /creative-file не блокирует цикл.

До фикса каждый предпросмотр делал `root.rglob("*")` прямо в цикле событий,
и пока Builder грузил видео, остальные запросы ждали 10-30 с.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.meta_campaign_agent import creative_finder as cf  # noqa: E402
from src.meta_campaign_agent.creative_finder import (  # noqa: E402
    AmbiguousCreativeError,
    CreativeNotFoundError,
    find_creative_file,
)

_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _clean_index():
    cf.reset_creative_index()
    yield
    cf.reset_creative_index()


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "A" / "1_es_2292_post.mp4").write_bytes(_MP4)
    (tmp_path / "B" / "1_es_2292_post.mp4").write_bytes(_MP4)
    (tmp_path / "A" / "solo.mp4").write_bytes(_MP4)
    return tmp_path


def _count_scandir(monkeypatch):
    calls = []
    real = os.scandir

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(cf.os, "scandir", counting)
    return calls


def test_one_tree_walk_serves_many_lookups(tree, monkeypatch):
    calls = _count_scandir(monkeypatch)
    for _ in range(20):
        assert find_creative_file(tree, "solo.mp4").name == "solo.mp4"
        assert find_creative_file(tree, "SOLO.MP4").name == "solo.mp4"
    assert len(calls) == 3  # root + A + B, один раз


def test_duplicates_are_ambiguous_and_suffix_disambiguates(tree):
    with pytest.raises(AmbiguousCreativeError):
        find_creative_file(tree, "1_es_2292_post.mp4")
    assert find_creative_file(tree, "A/1_es_2292_post.mp4").parent.name == "A"
    assert find_creative_file(tree, "b\\1_es_2292_post.mp4").parent.name == "B"  # linux-ok: бэкслэш в имени — данные, basename берётся из нормализованного имени


def test_not_found_is_404_class(tree):
    with pytest.raises(CreativeNotFoundError):
        find_creative_file(tree, "nope.mp4")


def test_new_file_is_picked_up(tree):
    with pytest.raises(CreativeNotFoundError):
        find_creative_file(tree, "fresh.mp4")
    (tree / "B" / "fresh.mp4").write_bytes(_MP4)
    # Промах -> внеочередная проверка mtime -> перестройка.
    assert find_creative_file(tree, "fresh.mp4").name == "fresh.mp4"


def test_deleted_file_is_never_returned(tree):
    assert find_creative_file(tree, "solo.mp4").exists()
    (tree / "A" / "solo.mp4").unlink()
    with pytest.raises(CreativeNotFoundError):
        find_creative_file(tree, "solo.mp4")


def test_moved_file_is_found_at_new_place(tree):
    old = find_creative_file(tree, "solo.mp4")
    new = tree / "B" / "solo.mp4"
    old.rename(new)
    assert find_creative_file(tree, "solo.mp4") == new


def test_deleted_duplicate_stops_being_ambiguous(tree):
    with pytest.raises(AmbiguousCreativeError):
        find_creative_file(tree, "1_es_2292_post.mp4")
    (tree / "B" / "1_es_2292_post.mp4").unlink()
    assert find_creative_file(tree, "1_es_2292_post.mp4").parent.name == "A"


def test_stale_entry_rebuilds_once_even_when_mtime_unchanged(tree, monkeypatch):
    find_creative_file(tree, "solo.mp4")
    # Имитируем, что проверка mtime ничего не заметила, а файл уже исчез.
    monkeypatch.setattr(cf, "_dirs_changed", lambda snap: False)
    (tree / "A" / "solo.mp4").unlink()
    calls = _count_scandir(monkeypatch)
    with pytest.raises(CreativeNotFoundError):
        find_creative_file(tree, "solo.mp4")
    assert len(calls) == 3  # ровно одна перестройка


def test_realpath_escape_is_rejected_from_index(tree, monkeypatch):
    find_creative_file(tree, "solo.mp4")
    outside = str(tree.parent / "elsewhere" / "secret.mp4")
    real = os.path.realpath

    def fake(p, *a, **k):
        return outside if os.fspath(p).endswith("solo.mp4") else real(p, *a, **k)

    monkeypatch.setattr(cf.os.path, "realpath", fake)
    with pytest.raises(CreativeNotFoundError):
        find_creative_file(tree, "solo.mp4")


def test_real_symlink_pointing_outside_is_rejected(tree, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.mp4"
    outside.write_bytes(_MP4)
    link = tree / "A" / "leak.mp4"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink privilege is not available here")
    with pytest.raises(CreativeNotFoundError):
        find_creative_file(tree, "leak.mp4")


def test_concurrent_lookups_build_the_index_once(tree, monkeypatch):
    calls = _count_scandir(monkeypatch)
    errors, results = [], []

    def work():
        try:
            results.append(find_creative_file(tree, "solo.mp4").name)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert results == ["solo.mp4"] * 16
    assert len(calls) == 3


# ---- эндпоинт ---------------------------------------------------------------

from src.web import app as web  # noqa: E402


class _SyncClient:
    """TestClient в этом окружении несовместим с httpx — ходим через ASGI."""

    def get(self, url, params=None, headers=None):
        import httpx

        async def go():
            transport = httpx.ASGITransport(app=web.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
                return await c.get(url, params=params, headers=headers)

        return asyncio.run(go())


def test_endpoint_does_not_block_the_event_loop(tree, monkeypatch):
    """Быстрый запрос обязан завершиться, ПОКА медленный ещё внутри finder-а.
    Если поиск вернуть в цикл событий, медленный запрос держит цикл, быстрый
    не стартует, и таймаут события срабатывает."""
    monkeypatch.setattr(web, "get_creatives_root", lambda: str(tree))
    real = web._find_creative_file_scoped
    inside = threading.Event()
    release = threading.Event()

    def slow(root, path):
        if path == "slow.mp4":
            inside.set()
            assert release.wait(10), "быстрый запрос не завершился, пока медленный внутри finder-а"
            return tree / "A" / "solo.mp4"
        return real(root, path)

    monkeypatch.setattr(web, "_find_creative_file_scoped", slow)

    async def scenario():
        slow_task = asyncio.create_task(web.creative_file(path="slow.mp4"))
        # Ждём, пока медленный поток реально внутри finder-а (без блокировки цикла).
        for _ in range(200):
            if inside.is_set():
                break
            await asyncio.sleep(0.01)
        assert inside.is_set()
        fast = await asyncio.wait_for(web.creative_file(path="solo.mp4"), timeout=5)
        slow_still_inside = not slow_task.done()
        release.set()
        slow_resp = await slow_task
        return fast.status_code, slow_still_inside, slow_resp.status_code

    try:
        fast_status, slow_still_inside, slow_status = asyncio.run(scenario())
    finally:
        release.set()
    assert fast_status == 200
    assert slow_still_inside
    assert slow_status == 200


def test_endpoint_sends_cache_headers_and_honours_conditional_requests(tree, monkeypatch):
    monkeypatch.setattr(web, "get_creatives_root", lambda: str(tree))
    client = _SyncClient()
    r = client.get("/creative-file", params={"path": "A/solo.mp4"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, max-age=3600"
    etag, lm = r.headers["etag"], r.headers["last-modified"]
    assert etag and lm

    r2 = client.get("/creative-file", params={"path": "A/solo.mp4"}, headers={"If-None-Match": etag})
    assert r2.status_code == 304
    r3 = client.get("/creative-file", params={"path": "A/solo.mp4"}, headers={"If-Modified-Since": lm})
    assert r3.status_code == 304
    r4 = client.get("/creative-file", params={"path": "A/solo.mp4"}, headers={"If-None-Match": '"other"'})
    assert r4.status_code == 200


def test_endpoint_range_request_for_video_still_returns_206(tree, monkeypatch):
    monkeypatch.setattr(web, "get_creatives_root", lambda: str(tree))
    client = _SyncClient()
    r = client.get("/creative-file", params={"path": "A/solo.mp4"}, headers={"Range": "bytes=0-9"})
    assert r.status_code == 206
    assert r.headers["content-range"].startswith("bytes 0-9/")
    assert len(r.content) == 10
    assert r.headers["cache-control"] == "private, max-age=3600"


def test_endpoint_status_codes_unchanged(tree, monkeypatch):
    monkeypatch.setattr(web, "get_creatives_root", lambda: str(tree))
    client = _SyncClient()
    assert client.get("/creative-file", params={"path": "nope.mp4"}).status_code == 404
    assert client.get("/creative-file", params={"path": "1_es_2292_post.mp4"}).status_code == 409
    assert client.get("/creative-file", params={"path": ""}).status_code == 400


def test_endpoint_suffix_and_open_ranges_and_unsatisfiable(tree, monkeypatch):
    monkeypatch.setattr(web, "get_creatives_root", lambda: str(tree))
    client = _SyncClient()
    size = (tree / "A" / "solo.mp4").stat().st_size
    r = client.get("/creative-file", params={"path": "A/solo.mp4"}, headers={"Range": "bytes=-5"})
    assert r.status_code == 206 and r.content == _MP4[-5:]
    r = client.get("/creative-file", params={"path": "A/solo.mp4"}, headers={"Range": f"bytes={size - 3}-"})
    assert r.status_code == 206 and r.content == _MP4[-3:]
    r = client.get("/creative-file", params={"path": "A/solo.mp4"}, headers={"Range": f"bytes={size + 10}-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{size}"


def test_endpoint_other_routes_stay_no_store(tree, monkeypatch):
    client = _SyncClient()
    r = client.get("/creative-file", params={"path": ""})
    assert r.status_code == 400 and r.headers["cache-control"] == "no-store"


def test_range_end_beyond_file_is_clamped(tree, monkeypatch):
    monkeypatch.setattr(web, "get_creatives_root", lambda: str(tree))
    size = len(_MP4)
    r = _SyncClient().get("/creative-file", params={"path": "A/solo.mp4"}, headers={"Range": "bytes=0-999999"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-{size - 1}/{size}"
    assert r.headers["content-length"] == str(size)
    assert r.content == _MP4


def test_zero_length_suffix_range_is_unsatisfiable(tree, monkeypatch):
    monkeypatch.setattr(web, "get_creatives_root", lambda: str(tree))
    r = _SyncClient().get("/creative-file", params={"path": "A/solo.mp4"}, headers={"Range": "bytes=-0"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(_MP4)}"


def test_416_from_creative_file_still_leaves_with_no_store(tree, monkeypatch):
    monkeypatch.setattr(web, "get_creatives_root", lambda: str(tree))
    r = _SyncClient().get("/creative-file", params={"path": "A/solo.mp4"}, headers={"Range": "bytes=9999-"})
    assert r.status_code == 416
    assert "cache-control" in r.headers  # маршрут его выставил сам
    assert r.headers["cache-control"] == "no-store"


def test_other_route_with_own_cache_control_still_gets_no_store(monkeypatch):
    @web.app.get("/__acl1615_probe")
    async def probe():
        return web.Response(content="x", headers={"Cache-Control": "public, max-age=999"})

    try:
        r = _SyncClient().get("/__acl1615_probe")
    finally:
        web.app.router.routes[:] = [
            x for x in web.app.router.routes if getattr(x, "path", "") != "/__acl1615_probe"
        ]
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"


def test_error_statuses_from_creative_file_stay_no_store(tree, monkeypatch):
    monkeypatch.setattr(web, "get_creatives_root", lambda: str(tree))
    for path, code in (("nope.mp4", 404), ("1_es_2292_post.mp4", 409)):
        r = _SyncClient().get("/creative-file", params={"path": path})
        assert r.status_code == code and r.headers["cache-control"] == "no-store"


def test_fresh_lookup_sees_a_duplicate_added_inside_the_throttle_window(tree):
    assert find_creative_file(tree, "solo.mp4").parent.name == "A"
    # Свежая проверка mtime: на быстрых ФС mtime каталога может совпасть —
    # подправим его вручную, как будто добавление было позже построения.
    (tree / "B" / "solo.mp4").write_bytes(_MP4)
    st = os.stat(tree / "B")
    os.utime(tree / "B", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    # Обычный вызов внутри окна 5 с ещё видит старый индекс ...
    assert find_creative_file(tree, "solo.mp4").parent.name == "A"
    # ... а вызов публикации — нет.
    with pytest.raises(AmbiguousCreativeError):
        find_creative_file(tree, "solo.mp4", fresh=True)


def test_full_rebuild_safety_net_finds_file_when_dir_mtime_never_changes(tree, monkeypatch):
    with pytest.raises(CreativeNotFoundError):
        find_creative_file(tree, "late.mp4")
    (tree / "B" / "late.mp4").write_bytes(_MP4)
    monkeypatch.setattr(cf, "_dirs_changed", lambda snap: False)  # ФС без mtime каталога
    with pytest.raises(CreativeNotFoundError):
        find_creative_file(tree, "late.mp4")
    monkeypatch.setattr(cf, "_FULL_REBUILD_SECONDS", 0.0)
    assert find_creative_file(tree, "late.mp4").name == "late.mp4"


def test_concurrent_stale_callers_cause_a_single_rebuild(tree, monkeypatch):
    find_creative_file(tree, "solo.mp4")
    monkeypatch.setattr(cf, "_dirs_changed", lambda snap: False)
    (tree / "A" / "solo.mp4").unlink()
    key = os.path.normcase(os.path.abspath(tree))
    seen = cf._snapshots[key]
    builds = []
    real = cf._build_snapshot

    def counting(root):
        builds.append(root)
        return real(root)

    monkeypatch.setattr(cf, "_build_snapshot", counting)
    threads = [threading.Thread(target=cf._get_snapshot, args=(key,), kwargs={"seen": seen}) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(builds) == 1
