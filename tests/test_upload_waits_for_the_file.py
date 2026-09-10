"""Тяжёлый креатив грузится дольше, чем длится наш счёт до шестидесяти.

10 сентября видео грузилось девять минут. Код ждал фиксированные шестьдесят
проходов по секунде, досчитал своё и объявил отказ — а на экране в этот момент
было «Uploading video 2_ar_2221_story.mp4» и кнопка Cancel. Объявление
осталось без креатива.

Размер файла коду неизвестен: картинка в полмегабайта и видео на сотню — один
и тот же шаг. Никакое фиксированное число проходов не будет верным для обоих,
поэтому ждать надо по состоянию страницы.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom  # noqa: E402


class _Page:
    """Страница, которая отвечает заранее заданной чередой состояний."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.asked = 0

    def evaluate(self, js, *a):
        self.asked += 1
        cur = self._texts[min(self.asked - 1, len(self._texts) - 1)]
        return any(t.startswith("Uploading ") for t in cur)


def test_it_says_yes_while_the_page_is_uploading():
    assert meta_dom._upload_in_progress(_Page([["Uploading video x.mp4"]])) is True


def test_it_says_no_on_a_quiet_page():
    assert meta_dom._upload_in_progress(_Page([["Edit media"]])) is False


def test_a_page_that_throws_is_not_taken_for_uploading():
    """Иначе застрявший шаг ждал бы до самого потолка."""

    class _Broken:
        def evaluate(self, js, *a):
            raise RuntimeError("target closed")

    assert meta_dom._upload_in_progress(_Broken()) is False


def test_the_pattern_matches_what_meta_actually_writes():
    """Текст снят с настоящего снимка отказа 10 сентября."""
    assert meta_dom._UPLOADING_TEXT.match("Uploading video 2_ar_2221_story.mp4")
    assert meta_dom._UPLOADING_TEXT.match("Uploading image banner.png")
    for other in ("Upload", "Uploaded video x.mp4", "Reuploading video x.mp4"):
        assert not meta_dom._UPLOADING_TEXT.match(other), f"{other!r} принят за загрузку"


# ── как устроено ожидание ─────────────────────────────────────────────────

def _wait_block() -> str:
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    start = source.index("    # Ждём по состоянию, а не по счёту итераций")
    return source[start : source.index("# Uploading does NOT always select the tile", start)]


def test_the_wait_asks_the_page_instead_of_counting():
    """Сторож: возврат к счёту проходов вернёт и отказ на тяжёлом файле."""
    block = _wait_block()
    assert "_upload_in_progress(page)" in block, "ожидание снова не спрашивает страницу"
    assert "for _ in range(60)" not in block, "вернулся фиксированный счёт проходов"


def test_the_idle_counter_resets_only_while_uploading():
    """Смысл в этой строке: идёт загрузка — простой не копится."""
    assert "idle = 0 if _upload_in_progress(page) else idle + 1" in _wait_block()


def test_a_stuck_upload_still_ends():
    """Ожидание без потолка — это прогон, который не кончится никогда.

    Надпись может остаться висеть; отказ через двадцать минут читаем, сутки
    ожидания — нет.
    """
    block = _wait_block()
    assert "_UPLOAD_MAX_S" in block, "у ожидания нет общего потолка"
    assert 10 * 60 <= meta_dom._UPLOAD_MAX_S <= 60 * 60, (
        f"потолок {meta_dom._UPLOAD_MAX_S}с — либо не переживёт тяжёлое видео, либо слишком долгий"
    )


def test_a_quiet_page_still_gives_up_at_about_a_minute():
    """Раньше семидесяти секунд хватало, когда грузить нечего. Так и осталось."""
    block = _wait_block()
    assert "idle < 60" in block, "порог простоя изменился — проверить, что шаг не висит зря"


# ── проверка «лёг ли креатив» ─────────────────────────────────────────────
#
# Здесь и был настоящий отказ 10 сентября. Первый цикл ожидания тоже считал
# проходы, но объявление признали пустым именно тут — на 537-й секунде, при
# видимой на экране надписи «Uploading video 2_ar_2221_story.mp4».

def _verify_block() -> str:
    """Тело функции без строк-комментариев.

    Комментарий, описывающий убранную конструкцию, — не её возвращение.
    Сторож, читающий текст целиком, ловит собственное объяснение: за эту
    сессию так вышло трижды.
    """
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    start = source.index("def _verify_ad_media_present")
    body = source[start : source.index("\ndef ", start + 10)]
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))


def test_the_media_check_counts_only_quiet_passes():
    """Раньше стояло `for _ in range(150)` с припиской «только пока идёт работа».

    Приписка была неверной: счёт шёл всегда, и две с половиной минуты
    кончались задолго до конца тяжёлой загрузки.
    """
    block = _verify_block()
    assert "for _ in range(150)" not in block, "вернулся счёт проходов независимо от состояния"
    assert "while idle < 150" in block, "порог тишины пропал"
    assert "idle += 1" in block, "проходы не считаются вовсе — шаг будет висеть до потолка"


def test_the_media_check_does_not_count_while_uploading():
    """Ветка загрузки уходит на следующий круг, не трогая счётчик тишины."""
    block = _verify_block()
    upload_branch = block[block.index('if state.get("uploading")'):]
    upload_branch = upload_branch[: upload_branch.index("continue")]
    assert "idle" not in upload_branch, "счётчик тишины растёт во время загрузки"


def test_the_media_check_still_ends_on_a_stuck_page():
    assert "_UPLOAD_MAX_S" in _verify_block(), "у проверки нет потолка по часам"


# ══ застрявшая загрузка ═══════════════════════════════════════════════════
#
# 10 сентября два объявления подряд простояли 537 и 327 секунд с надписью
# «Uploading video 2_ar_2221_story.mp4», и в обоих снимках отказа
# aria-valuenow="0" при файле в 25.8 МБ. Загрузка не шла медленно — она не
# начиналась. Те же 25 МБ в других попытках ушли за 62 и 90 секунд.

class _ProgressPage:
    """Страница с индикатором, отдающая заданную череду процентов."""

    def __init__(self, values, uploading=True):
        self._values = list(values)
        self._uploading = uploading
        self.reads = 0

    def evaluate(self, js, *a):
        if "Uploading" in js:
            return self._uploading
        self.reads += 1
        return str(self._values[min(self.reads - 1, len(self._values) - 1)])


def test_the_progress_is_read_from_metas_own_indicator():
    assert meta_dom._upload_progress(_ProgressPage([37])) == 37


def test_no_indicator_means_no_answer():
    """`None` — это «не знаю», и застоем его считать нельзя."""

    class _NoBar:
        def evaluate(self, js, *a):
            return None

    assert meta_dom._upload_progress(_NoBar()) is None


def test_a_frozen_bar_is_called_stalled(monkeypatch):
    monkeypatch.setattr(meta_dom.time, "sleep", lambda s: None)
    assert meta_dom._upload_stalled(_ProgressPage([0, 0])) is True


def test_a_moving_bar_is_not(monkeypatch):
    """Ноль бывает и в первую секунду настоящей загрузки."""
    monkeypatch.setattr(meta_dom.time, "sleep", lambda s: None)
    assert meta_dom._upload_stalled(_ProgressPage([0, 4])) is False


def test_a_finished_upload_is_not_stalled(monkeypatch):
    """Надпись пропала между замерами — значит всё получилось."""
    monkeypatch.setattr(meta_dom.time, "sleep", lambda s: None)

    class _Finishing(_ProgressPage):
        def evaluate(self, js, *a):
            if "Uploading" in js:
                self._uploading = not self._uploading
                return not self._uploading
            return super().evaluate(js, *a)

    assert meta_dom._upload_stalled(_Finishing([0, 0])) is False


def test_a_page_with_nothing_uploading_is_not_stalled(monkeypatch):
    monkeypatch.setattr(meta_dom.time, "sleep", lambda s: None)
    assert meta_dom._upload_stalled(_ProgressPage([0, 0], uploading=False)) is False


# ── что делает повтор ─────────────────────────────────────────────────────

def _retry_block() -> str:
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    start = source.index("def upload_ad_creative_with_retry")
    body = source[start : source.index("\ndef _upload_progress", start)]
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))


def test_a_stall_reloads_before_trying_again():
    """Повтор на той же странице застоя не лечит: состояние висит во вкладке."""
    block = _retry_block()
    assert "_upload_stalled(page)" in block, "застой не распознаётся"
    assert "_reload_editor(page, edit_url)" in block, "страница не перезагружается"


def test_the_stall_is_written_down():
    """Молчаливая перезагрузка — потерянный след.

    Отмена и повтор могут оставить в библиотеке аккаунта недогруженный файл;
    об этом должно остаться, что искать.
    """
    assert 'record("creative_upload_stalled"' in _retry_block()


def test_the_reload_stays_inside_the_profile():
    """Красная линия: перезапуск браузера снимает прокси. Здесь его нет."""
    source = (ROOT / "src" / "fb_ads_rpa" / "meta_dom.py").read_text(encoding="utf-8")
    start = source.index("def _reload_editor")
    block = source[start : source.index("\ndef ", start + 10)]
    for forbidden in ("launch(", "new_context", "close(", "connect_over_cdp"):
        assert forbidden not in block, f"перезагрузка трогает браузер: {forbidden}"
    assert "page.goto" in block and "page.reload" in block


def test_the_reload_does_not_happen_after_the_last_attempt():
    """Перезагружать редактор, чтобы тут же сдаться, — потерянная минута."""
    assert "attempt + 1 < attempts" in _retry_block()
