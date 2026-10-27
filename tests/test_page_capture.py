"""Снимок страницы в момент отказа: разметка, обстановка, границы.

Второй этап Navigator и одновременно стенд для `meta_dom` — 3175 строк при 6%
покрытия, где 103 из 136 функций принимают страницу и без разметки не
проверяются никак.

Скриншоты снимались с самого начала и говорят, **как выглядело**. На вопрос
«почему не нашлась кнопка» отвечает то, что было в DOM: как её теперь зовут,
чем закрыта, в каком контейнере. По картинке селектор не напишешь.

Проверяется здесь не содержимое разметки Meta, а наши собственные границы:
объём, счёт, вычистка и то, что снимок не может уронить прогон.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import page_capture as pc  # noqa: E402


class _Page:
    def __init__(self, html="<html><body>ok</body></html>", url="https://x.example/p", raises=False):
        self._html = html
        self.url = url
        self._raises = raises

    def locator(self, selector):
        assert selector == "html"
        return self

    def inner_html(self, *, timeout):
        assert timeout > 0
        if self._raises:
            raise RuntimeError("target closed")
        return self._html


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "get_runtime_workspace", lambda: tmp_path)
    pc._taken.clear()
    return tmp_path


def _read(folder, name):
    return (Path(folder) / name).read_text(encoding="utf-8")


def _scrub_elsewhere(size: int, seconds: int = 20) -> float:
    """Вычистить страницу такого размера в отдельном процессе. Секунды или отказ.

    **Потоком это не ограничить.** Разбор регулярных выражений в CPython не
    отпускает GIL, поэтому главный поток не просыпается по таймауту, пока
    выражение считает, — проверка не падает, а вешает весь прогон вместе с
    собой. Первая версия этого файла делала именно так и висела по пять минут.

    Сторож, вешающий прогон вместо отказа, хуже отсутствующего: его выключают,
    а не чинят. Подпроцесс убивается по времени, чего бы он ни считал.
    """
    import subprocess
    import sys as _sys
    import time

    code = (
        "import sys, time; sys.path.insert(0, %r);"
        "from src.fb_ads_rpa import page_capture as pc;"
        "t=time.time(); pc.scrub_html('<div>'+('x'*%d)+'</div>'); print(time.time()-t)"
        % (str(ROOT), size)
    )
    started = time.time()
    try:
        done = subprocess.run(
            [_sys.executable, "-c", code], capture_output=True, text=True, timeout=seconds
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"вычистка {size} байт не закончилась за {seconds}с — вернулось квадратичное время"
        )
    assert done.returncode == 0, done.stderr[-400:]
    return time.time() - started


# ── ловушка, на которой этот файл однажды повис ───────────────────────────
#
# Стоит первой намеренно: она падает за миллисекунды, до всего, что работает
# с большими страницами. Иначе прогон встанет раньше, чем дойдёт до неё.

def test_the_email_pattern_stays_bounded():
    """Потолки в выражении — не украшение, а защита от зависания.

    Без них `[A-Za-z0-9._%+-]+@` на странице **без единой собачки** заставляет
    разборщик проходить хвост заново от каждой позиции: время растёт квадратом
    от размера. На четырёх мегабайтах разметки Ads Manager прогон встаёт
    намертво.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "page_capture.py").read_text(encoding="utf-8")
    line = [ln for ln in source.splitlines() if ln.startswith("_EMAIL")][0]
    assert "+@" not in line, "безграничная часть до собачки вернулась"
    assert "{1,64}" in line


def test_the_cut_happens_before_the_scrub(monkeypatch):
    """Иначе платим разбором за кусок, который всё равно будет отброшен."""
    from config import token_vault

    monkeypatch.setattr(pc, "MAX_HTML_BYTES", 100)
    monkeypatch.setattr(token_vault, "_secrets_snapshot", lambda: (frozenset(), ()))
    seen = []

    def general_scrub(text):
        seen.append(text)
        assert len(text.encode("utf-8")) <= pc.MAX_HTML_BYTES
        return text

    monkeypatch.setattr(pc, "scrub", general_scrub)
    text, truncated = pc.scrub_html("é" * 1000)
    assert seen and truncated and len(text.encode("utf-8")) <= 100


# ── что снимается ─────────────────────────────────────────────────────────

def test_the_markup_and_the_context_land_next_to_each_other():
    folder = pc.capture(_Page(), step="ad_copy", run_id="r1", campaign="SA_test", diag={"dialogs": 1})
    assert folder, "снимок не сделан"
    assert "ok" in _read(folder, "page.html")
    ctx = json.loads(_read(folder, "context.json"))
    assert ctx["step"] == "ad_copy" and ctx["campaign"] == "SA_test"
    assert ctx["diag"] == {"dialogs": 1}, "причина отказа не сохранена рядом с разметкой"


def test_the_folder_is_named_so_the_order_of_failures_survives():
    """Разбирают их по порядку: что упало первым, что за ним."""
    first = pc.capture(_Page(), step="placements", run_id="r1")
    second = pc.capture(_Page(), step="ad_copy", run_id="r1")
    assert Path(first).name.startswith("00_") and Path(second).name.startswith("01_")


def test_captures_of_different_runs_do_not_mix():
    a = pc.capture(_Page(), step="ad_copy", run_id="r1")
    b = pc.capture(_Page(), step="ad_copy", run_id="r2")
    assert Path(a).parent.name != Path(b).parent.name


# ── границы ───────────────────────────────────────────────────────────────

def test_a_run_cannot_fill_the_disk():
    """Прогон бывает сорвавшимся, и тогда отказ идёт за отказом."""
    for i in range(pc.MAX_PER_RUN + 5):
        pc.capture(_Page(), step=f"s{i}", run_id="r1")
    saved = list((Path(pc._root()) / "r1").iterdir())
    assert len(saved) == pc.MAX_PER_RUN


def test_oversized_markup_is_cut_and_says_so():
    """Молчаливое урезание читалось бы как «вот вся страница».

    По такому обрезку напишут селектор, которого на странице нет.
    """
    huge = "<div>" + ("x" * (pc.MAX_HTML_BYTES + 5000)) + "</div>"
    folder = pc.capture(_Page(html=huge), step="ad_copy", run_id="r1")
    ctx = json.loads(_read(folder, "context.json"))
    assert ctx["html_truncated"] is True
    assert ctx["html_bytes"] <= pc.MAX_HTML_BYTES


def test_markup_that_fits_is_not_marked_as_cut():
    folder = pc.capture(_Page(), step="ad_copy", run_id="r1")
    assert json.loads(_read(folder, "context.json"))["html_truncated"] is False


def test_old_runs_are_pruned():
    """Ставится на машину каждого байера. Растущее вечно хранилище кончится не вовремя."""
    for i in range(pc.KEEP_RUNS + 4):
        pc._taken.clear()
        pc.capture(_Page(), step="s", run_id=f"run{i:03d}")
    assert len(list(Path(pc._root()).iterdir())) <= pc.KEEP_RUNS


# ── вычистка ──────────────────────────────────────────────────────────────

def test_an_account_number_does_not_survive_in_the_markup():
    html = '<a href="/manage/campaigns?act=1234567890123456">Ads</a>'
    folder = pc.capture(_Page(html=html), step="ad_copy", run_id="r1")
    saved = _read(folder, "page.html")
    assert "1234567890123456" not in saved
    assert "act=<hidden>" in saved


def test_an_email_does_not_survive_in_the_markup():
    """В разметке попадаются адреса — сотрудника или клиента."""
    folder = pc.capture(_Page(html="<span>someone@example.com</span>"), step="s", run_id="r1")
    saved = _read(folder, "page.html")
    assert "someone@example.com" not in saved and "<email>" in saved


def test_the_context_says_the_files_stay_here():
    """Правило должно быть внутри папки, а не только в голове того, кто её создал.

    Строки телеметрии уезжают в общую таблицу; эти файлы — нет. Тот, кто найдёт
    папку через месяц, должен прочитать это в ней самой.
    """
    folder = pc.capture(_Page(), step="s", run_id="r1")
    assert json.loads(_read(folder, "context.json"))["never_uploaded"] is True


def test_nothing_here_is_wired_into_the_telemetry_upload():
    """Снимок, уехавший в общую таблицу, — это утечка разметки живого кабинета."""
    sync = (ROOT / "src" / "fb_ads_rpa" / "telemetry_sync.py").read_text(encoding="utf-8")
    assert "page_capture" not in sync and pc.CAPTURE_DIR not in sync


# ── снимок не может уронить прогон ────────────────────────────────────────

def test_a_page_that_is_gone_returns_nothing_and_does_not_raise():
    assert pc.capture(_Page(raises=True), step="ad_copy", run_id="r1") == ""


def test_an_unwritable_place_does_not_raise(monkeypatch):
    monkeypatch.setattr(pc, "_root", lambda: (_ for _ in ()).throw(OSError("нет места")))
    assert pc.capture(_Page(), step="ad_copy", run_id="r1") == ""


# ── где это включено ──────────────────────────────────────────────────────

def test_the_capture_runs_only_on_failures():
    """Снимать удачные шаги значило бы писать мегабайты на каждый шаг."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("def _shot(label: str"):]
    block = block[: block.index("_telemetry.record(")]
    assert "page_capture" in block, "снимок не подключён к точке записи"
    assert block.count("ok is False") >= 1


def test_the_saved_path_reaches_the_telemetry_row():
    """Иначе снимок есть, а найти его по записи об отказе нельзя."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index("def _shot(label: str"):]
    block = block[: block.index("_telemetry.record(")]
    assert '"page": saved' in block


# ── время ─────────────────────────────────────────────────────────────────

def test_scrubbing_a_large_page_finishes_in_seconds():
    """Настоящая проверка времени: считает отдельный процесс, его можно убить.

    Статический сторож выше ловит известную ловушку. Этот ловит любую другую —
    если завтра в вычистку добавят выражение, которое ведёт себя так же.
    """
    took = _scrub_elsewhere(pc.MAX_HTML_BYTES + 5000)
    assert took < 20


# ── номер прогона ─────────────────────────────────────────────────────────

def test_a_capture_finds_the_run_it_belongs_to_without_being_told():
    """Иначе снимок ложится в «no_run» и к прогону его потом не привязать.

    Так 9 сентября записался `create_additional_ad`: он пишет в телеметрию
    напрямую, номера прогона не знал, и при разборе его пришлось искать по
    времени среди чужих строк.
    """
    from src.fb_ads_rpa import run_telemetry as tel

    tel.set_current_run("run77", "SA_test")
    try:
        folder = pc.capture(_Page(), step="create_additional_ad")
        assert Path(folder).parent.name == "run77"
        assert json.loads(_read(folder, "context.json"))["campaign"] == "SA_test"
    finally:
        tel.clear_current_run()


def test_an_explicit_run_id_still_wins():
    from src.fb_ads_rpa import run_telemetry as tel

    tel.set_current_run("run77", "SA_test")
    try:
        folder = pc.capture(_Page(), step="s", run_id="explicit")
        assert Path(folder).parent.name == "explicit"
    finally:
        tel.clear_current_run()


def test_the_create_step_saves_its_markup_too():
    """Сторож: шаг, писавший мимо общей точки, снова остался бы без снимка."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    block = source[source.index('def _diag(stage: str'):]
    block = block[: block.index('_telemetry.record(')]
    assert "page_capture" in block, "у create_additional_ad снова нет снимка"


def test_selected_block_is_saved_scrubbed_even_after_page_truncation():
    class Page(_Page):
        def evaluate(self, js, expression):
            assert expression == "document.querySelector('#group')"
            return '<div id="group"><span>someone@example.com</span><button>Close</button></div>'
    page = Page(html="x" * (pc.MAX_HTML_BYTES + 20))
    folder = pc.capture(page, step="targeting", run_id="block-run",
                        blocks={"groups": "document.querySelector('#group')"})
    saved = _read(folder, "block_groups.html")
    assert "Close" in saved and "someone@example.com" not in saved
    ctx = json.loads(_read(folder, "context.json"))
    assert ctx["html_truncated"] is True
    assert ctx["blocks"]["groups"]["found"] is True
    assert ctx["blocks"]["groups"]["truncated"] is False


def test_a_missing_block_is_recorded_without_discarding_the_page():
    class Page(_Page):
        def evaluate(self, js, expression):
            return None
    folder = pc.capture(Page(), step="targeting", run_id="missing-block",
                        blocks={"groups": "document.querySelector('#missing')"})
    assert _read(folder, "page.html")
    assert json.loads(_read(folder, "context.json"))["blocks"]["groups"] == {"found": False}
