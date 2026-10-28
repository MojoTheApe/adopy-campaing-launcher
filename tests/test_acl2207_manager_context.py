import asyncio
import threading

import pytest

from src.web import app, buyer, cache, report_store


@pytest.mark.parametrize("initial_cm", ["Manager A", ""])
@pytest.mark.parametrize("switch_when", ["queued", "rendering"])
def test_manager_switch_never_saves_another_managers_numbers(monkeypatch, tmp_path, initial_cm, switch_when):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MY_CM_NAME", "")
    key = {"code": "same", "source": "same", "settings": "same"}
    reports = {initial_cm: {"my_cm": initial_cm}, "Manager B": {"my_cm": "Manager B"}}
    monkeypatch.setitem(cache._CACHE, "entry", ("same", reports, None, []))
    monkeypatch.setattr(cache, "_resolve_excel_path", lambda _path=None: "fake.xlsx")
    monkeypatch.setattr(cache, "_current_key", lambda _path: "same")
    monkeypatch.setattr(cache.bi_stats, "bi_enabled", lambda: False)
    monkeypatch.setattr(app, "_page_ready", lambda *args: True)
    monkeypatch.setattr(app, "_store_key", lambda _source: key)
    written = threading.Event()
    saved, pool = report_store.save, app._in_report_pool
    outside = []

    def save(*args):
        try:
            return saved(*args)
        finally:
            written.set()

    def render(_request, **kwargs):
        if switch_when == "rendering":
            assert buyer.set_selected_cm("Manager B") == "Manager B"
        observer = threading.Thread(target=lambda: outside.append(buyer.get_selected_cm()))
        observer.start()
        observer.join(timeout=2)
        assert not observer.is_alive()
        report, _, _ = cache.get_cached_report()
        if switch_when == "rendering":
            assert buyer.set_selected_cm(initial_cm) == initial_cm
        response = app.HTMLResponse("<body>" + (report["my_cm"] or "all") + " numbers</body>")
        response.template = type("Template", (), {"name": "report.html"})()
        return response

    monkeypatch.setattr(app, "_render_report", render)
    monkeypatch.setattr(report_store, "save", save)
    buyer.set_selected_cm(initial_cm)
    inputs = app._store_inputs("", "", "", "", "", "")

    async def run():
        queued, release = asyncio.Event(), asyncio.Event()

        async def gate(fn, *args, **kwargs):
            queued.set()
            await release.wait()
            return await pool(fn, *args, **kwargs)

        monkeypatch.setattr(app, "_in_report_pool", gate)
        task = asyncio.create_task(app.index(None))
        await queued.wait()
        if switch_when == "queued":
            buyer.set_selected_cm("Manager B")
        release.set()
        response = await task
        assert await asyncio.to_thread(written.wait, 5)
        return response

    response = asyncio.run(run())
    expected = "<body>" + (initial_cm or "all") + " numbers</body>"
    assert response.body.decode() == expected
    stored = report_store.load(inputs, key)
    assert stored is not None and stored.html == expected and not stored.outdated
    assert outside == ["Manager B"]
    assert buyer.get_selected_cm() == ("Manager B" if switch_when == "queued" else initial_cm)


def test_failed_render_does_not_leak_its_manager_context(monkeypatch, tmp_path):
    monkeypatch.setenv("ACL_CONFIG_DIR", str(tmp_path))
    buyer.set_selected_cm("Manager A")
    inputs = app._store_inputs("", "", "", "", "", "")
    monkeypatch.setattr(app, "_store_key", lambda _source: {"source": "same"})

    def render(_request, **kwargs):
        assert buyer.set_selected_cm("Manager B") == "Manager B"
        raise RuntimeError("render failed")

    monkeypatch.setattr(app, "_render_report", render)
    with pytest.raises(RuntimeError, match="render failed"):
        app._render_and_remember(None, inputs)
    assert buyer.get_selected_cm() == "Manager B"
