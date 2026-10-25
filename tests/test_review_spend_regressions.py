"""Regression cases from the merge review; browser interactions are synthetic."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS

import pytest

from src.analysis_agent import account_spend as sp
from src.fb_ads_rpa import browser as br

DAY = "2026-09-06"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "_store_path", lambda: tmp_path / "spend.json")
    sp._RUNNING.clear()
    sp._UNSAVED.clear()
    yield
    for thread in threading.enumerate():
        if thread.name.startswith("spend:"):
            thread.join(3)
    sp._UNSAVED.clear()


def wait_workers():
    for thread in threading.enumerate():
        if thread.name.startswith("spend:"):
            thread.join(3)
            assert not thread.is_alive()


@pytest.mark.parametrize("text,amount", [("$1234.56", 1234.56), ("€12044", 12044), ("$123456.78", 123456.78), ("₪1\u00a0234.50", 1234.5), ("$1,234,567.89", 1234567.89)])
def test_full_amount_is_read(text, amount):
    assert sp._first_money(text) == amount


def test_currency_comes_from_the_total_not_other_page_amounts():
    page = NS(evaluate=lambda js: {"spend": "€1234.56", "label": True}, inner_text=lambda _: "Budget $20.00")
    assert sp._scan_totals(page) == (1234.56, True)
    # Знак передаётся как написан, без перевода в код (решение владельца
    # 15.09). Смысл проверки прежний: валюта берётся из строки итогов, а не
    # из «Budget $20.00» по соседству.
    assert sp._currency(page) == "€"


def test_concurrent_readings_and_statuses_survive():
    """Двенадцать записей одновременно: ни одна не теряется, `.tmp` не остаётся.

    Барьер держал таймаут в три секунды, и это был не факт о коде, а ставка на
    скорость машины. В полном прогоне процессор занят браузерами Playwright,
    двенадцать потоков до барьера за три секунды не доходят, `wait` бросает
    `BrokenBarrierError`, `pool.map` его пробрасывает — часть показаний не
    сохраняется, и проверка падает на исправном коде (12.09).

    Смысл проверки — «пишут вместе», и он от щедрого предела не страдает.
    Предел остаётся: без него зависший поток остановил бы прогон навсегда.
    """
    gate = threading.Barrier(12)
    def save(i):
        gate.wait(60)
        sp._put(sp.SpendReading(account=f"Acc{i}", date_from=DAY, date_to=DAY, spend=i, state="ok", status="clear"))
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(save, range(12)))
    assert len(sp.all_readings(DAY, DAY)) == 12
    assert all(sp.get_status(f"Acc{i}")["status"] == "clear" for i in range(12))
    assert not list(sp._store_path().parent.glob("*.tmp"))


def test_restart_turns_orphaned_running_reading_into_retryable_failure():
    sp._put(sp.SpendReading(account="Acc", date_from=DAY, date_to=DAY, state="running"))
    assert sp.get_reading("Acc", DAY, DAY).state == "failed"
    assert sp.all_readings(DAY, DAY)["Acc"].state == "failed"
    sp.start_read("Acc", DAY, DAY, reader=lambda a, f, t: sp.SpendReading(a, f, t, state="ok", spend=7))
    wait_workers()
    assert sp.get_reading("Acc", DAY, DAY).spend == 7


def test_failed_initial_save_does_not_leave_a_phantom_worker(monkeypatch):
    def fail(_):
        raise OSError("disk full")
    monkeypatch.setattr(sp, "_save_all", fail)
    with pytest.raises(OSError):
        sp.start_read("Acc", DAY, DAY)
    assert not sp.is_running("Acc", DAY, DAY)


def test_failed_final_save_does_not_leave_reading_disabled(monkeypatch):
    save = sp._save_all
    def final_fails(data):
        if next(iter(data.values()))["state"] != "running":
            raise OSError("disk full")
        save(data)
    monkeypatch.setattr(sp, "_save_all", final_fails)
    sp.start_read("Acc", DAY, DAY, reader=lambda a, f, t: sp.SpendReading(a, f, t, state="ok", spend=7))
    wait_workers()
    assert not sp.is_running("Acc", DAY, DAY)
    assert sp.get_reading("Acc", DAY, DAY).state == "failed"
    assert "save" in sp.get_reading("Acc", DAY, DAY).error


def test_failed_thread_start_can_be_retried(monkeypatch):
    def fail(self):
        raise RuntimeError("thread limit")
    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError):
        sp.start_read("Acc", DAY, DAY)
    assert not sp.is_running("Acc", DAY, DAY)
    assert sp.get_reading("Acc", DAY, DAY).state == "failed"


def test_read_and_probe_wait_for_publish_lease(monkeypatch):
    from src.web import publish_jobs
    # Замок один на профиль: публикация держит его на весь прогон, чтение
    # того же профиля — на время чтения, и очередь у них общая (acl-113e →
    # acl-1210). Тестовая карта связывает «Acc» с физическим UUID;
    # публикация берёт тот же ключ тем же правилом.
    assert publish_jobs._publish_serial is sp.publish_lease
    entered = threading.Event()
    def reader(a, f, t):
        entered.set()
        return sp.SpendReading(a, f, t, state="ok", spend=1)
    with publish_jobs._publish_serial(sp._profile_key("Acc")):
        sp.start_read("Acc", DAY, DAY, reader=reader)
        assert not entered.wait(.1)
    assert entered.wait(2)
    wait_workers()
    entered.clear()
    monkeypatch.setattr(sp, "_probe_entry", lambda *a: entered.set())
    monkeypatch.setattr(sp, "_attach_only", lambda *a: (_ for _ in ()).throw(sp.ProfileNotAttachable("test")))
    with publish_jobs._publish_serial("550e8400-e29b-41d4-a716-446655440000"):
        thread = threading.Thread(target=sp.read_ad_account, args=("550e8400-e29b-41d4-a716-446655440000",))
        thread.start()
        assert not entered.wait(.1)
    thread.join(2)
    assert entered.is_set()


def process(udd, port=19222, listening=True):
    return NS(info={"exe": "/mlx/mimic/chrome", "cmdline": [f"--user-data-dir={udd}", f"--remote-debugging-port={port}"]},
              net_connections=lambda **kw: [NS(status="LISTEN", laddr=NS(port=port))] if listening else [])


def test_strict_attach_refuses_other_profiles_global_port(tmp_path, monkeypatch):
    import psutil
    wanted = tmp_path / "550e8400-e29b-41d4-a716-446655440000"
    wanted.mkdir()
    (wanted / "DevToolsActivePort").write_text("19222\n", encoding="utf-8")
    monkeypatch.setattr(psutil, "process_iter", lambda _: [process(str(wanted), 0, False), process(str(tmp_path / "6ba7b810-9dad-11d1-80b4-00c04fd430c8"))])
    provider = br.MultiloginDirectCDPProvider()
    monkeypatch.setattr(provider, "_cdp_accepting", lambda *a: True)
    assert provider._discover_owned_debug_port(str(wanted)) is None
    monkeypatch.setattr(psutil, "process_iter", lambda _: [process(str(wanted))])
    assert provider._discover_owned_debug_port(str(wanted)) == 19222


def test_attach_requests_owned_profile_and_new_tab(monkeypatch, tmp_path):
    calls = []
    class Direct:
        # Публичная пара, как у настоящего провайдера: она делегирует
        # приватным, а strict_profile зафиксирован внутри неё.
        def attach_existing_only(self, profile_id, user_data_dir, *, new_page=False):
            return self._try_attach_existing(
                profile_id, user_data_dir, strict_profile=True, new_page=new_page
            )

        def profile_is_running(self, user_data_dir, profile_id=""):
            return self._profile_process_running(user_data_dir, profile_id)

        def _try_attach_existing(self, *args, **kwargs):
            calls.append(kwargs)
            return NS()
    monkeypatch.setattr(br, "MultiloginDirectCDPProvider", Direct)
    monkeypatch.setattr(sp, "_mlx_profile_dir", lambda *a: tmp_path)
    with sp.profile_lock("550e8400-e29b-41d4-a716-446655440000"):
        sp._attach_only(NS(profile_id="550e8400-e29b-41d4-a716-446655440000"), "Acc")
    assert calls == [{"strict_profile": True, "new_page": True}]


def test_new_tab_does_not_use_or_close_draft(monkeypatch):
    calls = []
    old_page = NS()
    new_page = NS()
    context = NS(pages=[old_page], new_page=lambda: new_page)
    browser = NS(contexts=[context])
    pw = NS(chromium=NS(connect_over_cdp=lambda _: browser), stop=lambda: calls.append("stop"))
    monkeypatch.setattr(br, "sync_playwright", lambda: NS(start=lambda: pw))
    for name in ["ensure_playwright_browsers_path", "_stop_thread_playwright", "_reset_asyncio_running_loop", "_disable_background_throttling", "_ensure_wide_window", "_keep_renderer_active"]:
        monkeypatch.setattr(br, name, lambda *a: None)
    conn = br._connect_playwright(cdp_url="test", provider="multilogin_direct", debug_port="123", ws_endpoint=None, new_page=True)
    assert conn.page is new_page
    conn.close(keep_browser_open=True)
    assert calls == ["stop"]


@pytest.mark.parametrize("redirect_at", ["initial", "after_total", None])
def test_account_identity_is_checked_and_only_owned_tab_is_closed(monkeypatch, redirect_at):
    calls = []
    page = NS(url="https://adsmanager.facebook.com/?act=111", goto=lambda *a, **kw: None, close=lambda: calls.append("tab closed"))
    if redirect_at == "initial":
        page.url = "https://adsmanager.facebook.com/?act=222"
    monkeypatch.setattr(sp, "_entry_for", lambda _: NS(ad_account_id="111", profile_id="550e8400-e29b-41d4-a716-446655440000"))
    monkeypatch.setattr(sp, "_attach_only", lambda *a: NS(page=page, close=lambda **kw: calls.append(kw)))
    monkeypatch.setattr(sp, "_wait_for_grid", lambda _: True)
    monkeypatch.setattr(sp, "_read_status", lambda _: ("clear", ""))
    monkeypatch.setattr(sp, "_shown_period", lambda _: DAY)
    def total(_):
        if redirect_at == "after_total":
            page.url = "https://adsmanager.facebook.com/?act=222"
        return 7, "test"
    monkeypatch.setattr(sp, "_read_total", total)
    out = sp.read_via_rpa("Acc", DAY, DAY)
    assert out.state == ("failed" if redirect_at else "ok")
    if redirect_at:
        assert out.spend is None and not out.status
    assert calls == ["tab closed", {"keep_browser_open": True}]


@pytest.mark.parametrize("running,owned_port", [(False, None), (True, None), (True, 19222)])
def test_public_attach_uses_only_owned_port_without_profile_lifecycle(monkeypatch, running, owned_port):
    """Exercise the public provider with the real attach implementation.

    Discovery and the CDP connection are synthetic; lifecycle/fallback paths
    must never be used, including after a failed CDP connection.
    """
    from unittest.mock import Mock
    provider = br.MultiloginDirectCDPProvider()
    monkeypatch.setattr(provider, "_profile_process_running", Mock(return_value=running))
    owned = Mock(return_value=owned_port)
    monkeypatch.setattr(provider, "_discover_owned_debug_port", owned)
    forbidden = Mock(side_effect=AssertionError("unsafe fallback or profile lifecycle"))
    for name in ("_discover_debug_port", "_kill_profile_browser", "open"):
        monkeypatch.setattr(provider, name, forbidden)
    connection = object()
    connect = Mock(return_value=connection)
    monkeypatch.setattr(br, "_connect_playwright", connect)
    got = provider.attach_existing_only("synthetic-profile", "/synthetic/profile", new_page=True)
    if running and owned_port:
        assert got is connection
        connect.assert_called_once_with(cdp_url="http://127.0.0.1:19222", provider="multilogin_direct",
                                        debug_port="19222", ws_endpoint=None, new_page=True)
        connect.side_effect = RuntimeError("CDP unavailable")
        assert provider.attach_existing_only("synthetic-profile", "/synthetic/profile", new_page=True) is None
    else:
        assert got is None
        connect.assert_not_called()
    forbidden.assert_not_called()
    if not running:
        owned.assert_not_called()


def test_the_new_tab_gives_the_screen_back(monkeypatch):
    """Новая вкладка в Chrome сразу становится активной, и окно человека на
    минуты уезжает на нашу страницу. Возвращаем то, на что он смотрел.

    Наша вкладка уходит в фон и продолжает работать: торможение фоновых
    вкладок здесь снято подделкой фокуса и удержанием рендерера — ради этого
    те вызовы и делались (Test139, 29.07).
    """
    fronted = []
    old_page = NS(evaluate=lambda _js: "visible", bring_to_front=lambda: fronted.append("old"))
    hidden = NS(evaluate=lambda _js: "hidden", bring_to_front=lambda: fronted.append("hidden"))
    new_page = NS(bring_to_front=lambda: fronted.append("new"))
    context = NS(pages=[hidden, old_page], new_page=lambda: new_page)
    browser = NS(contexts=[context])
    pw = NS(chromium=NS(connect_over_cdp=lambda _: browser), stop=lambda: None)
    monkeypatch.setattr(br, "sync_playwright", lambda: NS(start=lambda: pw))
    for name in ["ensure_playwright_browsers_path", "_stop_thread_playwright",
                 "_reset_asyncio_running_loop", "_disable_background_throttling",
                 "_ensure_wide_window", "_keep_renderer_active"]:
        monkeypatch.setattr(br, name, lambda *a: None)
    conn = br._connect_playwright(cdp_url="test", provider="multilogin_direct",
                                  debug_port="123", ws_endpoint=None, new_page=True)
    assert conn.page is new_page, "работаем не в своей вкладке"
    assert fronted == ["old"], f"экран отдали не той вкладке: {fronted}"


def test_the_screen_is_left_alone_when_we_take_no_new_tab(monkeypatch):
    """Публикация работает в той же вкладке — там возвращать нечего."""
    fronted = []
    old_page = NS(evaluate=lambda _js: "visible", bring_to_front=lambda: fronted.append("old"))
    context = NS(pages=[old_page], new_page=lambda: NS())
    browser = NS(contexts=[context])
    pw = NS(chromium=NS(connect_over_cdp=lambda _: browser), stop=lambda: None)
    monkeypatch.setattr(br, "sync_playwright", lambda: NS(start=lambda: pw))
    for name in ["ensure_playwright_browsers_path", "_stop_thread_playwright",
                 "_reset_asyncio_running_loop", "_disable_background_throttling",
                 "_ensure_wide_window", "_keep_renderer_active"]:
        monkeypatch.setattr(br, name, lambda *a: None)
    conn = br._connect_playwright(cdp_url="test", provider="multilogin_direct",
                                  debug_port="123", ws_endpoint=None, new_page=False)
    assert conn.page is old_page
    assert fronted == [], "дёрнули вкладку без нужды"


def test_a_page_that_cannot_answer_is_not_fronted(monkeypatch):
    """Ничего не нашли — значит и возвращать нечего; молчим, а не гадаем."""
    broken = NS(evaluate=_boom, bring_to_front=lambda: (_ for _ in ()).throw(AssertionError("не та")))
    context = NS(pages=[broken], new_page=lambda: NS())
    browser = NS(contexts=[context])
    pw = NS(chromium=NS(connect_over_cdp=lambda _: browser), stop=lambda: None)
    monkeypatch.setattr(br, "sync_playwright", lambda: NS(start=lambda: pw))
    for name in ["ensure_playwright_browsers_path", "_stop_thread_playwright",
                 "_reset_asyncio_running_loop", "_disable_background_throttling",
                 "_ensure_wide_window", "_keep_renderer_active"]:
        monkeypatch.setattr(br, name, lambda *a: None)
    br._connect_playwright(cdp_url="test", provider="multilogin_direct",
                           debug_port="123", ws_endpoint=None, new_page=True)


def _boom(_js):
    raise RuntimeError("target closed")


def test_the_new_tab_is_created_in_the_background(monkeypatch):
    """`context.new_page()` не просто делает вкладку активной — Chrome
    поднимает само окно профиля. Человек сидит в другом приложении, а ему на
    минуты выбрасывает Ads Manager (проверено вживую 12.09; возврата фокуса
    соседней вкладке не хватило — окно уже всплыло).

    У CDP флаг `background` есть, и он ровно про это.
    """
    sent = []
    made = NS(evaluate=lambda _js: "hidden")
    base = NS(evaluate=lambda _js: "hidden")
    pages = [base]

    unrelated = NS(evaluate=lambda _js: "hidden")
    def _session(target):
        def send(method, params=None):
            sent.append((method, params))
            if method == "Target.createTarget":
                pages.extend([unrelated, made])  # a user's concurrent tab is first
                return {"targetId":"owned"}
            return {"targetInfo":{"targetId":"owned" if target is made else "user"}}
        return NS(send=send, detach=lambda:None)
    context = NS(pages=pages, new_cdp_session=_session,
                 new_page=lambda: (_ for _ in ()).throw(AssertionError("expected background tab")))
    browser = NS(contexts=[context])
    pw = NS(chromium=NS(connect_over_cdp=lambda _: browser), stop=lambda: None)
    monkeypatch.setattr(br, "sync_playwright", lambda: NS(start=lambda: pw))
    for name in ["ensure_playwright_browsers_path", "_stop_thread_playwright",
                 "_reset_asyncio_running_loop", "_disable_background_throttling",
                 "_ensure_wide_window", "_keep_renderer_active"]:
        monkeypatch.setattr(br, name, lambda *a: None)
    conn = br._connect_playwright(cdp_url="test", provider="multilogin_direct",
                                  debug_port="123", ws_endpoint=None, new_page=True)
    assert conn.page is made
    created = [params for method, params in sent if method == "Target.createTarget"]
    assert len(created) == 1 and created[0]["background"] is True


def test_a_tab_we_could_not_open_in_the_background_is_still_opened(monkeypatch):
    """Своя вкладка нужнее тишины: без неё чтение уводило бы страницу человека."""
    fallback = NS()
    context = NS(pages=[NS(evaluate=lambda _js: "hidden")],
                 new_cdp_session=lambda _p: (_ for _ in ()).throw(RuntimeError("no cdp")),
                 new_page=lambda: fallback)
    browser = NS(contexts=[context])
    pw = NS(chromium=NS(connect_over_cdp=lambda _: browser), stop=lambda: None)
    monkeypatch.setattr(br, "sync_playwright", lambda: NS(start=lambda: pw))
    for name in ["ensure_playwright_browsers_path", "_stop_thread_playwright",
                 "_reset_asyncio_running_loop", "_disable_background_throttling",
                 "_ensure_wide_window", "_keep_renderer_active"]:
        monkeypatch.setattr(br, name, lambda *a: None)
    conn = br._connect_playwright(cdp_url="test", provider="multilogin_direct",
                                  debug_port="123", ws_endpoint=None, new_page=True)
    assert conn.page is fallback


@pytest.fixture(autouse=True)
def mapped_profiles(monkeypatch):
    from types import SimpleNamespace
    from tests.profile_lease_fakes import fake_profile
    monkeypatch.setattr(sp, "_entry_for", lambda label: SimpleNamespace(profile_id=fake_profile(label)))
