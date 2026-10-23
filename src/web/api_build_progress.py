"""Процент сборки API-кампании — по созданным объектам (ACL-1992).

Модель ACL-1347 в `report.html` угадывала процент по именам шагов робота и их
типичному времени. API-путь пишет в телеметрию один шаг `api_create_step`, его
модель не узнаёт — процент стоял на одной цифре весь прогон.

Здесь процент честный: `done / total` объектов, которые прогон создаёт для
кампании. `total` известен из плана до первой записи; `done` растёт по строкам
прогресса, которые создатель (`src/fb_api/creator.py`) уже пишет. Самого
создателя не трогаем — его правят несколько открытых PR; опираемся только на
устойчивые начала строк («campaign:», «ad set …:», «ad N: …», «readback:»).

Единицы: предпроверка (1) + кампания (1) + группы + на объявление — медиа
(картинка 1, видео 2: загрузка и обработка) + креатив (1) + объявление (1) +
сверка (1). Формы (ACL-1776) в плане не видны заранее: каждая строка формы
добавляет единицу и в `done`, и в `total`.
"""
from __future__ import annotations

import re
from typing import Any

# Шаблоны привязаны к концу строки: имя группы или формы стоит внутри строки,
# и «ad set 'a: found': result unknown — reconciling» не должна засчитаться.
_AD = re.compile(r"^ad (\d+): (.+)$")
_AD_SET = re.compile(r"^ad set .+: (created ACTIVE \(campaign PAUSED\), tag kept|found from a previous run)$")
_FORM = re.compile(r"^form .+: (created|found on the Page, reused)$")
_AD_DONE = re.compile(r"^(image uploaded|image already in the ad account|video uploaded"
                      r"|video already in the ad account|video processed|creative created"
                      r"|creative found from a previous run|created ACTIVE \(campaign PAUSED\)|found from a previous run)$")


def _ad_units(ad: Any) -> int:
    media = 2 if str(getattr(ad, "kind", "") or "") == "video" else 1
    return media + 2   # медиа + креатив + объявление


def plan_total(plan: Any) -> int:
    """Сколько единиц у кампании по плану (без форм — они добавляются по ходу)."""
    total = 3   # предпроверка, кампания, сверка
    for ad_set in getattr(plan, "ad_sets", None) or []:
        total += 1
        total += sum(_ad_units(ad) for ad in getattr(ad_set, "ads", None) or [])
    return total


class BuildCounter:
    """Считает единицы по строкам прогресса одной кампании.

    Каждая единица засчитывается один раз (ключ), поэтому повтор строки —
    например, «video uploaded» дважды при сверке — процент не накручивает.
    Последующий этап подразумевает предыдущие: кампания создана — значит,
    предпроверка прошла, даже если её строка изменилась.
    """

    def __init__(self, plan: Any):
        self.total = plan_total(plan)
        self._kinds: dict[tuple[int, int], int] = {}
        self._set = -1      # номер текущей группы: номера объявлений в разных группах могут совпасть
        self._done: set[tuple] = set()
        self.last = ""
        for s, ad_set in enumerate(getattr(plan, "ad_sets", None) or []):
            for ad in getattr(ad_set, "ads", None) or []:
                self._kinds[(s, int(getattr(ad, "index", 0) or 0))] = _ad_units(ad) - 2

    @property
    def done(self) -> int:
        return min(len(self._done), self.total)

    def _mark(self, *keys: tuple) -> None:
        self._done.update(keys)

    def feed(self, text: str) -> bool:
        """Учесть строку прогресса; True — счёт изменился."""
        text = str(text or "").strip()
        if not text:
            return False
        before = (len(self._done), self.total)
        self.last = text[:200]
        if text == "preflight: creatives read":
            self._mark(("preflight",))
        elif _FORM.match(text):
            key = ("form", text.split(":", 1)[0])
            if key not in self._done:
                self.total += 1
                self._mark(key)
        elif text in ("campaign: created PAUSED, tag kept", "campaign: found from a previous run"):
            self._mark(("preflight",), ("campaign",))
        elif _AD_SET.match(text):
            self._set += 1
            self._mark(("preflight",), ("campaign",), ("set", self._set))
        elif text.startswith("readback:"):
            self._mark(("readback",))
        else:
            m = _AD.match(text)
            if m and _AD_DONE.match(m.group(2)):
                self._ad(int(m.group(1)), m.group(2))
        return (len(self._done), self.total) != before

    def _ad(self, n: int, what: str) -> None:
        s = max(self._set, 0)
        media = self._kinds.get((s, n), 1)
        key = ("ad", s, n)
        if what.startswith("found from a previous run"):
            self._mark(*[key + ("media", i) for i in range(media)], key + ("creative",), key + ("ad",))
        elif what.startswith(("image uploaded", "image already", "video uploaded", "video already")):
            self._mark(key + ("media", 0))   # у видео вторая единица — строка «processed»
        elif what.startswith("video processed"):
            self._mark(*[key + ("media", i) for i in range(media)])
        elif what.startswith("creative"):     # created / found from a previous run
            self._mark(*[key + ("media", i) for i in range(media)], key + ("creative",))
        elif what.startswith("created ACTIVE (campaign PAUSED)"):
            self._mark(*[key + ("media", i) for i in range(media)], key + ("creative",), key + ("ad",))

    def view(self) -> dict[str, Any]:
        return {"done": self.done, "total": self.total, "last": self.last}
