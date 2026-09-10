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
