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

**Исключение из первого правила — раздел упавшего шага (ACL-1325).** Решение
владельца 26.09: к событию Sentry о блокирующем отказе и об остановке кампании
прикладывается вычищенная копия ОДНОГО раздела страницы — карточки, с которой
работал шаг (Audience, Conversion, Ad creative …), не больше SECTION_MAX_BYTES.
Шаг вне редактора (список кампаний: create, objective*, stall_*) — открытое
окно, а без него область списка (`[role=main]` / контейнер таблицы) под своим
потолком SECTION_LIST_RAW_MAX_CHARS. Никогда не вся страница (`document.body`)
и никогда не скриншот. Уточнение владельца 26.09: снимается только то, что
внутри браузера Multilogin (рабочие аккаунты, закрытая среда) — имена других
кампаний списка остаться могут; id, адреса, контакты, токены и uuid
вычищаются всегда. Вычистка здесь уже не «второй слой», а условие отправки,
поэтому она устроена иначе, чем `scrub_html`: разметка пересобирается заново
по белому списку тегов и атрибутов (`scrub_section_html`), а не чистится
выражениями. Выключается машинной настройкой `ACL_SENTRY_PAGE_SNAPSHOT=0`.
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from config.paths import get_runtime_workspace
from src.fb_ads_rpa.run_telemetry import hide_secrets_deep, scrub

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

#: Снимки со своей квотой (ACL-1426: шаги мастера креатива). Считаются
#: отдельно от `_taken`, чтобы не съедать MAX_PER_RUN настоящих отказов;
#: ключ — (прогон, квота).
_quota_taken: dict[tuple[str, str], int] = {}

#: Мастер креатива «Set up your creative» — отдельным блоком (ACL-1348).
#:
#: Страница редактора весит больше MAX_HTML_BYTES, а портал мастера лежит в
#: самом хвосте документа: в снимках отказа `ad2_creative_upload` 25.09
#: (956d0d184138, 08ca37a0b941) страница обрезана на 3,98 МБ, и от мастера не
#: осталось ни строки. Целиком он попадал только в блок медиатеки
#: (`library_pick_selected`) — то есть лишь когда робот уже дошёл до Media;
#: при отказе на пути к Media мастера в снимке не было. Блок сохраняется
#: целиком и вычищается так же, как страница (`scrub_html`).
#:
#: Ищем открытый `role=dialog` с тремя и больше подписями шагов, а если мастер
#: без этой роли (так бывало с 03.08) — предка подписи «Media», в котором есть
#: три подписи шагов и кнопка Next/Cancel/Done.
CREATIVE_WIZARD_BLOCK_JS = r"""(() => {
  const names = ['Creative setup', 'Media', 'Trim', 'Crop', 'Text', 'Image generation',
                 'Creative generation', 'Enhancements', 'Translation'];
  const norm = (s) => (s || '').replace(/[​-‍﻿]/g, '').replace(/\s+/g, ' ').trim();
  const hits = (el) => { const t = norm(el.textContent); return names.filter(n => t.includes(n)).length; };
  const box = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const dialog = [...document.querySelectorAll('[role=dialog]')].find(d => box(d) && hits(d) >= 3);
  if (dialog) return dialog;
  const nav = ['Next', 'Cancel', 'Done'];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let t = w.nextNode(); t; t = w.nextNode()) {
    if (norm(t.nodeValue) !== 'Media') continue;
    for (let n = t.parentElement, i = 0; n && n !== document.body && i < 15; n = n.parentElement, i++) {
      if (hits(n) >= 3 && box(n) && [...n.querySelectorAll('[role=button],button')]
          .some(b => nav.includes(norm(b.textContent)))) return n;
    }
  }
  return null;
})()"""

#: Шаг → блоки, которые снимаются при его отказе сами, без участия вызывающего.
_STEP_BLOCKS: tuple[tuple[re.Pattern[str], dict[str, str]], ...] = (
    # `ad_creative_media_step` снимает сам `upload_ad_creative`, пока мастер
    # ещё открыт: к строке `fail_ad2_creative_upload` его уже закрыли.
    (re.compile(r"^ad\d*_creative_(?:upload|media_step)$"), {"creative_wizard": CREATIVE_WIZARD_BLOCK_JS}),
)


def blocks_for_step(step: str) -> dict[str, str]:
    """Блоки, положенные шагу `step` по умолчанию (см. `_STEP_BLOCKS`)."""
    found: dict[str, str] = {}
    for pattern, blocks in _STEP_BLOCKS:
        if pattern.match(step or ""):
            found.update(blocks)
    return found


def success_captures_enabled() -> bool:
    """Снимать ли страницу и PNG на УДАЧНОМ шаге. По умолчанию — нет.

    ACL-1313, прогоны 25.09: снимки удачных шагов стоили 1–6 с разметкой
    (advantage_plus_before/control, library_pick_selected, ad_media_ready,
    targeting_and_groups — 15–20 с на кампанию) и 5–7 с каждый PNG, и вдобавок
    съедали лимит MAX_PER_RUN, так что настоящему отказу в конце прогона
    снимка уже не доставалось. Отказы снимаются всегда. Вернуть снимки на
    каждом шаге: FB_RPA_SHOT_ALL=1.
    """
    return os.environ.get("FB_RPA_SHOT_ALL", "").strip().lower() in ("1", "true", "yes")


def capture_on_success(page, **kwargs) -> str:
    """`capture` для удачного шага: только при FB_RPA_SHOT_ALL=1, иначе ""."""
    if not success_captures_enabled():
        return ""
    return capture(page, **kwargs)


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
    from config import token_vault

    # A known token may straddle the byte cut. Exact matching alone sees a
    # bounded character overlap before either cut; general regexes remain capped.
    text, truncated = token_vault.hide_configured_secrets_prefix(html or "", MAX_HTML_BYTES)
    text, byte_cut = _cut(text)
    text = _EMAIL.sub("<email>", scrub(text))
    text, again = _cut(text)
    return text, truncated or byte_cut or again


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
    blocks: dict[str, str] | None = None,
    quota: str = "",
    quota_max: int = 0,
    page_html: bool = True,
    remember: bool = True,
) -> str:
    """Снять страницу упавшего шага. Возвращает путь к папке снимка или "".

    `blocks` — имя → JS-выражение, возвращающее элемент. Каждый найденный
    элемент сохраняется в `block_<имя>.html` целиком: страница Ads Manager
    больше MAX_HTML_BYTES и обрезается, а проверка по снимку должна видеть
    нужный блок полностью, а не кусок до обрезки (ревью ACL-1219/1171/1192).

    ACL-1426 — пассивные снимки (шаги мастера креатива):

    * `quota`/`quota_max` — свой счёт на прогон вместо MAX_PER_RUN, лимит
      отказов они не трогают;
    * `page_html=False` — страница целиком пишется, только если ни один блок
      не нашёлся (она весит 4 МБ и режется раньше портала мастера);
    * `remember=False` — раздел для Sentry не запоминается: это не отказ.

    Пустая строка — обычный ответ, а не сбой: снимков за прогон может быть
    больше положенного, страница может быть уже закрыта. Ни то, ни другое не
    повод уронить прогон.
    """
    try:
        # Номер прогона можно не передавать: место, которое его знает,
        # называет его один раз в `run_telemetry.set_current_run`. Снимок,
        # легший в папку «no_run», к прогону потом не привязать.
        if not run_id or not campaign:
            try:
                from src.fb_ads_rpa import run_telemetry as _tel

                run_id = run_id or _tel.current_run_id()
                campaign = campaign or _tel.current_campaign()
            except Exception:
                pass
        key = run_id or "no_run"
        if quota:
            if _quota_taken.get((key, quota), 0) >= max(0, quota_max):
                return ""
        elif _taken.get(key, 0) >= MAX_PER_RUN:
            return ""

        def _page_text() -> str | None:
            try:
                return "<html>" + page.locator("html").inner_html(timeout=timeout_ms) + "</html>"
            except Exception:
                return None

        started = time.time()
        html = _page_text() if page_html else ""
        if html is None:
            return ""

        # ACL-1325: раздел упавшего шага — в память, для события Sentry. Что
        # `inner_html` ответил, не значит, что ответит и следующий вызов:
        # сюда приходят из `_stall_evidence`, с вкладки, у которой уже был
        # stall. Поэтому снятие раздела ограничено по времени
        # (SECTION_EVAL_TIMEOUT_S, ревью PR #328, F5).
        if remember:
            try:
                remember_section(page, step=step, run_id=run_id, campaign=campaign)
            except Exception:
                pass

        # Номер папки — общий для прогона: снимки с квотой идут в тот же ряд.
        index = _taken.get(key, 0) + sum(n for (k, _q), n in _quota_taken.items() if k == key)
        folder = _root() / _safe(key) / f"{index:02d}_{_safe(step)}"
        folder.mkdir(parents=True, exist_ok=True)
        truncated = False
        body_bytes = 0
        if page_html:
            body, truncated = scrub_html(html)
            (folder / "page.html").write_text(body, encoding="utf-8")
            body_bytes = len(body.encode("utf-8", "replace"))

        saved_blocks: dict[str, Any] = {}
        for name, js in {**blocks_for_step(step), **(blocks or {})}.items():
            try:
                outer = page.evaluate(
                    "(expr) => { const el = (new Function('return (' + expr + ')'))();"
                    " return el ? el.outerHTML : null; }",
                    js,
                )
            except Exception as exc:
                saved_blocks[name] = {"found": False, "error": str(exc)[:120]}
                continue
            if not outer:
                saved_blocks[name] = {"found": False}
                continue
            block_body, block_cut = scrub_html(outer)
            (folder / f"block_{_safe(name)}.html").write_text(block_body, encoding="utf-8")
            saved_blocks[name] = {"found": True, "bytes": len(block_body.encode("utf-8", "replace")),
                                  "truncated": block_cut}

        if not page_html and not any(b.get("found") for b in saved_blocks.values()):
            # Блока нет — тогда хоть страница, чтобы снимок не был пустым.
            html = _page_text()
            if html:
                body, truncated = scrub_html(html)
                (folder / "page.html").write_text(body, encoding="utf-8")
                body_bytes = len(body.encode("utf-8", "replace"))

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
                    "diag": hide_secrets_deep(diag or {}),  # ACL-1358
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "html_bytes": body_bytes,
                    # Обрезанная разметка, поданная как полная, — это селектор,
                    # написанный по тому, чего на странице нет.
                    "html_truncated": truncated,
                    "blocks": saved_blocks,
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

        if quota:
            _quota_taken[(key, quota)] = _quota_taken.get((key, quota), 0) + 1
        else:
            _taken[key] = _taken.get(key, 0) + 1
        if index == 0:
            prune()
        return str(folder)
    except Exception:
        # Снимок — помощь при разборе, а не условие работы.
        return ""


# ── Раздел упавшего шага для Sentry (ACL-1325) ──────────────────────────────
#
# Зачем. 26.09 новая раскладка карточки Audience («Suggest an audience») на
# FacebookDoc6540 сломала «Further limit the reach» — и нашлась только потому,
# что прогон шёл на машине владельца, где лежат снимки страниц. У байеров
# событие Sentry несло шаг и причину, но не то, что было на экране; по тексту
# «not found» новую раскладку не опознать. Раздел страницы отвечает на это.
#
# Как устроено:
#
# * в момент отказа (`capture` → `remember_section`) страница сама отдаёт
#   карточку шага, уже без скриптов, стилей, svg и лишних атрибутов, — это
#   дёшево и небольшое; разметка остаётся в памяти процесса (ограниченно);
# * при отправке события (`sentry_signal`) она пересобирается вычистителем
#   `scrub_section_html`, которому к этому времени известны имена кампании,
#   кабинета, групп и объявлений, и прикладывается к событию файлом.
#
# Вычищенное уходит только в событие; на диск и в телеметрию ничего нового
# не пишется.

#: Машинная настройка. По умолчанию включено (решение владельца 26.09).
SECTION_SETTING = "ACL_SENTRY_PAGE_SNAPSHOT"
#: Потолок вложения. Карточки редактора на живых снимках 24–26.09 весят
#: 4–85 КБ сырыми; после вычистки заметно меньше.
SECTION_MAX_BYTES = 200_000
#: Потолок того, что страница отдаёт из JS, — до вычистки.
SECTION_RAW_MAX_CHARS = 1_500_000
#: Потолок области списка кампаний (шаги вне редактора: create, objective*,
#: stall_*). Список бывает на тысячи строк; обход в странице кончается на
#: этом потолке, а после вычистки файл всё равно не больше SECTION_MAX_BYTES.
SECTION_LIST_RAW_MAX_CHARS = 400_000
#: Потолки строки текста и значения атрибута.
SECTION_TEXT_MAX = 300
SECTION_ATTR_MAX = 200
#: Сколько разделов держать в памяти. Процесс дешборда живёт сутками.
_SECTIONS_MAX = 16
#: Сколько ждать страницу со снятием раздела. Не ответила — раздела нет, а
#: поток публикации не висит в сборе улик (ревью PR #328, F5).
SECTION_EVAL_TIMEOUT_S = 5.0

_sections: "OrderedDict[tuple[str, str], dict[str, Any]]" = OrderedDict()
_sections_lock = threading.Lock()


def section_snapshots_enabled() -> bool:
    """Прикладывать ли раздел страницы к событиям Sentry. По умолчанию — да."""
    value = (os.environ.get(SECTION_SETTING) or "").strip().lower()
    return value not in ("0", "false", "no", "off")


#: Заголовки карточек редактора Ads Manager (role=heading, aria-level=3) —
#: из живых снимков 24–26.09. Граница карточки — ближайший предок заголовка,
#: в котором уже есть заголовок ДРУГОЙ карточки из этого списка.
SECTION_TITLES: tuple[str, ...] = (
    "campaign name", "campaign details", "special ad categories", "budget",
    "ad set name", "conversion", "budget & schedule", "audience", "placements",
    "dynamic creative", "live video ad", "ad transparency",
    "brand safety and suitability", "campaign score",
    "ad name", "partnership ad", "identity", "ad setup", "ad creative",
    "destination", "creative testing", "languages", "tracking",
)

#: Шаг → какие карточки он трогает. Порядок важен: первое совпадение.
#: `language` здесь — строка карточки Audience уровня группы; одноимённая
#: карточка уровня объявления (переводы) к шагу `languages` не относится.
_SECTION_OF_STEP: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"adset_name|ad_set_name"), ("ad set name",)),
    (re.compile(r"destination|website_url|lead_form|instant_form"), ("destination",)),
    (re.compile(r"conversion|pixel|dataset|lead_terms|website_pick"), ("conversion",)),
    (re.compile(r"budget|schedule|bid_"), ("budget & schedule", "budget")),
    (re.compile(r"audience|advantage_plus|further_limit|location|age|gender|language"
                r"|targeting|interest|suggest"), ("audience",)),
    (re.compile(r"placement|devices|ios"), ("placements",)),
    (re.compile(r"identity|facebook_page|instagram"), ("identity",)),
    (re.compile(r"copy|primary_text|headline|creative|media|upload|library|reuse"),
     ("ad creative", "ad setup")),
    (re.compile(r"^ad\d*_name$|^ad_name"), ("ad name",)),
    (re.compile(r"campaign_name|objective|special_ad"),
     ("campaign name", "campaign details", "special ad categories")),
)


def section_titles_for(step: str) -> tuple[str, ...]:
    """Карточки, с которыми работает шаг. Пусто — шаг не узнан, берём запасной путь."""
    name = (step or "").strip().lower()
    for pattern, titles in _SECTION_OF_STEP:
        if pattern.search(name):
            return titles
    return ()


#: Шаги, у которых раздел для Sentry — дерево редактора и открытое меню, а
#: не карточка (ACL-1353: список объявлений и меню «Create ad» нового
#: редактора). Без этого запасной путь брал карточку с фокусом — «Ad
#: creative» после текста объявления (событие 149854712, 28.09).
_TREE_STEPS = re.compile(r"more_ads")


def section_is_tree(step: str) -> bool:
    """Раздел шага — дерево редактора и открытое меню."""
    return bool(_TREE_STEPS.search((step or "").strip().lower()))


#: Атрибуты, которые переживают вычистку (плюс все aria-*). Всё остальное
#: (class, style, href, src, value, data-* кроме data-surface, обработчики)
#: снимается.
_KEEP_ATTRS = ("role", "data-surface", "placeholder", "type", "checked", "disabled", "id")

#: Окна, которые в Ads Manager открыты всегда и к шагу не относятся (панель
#: уведомлений висела во всех снимках 24–26.09).
_SKIP_DIALOGS = ("notifications",)

_SECTION_JS = r"""
(cfg) => {
  const norm = s => (s || '').replace(/[\s\u200b]+/g, ' ').trim().toLowerCase();
  const HSEL = '[role=heading], h1, h2, h3, h4';
  const known = new Set(cfg.sections);
  // Название карточки — весь текст заголовка или его первая строка: в
  // раскладке «Suggest an audience» (26.09) внутри заголовка Audience ещё и
  // кнопки «Explore audience opportunities», «More options».
  const titleOf = (h) => {
    const all = norm(h.textContent);
    if (known.has(all)) return all;
    const walk = document.createTreeWalker(h, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      const s = norm(n.nodeValue);
      if (s) return known.has(s) ? s : '';
    }
    return '';
  };
  const heads = [];
  const titles = new Map();
  for (const h of document.querySelectorAll(HSEL)) {
    const title = titleOf(h);
    if (title) { heads.push(h); titles.set(h, title); }
  }
  const shown = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const cardOf = (h) => {
    const mine = titles.get(h);
    let box = h;
    for (let p = h.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      if (heads.some(x => x !== h && titles.get(x) !== mine && p.contains(x))) break;
      box = p;
    }
    return box;
  };
  const keep = (name) => name.startsWith('aria-') || cfg.keep.includes(name);
  const DROP = new Set(['script', 'style', 'svg', 'noscript', 'template', 'iframe', 'object',
                        'embed', 'canvas', 'link', 'meta', 'math']);
  const VOID = new Set(['area', 'br', 'col', 'hr', 'img', 'input', 'param', 'source', 'track', 'wbr']);
  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  // Копия узла под потолком `max` символов. Обход останавливается, как только
  // потолок набран: список кампаний на тысячи строк не превращается в долгую
  // задачу на и без того медленной вкладке (ревью PR #328, F1/F5). Раньше —
  // `cloneNode(true)` всего узла и обход всех атрибутов, а потолок — после.
  // Незакрытые на обрыве теги закроет вычиститель (`scrub_section_html`).
  const strip = (root, max) => {
    const out = [];
    let size = 0, full = false;
    const put = (s) => {
      if (size + s.length > max) { full = true; return false; }
      out.push(s); size += s.length; return true;
    };
    const walk = (n) => {
      if (full) return;
      if (n.nodeType === 3) { put(esc(n.nodeValue || '')); return; }
      if (n.nodeType !== 1) return;
      const tag = n.tagName.toLowerCase();
      if (DROP.has(tag)) return;
      let open = '<' + tag;
      for (const a of n.attributes) {
        if (a.name === 'checked' || !keep(a.name)) continue;
        open += ' ' + a.name + '="' + esc(a.value).replace(/"/g, '&quot;') + '"';
      }
      if (n.checked === true || n.hasAttribute('checked')) open += ' checked';
      if (!put(open + '>') || VOID.has(tag)) return;
      for (let c = n.firstChild; c && !full; c = c.nextSibling) walk(c);
      put('</' + tag + '>');
    };
    walk(root);
    return out.join('');
  };
  const parts = [];
  const taken = new Set();
  for (const title of cfg.titles) {
    const h = heads.find(x => titles.get(x) === title && shown(x))
           || heads.find(x => titles.get(x) === title);
    if (!h) continue;
    const card = cardOf(h);
    if (taken.has(card)) continue;
    taken.add(card);
    parts.push({name: title, html: strip(card, cfg.maxChars)});
  }
  if (cfg.tree) {
    // ACL-1353: дерево редактора (список объявлений) и открытое меню его
    // строки — раньше карточки с фокусом.
    const tree = document.getElementById('campaign_structure_tree_root')
      || document.querySelector('[data-surface="/am/editor_drawer"]');
    if (tree) { taken.add(tree); parts.push({name: 'editor tree', html: strip(tree, cfg.maxChars)}); }
    for (const m of document.querySelectorAll('[role=menu]')) {
      if (taken.has(m) || !shown(m)) continue;
      taken.add(m);
      parts.push({name: 'menu', html: strip(m, cfg.maxChars)});
      break;
    }
  }
  let listMode = false;
  if (!parts.length) {
    // Шаг не узнан или его карточки нет: наименьший раздел редактора вокруг
    // фокуса, потом сам редактор — каждый под потолком. Вне редактора (список
    // кампаний: create, objective*, stall_*) — открытое окно, а без него
    // область списка ниже. Всю страницу (`document.body`) не берём никогда
    // (ревью PR #328, F1).
    let card = null;
    for (let el = document.activeElement; el && el !== document.body; el = el.parentElement) {
      const h = heads.find(x => el.contains(x));
      if (h) { card = cardOf(h); break; }
    }
    const drawer = document.querySelector('[data-surface="/am/editor_drawer"]');
    if (card) parts.push({name: 'focused section', html: strip(card, cfg.maxChars)});
    else if (drawer) parts.push({name: 'editor', html: strip(drawer, cfg.maxChars)});
    else listMode = true;
  }
  // Открытое окно (диалог «Reach your preferred audiences», мастер
  // креатива) почти всегда и есть то, на чём шаг упал. Сам редактор окном
  // не считается: в нём есть заголовки карточек.
  for (const d of document.querySelectorAll('[role=dialog], [aria-modal=true]')) {
    if (taken.has(d) || !shown(d) || heads.some(x => d.contains(x))) continue;
    const dh = d.querySelector(HSEL);
    if (dh && cfg.skipDialogs.includes(norm(dh.textContent))) continue;
    taken.add(d);
    parts.push({name: 'dialog', html: strip(d, cfg.maxChars)});
    if (parts.length >= 4) break;
  }
  if (listMode && !parts.length) {
    // Список кампаний без окна: основная область (панель действий + таблица),
    // а не страница целиком — шапка профиля и меню кабинета сюда не входят.
    const region = [...document.querySelectorAll('[role=main], main')].find(shown)
      || (() => {
        const table = [...document.querySelectorAll('[role=grid], [role=treegrid], [role=table], table')]
          .find(shown);
        return table ? (table.parentElement || table) : null;
      })();
    if (region && region !== document.body && region !== document.documentElement) {
      parts.push({name: 'campaigns list', html: strip(region, cfg.listMaxChars)});
    }
  }
  return parts;
}
"""


def _eval_bounded(page, js: str, arg: Any, timeout_s: float = SECTION_EVAL_TIMEOUT_S) -> Any:
    """`page.evaluate` с таймаутом, который соблюдает драйвер.

    Тот же приём, что `meta_dom._eval_bounded` (ACL-1320): у голого
    `page.evaluate` таймаута нет, а `wait_for_function` — есть. Функция
    заворачивается в объект `{v: …}`, он всегда «истинен», и ожидание
    кончается на первом же вычислении. Зависшая вкладка — TimeoutError через
    `timeout_s`. Свой, а не из `meta_dom`: тот сам зовёт `capture`.
    """
    handle = page.wait_for_function(f"(arg) => ({{ v: ({js.strip()})(arg) }})", arg=arg,
                                    timeout=int(timeout_s * 1000))
    try:
        got = handle.json_value()
    finally:
        try:
            handle.dispose()
        except Exception:
            pass
    return got.get("v") if isinstance(got, dict) else None


def _snapshot_take(page, titles: tuple[str, ...], *, tree: bool = False) -> list[dict[str, str]]:
    parts = _eval_bounded(page, _SECTION_JS, {
        "tree": bool(tree),
        "titles": list(titles),
        "sections": list(SECTION_TITLES),
        "keep": list(_KEEP_ATTRS),
        "maxChars": SECTION_RAW_MAX_CHARS,
        "listMaxChars": SECTION_LIST_RAW_MAX_CHARS,
        "skipDialogs": list(_SKIP_DIALOGS),
    })
    out: list[dict[str, str]] = []
    for part in parts or []:
        if isinstance(part, dict) and part.get("html"):
            out.append({"name": str(part.get("name") or "section")[:40], "html": str(part["html"])})
    return out


def remember_section(page, *, step: str, run_id: str = "", campaign: str = "") -> bool:
    """Снять карточку упавшего шага и запомнить её до отправки события.

    Никогда не бросает. False — не снято (выключено настройкой, страница не
    ответила за SECTION_EVAL_TIMEOUT_S, ничего не нашлось).
    """
    if not section_snapshots_enabled() or not step:
        return False
    try:
        started = time.time()
        parts = _snapshot_take(page, section_titles_for(step), tree=section_is_tree(step))
        if not parts:
            return False
        entry = {
            "step": step,
            "campaign": campaign,
            "parts": parts,
            "took_s": round(time.time() - started, 2),
        }
        with _sections_lock:
            _sections.pop((run_id or "", step), None)
            _sections[(run_id or "", step)] = entry
            while len(_sections) > _SECTIONS_MAX:
                _sections.popitem(last=False)
        return True
    except Exception:
        return False


def _same_campaign(entry: dict[str, Any], run_id: str, campaign: str) -> bool:
    """Раздел снят в той же кампании, о которой событие (ревью PR #328, F2).

    С номером прогона — разные известные имена кампании значат чужой раздел.
    Без номера прогона ключ ("", шаг) общий для всех кампаний потока, и
    раздел годится только при совпадении обоих известных имён.
    """
    mine = str(entry.get("campaign") or "").strip()
    theirs = (campaign or "").strip()
    if run_id:
        return not (mine and theirs and mine != theirs)
    return bool(mine and theirs and mine == theirs)


def _find_section(run_id: str, steps: Iterable[str], campaign: str = "") -> dict[str, Any] | None:
    run_id = run_id or ""
    with _sections_lock:
        for step in steps:
            found = _sections.get((run_id, step))
            if found and _same_campaign(found, run_id, campaign):
                return found
        # Без номера прогона «последний раздел прогона» — это последний раздел
        # чего угодно в процессе: не берём (ревью PR #328, F2).
        if not run_id:
            return None
        # Сам шаг снимка не оставил (отказ без `_shot`, итог кампании):
        # последний раздел этого прогона — с честной пометкой, чей он.
        for (rid, _step), entry in reversed(_sections.items()):
            if rid == run_id and _same_campaign(entry, run_id, campaign):
                return entry
    return None


def forget_sections() -> None:
    """Забыть снятые разделы (тесты)."""
    with _sections_lock:
        _sections.clear()


# ── Вычистка раздела ────────────────────────────────────────────────────────

#: Выкидываются вместе с содержимым.
_DROP_TAGS = frozenset({
    "script", "style", "svg", "noscript", "template", "iframe", "object", "embed",
    "canvas", "link", "meta", "head", "title", "math", "base",
})
_VOID_TAGS = frozenset({
    "area", "br", "col", "hr", "img", "input", "param", "source", "track", "wbr",
})
_ID_REF_ATTRS = frozenset({
    "id", "aria-labelledby", "aria-describedby", "aria-controls", "aria-owns",
    "aria-activedescendant", "aria-details", "aria-errormessage", "aria-flowto",
})
#: Служебные id рендера Meta: `js_1a`, `u_0_2f_Xy`, React `:r1f:` / `_r_1f_`.
#: Они новые на каждой загрузке, и по ним ничего не найти; заменяются на
#: сквозные `syn_N`, чтобы ссылки aria-labelledby остались связанными.
_RENDER_ID = re.compile(r"^(?:js_|u_\d|:r|_r_)[\w:.-]{0,80}$")
_TAG_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")

_URL = re.compile(r"\b(?:https?://|www\.)[^\s\"'<>]{1,2048}", re.I)
_ID_PARAM = re.compile(
    r"\b(act|[a-z_]{1,40}_id|id)=[^&\s\"'<>,;]{1,256}", re.I)
_PHONE = re.compile(r"(?<![\w<])\+?\d[\d ()./-]{7,22}\d(?![\w>])")
_DIGITS = re.compile(r"\d{6,}")
#: Разметка, которая дошла до текста: экранированная на странице
#: (`&lt;div class=…&gt;`) или тег, который парсер не разобрал и отдал текстом
#: (старый `html.parser` в Python 3.11 — CI PR #328). Атрибуты такого тега
#: мимо белого списка не едут: кусок вырезается целиком.
_MARKUP_IN_TEXT = re.compile(r"</?[A-Za-z][^<>]*>?")
#: Хвост разорванного тега: `>` внутри значения атрибута режет тег, и остаток
#: (` x" class="y" href="…">`) приходит текстом. Пары `имя="значение"` в тексте
#: вырезаются, как и сама разметка.
_ATTR_IN_TEXT = re.compile(r"""(?<![\w-])[A-Za-z_:][\w:.-]*\s*=\s*(?:"[^"]*"|'[^']*')""")


def _phone(match: re.Match[str]) -> str:
    text = match.group(0)
    return "<phone>" if sum(ch.isdigit() for ch in text) >= 9 else text


def _names_pattern(names: Iterable[tuple[str, str]]) -> tuple[re.Pattern[str] | None, dict[str, str]]:
    """Одно выражение на все известные имена, длинные раньше коротких."""
    table: dict[str, str] = {}
    for value, placeholder in names:
        value = (value or "").strip()
        if len(value) >= 3 and value.lower() not in table:
            table[value.lower()] = placeholder
    if not table:
        return None, table
    ordered = sorted(table, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(v) for v in ordered) + r")(?![A-Za-z0-9])",
        re.I,
    )
    return pattern, table


def private_names(campaign: str = "", account: str = "", names: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Имена этого прогона → заглушки. Кабинет — целиком и по словам
    (`FacebookDoc19 Ivan Petrov`: ярлык и имя персоны)."""
    out: list[tuple[str, str]] = []
    if campaign:
        out.append((campaign, "<campaign>"))
    if account:
        out.append((account, "<account>"))
        for word in re.split(r"[\s()\[\],;|]+", account):
            out.append((word, "<account>"))
    for name in names or ():
        if name:
            out.append((str(name), "<name>"))
    return out


class _SectionScrubber(HTMLParser):
    """Пересборка разметки по белому списку. Всё, чего нет в списке, — не едет."""

    def __init__(self, names_re: re.Pattern[str] | None, names: dict[str, str], budget: int) -> None:
        super().__init__(convert_charrefs=True)
        self._names_re = names_re
        self._names = names
        self._budget = budget
        self._out: list[str] = []
        self._size = 0
        self._stack: list[str] = []
        self._drop_tag = ""
        self._drop_depth = 0
        self._ids: dict[str, str] = {}
        self.truncated = False

    # -- текст -------------------------------------------------------------
    def _text(self, value: str, limit: int) -> str:
        # Адреса и почта — раньше имён: имя внутри адреса почты заменилось
        # бы по частям, а домен остался бы.
        text = _URL.sub("<url>", value or "")
        text = _EMAIL.sub("<email>", text)
        if self._names_re is not None:
            text = self._names_re.sub(lambda m: self._names.get(m.group(0).lower(), "<name>"), text)
        text = scrub(text)
        text = _ID_PARAM.sub(lambda m: m.group(1) + "=<hidden>", text)
        text = _PHONE.sub(_phone, text)
        text = _DIGITS.sub("<digits>", text)
        if len(text) > limit:
            text = text[:limit] + "…"
        return text

    def _render_id(self, value: str) -> str:
        if not _RENDER_ID.match(value):
            return value
        if value not in self._ids:
            self._ids[value] = f"syn_{len(self._ids) + 1}"
        return self._ids[value]

    def _attrs(self, attrs: list[tuple[str, str | None]]) -> str:
        out = []
        for name, value in attrs:
            name = (name or "").lower()
            if not (name.startswith("aria-") or name in _KEEP_ATTRS):
                continue
            if not _TAG_NAME.fullmatch(name):
                continue
            if value is None:
                out.append(f" {name}")
                continue
            if name in _ID_REF_ATTRS:
                value = " ".join(self._render_id(v) for v in value.split())
            value = self._text(value, SECTION_ATTR_MAX)
            out.append(f' {name}="{_html.escape(value, quote=True)}"')
        return "".join(out)

    # -- вывод -------------------------------------------------------------
    def _emit(self, piece: str, opens: str = "") -> bool:
        """Добавить кусок, если после него хватит места закрыть все теги
        (включая `opens` — тег, который этот кусок открывает)."""
        if self.truncated:
            return False
        closing = sum(len(t) + 3 for t in self._stack) + (len(opens) + 3 if opens else 0)
        size = len(piece.encode("utf-8", "replace"))
        if self._size + size + closing > self._budget:
            self.truncated = True
            return False
        self._out.append(piece)
        self._size += size
        return True

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag:
                self._drop_depth += 1
            return
        if tag in _DROP_TAGS:
            self._drop_tag, self._drop_depth = tag, 1
            return
        if not _TAG_NAME.fullmatch(tag):
            return
        opens = "" if tag in _VOID_TAGS else tag
        if self._emit(f"<{tag}{self._attrs(attrs)}>", opens) and opens:
            self._stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self._drop_depth or tag in _DROP_TAGS or not _TAG_NAME.fullmatch(tag):
            return
        piece = f"<{tag}{self._attrs(attrs)}>"
        if tag not in _VOID_TAGS:
            piece += f"</{tag}>"
        self._emit(piece)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag:
                self._drop_depth -= 1
            return
        if tag not in self._stack:
            return
        while self._stack:
            top = self._stack.pop()
            # Закрывающие теги уже учтены в `_emit` как «место на закрытие».
            self._out.append(f"</{top}>")
            self._size += len(top) + 3
            if top == tag:
                break

    def handle_data(self, data):
        if self._drop_depth or not data:
            return
        if not data.strip():
            self._emit(" ")
            return
        data = _ATTR_IN_TEXT.sub(" ", _MARKUP_IN_TEXT.sub(" ", data))
        if not data.strip():
            self._emit(" ")
            return
        self._emit(_html.escape(self._text(data, SECTION_TEXT_MAX), quote=False))

    # Комментарии, doctype, инструкции обработки — не едут.
    def handle_comment(self, data):
        return

    def handle_decl(self, decl):
        return

    def handle_pi(self, data):
        return

    def unknown_decl(self, data):
        return

    def close(self):
        # Раздел обрезан посреди тега (SECTION_RAW_MAX_CHARS): Python 3.11 при
        # закрытии отдаёт недописанный тег текстом — вместе с атрибутами и
        # набранным в поле value. 3.13+ такой хвост отбрасывает, как велит
        # HTML5; делаем так же на любой версии (ревью PR #328).
        if self.rawdata.startswith("<"):
            self.rawdata = ""
        super().close()

    def result(self) -> str:
        while self._stack:
            self._out.append(f"</{self._stack.pop()}>")
        return "".join(self._out)


def scrub_section_html(
    html: str,
    *,
    campaign: str = "",
    account: str = "",
    names: Iterable[str] = (),
    max_bytes: int = SECTION_MAX_BYTES,
) -> tuple[str, bool]:
    """Вычистить раздел страницы для отправки. `(разметка, обрезано ли)`.

    Пересобирает разметку заново, а не правит исходную: остаются только теги
    (без script/style/svg/iframe…), атрибуты role/aria-*/data-surface/
    placeholder/type/checked/disabled/id и текст. В тексте и значениях
    атрибутов: имена кампании/кабинета/групп/объявлений → заглушки, адреса →
    `<url>`, `act=`/`*_id=` → `<hidden>`, почта, телефоны, числа от шести
    цифр, токены и uuid (`run_telemetry.scrub`); каждая строка — не длиннее
    SECTION_TEXT_MAX. Служебные id рендера (`js_*`) → `syn_N`. Результат —
    не больше `max_bytes` байт, с закрытыми тегами.
    """
    names_re, table = _names_pattern(private_names(campaign, account, names))
    parser = _SectionScrubber(names_re, table, max(0, int(max_bytes)))
    try:
        parser.feed((html or "")[:SECTION_RAW_MAX_CHARS])
        parser.close()
    except Exception:
        # Разбор не удался — не отправляем ничего, а не «что получилось».
        return "", True
    return parser.result(), parser.truncated or len(html or "") > SECTION_RAW_MAX_CHARS


def section_attachment(
    step: str,
    run_id: str = "",
    *,
    campaign: str = "",
    account: str = "",
    names: Iterable[str] = (),
    related: Iterable[str] = (),
) -> dict[str, Any] | None:
    """Вложение для события Sentry о шаге `step`: вычищенный раздел или None.

    `{"filename", "bytes", "content_type", "context"}`. `context` — короткая
    справка для самого события (какая карточка, с какого шага снята, размер,
    обрезана ли), чтобы разбор видел это без скачивания файла.
    """
    if not section_snapshots_enabled():
        return None
    try:
        related = list(related or ())
        entry = _find_section(run_id, [step, *related], campaign)
        if not entry:
            return None
        campaign = campaign or entry.get("campaign") or ""
        names_list = list(names or ())
        captured_at = str(entry.get("step") or "")
        # Заголовок — до частей, чтобы бюджет частей считался от остатка.
        head_room = 800
        budget = SECTION_MAX_BYTES - head_room
        chunks: list[str] = []
        shown: list[str] = []
        cut_any = False
        for part in entry.get("parts") or []:
            header = f"<!-- part: {_safe(part.get('name') or 'section')} -->\n"
            room = budget - len(header.encode("utf-8")) - 1
            if room <= 256:
                cut_any = True
                break
            body, cut = scrub_section_html(part.get("html") or "", campaign=campaign,
                                           account=account, names=names_list, max_bytes=room)
            cut_any = cut_any or cut
            if not body:
                continue
            chunk = header + body + "\n"
            chunks.append(chunk)
            shown.append(str(part.get("name") or "section"))
            budget -= len(chunk.encode("utf-8"))
        if not chunks:
            return None
        head = (
            f"<!-- ACL section snapshot (ACL-1325) | step: {_safe(step)}"
            f" | captured at step: {_safe(captured_at)}"
            f" | parts: {', '.join(_safe(s) for s in shown)}"
            f" | truncated: {'yes' if cut_any else 'no'}"
            " | scrubbed (whitelisted tags/attributes; names, ids, urls, emails, phones replaced);"
            " screenshots are never sent -->\n"
        )
        payload = (head + "".join(chunks)).encode("utf-8", "replace")
        return {
            "filename": f"section_{_safe(step)}.html",
            "bytes": payload,
            "content_type": "text/html",
            "context": {
                "parts": shown,
                "captured_at_step": captured_at,
                "same_step": captured_at == step or captured_at in related,
                "bytes": len(payload),
                "truncated": cut_any,
            },
        }
    except Exception:
        return None
