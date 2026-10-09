"""Пассивная запись шагов мастера креатива (ACL-1426, фаза A).

Зачем. После выбора файла в медиатеке (и после загрузки) Meta открывает мастер
Trim → Crop → Text → Image generation → Enhancements → Translation, и робот
проходит его кнопкой «вперёд» до закрытия (`meta_dom._walk_creative_wizard`).
Шаг Crop («Select media crop for placements»: карточки Square и Vertical,
Original / 9:16, «Crop and expand (Meta AI)») при этом остаётся с умолчаниями
Meta, а снимка ни одного шага мастера после Media у нас нет: страница редактора
режется на 4 МБ раньше портала мастера. Фаза B (настоящая работа с Crop)
пишется по этим снимкам.

Что делает. Пока обход видит открытый мастер, на каждом круге ДО нажатия:

* читает строку шагов, текущий шаг, заголовки тела шага, видимые кнопки и
  отметки (checkbox/radio/switch) мастера — одним `evaluate`, только чтение;
* первый раз за обход увидев шаг, снимает блок мастера (вычищенный
  `page_capture.scrub_html`) в `output/rpa_pages/<прогон>/NN_wizard_<шаг>` —
  только после выбора в медиатеке или после загрузки, не больше
  MAX_PER_RUN за прогон, своей квотой (лимит снимков отказов не трогается);
* в конце обхода пишет одну строку телеметрии `ad_creative_wizard`:
  откуда пришли, последовательность шагов с временем и нажатой кнопкой.

Чего не делает. Ничего не нажимает, не прокручивает и не ждёт; любой сбой
внутри глотается. Набор и порядок нажатий обхода с записью и без неё один и
тот же (tests/test_wizard_steps_are_captured_passively.py).

Выключить снимки на машине: `FB_RPA_WIZARD_CAPTURE=0` (строка телеметрии
остаётся — она дешёвая).
"""
from __future__ import annotations

import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from src.fb_ads_rpa import run_telemetry as _telemetry

#: Машинная настройка снимков шагов мастера. По умолчанию включено.
SETTING = "FB_RPA_WIZARD_CAPTURE"

#: Снимков шагов мастера за прогон: три объявления × шесть шагов.
MAX_PER_RUN = 18

#: Квота в `page_capture.capture`.
QUOTA = "wizard"

#: Откуда пришли в мастер — когда снимать (выбор в медиатеке или загрузка).
CAPTURE_ORIGINS = ("library_pick", "upload")

#: Подписи шагов мастера (как `meta_dom._WIZARD_STEP_NAMES`).
STEP_NAMES = ("Creative setup", "Media", "Trim", "Crop", "Text", "Image generation",
              "Creative generation", "Enhancements", "Translation")

#: Сколько кругов обхода держать в строке телеметрии.
_MAX_SEEN = 24

#: Мастер и что на нём видно — только чтение. Мастер ищется так же, как
#: `page_capture.CREATIVE_WIZARD_BLOCK_JS`: открытый `role=dialog` с тремя и
#: больше подписями шагов, иначе предок подписи «Media» с тремя подписями и
#: кнопкой Next/Cancel/Done. Текущий шаг — пункт строки с `aria-current` /
#: `aria-selected` (разметка 25.09, фикстура creative_wizard_steps_2026_09_25).
STEP_STATE_JS = r"""
(names) => {
  const norm = (s) => (s || '').replace(/[\u200b-\u200d\ufeff]/g, '').replace(/\s+/g, ' ').trim();
  const box = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const hits = (el) => { const t = norm(el.textContent); return names.filter(n => t.includes(n)).length; };
  const nav = ['Next', 'Cancel', 'Done'];
  let root = [...document.querySelectorAll('[role=dialog]')].find(d => box(d) && hits(d) >= 3) || null;
  if (!root) {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    outer: for (let t = w.nextNode(); t; t = w.nextNode()) {
      if (norm(t.nodeValue) !== 'Media') continue;
      for (let n = t.parentElement, i = 0; n && n !== document.body && i < 15; n = n.parentElement, i++) {
        if (hits(n) >= 3 && box(n) && [...n.querySelectorAll('[role=button],button')]
            .some(b => nav.includes(norm(b.textContent)))) { root = n; break outer; }
      }
    }
  }
  if (!root) return { open: false };
  const steps = [];
  let current = null;
  for (const e of root.querySelectorAll('*')) {
    if (e.children.length || !box(e)) continue;
    const t = norm(e.textContent);
    if (!names.includes(t) || steps.includes(t)) continue;
    steps.push(t);
    for (let n = e, i = 0; n && n !== root && i < 7; n = n.parentElement, i++) {
      const cur = n.getAttribute('aria-current');
      if ((cur && cur !== 'false') || n.getAttribute('aria-selected') === 'true') {
        if (!current) current = t;
        break;
      }
    }
  }
  const short = (s, n) => (s.length > n ? s.slice(0, n) : s);
  const headings = [...root.querySelectorAll('[role=heading],h1,h2,h3,h4')]
    .filter(box).map(e => short(norm(e.textContent), 80)).filter(Boolean).slice(0, 6);
  const buttons = [];
  for (const b of root.querySelectorAll('[role=button],button')) {
    if (!box(b)) continue;
    const t = short(norm(b.getAttribute('aria-label') || b.textContent), 40);
    if (!t || names.includes(t) || buttons.some(x => x.label === t)) continue;
    buttons.push({ label: t, disabled: b.getAttribute('aria-disabled') === 'true' || b.disabled === true });
    if (buttons.length >= 15) break;
  }
  const checks = [];
  for (const c of root.querySelectorAll(
      '[role=checkbox],[role=radio],[role=switch],input[type=checkbox],input[type=radio]')) {
    let label = c.getAttribute('aria-label') || '';
    if (!label) {
      const by = c.getAttribute('aria-labelledby');
      if (by) label = by.split(/\s+/).map(id => { const e = document.getElementById(id); return e ? e.textContent : ''; }).join(' ');
    }
    if (!label) {
      const l = c.closest('label') || (c.parentElement && c.parentElement.parentElement);
      label = l ? l.textContent : '';
    }
    const aria = c.getAttribute('aria-checked');
    checks.push({
      role: c.getAttribute('role') || c.type || '',
      label: short(norm(label), 60),
      checked: aria !== null ? aria === 'true' : !!c.checked,
    });
    if (checks.length >= 20) break;
  }
  return { open: true, steps, current, headings, buttons, checks };
}
"""


def capture_enabled() -> bool:
    """Снимать ли шаги мастера (машинная настройка, по умолчанию да)."""
    value = (os.environ.get(SETTING) or "").strip().lower()
    return value not in ("0", "false", "no", "off")


def step_label(name: str | None) -> str:
    """`wizard_crop`, `wizard_image_generation`, … ; `wizard_unknown`, если имени нет."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")[:30]
    return "wizard_" + (slug or "unknown")


_origin_tls = threading.local()


@contextmanager
def origin(name: str, file: str = "") -> Iterator[None]:
    """Обходы мастера внутри блока пришли из `name` (library_pick / upload)."""
    before = getattr(_origin_tls, "value", None)
    _origin_tls.value = (name, file)
    try:
        yield
    finally:
        _origin_tls.value = before


def current_origin() -> tuple[str, str]:
    value = getattr(_origin_tls, "value", None)
    return value if value else ("", "")


def read_state(page) -> dict[str, Any] | None:
    """Состояние мастера на экране (`STEP_STATE_JS`); None — не прочиталось."""
    try:
        state = page.evaluate(STEP_STATE_JS, list(STEP_NAMES))
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _step_name(state: dict[str, Any]) -> str:
    current = state.get("current")
    if current:
        return str(current)
    for heading in state.get("headings") or ():
        if heading in STEP_NAMES:
            return str(heading)
    heads = state.get("headings") or ()
    return str(heads[0]) if heads else ""


class Trail:
    """Дневник одного обхода мастера. Ни один метод не бросает."""

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 capture: Callable[..., str] | None = None) -> None:
        self.origin, self.file = current_origin()
        self._clock = clock
        self._capture = capture
        self._started = clock()
        self.seen: list[dict[str, Any]] = []
        self.captured: dict[str, str] = {}
        #: The step on screen at the last `see` ("" when unread).
        self.current = ""
        #: ACL-1551: чем мастер закрыт (подпись нажатой кнопки), остался ли
        #: на экране диалог, выход своей кнопкой мастера и ожидания «занят».
        self.closed_by: str | None = None
        self.dialog_left: bool | None = None
        self.exit: dict[str, Any] | None = None
        self.busy_waits: list[float] = []
        #: ACL-1736: первый шаг ДО Crop, на который мастер вернулся с Crop.
        self.looped_back_to = ""

    def loop_back(self, step: str) -> None:
        """ACL-1736: Next на Crop привёл на более ранний шаг (Media и т.п.)."""
        if not self.looped_back_to:
            self.looped_back_to = step

    def see(self, page) -> None:
        """Прочитать текущий шаг и, если он новый для обхода, снять его."""
        self.current = ""
        try:
            state = read_state(page)
            if not state or not state.get("open"):
                return
            name = _step_name(state)
            self.current = name
            at = round(self._clock() - self._started, 1)
            if self.seen and self.seen[-1]["step"] == name:
                return   # тот же шаг на следующем круге — не новая запись
            if len(self.seen) >= _MAX_SEEN:
                return
            self.seen.append({"step": name, "at_s": at, "pressed": []})
            label = step_label(name)
            if label in self.captured or self.origin not in CAPTURE_ORIGINS or not capture_enabled():
                return
            self.captured[label] = self._snap(page, label, state)
        except Exception:
            return

    def pressed(self, label: str | None) -> None:
        try:
            if self.seen and len(self.seen[-1]["pressed"]) < 6:
                # None — на этом круге кнопки «вперёд» не нашлось.
                self.seen[-1]["pressed"].append(label)
        except Exception:
            return

    def exited(self, out: dict[str, Any]) -> None:
        """ACL-1551: мастер покинут своей кнопкой (Save/Done или Close)."""
        try:
            self.exit = {k: out.get(k) for k in ("closed_by", "closed", "dialog_left", "tried",
                                                   "why", "step", "next_presses", "looped_back_to")}
            if out.get("closed"):
                self.closed_by = out.get("closed_by")
        except Exception:
            return

    def busy(self, seconds: float) -> None:
        """ACL-1551: сколько ждали, пока мастер «занят» (Next/Cancel заперты)."""
        try:
            if len(self.busy_waits) < 6:
                self.busy_waits.append(seconds)
        except Exception:
            return

    def finish(self, closed: bool) -> None:
        """Одна строка телеметрии `ad_creative_wizard`, если мастер был виден."""
        try:
            if not self.seen:
                return
            end = round(self._clock() - self._started, 1)
            steps = []
            for i, entry in enumerate(self.seen):
                nxt = self.seen[i + 1]["at_s"] if i + 1 < len(self.seen) else end
                steps.append({**entry, "s": round(max(0.0, nxt - entry["at_s"]), 1)})
            # ACL-1551: закрыт его Close (не завершён) — так и сказано в note.
            by_close = bool(closed) and self.closed_by == "Close"
            diag = {"origin": self.origin or "other", "file": self.file,
                    "sequence": [s["step"] for s in steps], "steps": steps,
                    "closed": bool(closed), "closed_by": self.closed_by if closed else None,
                    "dialog_left": self.dialog_left,
                    "captures": {k: bool(v) for k, v in self.captured.items()}}
            if self.exit:
                diag["exit"] = self.exit
            if self.looped_back_to:
                diag["looped_back_to"] = self.looped_back_to
            if self.busy_waits:
                diag["busy_s"] = self.busy_waits
            _telemetry.record(
                "ad_creative_wizard", None,
                note=(self.origin or "other") + (":closed_by_close" if by_close
                                                  else ":closed" if closed else ":left_open"),
                duration_s=end,
                diag=diag,
            )
        except Exception:
            return

    def _snap(self, page, label: str, state: dict[str, Any]) -> str:
        if not _telemetry.current_run_id():
            return ""   # вне прогона (тесты, ручные вызовы) — без снимков
        try:
            from src.fb_ads_rpa import page_capture as _pages

            take = self._capture or _pages.capture
            return take(
                page, step=label,
                diag={"origin": self.origin, "file": self.file, "state": state,
                      "at_s": self.seen[-1]["at_s"] if self.seen else 0.0},
                blocks={"creative_wizard": _pages.CREATIVE_WIZARD_BLOCK_JS},
                quota=QUOTA, quota_max=MAX_PER_RUN, page_html=False, remember=False,
            ) or ""
        except Exception:
            return ""
