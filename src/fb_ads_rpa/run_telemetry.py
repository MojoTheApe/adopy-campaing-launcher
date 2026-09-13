"""One append-only telemetry log for every RPA run — CLI and dashboard alike.

Replaces the ad-hoc debug dumps that grew per-bug during the 2026-07-25..29
debugging push (`debug_ad_copy_locator_miss.json`,
`debug_create_ad.json`, `debug_ad_level_soft_fails.txt`). Those each had a
different format, only one was append-only, and answering "how often does
this step actually fail, and with what diagnosis" meant reading PNG
screenshots one at a time.

Format: one JSON object per line (JSONL), append-only, never rewritten, so
concurrent runs and crashed runs can't lose or truncate history.

Each record:
    run_id     — groups records from one publish run
    ts         — UTC ISO timestamp
    step       — step name, normalised (no after_/fail_ prefix)
    ok         — True (step succeeded), False (failed), None (informational)
    campaign   — campaign name, when known
    duration_s — seconds since the previous step of the same run
    diag       — free-form dict with whatever the failing site knows
    note       — short free text
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
from config.paths import get_runtime_workspace
TELEMETRY_PATH = get_runtime_workspace() / "output" / "rpa_telemetry.jsonl"


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def normalise_step(label: str) -> tuple[str, bool | None]:
    """Split a screenshot label into (step, ok).

    The recorded flow already names every screenshot `after_<step>` on
    success and `fail_<step>` on failure, so those labels are a complete,
    zero-maintenance description of the run — no separate instrumentation
    to keep in sync with the steps themselves.
    """
    if label.startswith("after_"):
        return label[len("after_"):], True
    if label.startswith("fail_"):
        return label[len("fail_"):], False
    if label.startswith("success_"):
        return label[len("success_"):], True
    # «Пропущено» — третий исход, и он не то же самое, что удача или отказ.
    # Шаг детального таргетинга не выполнялся вовсе, когда в черновике не было
    # интересов, и не писал об этом ни строки: в потоке его просто не
    # существовало. 9 сентября это выглядело так, что интересы «не
    # проставились», а по телеметрии нельзя было отличить пропуск от отказа.
    # `ok=None` не попадает в долю неудач — пропуск не провал.
    if label.startswith("skip_"):
        return label[len("skip_"):], None
    return label, None


#: Что похоже на номер кабинета, id профиля или иной опознавательный знак.
#: Контекст отказа уезжает в общую таблицу телеметрии, которую читает вся
#: компания, — идентификаторы туда не едут.
_SCRUB = (
    (re.compile(r"((?:access_token|token|password|authorization|fb_dtsg|lsd)=)[^&\s\"'<>]{1,16384}", re.I), r"\1<hidden>"),
    (re.compile(r"\bBearer [A-Za-z0-9._~+/-]{1,16384}", re.I), "Bearer <hidden>"),
    (re.compile(r"(act=)(?:act_)?\d{6,}"), r"\1<hidden>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{10,}\b"), "<digits>"),
)


def scrub(text: str) -> str:
    """Убрать из строки всё, по чему опознаётся аккаунт или профиль."""
    out = str(text or "")
    for pattern, replacement in _SCRUB:
        out = pattern.sub(replacement, out)
    return out


def scrub_diag(value):
    """Explicit step diagnostics share the same export boundary as page context."""
    if isinstance(value, dict):
        return {key: ("<hidden>" if str(key).lower() in {"token", "access_token", "password", "authorization", "fb_dtsg", "lsd"} else scrub_diag(item))
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_diag(item) for item in value]
    return scrub(value) if isinstance(value, str) else value


def merge_diag(collected: dict | None, explicit: dict | None) -> dict | None:
    """Соединить общий снимок обстановки с тем, что шаг рассказал о себе сам.

    Два правила, и оба нужны:

    * **Точное важнее общего.** Если шаг назвал причину своим ключом, снимок
      обстановки его не затирает.
    * **Общее не теряется.** Шаг, рассказавший о себе, не должен из-за этого
      лишиться адреса страницы и заголовка — по ним отказ находят в снимках.

    Раньше это было условием `if diag is None` в теле `_shot`: шаг с
    собственной причиной оставался без адреса страницы. Правило вынесено
    сюда, чтобы его можно было проверить, а не угадывать по виду кода.
    """
    merged = {**(collected or {}), **(explicit or {})}
    return merged or None


def screen_locked() -> bool | None:
    """Заблокирован ли сейчас экран Windows. `None`, если спросить не удалось.

    Блокировка останавливает страницу так же, как свёрнутое окно, только
    сильнее: система уходит на защищённый рабочий стол, композитор перестаёт
    отдавать кадры, и эмуляция фокуса через CDP уже не помогает.

    Это не догадка. 29 июля с окном в фоне шаги шли в 10-50 раз медленнее, а
    загрузка креатива второго объявления заняла двенадцать минут и «упала».
    10 сентября то же самое повторилось с заблокированным экраном: сорок две
    минуты на файл, который соседнее объявление приняло за семьдесят четыре
    секунды — сразу после того, как человек вернулся к машине.

    Признак — процесс `LogonUI.exe`: он живёт ровно пока показан экран
    блокировки. Способ грубый, но дешёвый и не требует ни прав, ни библиотек;
    ошибиться он может только в сторону «не знаю», а это честный ответ.

    Снять блокировку мы не можем и не должны — это настройка безопасности
    машины байера. Но назвать её причиной вместо разметки Meta обязаны: за
    один день она дважды увела разбор не туда.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import psutil
    except Exception:
        return None
    try:
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower() == "logonui.exe":
                return True
        return False
    except Exception:
        return None


def page_context(page, *, timeout_ms: int = 1200) -> dict:
    """Что было на странице в момент отказа — коротко и безопасно.

    До сих пор у отказов записывался только факт: «ad_copy: false». Причины не
    было ни у человека, ни у будущего агента. 9 сентября прогон дал шесть
    отказов, и по телеметрии нельзя было сказать про них ничего.

    Собирается в **одной** точке — там же, где пишется сама запись, — а не в
    сорока трёх местах вызова. Разойтись с ними нечему, и новый шаг получит
    контекст, ничего для этого не делая.

    Три правила, которые здесь важнее полноты:

    * **Никогда не бросает.** Строка лога не должна стоить кампании — то же
      правило, что у `record`.
    * **Ограничено по времени.** Ожидание на упавшем шаге платит человек,
      сидящий перед экраном.
    * **Вычищено.** Адрес Ads Manager содержит номер кабинета, и он не поедет
      в общую таблицу.
    """
    info: dict[str, Any] = {}
    try:
        info["url"] = scrub(str(getattr(page, "url", "") or ""))[:200]
    except Exception:
        pass
    for name, call in (
        ("title", lambda: page.title()),
        ("dialogs", lambda: page.get_by_role("dialog").count()),
    ):
        try:
            value = call()
            if isinstance(value, str):
                value = scrub(value)[:120]
            info[name] = value
        except Exception:
            continue
    # Верхний баннер Meta закрывает и сдвигает контролы — это самая частая
    # причина, по которой шаг «не нашёл» существующую кнопку.
    try:
        banner = page.get_by_role("banner").first
        if banner.is_visible(timeout=timeout_ms):
            info["banner"] = scrub(banner.inner_text(timeout=timeout_ms))[:120]
    except Exception:
        pass
    # Страница-ошибка самого браузера. Когда она открыта, все прочие выводы —
    # шум: шаг честно не найдёт кнопку, которой нет, и сообщит «не нашёл поле
    # имени кампании» вместо «нет связи». 10 сентября прогон встал на
    # ERR_SSL_PROTOCOL_ERROR — на проксированном профиле это почти всегда сам
    # прокси, и искать надо там, а не в разметке Meta.
    err = browser_error(page)
    if err:
        info["browser_error"] = err
    # Заблокированный экран останавливает страницу так же, как свёрнутое окно,
    # только сильнее. Без этой строки отказ читается как поломка разметки, а
    # человек уходит искать её в Ads Manager.
    if screen_locked():
        info["screen_locked"] = True
    return info


_ERROR_CODE = re.compile(r"\bERR_[A-Z0-9_]{3,40}\b")


def browser_error(page) -> str:
    """Код ошибки браузера, если открыта его собственная страница-ошибка.

    Chrome рисует её со своим узлом `#main-frame-error` и кодом внутри —
    `ERR_SSL_PROTOCOL_ERROR`, `ERR_PROXY_CONNECTION_FAILED` и подобными.
    Ищем узел, а не текст по всей странице: слово `ERR_…` может встретиться и
    в скриптах живой страницы, а нам нужен именно этот экран.
    """
    try:
        text = page.evaluate(
            """
            () => {
              const el = document.querySelector('#main-frame-error');
              if (!el) return '';
              const code = el.querySelector('.error-code');
              return (code ? code.textContent : el.textContent) || '';
            }
            """
        )
    except Exception:
        return ""
    match = _ERROR_CODE.search(text or "")
    return match.group(0) if match else ""


_current: dict[str, str] = {"run_id": "", "campaign": ""}


def set_current_run(run_id: str = "", campaign: str = "") -> None:
    """Запомнить, какой прогон идёт сейчас.

    Об отказах сообщают сорок три места, и не все они знают номер прогона:
    `create_additional_ad` 9 сентября записался с пустым `run_id` и потому
    не попал ни в один прогон — при разборе его пришлось искать по времени.
    Место, которое знает номер, называет его один раз; остальные получают
    его даром.
    """
    _current["run_id"] = run_id or _current["run_id"]
    _current["campaign"] = campaign or _current["campaign"]


def record(
    step: str,
    ok: bool | None = None,
    *,
    run_id: str = "",
    campaign: str = "",
    duration_s: float | None = None,
    diag: dict[str, Any] | None = None,
    note: str = "",
    path: Path | None = None,
) -> None:
    """Append one record. Never raises — telemetry must not break a run."""
    try:
        p = path or TELEMETRY_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        rec: dict[str, Any] = {
            "run_id": run_id or _current["run_id"],
            "ts": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "ok": ok,
        }
        campaign = campaign or _current["campaign"]
        if campaign:
            rec["campaign"] = campaign
        if duration_s is not None:
            rec["duration_s"] = round(duration_s, 1)
        if diag:
            rec["diag"] = scrub_diag(diag)
        if note:
            rec["note"] = note
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or TELEMETRY_PATH
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def summarize(path: Path | None = None, last_runs: int | None = None) -> list[dict[str, Any]]:
    """Per-step reliability, worst first — the question screenshots can't answer."""
    rows = load(path)
    if last_runs:
        seen: list[str] = []
        for r in rows:
            rid = r.get("run_id") or ""
            if rid and rid not in seen:
                seen.append(rid)
        keep = set(seen[-last_runs:])
        rows = [r for r in rows if (r.get("run_id") or "") in keep]

    stats: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("ok") is None:
            continue
        s = stats.setdefault(
            r["step"], {"step": r["step"], "ok": 0, "fail": 0, "slowest_s": 0.0, "last_diag": None}
        )
        if r["ok"]:
            s["ok"] += 1
        else:
            s["fail"] += 1
            if r.get("diag"):
                s["last_diag"] = r["diag"]
        d = r.get("duration_s")
        if isinstance(d, (int, float)) and d > s["slowest_s"]:
            s["slowest_s"] = float(d)

    out = list(stats.values())
    for s in out:
        total = s["ok"] + s["fail"]
        s["runs"] = total
        s["fail_rate"] = round(s["fail"] / total, 2) if total else 0.0
    out.sort(key=lambda s: (-s["fail_rate"], -s["fail"], s["step"]))
    return out


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Summarise RPA run telemetry.")
    ap.add_argument("--last-runs", type=int, default=None, help="only the N most recent runs")
    args = ap.parse_args()

    rows = summarize(last_runs=args.last_runs)
    if not rows:
        print(f"No telemetry yet at {TELEMETRY_PATH}")
        return
    print(f"{'step':38s} {'runs':>5s} {'fail':>5s} {'rate':>5s} {'slowest':>8s}")
    print("-" * 70)
    for s in rows:
        print(
            f"{s['step'][:38]:38s} {s['runs']:5d} {s['fail']:5d} "
            f"{s['fail_rate']:5.2f} {s['slowest_s']:7.1f}s"
        )
    worst = [s for s in rows if s["fail"] and s["last_diag"]]
    if worst:
        print("\nlast diagnosis per failing step:")
        for s in worst[:8]:
            print(f"  {s['step']}: {json.dumps(s['last_diag'], ensure_ascii=False)[:160]}")


if __name__ == "__main__":
    _main()
