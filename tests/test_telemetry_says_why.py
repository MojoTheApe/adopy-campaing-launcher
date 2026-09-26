"""Телеметрия обязана говорить не только «что», но и «почему».

Прогон 9 сентября дал шесть отказов подряд — `placements`, `ad_copy`,
`ad_destination` дважды, — и по записям нельзя было сказать про них ничего:
у всех `diag` пуст. Сорок три места в потоке умеют сообщить об отказе, и **ни
одно** не передавало причину.

Хуже другое: шаг детального таргетинга целиком стоял под `if targeting_label:`.
Пустой список интересов не оставлял ни строки — шага в потоке просто не
существовало. На экране это выглядело как «интересы не проставились», а
отличить «нечего было вводить» от «не нашёл поле» было нечем: оба исхода
выглядели одинаково, то есть никак.

Это первый этап проекта Navigator и условие для всего остального: агент,
слушающий такой поток, узнает ровно столько же, сколько человек. Но пункт
полезен и сам по себе — причина отказа нужна тому, кто сидит перед экраном.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import run_telemetry as rt  # noqa: E402


class _Page:
    """Страница, отвечающая ровно то, что ей велели."""

    def __init__(self, url="", title="", dialogs=0, banner=None, raises=()):
        self.url = url
        self._title = title
        self._dialogs = dialogs
        self._banner = banner
        self._raises = raises

    def title(self):
        if "title" in self._raises:
            raise RuntimeError("нет")
        return self._title

    def get_by_role(self, role):
        if role in self._raises:
            raise RuntimeError("нет")
        page = self

        class _Loc:
            def count(self):
                return page._dialogs

            @property
            def first(self):
                return self

            def is_visible(self, timeout=0):
                return page._banner is not None

            def inner_text(self, timeout=0):
                return page._banner or ""

        return _Loc()


# ── третий исход: пропущено ───────────────────────────────────────────────

def test_a_skipped_step_is_neither_success_nor_failure():
    """`ok=None` не попадает в долю неудач: пропуск — не провал.

    Но и не удача: раньше пустые интересы засчитывались успехом молча.
    """
    assert rt.normalise_step("skip_targeting") == ("targeting", None)


def test_success_and_failure_still_mean_what_they_meant():
    assert rt.normalise_step("after_languages") == ("languages", True)
    assert rt.normalise_step("fail_placements") == ("placements", False)


# ── контекст отказа ───────────────────────────────────────────────────────

def test_the_failure_carries_what_was_on_the_page():
    ctx = rt.page_context(_Page(url="https://adsmanager.facebook.com/x", title="Ads Manager", dialogs=1))
    assert ctx["url"].endswith("/x")
    assert ctx["title"] == "Ads Manager"
    assert ctx["dialogs"] == 1


def test_the_banner_is_captured_because_it_is_what_hides_the_button():
    """Верхний баннер Meta сдвигает и закрывает контролы.

    24 июля из-за него прогон не проходил дальше списка кампаний, и понять это
    удалось только по скриншоту.
    """
    ctx = rt.page_context(_Page(banner="We're evolving how we measure results"))
    assert "evolving" in ctx["banner"]


def test_the_account_number_never_reaches_the_context():
    """Контекст уезжает в общую таблицу, которую читает вся компания."""
    ctx = rt.page_context(_Page(url="https://adsmanager.facebook.com/manage/campaigns?act=1234567890123456"))
    assert "1234567890123456" not in ctx["url"]
    assert "act=<hidden>" in ctx["url"]


def test_a_profile_id_never_reaches_the_context():
    ctx = rt.page_context(_Page(title="550e8400-e29b-41d4-a716-446655440000"))
    assert "550e8400" not in ctx["title"]


def test_a_long_number_anywhere_is_hidden():
    """Номер кабинета встречается не только в `act=`."""
    assert "9876543210123" not in rt.scrub("campaign 9876543210123 selected")


def test_a_page_that_answers_nothing_still_yields_what_it_can():
    """Строка лога не должна стоить кампании — то же правило, что у `record`.

    Страница на упавшем шаге бывает в любом состоянии. Что удалось спросить —
    записываем, на чём споткнулись — молча пропускаем, но не теряем остальное.
    """
    ctx = rt.page_context(_Page(url="https://x.example/page", raises=("title", "dialog", "banner")))
    assert ctx == {"url": "https://x.example/page"}, ctx


def test_a_page_that_is_gone_entirely_still_returns_a_dict():
    class _Dead:
        @property
        def url(self):
            raise RuntimeError("target closed")

        def title(self):
            raise RuntimeError("target closed")

        def get_by_role(self, role):
            raise RuntimeError("target closed")

    assert rt.page_context(_Dead()) == {}


# ── где это включено ──────────────────────────────────────────────────────

def test_the_context_is_attached_at_the_single_choke_point():
    """Сорок три места сообщают об отказе, и ни одно не передавало причину.

    Собирать её в каждом значило бы завести сорок три места, которые разойдутся.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("def _shot(label: str"):]
    block = block[: block.index("_shot_clock[\"t\"] = now")]
    assert "_telemetry.page_context(page)" in block, "контекст не собирается в точке записи"
    assert "ok is False" in block, "контекст собирается не только у отказов"


def test_an_explicit_diag_is_not_overwritten_by_the_collected_one():
    """Где причина известна точно, она важнее общего снимка обстановки."""
    merged = rt.merge_diag({"url": "u", "why": "общее"}, {"why": "точное"})
    assert merged["why"] == "точное"


def test_a_step_that_explains_itself_still_keeps_the_page_address():
    """Иначе за собственную причину шаг платит адресом страницы.

    По адресу и заголовку отказ сопоставляют со снимком разметки. Прежнее
    условие `if diag is None` их отбрасывало ровно у тех шагов, которые
    рассказали о себе больше всех.
    """
    merged = rt.merge_diag({"url": "u", "title": "t"}, {"why": "видно 3 категории из 5"})
    assert merged == {"url": "u", "title": "t", "why": "видно 3 категории из 5"}


def test_nothing_to_say_stays_nothing():
    """Пустой словарь в записи читался бы как «причину искали и не нашли»."""
    assert rt.merge_diag(None, None) is None
    assert rt.merge_diag({}, {}) is None


def test_the_merge_is_the_one_the_step_recorder_uses():
    """Сторож: правило проверяется здесь только если код зовёт именно его."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("def _shot(label: str"):]
    block = block[: block.index("_shot_clock[\"t\"] = now")]
    assert "merge_diag" in block, "запись отказа соединяет причины сама по себе"


def test_an_empty_targeting_now_says_so():
    """Тот самый молчащий шаг: пустой список интересов не писал ни строки."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("if targeting_label:"):]
    block = block[: block.index("# Placements")]
    assert 'skip_targeting' in block, "пропуск таргетинга снова молчит"
    assert "else:" in block, "у ветки «интересов нет» нет своей записи"


# ── номер прогона ─────────────────────────────────────────────────────────

def test_a_direct_writer_inherits_the_current_run(tmp_path):
    """Сорок три места пишут об отказах, и не все знают номер прогона."""
    rt.set_current_run("runA", "SA_camp")
    try:
        p = tmp_path / "t.jsonl"
        rt.record("create_additional_ad", False, path=p)
        row = json.loads(p.read_text(encoding="utf-8").strip())
        assert row["run_id"] == "runA" and row["campaign"] == "SA_camp"
    finally:
        rt.clear_current_run()


def test_a_writer_that_knows_better_is_not_overruled(tmp_path):
    rt.set_current_run("runA", "SA_camp")
    try:
        p = tmp_path / "t.jsonl"
        rt.record("s", True, run_id="runB", campaign="other", path=p)
        row = json.loads(p.read_text(encoding="utf-8").strip())
        assert row["run_id"] == "runB" and row["campaign"] == "other"
    finally:
        rt.clear_current_run()


def test_the_flow_names_the_run_once():
    """Сторож: без этого вызова наследовать нечего."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    assert "_telemetry.set_current_run(run_id" in source


# ── попытка — это не отказ ────────────────────────────────────────────────

def test_a_retry_is_not_recorded_as_a_failed_step():
    """Шаг, который не падал ни разу, имел сто процентов отказов.

    Первый клик через CDP регулярно не передаёт фокус, текст не ложится, и
    вторая попытка кладёт его. Каждая первая попытка писалась отдельной
    строкой `ok=False` — 9 сентября таких три за прогон при трёх успешных
    `ad_copy`. По такой статистике чинят то, что работает, и не замечают
    того, что сломано.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("def _dump_stage"):]
    block = block[: block.index("def _locate")]
    assert "_telemetry.record(" not in block, "попытка снова пишется отдельной записью"
    assert 'report["attempts"]' in block, "попытка никуда не записывается — хуже прежнего"


def test_the_attempts_reach_the_failure_record():
    """Убрать шум — не значит потерять подробности.

    Если шаг всё-таки упал, стадии всех попыток должны лежать рядом с
    отказом: без них останется голое «ad_copy ✗», с которого всё начиналось.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    assert '_shot("fail_ad_copy", copy_report)' in source
    assert '_shot(f"fail_{tag}_copy", copy_report)' in source


# ── страница-ошибка браузера ──────────────────────────────────────────────
#
# 10 сентября прогон встал на ERR_SSL_PROTOCOL_ERROR: Ads Manager не
# открывался вовсе. Шаг честно не нашёл бы кнопку, которой нет, и сообщил бы
# «не нашёл поле имени кампании» — то есть соврал бы о причине.

class _ErrorPage:
    """Страница, отдающая заданный узел #main-frame-error."""

    def __init__(self, html_text=None, raises=False):
        self._text = html_text
        self._raises = raises
        self.url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"

    def evaluate(self, js, *a):
        if self._raises:
            raise RuntimeError("target closed")
        return self._text or ""

    def title(self):
        return "adsmanager.facebook.com"

    def get_by_role(self, role):
        raise RuntimeError("нет")


def test_the_browser_error_code_is_named():
    assert rt.browser_error(_ErrorPage("ERR_SSL_PROTOCOL_ERROR")) == "ERR_SSL_PROTOCOL_ERROR"


def test_a_proxy_failure_is_named_too():
    """Самый ожидаемый сосед: прокси не отвечает."""
    assert rt.browser_error(_ErrorPage("ERR_PROXY_CONNECTION_FAILED")) == "ERR_PROXY_CONNECTION_FAILED"


def test_a_healthy_page_reports_nothing():
    """Узла нет — значит и экрана ошибки нет."""
    assert rt.browser_error(_ErrorPage("")) == ""


def test_a_page_that_throws_reports_nothing():
    assert rt.browser_error(_ErrorPage(raises=True)) == ""


def test_the_code_reaches_the_failure_context():
    """Иначе находка останется внутри функции и до записи об отказе не дойдёт."""
    ctx = rt.page_context(_ErrorPage("ERR_SSL_PROTOCOL_ERROR"))
    assert ctx["browser_error"] == "ERR_SSL_PROTOCOL_ERROR"


def test_a_working_page_carries_no_such_key():
    """Пустой ключ читался бы как «проверяли и не нашли», а проверять нечего."""
    assert "browser_error" not in rt.page_context(_ErrorPage(""))


def test_the_word_err_inside_a_live_page_is_not_an_error_screen():
    """`ERR_…` встречается и в скриптах живой страницы.

    Поэтому ищется узел `#main-frame-error`, а не слово по всему тексту:
    иначе каждый второй отказ сообщал бы о несуществующей потере связи.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "run_telemetry.py").read_text(encoding="utf-8")
    block = source[source.index("def browser_error"):]
    block = block[: block.index("\ndef ", 10)] if "\ndef " in block[10:] else block
    assert "#main-frame-error" in block, "экран ошибки ищется по тексту, а не по своему узлу"


# ── «застряло — обнови», но не везде ──────────────────────────────────────

class _RecoverPage:
    """Страница, у которой ошибка уходит после заданного числа перезагрузок."""

    def __init__(self, errors_until_ok):
        self._left = errors_until_ok
        self.reloads = 0
        self.url = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"

    def evaluate(self, js, *a):
        return "ERR_SSL_PROTOCOL_ERROR" if self._left > 0 else ""

    def reload(self, **kw):
        self.reloads += 1
        self._left -= 1

    def goto(self, url, **kw):
        self.reload()


def test_a_working_page_is_left_alone():
    """Перезагружать исправную страницу — терять время на ровном месте."""
    from src.fb_ads_rpa import meta_dom

    page = _RecoverPage(0)
    assert meta_dom._recover_from_browser_error(page) is False
    assert page.reloads == 0


def test_a_broken_page_is_reloaded_and_recovers(monkeypatch):
    from src.fb_ads_rpa import meta_dom

    monkeypatch.setattr(meta_dom, "_sleep_h", lambda *a, **k: None)
    page = _RecoverPage(1)
    assert meta_dom._recover_from_browser_error(page) is True
    assert page.reloads == 1


def test_a_page_that_stays_broken_gives_up(monkeypatch):
    """Прокси лежит — перезагружать вечно нельзя, отказ должен прийти."""
    from src.fb_ads_rpa import meta_dom

    monkeypatch.setattr(meta_dom, "_sleep_h", lambda *a, **k: None)
    page = _RecoverPage(99)
    assert meta_dom._recover_from_browser_error(page) is False
    assert page.reloads == 2, "число попыток не ограничено"


def test_the_break_is_written_down_even_if_it_healed(monkeypatch):
    """Связь рвалась — это важнее, чем то, что следующий шаг потом прошёл."""
    from src.fb_ads_rpa import meta_dom

    monkeypatch.setattr(meta_dom, "_sleep_h", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(meta_dom._telemetry, "record",
                        lambda *a, **k: calls.append((a, k)))
    meta_dom._recover_from_browser_error(_RecoverPage(1))
    assert calls and calls[0][0][0] == "browser_error"
    assert calls[0][1]["note"] == "ERR_SSL_PROTOCOL_ERROR"


def test_the_recovery_runs_where_nothing_is_entered_yet():
    """Правило намеренно не сплошное.

    На странице-ошибке терять нечего. Посреди мастера создания кампании та же
    перезагрузка стёрла бы несохранённые поля и превратила бы один отказ в
    потерянную кампанию.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    at = source.index("_recover_from_browser_error(page)", source.index("run_id = _telemetry.new_run_id()"))
    # ACL-1320: + определение `_adopt` перед `_open_campaign` и остановка
    # `profile_network`, когда страница ошибки сети пережила перезагрузки.
    after = source[at : at + 2800]
    # ACL-1320: Create жмёт `_open_campaign` — сразу за проверкой идёт он.
    assert 'name="Create"' in after or "_open_campaign(" in after, "проверка стоит не в начале потока"
    calls = source.count("_recover_from_browser_error(page)")
    assert calls <= 2, f"проверка расставлена в {calls} местах — правило стало сплошным"


# ── заблокированный экран ─────────────────────────────────────────────────
#
# Блокировка останавливает страницу как свёрнутое окно, только сильнее.
# 29 июля с окном в фоне шаги шли в 10-50 раз медленнее, загрузка креатива
# заняла 12 минут и «упала». 10 сентября то же с локскрином: 42 минуты на
# файл, который соседнее объявление приняло за 74 секунды — сразу после того,
# как человек вернулся к машине.

def _fake_procs(names):
    class _P:
        def __init__(self, n):
            self.info = {"name": n}
            self.pid = 100
    return lambda attrs=None: [_P(n) for n in names]


def test_the_lock_screen_process_is_the_signal(monkeypatch):
    monkeypatch.setattr(rt, "_process_session_id", lambda pid: 1)
    import psutil

    monkeypatch.setattr(psutil, "process_iter", _fake_procs(["chrome.exe", "LogonUI.exe"]))
    monkeypatch.setattr(rt.sys, "platform", "win32")
    assert rt.screen_locked() is True


def test_an_unlocked_machine_says_so(monkeypatch):
    monkeypatch.setattr(rt, "_process_session_id", lambda pid: 1)
    import psutil

    monkeypatch.setattr(psutil, "process_iter", _fake_procs(["chrome.exe", "explorer.exe"]))
    monkeypatch.setattr(rt.sys, "platform", "win32")
    assert rt.screen_locked() is False


def test_the_name_is_matched_regardless_of_case(monkeypatch):
    monkeypatch.setattr(rt, "_process_session_id", lambda pid: 1)
    import psutil

    monkeypatch.setattr(psutil, "process_iter", _fake_procs(["logonui.exe"]))
    monkeypatch.setattr(rt.sys, "platform", "win32")
    assert rt.screen_locked() is True


def test_elsewhere_the_answer_is_i_do_not_know(monkeypatch):
    """`None` честнее, чем False: на другой системе признака просто нет."""
    monkeypatch.setattr(rt.sys, "platform", "linux")
    assert rt.screen_locked() is None


def test_a_process_list_that_throws_does_not_break_the_record(monkeypatch):
    monkeypatch.setattr(rt, "_process_session_id", lambda pid: 1)
    import psutil

    def _boom(attrs=None):
        raise RuntimeError("access denied")

    monkeypatch.setattr(psutil, "process_iter", _boom)
    monkeypatch.setattr(rt.sys, "platform", "win32")
    assert rt.screen_locked() is None


def test_the_lock_reaches_the_failure_context(monkeypatch):
    """Иначе находка останется внутри функции и до записи об отказе не дойдёт."""
    monkeypatch.setattr(rt, "screen_locked", lambda: True)
    ctx = rt.page_context(_ErrorPage(""))
    assert ctx["screen_locked"] is True


def test_an_unlocked_run_carries_no_such_key(monkeypatch):
    """Ключ со значением False читался бы как повод о нём думать."""
    monkeypatch.setattr(rt, "screen_locked", lambda: False)
    assert "screen_locked" not in rt.page_context(_ErrorPage(""))


def test_diagnostic_tokens_are_removed_before_the_exportable_log(tmp_path):
    path = tmp_path / 'log.jsonl'
    account_id = '9' * 12  # generated synthetic identifier, never a live account
    rt.record('failed_step', False, path=path, diag={
        'url':f'https://example.test/?access_token=synthetic-secret&act={account_id}',
        'nested': [{'Authorization':'Bearer synthetic-secret','error':'Bearer synthetic-secret'}],
        'count':2,
    })
    text = path.read_text()
    assert 'synthetic-secret' not in text
    assert account_id not in text
    assert json.loads(text)['diag']['count'] == 2
