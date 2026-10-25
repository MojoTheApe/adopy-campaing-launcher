"""Отказы уезжают туда, где их видно всем сразу (ACL-354).

Зачем. Самый дорогой класс отказов у нас — Meta меняет разметку: 24.08 одна
переделка сломала четыре шага и стоила дня поштучной диагностики. Когда
докладывают десять машин, то же событие опознаётся за час — ровно ради этого
заводилась общая таблица телеметрии. Sentry делает то же самое лучше:
одинаковые отказы складывает сам, к каждому цепляет релиз и машину, всплеск
виден без чтения строк.

Чего здесь **нет** и не будет без отдельного решения:

* **Обратной связи байеру.** Задача просила показывать, что команда прочитала
  обращение; владелец 15.09 решил пока без неё. Sentry канала обратно в
  приложение не имеет, и делать вид, что имеет, хуже, чем не делать.
* **Персональных данных.** `send_default_pii` выключен намеренно. Наша
  телеметрия вычищает номера кабинетов и профилей перед отправкой, потому что
  их читает вся компания; отправлять то же самое в чужой сервис было бы
  странно. Тексты исключений проходят через тот же вычиститель
  (`run_telemetry.scrub`) — в них попадают адреса Ads Manager с `act=`.

  **Имена — решение владельца 01.10 (ACL-1447).** Раньше имена аккаунтов и
  кампаний считались той же чувствительной частью и в разделе страницы
  заменялись заглушками (ACL-1325). Теперь отображаемое имя аккаунта
  («FacebookDoc19») и имя кампании отправляются — в итоге кампании
  (`report_run_summary`) и тегами `account` / `campaign`: без них итог по
  машине байера не сопоставить с его кампаниями. Номера кабинетов и
  профилей, адреса Ads Manager, токены и почта по-прежнему вычищаются.
  Вычищенный раздел страницы (`sentry_signal`) остаётся с заглушками —
  решение про итог его не меняет.

  Каждое событие несёт тег `manager` — имя менеджера машины
  (`ACL_MACHINE_LABEL`, иначе `MY_CM_NAME`; мастер установки и настройки
  дешборда пишут первое). Тег ставится в `before_send`, а не один раз при
  `init`, — иначе имя, исправленное в настройках, доехало бы только после
  перезапуска.
* **Постоянного профайлера.** В присланном примере стояли стопроцентные
  трассировка и профилирование. На машине байера уже крутятся Multilogin,
  Chrome и дешборд, а замер 30.07 показал, чем оборачивается лишняя нагрузка
  на окно браузера: шаги шли в 10-50 раз медленнее. По умолчанию — ноль,
  включается переменной окружения, если понадобится.

DSN лежит здесь же, как и `GSHEET_TELEMETRY_ID`: он даёт право только писать,
читать им нечего, а требовать от каждого байера вписать его руками — это та
самая ручная установка, из-за которой у Дана 14.09 открывались окна терминала.
Переменная `ACL_SENTRY_DSN` перебивает значение, а пустая строка выключает
отправку совсем.
"""
from __future__ import annotations

import os
import platform
import re
import uuid
from typing import Any

#: Общий приёмник ACL. Право только на запись.
DEFAULT_DSN = (
    "https://a4a9e3270397d1eba7de8a30f5a54c08@o4512089106808832"
    ".ingest.de.sentry.io/4512089112379472"
)

_started = False


def dsn() -> str:
    """Куда слать. Пустая строка — не слать никуда."""
    value = os.environ.get("ACL_SENTRY_DSN")
    return (DEFAULT_DSN if value is None else value).strip()


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _scrub_deep(value: Any) -> Any:
    """Прогнать всё, что уезжает, через вычиститель телеметрии.

    Он написан для общей таблицы и знает наши опасные места: `act=`, токены,
    uuid профилей, длинные числа. Второй такой список разошёлся бы с первым.
    """
    from src.fb_ads_rpa.run_telemetry import scrub_diag

    return scrub_diag(value)


def manager_label() -> str:
    """Имя менеджера этой машины (ACL-1447): `ACL_MACHINE_LABEL`, иначе
    `MY_CM_NAME` — имя, под которым он входит в дешборд. Пусто — не задано."""
    for key in ("ACL_MACHINE_LABEL", "MY_CM_NAME"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value[:200]
    return ""


#: Теги машины, снятые при `init` (ACL-1447, ревью PR #450). Ставятся в
#: `before_send`, а не только `set_tag` при `init`: `set_tag` пишет в scope
#: того потока, что звал `init`, а итог кампании уходит из нового потока,
#: чей scope форкается от потока задания — без тегов. Тогда все итоги, кроме
#: первого, приходили без `machine` и складывались в «unknown».
_machine_tags: dict[str, str] = {}


def _set_event_tag(event: dict, key: str, value: str, *, override: bool = True) -> None:
    tags = event.get("tags")
    if isinstance(tags, dict):
        if override or not tags.get(key):
            tags[key] = value
    elif isinstance(tags, list):
        has = [t for t in tags if isinstance(t, (list, tuple)) and t and t[0] == key]
        if has and not override and any(len(t) > 1 and t[1] for t in has):
            return
        event["tags"] = [t for t in tags if not (isinstance(t, (list, tuple)) and t and t[0] == key)]
        event["tags"].append([key, value])
    else:
        event["tags"] = {key: value}


def _tag_manager(event: dict) -> None:
    _set_event_tag(event, "manager", manager_label() or "unset")


def _tag_machine(event: dict) -> None:
    # Тег, уже стоящий на событии, не перебиваем — он точнее нашего снимка.
    for key, value in _machine_tags.items():
        if value:
            _set_event_tag(event, key, value, override=False)


def _before_send(event: dict, _hint: dict) -> dict | None:
    try:
        _tag_manager(event)
    except Exception:
        pass  # без тега событие всё равно ценно
    try:
        _tag_machine(event)
    except Exception:
        pass
    try:
        return _scrub_deep(event)
    except Exception:
        # Не вычистили — не отправляем. Событие ценно, но не настолько.
        return None


def init() -> bool:
    """Поднять отправку. Возвращает, включена ли она.

    Повторный вызов ничего не делает: дешборд и RPA стартуют из одного
    процесса, и второй `init` сбросил бы уже собранный контекст.
    """
    global _started
    if _started:
        return True
    target = dsn()
    if not target:
        return False
    try:
        import sentry_sdk
    except Exception:
        return False

    from src.fb_ads_rpa import telemetry_sync

    try:
        machine_label, machine_id = telemetry_sync.machine_identity()
    except Exception:
        machine_label, machine_id = platform.node(), ""
    try:
        release = telemetry_sync.flow_version()
    except Exception:
        release = ""

    sentry_sdk.init(
        dsn=target,
        release=release or None,
        environment=os.environ.get("ACL_SENTRY_ENV") or "production",
        # Ни IP, ни заголовков: см. докстроку модуля.
        send_default_pii=False,
        include_local_variables=False,
        max_value_length=8000,  # Preserve the bounded manual report in its context.
        # Трассировка и профилирование по умолчанию выключены — это машина
        # байера, а не наш стенд.
        traces_sample_rate=_float_env("ACL_SENTRY_TRACES", 0.0),
        profile_session_sample_rate=_float_env("ACL_SENTRY_PROFILES", 0.0),
        before_send=_before_send,
        # Отправка не должна задерживать закрытие лаунчера: человек нажал
        # «выход», а мы держим окно ради отчёта — это про нас же плохо.
        shutdown_timeout=3,
    )
    _machine_tags.clear()
    _machine_tags["machine"] = str(machine_label or "")
    if machine_id:
        _machine_tags["machine_id"] = str(machine_id)
    # Глобальный scope виден всем потокам; `before_send` — страховка.
    try:
        global_scope = sentry_sdk.get_global_scope()
        for key, value in _machine_tags.items():
            if value:
                global_scope.set_tag(key, value)
    except Exception:
        for key, value in _machine_tags.items():
            if value:
                sentry_sdk.set_tag(key, value)
    _started = True
    return True


def report_job_exception(exc: BaseException, *, job: str = "", account: str = "", phase: str = "") -> str:
    """Исключение, которым кончилась работа задания публикации (ACL-1318).

    Повторное ревью v3 PR #314: `publish_jobs._run_rpa` ловил такое
    исключение (ошибка переподключения, падение вне шагов объявлений, ошибка
    в нашем коде) и писал только «RPA error: …» в задание — до Sentry оно не
    доходило, хотя это самая серьёзная остановка кампаний. Отправляем
    `capture_exception` со стеком; всё событие проходит тот же вычиститель
    `before_send`, что и остальные (адреса с `act=`, номера, пути).

    `phase` — где кончилось задание: `build_drafts` (сборка черновиков) или
    `publish` (работа браузера); тег `job_phase`.
    """
    if not init():
        return ""
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("failure_kind", "job_exception")
        # ACL-1403: тег машины ставим явно — событие PYTHON-4E пришло без него.
        for key, value in _machine_tags.items():
            if value:
                scope.set_tag(key, value)
        if phase:
            scope.set_tag("job_phase", phase)
        if account:
            scope.set_tag("account", account_tag(account))
        if job:
            scope.set_context("run", {"job": str(job).replace("-", "")})
        return sentry_sdk.capture_exception(exc) or ""


def report_step_stuck(step: str, idle_s: float, *, job: str = "", account: str = "",
                      campaign: str = "") -> str:
    """Робот не писал шагов дольше порога (ACL-1614, сигнал «завис»).

    Отпечаток — по шагу, на котором встали: одно и то же зависание на одном
    шаге у разных заданий складывается в один тикет, а не в событие на каждое.
    """
    if not init():
        return ""
    import sentry_sdk

    step = str(step or "unknown")
    with sentry_sdk.new_scope() as scope:
        scope.fingerprint = ["acl-step-stuck", step]
        scope.set_tag("failure_kind", "step_stuck")
        scope.set_tag("step", step)
        if account:
            scope.set_tag("account", account_tag(account))
        ctx = {"idle_s": round(float(idle_s), 1), "campaign": campaign}
        if job:
            ctx["job"] = str(job).replace("-", "")
        scope.set_context("run", ctx)
        return sentry_sdk.capture_message(f"step_stuck: {step}", level="error") or ""


def report_tab_unblocked(step: str, method: str, stuck_s: float, *, ok: bool, job: str = "",
                         account: str = "", campaign: str = "", api: str = "", where: str = "",
                         tried: list | None = None) -> str:
    """Сторож разблокировал зависшую вкладку (ACL-1702) — одно событие на случай.

    Отпечаток — по шагу и месту вызова: одно и то же зависание у разных
    заданий складывается в один тикет.
    """
    if not init():
        return ""
    import sentry_sdk

    step = str(step or "unknown")
    with sentry_sdk.new_scope() as scope:
        scope.fingerprint = ["acl-tab-unblocked", step, str(where or "")]
        scope.set_tag("failure_kind", "tab_hung")
        scope.set_tag("step", step)
        scope.set_tag("unblock_method", method or "none")
        scope.set_tag("unblock_ok", "yes" if ok else "no")
        if account:
            scope.set_tag("account", account_tag(account))
        ctx = {"stuck_s": round(float(stuck_s), 1), "campaign": campaign, "api": api,
               "where": where, "tried": list(tried or [])}
        if job:
            ctx["job"] = str(job).replace("-", "")
        scope.set_context("run", ctx)
        return sentry_sdk.capture_message(f"tab_unblocked: {step}", level="error") or ""


def account_tag(account: str) -> str:
    """Тег аккаунта — только короткий ярлык (первое слово: «FacebookDoc19»),
    без имени персоны после него (повторное ревью v3 PR #314)."""
    parts = (account or "").strip().split()
    return parts[0] if parts else ""


def report_step_failure(
    step: str,
    why: str = "",
    *,
    run_id: str = "",
    campaign: str = "",
    account: str = "",
    diag: dict | None = None,
    reason: str = "",
    job: str = "",
    kind: str = "",
    group_step: str = "",
    attempt: int | None = None,
    attachment: dict | None = None,
) -> str:
    """Сообщить об отказе шага. Возвращает номер события или пустую строку.

    ACL-1318: зовут это не на каждую строку телеметрии с `ok=False`, а только
    на настоящие отказы кампании (`src/fb_ads_rpa/sentry_signal.py`). Отсюда
    новые поля: `reason` — уже вычисленный вид причины (иначе он выводится из
    `why`), `job` — задание дешборда, `kind` — `campaign_stop` (попытка
    кампании кончилась) или `blocking` (черновик «не публиковать»),
    `group_step` — шаг для отпечатка без номера объявления (`ad2_copy` и
    `ad3_copy` — одна и та же поломка), `attempt` — номер попытки.
    ACL-1325: `attachment` — вычищенный раздел страницы упавшего шага
    (`page_capture.section_attachment`); скриншоты не прикладываются никогда.

    **Отпечаток задаётся вручную, и это важно.** Группировка Sentry хороша на
    исключениях со стеком, а у нас основная масса — отказы шагов: «placements
    ok=False», «таблица не отрисовалась». Без своего отпечатка всё это
    сложится в один вечный тикет «шаг не прошёл», и смысл группировки
    пропадёт. Отпечаток — шаг и вид причины, а не её полный текст: в тексте
    бывают номера и названия кампаний, и каждая давала бы свой тикет.
    """
    if not init():
        return ""
    import sentry_sdk

    reason = reason or _reason_kind(why)
    with sentry_sdk.new_scope() as scope:
        scope.fingerprint = ["acl-step", group_step or step, reason]
        scope.set_tag("step", step)
        scope.set_tag("reason", reason)
        if kind:
            scope.set_tag("failure_kind", kind)
        if account:
            scope.set_tag("account", account_tag(account))
        if run_id or campaign or job:
            run_ctx = {"run_id": run_id, "campaign": campaign}
            if job:
                # Номер задания — наш собственный uuid4, не профиль и не
                # кабинет, но вычиститель заменил бы его на `<uuid>` вместе с
                # id профилей Multilogin. Без дефисов он под шаблон не попадает
                # и остаётся сопоставимым с заданием в журнале дешборда.
                run_ctx["job"] = str(job).replace("-", "")
            if attempt is not None:
                run_ctx["attempt"] = attempt
            scope.set_context("run", run_ctx)
        if why:
            scope.set_context("failure", {"why": _scrub_deep(why)})
        if diag:
            scope.set_context("diag", _scrub_deep(diag))
        if attachment:
            _attach_section(scope, attachment)
        return sentry_sdk.capture_message(f"{step}: {reason}", level="error") or ""


def _attach_section(scope, attachment: dict) -> None:
    """Приложить вычищенный раздел страницы (ACL-1325).

    `attachment` готовит `page_capture.section_attachment`: он уже вычищен и
    ограничен по размеру — `before_send` вложения не видит, поэтому второй
    потолок стоит здесь. Не приложилось — событие уходит без вложения, а не
    пропадает.
    """
    try:
        from src.fb_ads_rpa.page_capture import SECTION_MAX_BYTES

        payload = attachment.get("bytes") or b""
        if not isinstance(payload, (bytes, bytearray)) or not payload or len(payload) > SECTION_MAX_BYTES:
            return
        scope.add_attachment(
            bytes=bytes(payload),
            filename=str(attachment.get("filename") or "section.html"),
            content_type=str(attachment.get("content_type") or "text/html"),
        )
        scope.set_tag("page_snapshot", "section")
        if attachment.get("context"):
            scope.set_context("page_snapshot", _scrub_deep(attachment["context"]))
    except Exception:
        pass


#: Виды причин, по которым отказы стоит складывать вместе. Список ведётся
#: руками: каждый вид сюда попадает, когда мы его увидели и назвали, а не
#: «на всякий случай». Всё остальное — «other», и это честно: неизвестную
#: причину лучше показать отдельным тикетом, чем спрятать в чужой.
_REASONS = (
    ("not-found", ("не найден", "не нашлось", "not found", "no rows", "ни одной строки")),
    ("still-loading", ("table-loading", "still loading", "не отрисовал", "грузится")),
    ("wrong-account", ("не тот кабинет", "different account", "changed during")),
    ("wrong-period", ("другой период", "period does not match", "период не")),
    # ACL-1403: порт автоматизации профиля отказал (раньше общего «refused») в соединении после старта.
    ("cdp-refused", ("did not accept the connection", "profile_cdp_refused")),
    ("refused", ("refused:", "отказ", "недоступна")),
    # Увиденное на живых прогонах 15.09 — каждая строка добавлена потому, что
    # отказ случился и был разобран, а не «на всякий случай».
    #
    # **Стоит выше `timeout` намеренно.** «FileChooser.set_files: Timeout
    # 30000ms exceeded» содержит слово timeout и без этого попадало бы в общий
    # вид, а чинится оно совсем иначе: там не «подождать подольше», там окно
    # выбора файла не дожило до передачи. Частное проверяем раньше общего.
    ("file-chooser-lost", ("filechooser", "file_chooser", "set_files")),
    ("upload-never-started", ("progress: 0", "fed: false")),
    ("timeout", ("timeout", "timed out", "не дождал")),
    # ACL-1318: так объявление без текста называет себя в итоге кампании
    # («ad_copy:<имя объявления> has no text») — имя в вид не едет.
    ("no-text", ("has no text",)),
    # 25.09 20:23: «ad3_create:Page.evaluate: Target page, context or browser
    # has been clos» — текст исключения обрезан до 60 знаков, отсюда «clos».
    ("page-closed", ("has been clos", "target closed")),
    # ACL-1320, живой прогон 26.09: профиль разлогинен или без сети. Эти
    # стоят после `page-closed`, но до всего общего — у них свои тикеты.
    ("logged-out", ("loginpage", "facebook.com/login", "login_page", "login_form",
                    "log in to facebook")),
    ("proxy-network", ("err_socks", "err_proxy", "err_tunnel", "err_ssl_protocol",
                       "err_connection", "proxy/network")),
    ("restart-failed", ("restart of unresponsive profile", "restarting the multilogin profile failed")),
    ("open-by-hand", ("open by hand", "profile_open_by_hand")),
    # ACL-1401: лаунчер Multilogin отказал в соединении — приложение не запущено.
    ("mlx-not-running", ("multilogin is not running", "multilogin_not_running")),
    ("simple-create", ("simple_create", "single-screen campaign editor")),
)


#: Причина, состоящая из одного машинного слова, — сама себе вид.
#:
#: Шаги пишут в `note` короткие устойчивые метки: `library_pick_left_selection`,
#: `same_file_already_failed_this_run`, `reload_and_retry`. Такая метка не
#: меняется от прогона к прогону и не несёт в себе ни имён кампаний, ни
#: номеров — то есть годится как вид причины без всякой таблицы.
#:
#: Это не косметика. 15.09 первый живой тикет пришёл с заголовком
#: `ad_creative_reuse: other`, а настоящая причина —
#: `library_pick_left_selection` — лежала внутри, в контексте. В списке тикетов
#: видно «прочее», и чтобы понять, про что он, надо открыть каждый. Правило
#: ниже делает заголовок говорящим само, без дописывания таблицы под каждый
#: новый отказ, — а таблица остаётся для причин, написанных словами.
#: Только буквы: цифра внутри метки почти всегда означает номер — кампании,
#: объявления, строки, — и такая «метка» дала бы отдельный тикет на каждый
#: номер. Ровно то, от чего отпечаток и защищает. Пример из живого прогона:
#: `destination_sweep:2292 - ES Text 5 Ait:row_not_found`.
_TOKEN_RE = re.compile(r"^[a-z]+(?:_[a-z]+){1,6}$")


#: Метки, которые наш код сам пишет в итог кампании сразу после «шаг:» —
#: только их текст отказа может целиком считаться меткой. Всё остальное после
#: двоеточия — имя объявления или текст исключения (ревью v3 PR #314:
#: «destination_sweep:summer_sale_ad» — имя, а не причина).
_RESULT_TOKENS = frozenset({"not_the_new_ad_after_reload"})


def _reason_kind(why: str, *, allow_token: bool = True) -> str:
    """Вид причины — то, по чему одинаковые отказы складываются в один тикет.

    Три исхода, и различать их важно:

    * найденный вид — отказ узнан;
    * сама метка — отказ имеет устойчивое машинное имя, годное как вид;
    * `silent` — шаг **не сказал ничего**. Это отдельная беда, а не
      разновидность «прочего»: три из четырёх отказов 15.09 пришли с пустой
      причиной, и сложить их в общую кучу с неузнанными значило бы спрятать
      самое частое, что у нас есть (ACL-1172).
    """
    text = (why or "").strip()
    if not text:
        return "silent"
    lowered = text.lower()
    for kind, needles in _REASONS:
        if any(n in lowered for n in needles):
            return kind
    if allow_token and _TOKEN_RE.match(lowered):
        return lowered.replace("_", "-")
    return "other"


#: Машинная метка в скобках в САМОМ КОНЦЕ текста отказа — так её ставит наш
#: код (`f" ({why})"`): «ad2_copy:1962 - AR Text 1 has no text
#: (copy_fields_not_revealed)». Скобки посреди текста — часть имени
#: объявления («My (summer_sale) ad …»), и в причину они не идут (ревью v3).
_EMBEDDED_TOKEN_RE = re.compile(r"\(([a-z]+(?:_[a-z]+){1,6})\)\s*$")


def _explicit_token(text: str, *, from_result: bool = False) -> str:
    """Устойчивая машинная метка внутри текста отказа, если она там есть.

    Ищем в трёх местах, от самого явного к менее явному: метка в скобках,
    кусок между двоеточиями (`destination_sweep:2292 - ES Text 5 Ait:row_not_found`)
    и весь текст целиком. Метка — только буквы (см. `_TOKEN_RE`), так что ни
    номер, ни имя кампании в неё не попадут.
    """
    text = (text or "").strip()
    if not text:
        return ""
    found = _EMBEDDED_TOKEN_RE.findall(text)
    if found:
        return found[-1].replace("_", "-")
    parts = text.split(":")
    if not from_result:
        # Строка телеметрии: note пишет сам шаг — метка может быть где угодно.
        for part in reversed(parts):
            part = part.strip().lower()
            if _TOKEN_RE.match(part):
                return part.replace("_", "-")
        return ""
    # Текст отказа из итога кампании: в нём имена объявлений. Метка — только
    # последний кусок после «имя:» (`2292 - ES Text 5 Ait:row_not_found`), не
    # сам текст целиком: «destination_sweep:summer_sale_ad» — это имя.
    if len(parts) >= 2:
        last = parts[-1].strip().lower()
        if _TOKEN_RE.match(last):
            return last.replace("_", "-")
    return ""


def failure_reason(failure: str, why: str = "") -> str:
    """Вид причины для отказа из итога кампании (ACL-1318).

    `failure` — строка из итога (`ad2_copy:… (copy_fields_not_revealed)`,
    `audience_location`, `ad_copy:Timeout 30000ms exceeded`), `why` — чем
    объяснила себя последняя строка телеметрии того же шага. Порядок —
    от точного к общему: метка в самом отказе, узнанный вид его текста, метка
    в телеметрии, узнанный вид там. `silent` — только когда не сказал ничего
    ни итог, ни телеметрия: 25.09 заголовок `ad_level_summary: silent` висел
    над отказом, у которого причина `copy_fields_not_revealed` была написана
    прямо в тексте.
    """
    detail = (failure or "").split(":", 1)[1].strip() if ":" in (failure or "") else ""
    why = (why or "").strip()
    for text in (detail, why):
        token = _explicit_token(text, from_result=text is detail)
        if token:
            return token
        if text:
            from_result = text is detail
            kind = _reason_kind(text, allow_token=not from_result
                                or text.strip().lower() in _RESULT_TOKENS)
            if kind != "other":
                return kind
    return "other" if (detail or why) else "silent"


#: Вид итога → уровень события. Не error: итог — не отказ, алертов не плодит.
_SUMMARY_LEVELS = {"clean": "info", "warnings": "info",
                   "do-not-publish": "warning", "stopped": "warning"}


def report_run_summary(summary: dict, *, attachment: dict | None = None) -> str:
    """Итог одной кампании (ACL-1447) — собирает `run_summary.build`.

    Отпечаток — только вид итога: четыре тикета на весь парк машин, события
    внутри них. Имена аккаунта и кампании — тегами (решение владельца 01.10,
    см. докстроку модуля); всё событие проходит тот же `before_send`.
    ACL-1554: `attachment` — строки телеметрии прогона
    (`run_summary.telemetry_attachment`), уже вычищенные и ограниченные.
    """
    if not init():
        return ""
    import sentry_sdk

    result = str(summary.get("result") or "unknown")
    with sentry_sdk.new_scope() as scope:
        scope.fingerprint = ["acl-run-summary", result]
        scope.set_tag("kind", "run_summary")
        scope.set_tag("result", result)
        scope.set_tag("editor", str(summary.get("editor") or "unknown"))
        if summary.get("account"):
            scope.set_tag("account", str(summary["account"])[:200])
        if summary.get("campaign"):
            scope.set_tag("campaign", str(summary["campaign"])[:200])
        ctx = dict(summary)
        if ctx.get("run_id"):
            ctx["run_id"] = str(ctx["run_id"]).replace("-", "")
        if attachment and not _attach_telemetry(scope, attachment):
            ctx["telemetry_attachment"] = "error"
        scope.set_context("run_summary", ctx)
        return sentry_sdk.capture_message(
            f"Run summary: {result}", level=_SUMMARY_LEVELS.get(result, "info")
        ) or ""


def _attach_telemetry(scope, attachment: dict) -> bool:
    """Приложить строки телеметрии прогона (ACL-1554). Вернуть, приложилось ли.

    Как и у `_attach_section`: `before_send` вложения не видит, поэтому
    второй потолок стоит здесь, и не приложилось — итог уходит без вложения.
    Только `.jsonl`: скриншот сюда не пролезет даже по ошибке.
    """
    try:
        from src.fb_ads_rpa.run_summary import TELEMETRY_MAX_BYTES

        payload = attachment.get("bytes") or b""
        filename = str(attachment.get("filename") or "")
        if (not isinstance(payload, (bytes, bytearray)) or not payload
                or len(payload) > TELEMETRY_MAX_BYTES or not filename.endswith(".jsonl")):
            return False
        scope.add_attachment(
            bytes=bytes(payload),
            filename=filename,
            content_type=str(attachment.get("content_type") or "application/x-ndjson"),
        )
        return True
    except Exception:
        return False


def report_problem(text: str, *, context: dict | None = None) -> str:
    """Обращение человека: свободный текст и то, что машина знает сама.

    Смысл ACL-354 в том, что байер пишет, что сломалось, а окружение
    собирается без него: сегодня это сообщение в переписке и фотография
    экрана — пересказанные, с опозданием и без фактов самой машины.

    **Каждое обращение — свой тикет Sentry (ACL-1273).** Раньше отпечаток был
    постоянным (`["acl-report", "manual"]`), и все обращения со всех машин
    складывались в один тикет с заголовком самого первого сообщения. Sentry
    уведомляет о новых тикетах, а не о новых событиях в старом, поэтому
    после первого обращения команда не узнала ни об одном. Теперь в отпечатке
    номер, свой на каждый вызов (он же тег `report_id`): даже повтор «всё ещё
    сломано» тем же текстом — новое обращение, а не дубль. Отказы шагов (`report_step_failure`)
    по-прежнему группируются по шагу и причине — там группировка и нужна.
    """
    if not init():
        return ""
    import sentry_sdk

    # Буква впереди — не украшение: вычиститель меняет длинные ряды цифр на
    # `<digits>`, и номер из одних цифр (бывает и у uuid4().hex) склеил бы
    # такие обращения в один тикет снова.
    report_id = "r" + uuid.uuid4().hex
    with sentry_sdk.new_scope() as scope:
        scope.fingerprint = ["acl-report", "manual", report_id]
        scope.set_tag("kind", "report")
        scope.set_tag("report_id", report_id)
        scope.set_context("report", {"text": _scrub_deep(text or "")[:4000]})
        if context:
            scope.set_context("acl", _scrub_deep(context))
        return sentry_sdk.capture_message(
            "Report: " + _scrub_deep(text or "")[:400], level="info"
        ) or ""
