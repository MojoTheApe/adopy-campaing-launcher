"""Снимок страницы в момент мягкого отказа: разметка, обстановка, параметры шага.

Второй этап проекта Navigator, и он же — стенд, которого ждёт тестирование
`meta_dom`. Один и тот же снимок кормит обе задачи, и делать это дважды было бы
глупо: `meta_dom` — 3175 строк при 6% покрытия, а 103 из 136 его функций
принимают страницу и без разметки не проверяются никак.

**Почему разметка, а не только скриншот.** Скриншоты снимаются с самого начала
и говорят, как выглядело. На вопрос «почему не нашлась кнопка» отвечает то, что
было в DOM: как её теперь зовут, чем закрыта, в каком она контейнере. По
картинке селектор не напишешь и тест не построишь.

Четыре правила, и первое из них важнее остальных:

* **Снимки не уезжают с машины.** Строки телеметрии уходят в общую таблицу,
  которую читает вся компания, — а эти файлы остаются здесь и прикладываются к
  разбору руками. Разметка Ads Manager содержит имена, кампании и номера, и
  вычистить её до конца нельзя. Поэтому защита не в вычистке, а в том, что файл
  никуда не отправляется; вычистка — вторым слоем, на случай, если его всё-таки
  кому-то передадут.
* **Никогда не бросает и ограничен по времени.** То же правило, что у
  телеметрии: снимок не должен стоить кампании, а ожидание на упавшем шаге
  платит человек перед экраном.
* **Ограничен по объёму, и об урезании говорит вслух.** Разметка Ads Manager
  весит мегабайты, отказов за прогон бывает шесть. Молчаливое урезание читалось
  бы как «вот вся страница», и по обрезку писали бы селектор, которого там нет.
* **Старое подчищается.** Это ставится на машину каждого байера. Хранилище,
  которое только растёт, однажды кончится не вовремя.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.paths import get_runtime_workspace
from src.fb_ads_rpa.run_telemetry import scrub

#: Куда складывать. Рядом со скриншотами, но своей папкой: у снимков страниц
#: своя жизнь — их прикладывают к разбору и по ним строят стенд.
CAPTURE_DIR = "output/rpa_pages"

#: Сколько отказов за прогон снимать. Шесть — обычное число для прогона,
#: двенадцать оставляет запас и не даёт сорвавшемуся прогону залить диск.
MAX_PER_RUN = 12

#: Потолок на разметку одной страницы. Ads Manager укладывается в это с
#: запасом; что не уложилось, обрезается и **называется** обрезанным.
MAX_HTML_BYTES = 4_000_000

#: Сколько прогонов хранить. Дальше — самые старые удаляются.
KEEP_RUNS = 20

#: Адреса почты в разметке встречаются — имя сотрудника или клиента.
#:
#: Длины частей ограничены не для красоты. Без потолков `[A-Za-z0-9._%+-]+`
#: на странице без единой собачки заставляет разборщик пройти хвост заново от
#: каждой позиции — время растёт квадратом от размера. На четырёх мегабайтах
#: разметки Ads Manager это вешает прогон намертво; поймано собственным тестом
#: до того, как дошло до живой машины.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}")

_taken: dict[str, int] = {}


def _root() -> Path:
    return Path(get_runtime_workspace()) / CAPTURE_DIR


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))[:60] or "step"


def _cut(text: str) -> tuple[str, bool]:
    raw = text.encode("utf-8", "replace")
    if len(raw) <= MAX_HTML_BYTES:
        return text, False
    return raw[:MAX_HTML_BYTES].decode("utf-8", "ignore"), True


def scrub_html(html: str) -> tuple[str, bool]:
    """Убрать опознавательные знаки и подрезать по потолку.

    Возвращает `(разметка, обрезано ли)` — второе значение и есть тот самый
    честный ответ на вопрос «это вся страница?».

    **Режем до вычистки, а не после.** Разбор выражениями стоит времени,
    пропорционального длине, и платить за него на куске, который всё равно
    будет отброшен, незачем. Второй проход после вычистки — потому что
    `<hidden>` и `<email>` бывают длиннее того, что заменили.
    """
    text, truncated = _cut(html or "")
    text = _EMAIL.sub("<email>", scrub(text))
    text, again = _cut(text)
    return text, truncated or again


def prune(keep: int = KEEP_RUNS) -> int:
    """Оставить последние `keep` прогонов. Возвращает, сколько удалено."""
    root = _root()
    if not root.is_dir():
        return 0
    try:
        runs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    except OSError:
        return 0
    removed = 0
    for old in runs[: max(0, len(runs) - keep)]:
        try:
            shutil.rmtree(old, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def capture(
    page,
    *,
    step: str,
    run_id: str = "",
    campaign: str = "",
    diag: dict[str, Any] | None = None,
    timeout_ms: int = 5000,
) -> str:
    """Снять страницу упавшего шага. Возвращает путь к папке снимка или "".

    Пустая строка — обычный ответ, а не сбой: снимков за прогон может быть
    больше положенного, страница может быть уже закрыта. Ни то, ни другое не
    повод уронить прогон.
    """
    try:
        key = run_id or "no_run"
        if _taken.get(key, 0) >= MAX_PER_RUN:
            return ""

        started = time.time()
        try:
            html = page.content()
        except Exception:
            return ""

        body, truncated = scrub_html(html)
        folder = _root() / _safe(key) / f"{_taken.get(key, 0):02d}_{_safe(step)}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "page.html").write_text(body, encoding="utf-8")

        try:
            url = scrub(str(getattr(page, "url", "") or ""))
        except Exception:
            url = ""
        (folder / "context.json").write_text(
            json.dumps(
                {
                    "step": step,
                    "run_id": run_id,
                    "campaign": campaign,
                    "url": url,
                    "diag": diag or {},
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "html_bytes": len(body.encode("utf-8", "replace")),
                    # Обрезанная разметка, поданная как полная, — это селектор,
                    # написанный по тому, чего на странице нет.
                    "html_truncated": truncated,
                    "took_s": round(time.time() - started, 2),
                    # Эти файлы никуда не отправляются. Написано здесь же,
                    # чтобы тот, кто найдёт папку через месяц, знал правило.
                    "never_uploaded": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        _taken[key] = _taken.get(key, 0) + 1
        if _taken[key] == 1:
            prune()
        return str(folder)
    except Exception:
        # Снимок — помощь при разборе, а не условие работы.
        return ""
