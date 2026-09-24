"""ACL-1233: зависшая загрузка Meta и страница ошибки Chrome — перезагрузка."""
from __future__ import annotations

import pytest

from src.fb_ads_rpa import meta_dom as md


class Page:
    def __init__(self, url="https://adsmanager.facebook.com/adsmanager/manage/ads/edit"):
        self.url = url
        self.reloads = 0

    def reload(self, **kw):
        self.reloads += 1
        if self.url.startswith("chrome-error://"):
            self.url = "https://adsmanager.facebook.com/adsmanager/manage/ads/edit"


@pytest.fixture
def env(monkeypatch):
    state = {"loading": False, "saved": False, "clears_after": None, "calls": 0, "events": []}

    def loading(page):
        state["calls"] += 1
        if state["clears_after"] is not None and state["calls"] > state["clears_after"]:
            return False
        return state["loading"]

    monkeypatch.setattr(md, "_loading_visible", loading)
    monkeypatch.setattr(md, "_all_edits_saved", lambda page: state["saved"])
    monkeypatch.setattr(md, "_chrome_error_page",
                        lambda page: "ERR_SSL_PROTOCOL_ERROR" if page.url.startswith("chrome-error://") else "")
    monkeypatch.setattr(md, "_sleep_h", lambda *a, **k: None)
    monkeypatch.setattr(md, "LOADING_STUCK_S", 0.05)
    monkeypatch.setattr(md._telemetry, "record", lambda step, ok, **kw: state["events"].append((step, ok, kw.get("note"))))
    return state


def test_a_chrome_error_page_is_always_reloaded(env):
    page = Page("chrome-error://chromewebdata/")
    assert md.recover_stuck_page(page, "ad2_before", editor=True) is True
    assert page.reloads == 1
    assert env["events"] == [("error_page_reload", True, "error_page:ERR_SSL_PROTOCOL_ERROR")]


def test_a_stuck_loading_modal_with_saved_edits_is_reloaded(env):
    env.update(loading=True, saved=True)
    page = Page()
    assert md.recover_stuck_page(page, "ad2_create", editor=True) is True
    assert page.reloads == 1 and env["events"][-1][:2] == ("loading_modal_reload", True)


def test_unsaved_edits_are_never_thrown_away(env):
    env.update(loading=True, saved=False)
    page = Page()
    assert md.recover_stuck_page(page, "ad2_create", editor=True) is False
    assert page.reloads == 0
    assert env["events"] == [("loading_modal_reload", False, "unsaved_edits")]


def test_a_loading_modal_that_clears_is_left_alone(env):
    env.update(loading=True, saved=True, clears_after=2)
    page = Page()
    assert md.recover_stuck_page(page, "ad2_create", editor=True) is False
    assert page.reloads == 0 and env["events"] == []


def test_the_campaign_list_has_nothing_to_lose_so_it_reloads(env):
    env.update(loading=True, saved=False)
    page = Page("https://adsmanager.facebook.com/adsmanager/manage/campaigns")
    assert md.recover_stuck_page(page, "objective", editor=False) is True
    assert page.reloads == 1


def test_a_healthy_page_is_not_touched(env):
    page = Page()
    assert md.recover_stuck_page(page, "ad3_before", editor=True) is False
    assert page.reloads == 0 and env["events"] == []


def test_no_objective_dialog_on_the_list_is_reloaded_even_without_loading_text(env):
    """19.09 18:12 CO_bnc Mix: окна цели нет, надписи «Loading…» тоже нет."""
    page = Page("https://adsmanager.facebook.com/adsmanager/manage/campaigns")
    assert md.recover_stuck_page(page, "objective", editor=False, force=True) is True
    assert page.reloads == 1 and env["events"][-1] == ("loading_modal_reload", True, "no_objective_dialog")


def test_force_never_reloads_an_editor(env):
    page = Page()
    assert md.recover_stuck_page(page, "ad2_create", editor=True, force=True) is False
    assert page.reloads == 0


def test_the_page_is_woken_before_it_is_reloaded(env, monkeypatch):
    # 19.09 вечер: свёрнутое окно и фоновая вкладка — перезагрузка без
    # «разбудить» снова упиралась в «Loading creation».
    from src.fb_ads_rpa import browser
    order = []
    page = Page("https://adsmanager.facebook.com/adsmanager/manage/campaigns")
    monkeypatch.setattr(browser, "wake_page", lambda p: order.append("wake") or {"minimized": True, "restored": True})
    real_reload = page.reload
    page.reload = lambda **kw: (order.append("reload"), real_reload(**kw))
    assert md.recover_stuck_page(page, "objective", editor=False, force=True) is True
    assert order == ["wake", "reload"]


def test_a_failing_wake_does_not_block_the_reload(env, monkeypatch):
    from src.fb_ads_rpa import browser

    def broken(p):
        raise RuntimeError("no cdp")

    monkeypatch.setattr(browser, "wake_page", broken)
    page = Page("chrome-error://chromewebdata/")
    assert md.recover_stuck_page(page, "ad2_before", editor=True) is True
    assert page.reloads == 1


def test_a_component_that_failed_to_load_is_reloaded(env, monkeypatch):
    """20.09 00:35 CO_bnc Mix: «Error: component failed to load» на месте
    панели плейсментов — следом отказали placements и devices_ios."""
    env.update(saved=True)
    monkeypatch.setattr(md, "_component_failed", lambda page: True)
    page = Page()
    assert md.recover_stuck_page(page, "placements", editor=True) is True
    assert page.reloads == 1 and env["events"][-1][:2] == ("loading_modal_reload", True)


def test_a_failed_component_keeps_unsaved_edits(env, monkeypatch):
    env.update(saved=False)
    monkeypatch.setattr(md, "_component_failed", lambda page: True)
    page = Page()
    assert md.recover_stuck_page(page, "placements", editor=True) is False
    assert page.reloads == 0 and env["events"] == [("loading_modal_reload", False, "unsaved_edits")]


def test_a_reload_that_times_out_falls_back_to_reloading_from_inside(env, monkeypatch):
    """20.09 00:55 CO_bnc GMB: `page.reload` не уложился в минуту дважды подряд."""
    env.update(loading=True, saved=True)
    calls = []

    class SlowPage(Page):
        def reload(self, **kw):
            calls.append("reload")
            raise RuntimeError("Page.reload: Timeout 60000ms exceeded.")

        def evaluate(self, script, *a):
            calls.append("js")
            return None

        def wait_for_selector(self, sel, **kw):
            calls.append("wait")
            return None

    page = SlowPage()
    assert md.recover_stuck_page(page, "ad2_create", editor=True) is True
    assert calls == ["reload", "js", "wait"], calls
    assert "js_reload" in env["events"][-1][2]


def test_the_pending_drafts_counter_is_read_from_the_button(env):
    """20.09 01:20: «Loading creation» держалось на аккаунтах с 10-11 черновиками."""

    class Btn(Page):
        def __init__(self, text):
            super().__init__()
            self.text = text

        def evaluate(self, script, *a):
            import re as _re
            m = _re.search(r"\((\d+)\)", self.text)
            return int(m.group(1)) if m else (0 if "Review and publish" in self.text else None)

    assert md._pending_drafts(Btn("Review and publish (11)")) == 11
    assert md._pending_drafts(Btn("Review and publish")) == 0
    assert md._pending_drafts(Btn("")) is None


def test_an_ssl_error_page_that_kept_the_meta_url_is_recognised(monkeypatch):
    """20.09 02:00 (профиль KLD_BFL_S15): «This site can't provide a secure
    connection / ERR_SSL_PROTOCOL_ERROR», а в адресной строке — адрес Meta."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        try:
            pg = b.new_page()
            pg.set_content("<div id='main-frame-error'>This site can&#8217;t provide a secure "
                           "connection<div>ERR_SSL_PROTOCOL_ERROR</div></div>")
            assert md._chrome_error_page(pg) == "ERR_SSL_PROTOCOL_ERROR"
            pg.set_content("<div>Ads Manager</div>")
            assert md._chrome_error_page(pg) == ""
        finally:
            b.close()


def test_long_text_is_inserted_after_a_few_real_keystrokes(monkeypatch):
    """20.09, решение пользователя: посимвольный набор стоил ~45 минут на круге.
    Начало строки печатаем нажатиями, длинный остаток вставляем целиком."""
    typed, inserted = [], []

    class Keyboard:
        def type(self, text, delay=None):
            typed.append(text)

        def insert_text(self, text):
            inserted.append(text)

    class Pg:
        keyboard = Keyboard()

    monkeypatch.delenv("FB_RPA_HUMAN_TYPING", raising=False)
    # И эту тоже: оператор, выставивший её у себя, красил бы этот тест
    # ровно тем, что тест проверяет (ревью 24.09).
    monkeypatch.delenv("FB_RPA_TYPING_LEAD", raising=False)
    monkeypatch.setattr(md.time, "sleep", lambda *_: None)
    md._type_human(Pg(), "Invest like a pro with Banco General today")
    assert "".join(typed) == "Invest l", typed
    assert inserted == ["ike a pro with Banco General today"], inserted


def test_the_old_per_character_typing_can_be_switched_back_on(monkeypatch):
    typed, inserted = [], []

    class Keyboard:
        def type(self, text, delay=None):
            typed.append(text)

        def insert_text(self, text):
            inserted.append(text)

    class Pg:
        keyboard = Keyboard()

    monkeypatch.setenv("FB_RPA_HUMAN_TYPING", "1")
    monkeypatch.setattr(md.time, "sleep", lambda *_: None)
    md._type_human(Pg(), "Invest like a pro")
    assert typed == ["Invest like a pro"] and inserted == []


def _keyboard_probe(monkeypatch):
    """Клавиатура, которая только записывает: нажатия и вставки порознь."""
    typed, inserted = [], []

    class Keyboard:
        def type(self, text, delay=None):
            typed.append(text)

        def insert_text(self, text):
            inserted.append(text)

    class Pg:
        keyboard = Keyboard()

    monkeypatch.delenv("FB_RPA_HUMAN_TYPING", raising=False)
    monkeypatch.delenv("FB_RPA_TYPING_LEAD", raising=False)
    monkeypatch.setattr(md.time, "sleep", lambda *_: None)
    return Pg(), typed, inserted


def test_the_lead_counts_keystrokes_and_not_characters(monkeypatch):
    """Голова считает НАЖАТИЯ, а не символы (ревью 24.09).

    Нажатие бывает только у ASCII: арабскую букву Playwright вставляет, а не
    набирает. Пока голова считала символы, арабское объявление — то есть
    почти любое наше — получало ровно ноль нажатий, то самое «мгновенно и
    целиком», от которого эта функция и защищает.
    """
    page, typed, inserted = _keyboard_probe(monkeypatch)
    # Длина как у настоящего текста объявления, иначе голова съедает строку
    # целиком и проверять нечего.
    text = ("استثمار مضمون "
            "مع بنك وخنرال "
            "اليوم وكل يوم "
            "بعده مع ضمان "
            "كامل للمبلغ")
    md._type_human(page, text)
    # Считаем нажатия, а не «есть ли хоть одно»: прежнее правило давало на
    # этой строке ровно одно (пробел на восьмом символе), и проверка «typed
    # не пуст» не отличала починку от поломки — ревью 24.09 это показало,
    # вернув ошибку и получив зелёный тест.
    assert len(typed) >= 3, f"нажатий всего {len(typed)}: {typed}"
    assert len(inserted[-1]) > 10, "хвост ушёл не одной вставкой"
    assert len(inserted) < len(text) / 2, "текст ушёл почти посимвольно"


def test_a_text_without_a_single_latin_letter_is_not_typed_letter_by_letter(monkeypatch):
    """Предел на голову: текст без ASCII не должен возвращать нас к
    посимвольному вводу, ради ухода от которого правка 20.09 и делалась."""
    page, typed, inserted = _keyboard_probe(monkeypatch)
    text = "مضمون" * 30
    md._type_human(page, text)
    assert len(inserted) < len(text) / 2, "текст ушёл посимвольно"
    assert inserted[-1] == text[-len(inserted[-1]):]


def test_a_short_text_without_latin_letters_does_not_run_off_the_end(monkeypatch):
    """Предел головы упирается в длину строки, а не только в множитель.

    Ревью 24.09: убери из предела `len(text) - 1` — и короткая строка без
    латиницы роняет набор `IndexError`, потому что счётчик нажатий до
    восьми не доходит никогда, а цикл идёт дальше конца текста. Оба других
    теста берут длинные строки, где предел упирается в множитель, и этой
    половины не трогают вовсе.
    """
    page, typed, inserted = _keyboard_probe(monkeypatch)
    text = "مضمون" * 2
    md._type_human(page, text)
    assert inserted and inserted[-1], "хвост пустой — цикл съел строку целиком"
    assert "".join(typed) + "".join(inserted) == text


# ── Сами распознаватели, без подмен (ревью 24.09) ─────────────────────────
#
# Тесты выше подменяют `_loading_visible`, `_all_edits_saved` и
# `_component_failed` заглушками — это правильно там, где проверяется
# ветвление вокруг них. Но тогда ни одна проверка не трогает то, чем они
# смотрят на страницу. Ревьюер заменил каждую на «всегда True» и получил
# зелёный набор: то есть Meta может переименовать надпись, и мы узнаем об
# этом от байера, а не от тестов. Ровно ради таких переименований вся эта
# часть и написана.


@pytest.fixture(scope="module")
def real_page():
    """Одна настоящая страница на весь файл.

    Пять тестов поднимали по своему браузеру — десять секунд там, где всё
    остальное в файле укладывается в сотые доли. И браузер утекал, если
    разметка не разбиралась: `set_content` стоял до `try` (ревью 24.09).
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            yield browser.new_page()
        finally:
            browser.close()


def test_the_loading_overlay_is_recognised_by_its_own_words(real_page):
    """Надпись видели двумя видами: «Loading...» и «Loading creation» —
    поэтому ищется слово в начале, а не точная строка."""
    page = real_page
    page.set_content("<div>Loading creation</div>")
    assert md._loading_visible(page) is True
    page.set_content("<div>Campaign name</div>")
    assert md._loading_visible(page) is False
    # Скрытая накладка — не накладка: она ничего не перекрывает.
    page.set_content("<div style='display:none'>Loading...</div>")
    assert md._loading_visible(page) is False


def test_all_edits_saved_is_read_from_the_page_itself(real_page):
    """Эта надпись — единственное, что стоит между перезагрузкой и потерей
    заполненных полей. Проверяется на настоящей разметке, иначе её
    переименование пройдёт незамеченным."""
    page = real_page
    page.set_content("<span>All edits saved</span>")
    assert md._all_edits_saved(page) is True
    # Похожие надписи Meta показывает постоянно — они не то же самое.
    for other in ("Verifying your edits...", "Saving edits", "All edits saved recently"):
        page.set_content(f"<span>{other}</span>")
        assert md._all_edits_saved(page) is False, other


def test_a_component_that_failed_to_load_is_recognised_by_its_own_words(real_page):
    """20.09 00:35 (CO_bnc Mix): «Error: component failed to load — Something
    went wrong, please try reloading the page» на месте панели плейсментов."""
    page = real_page
    page.set_content("<div>Error: component failed to load</div>"
                     "<div>Something went wrong, please try reloading the page</div>")
    assert md._component_failed(page) is True
    page.set_content("<div>Placements</div>")
    assert md._component_failed(page) is False


def test_the_pending_drafts_number_is_parsed_on_a_real_page(real_page):
    """Прежний тест подсовывал свой `evaluate`, который игнорировал сам
    скрипт и разбирал число заново на Python: проверялась подделка, а не
    то, что поедет в кабинет (ревью 24.09)."""
    page = real_page
    page.set_content("<div><span>Review and publish (11)</span></div>")
    assert md._pending_drafts(page) == 11
    page.set_content("<div><span>Review and publish</span></div>")
    assert md._pending_drafts(page) == 0
    page.set_content("<div><span>Publish</span></div>")
    assert md._pending_drafts(page) is None
