"""Orchestrate RPA publish: browser session + Ads Manager flow."""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from config.paths import get_ad_account_map_path
from src.fb_ads_rpa.ads_manager_flow import AdsManagerFlow
from src.fb_ads_rpa.browser import (
    ReopenCancelled,
    BrowserConnection,
    PlaywrightProfileProvider,
    get_browser_provider,
)
from src.fb_ads_rpa.build_draft import slots_to_campaign_drafts
from src.fb_ads_rpa.config import get_browser_provider as get_browser_provider_name, get_mlx_attach_only
from src.fb_ads_rpa.draft import CampaignDraft, RpaPublishResult
from src.fb_ads_rpa.operations import serialized_browser
from src.fb_ads_rpa.profile_lease import physical_profile_key, require_profile_lease, ProfileLeaseError
from src.fb_ads_rpa.enrich_draft import enrich_campaign_drafts
from src.fb_ads_rpa import profile_trouble
from src.fb_ads_rpa import run_telemetry as _telemetry
from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry

log = logging.getLogger(__name__)


def profile_key_for(account_label: str, *, provider: str | None = None) -> str:
    """Whose browser a publish of this account occupies: the profile, not the label.

    The same rule the spend reader applies (`account_spend._profile_key`), so
    a publish and a read of one account queue on one lock. Two labels can map
    to one profile; two queues to one browser is exactly what the lock is
    against. An unresolved label refuses the operation: a label is not a
    physical identity and cannot protect a browser in another ACM process.
    """
    label = str(account_label or "").strip()
    try:
        entry = resolve_account_entry(load_ad_account_map(get_ad_account_map_path()), label)
    except Exception:
        entry = None
    profile = (getattr(entry, "profile_id", "") or "").strip() if entry else ""
    if not profile:
        raise ProfileLeaseError("Cannot resolve a physical profile from the account map; check the account connection.")
    return physical_profile_key(profile, provider=provider or get_browser_provider_name())


#: ACL-1444: машинный выключатель — `1` держит открытыми все окна профилей,
#: как до ACL-1444 (чистое задание профиль не закрывает).
ENV_KEEP_PROFILES_OPEN = "FB_RPA_KEEP_PROFILES_OPEN"

#: Строка в панели задания, когда робот закрыл профиль сам.
PROFILE_CLOSED_NOTICE = "Closed the profile — all campaigns are ready"


def keep_profiles_open() -> bool:
    """Выключатель ACL-1444 из окружения (машинный `.env`), читается при каждом задании."""
    return str(os.environ.get(ENV_KEEP_PROFILES_OPEN, "") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _on_campaign_list(url: str) -> bool:
    """Список кампаний Ads Manager — не редактор (ни `/edit`, ни `simple_create`)."""
    from src.fb_ads_rpa.simple_create import url_is_simple_create

    path = (url or "").split("?", 1)[0]
    return ("/adsmanager/manage/campaigns" in path and "/edit" not in path
            and not url_is_simple_create(url))


def _resolve_account(account_label: str):
    try:
        return resolve_account_entry(load_ad_account_map(get_ad_account_map_path()), account_label)
    except Exception:
        return None


def close_account_profile(account_label: str) -> str:
    """Закрыть профиль аккаунта по кнопке «Close profile» (ACL-1444).

    Мягкая остановка через MLX API, без kill (`browser.stop_profile_on_request`).
    Работает ли на профиле задание, проверяет дешборд (`publish_jobs.close_profile`)
    под замком профиля.
    """
    from src.fb_ads_rpa import browser as _browser

    entry = _resolve_account(account_label)
    if entry is None or not (getattr(entry, "profile_id", "") or "").strip():
        raise RuntimeError(f"{account_label}: no Multilogin profile in the account map")
    require_profile_lease(physical_profile_key(entry.profile_id))
    return _browser.stop_profile_on_request(entry, account_label)


class RpaPublisher:
    """Publish dashboard slots via Playwright + antidetect browser."""

    def __init__(
        self,
        *,
        browser_provider: str | None = None,
        dry_run: bool = False,
        hand_off: bool = False,
        selectors_path: str | None = None,
    ):
        self.browser_provider_name = browser_provider or get_browser_provider_name()
        self.dry_run = dry_run
        self.hand_off = hand_off  # fill form, disconnect, leave browser open for user
        self.selectors_path = selectors_path
        self._provider = get_browser_provider(self.browser_provider_name)
        self._conn: BrowserConnection | None = None
        self._entry = None
        self._cancelled = False
        # ACL-1614: Stop только сигналит. Поток задания слышит Event в ожиданиях
        # (`job_cancel.check`) и сам закрывает соединение на выходе.
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        """Сигнал остановки; безопасно из любого потока.

        Соединение Playwright и браузер отсюда НЕ закрываются (ACL-1614): sync
        Playwright принадлежит потоку задания, а закрытие из чужого потока
        оставляло его в бесконечном цикле с GIL в руках (05.10). Поток
        задания увидит флаг, выйдет `JobCancelled` и вызовет `close` сам;
        окно Multilogin остаётся открытым, как и раньше.
        """
        self._cancelled = True
        self.cancel_event.set()

    def build_drafts(
        self,
        slots: list[dict[str, Any]],
        *,
        account_label: str,
        country: str,
        activate: bool = False,
        preview_mode: bool = False,
    ) -> list[CampaignDraft]:
        # ACL-1313: длительность сборки (вместе с библиотекой и книгой) — в
        # телеметрию, а момент конца — для замера ожидания профиля.
        with _telemetry.phase("build_drafts", diag={"slots": len(slots or []),
                                                    "preview": bool(preview_mode)}):
            drafts = slots_to_campaign_drafts(
                slots,
                account_label=account_label,
                country=country,
                activate=activate,
            )
            drafts = enrich_campaign_drafts(
                drafts,
                account_label=account_label,
                preview_mode=preview_mode,
            )
            # ACL-1768: робот ведёт один набор (`ad_sets[0]`); остальные не отбрасываем молча.
            for d in drafts:
                if len(d.ad_sets) > 1:
                    d.errors.append("Several ad sets need the API backend: the RPA backend builds "
                                    "one ad set per campaign")
        self._built_at = time.monotonic()
        if not preview_mode and any(d.page_provided and not d.errors for d in drafts):
            from src.web.account_pages import read_pages, resolve_selection
            from src.fb_api.graph import normalize_act

            available = read_pages(account_label)
            for draft in drafts:
                if not draft.page_provided or draft.errors:
                    continue
                try:
                    if not available.get("ok"):
                        raise ValueError(available.get("message") or "Could not load Facebook Pages. Refresh pages to retry.")
                    if available.get("act") != normalize_act(draft.ad_account_id or ""):
                        raise ValueError("The account changed while reading Facebook Pages. Refresh pages and retry.")
                    chosen = resolve_selection(available["pages"], draft.page_id or "")
                    draft.page_id, draft.page_name = chosen["id"], chosen["name"]
                    draft.page_name_unique = sum(p["name"] == chosen["name"] for p in available["pages"]) == 1
                except ValueError as exc:
                    draft.errors.append(str(exc))
        return drafts

    def _resolve_entry(self, account_label: str):
        account_map = load_ad_account_map(get_ad_account_map_path())
        return resolve_account_entry(account_map, account_label)

    def _reset_browser(self, *, keep_browser_open: bool = False) -> None:
        # ACL-1702 (ревью PR #704, m3): адрес CDP старого соединения сторожу больше не нужен.
        try:
            from src.fb_ads_rpa import tab_watchdog

            tab_watchdog.forget_endpoint()
        except Exception:
            pass
        if self._conn:
            try:
                self._conn.close(keep_browser_open=keep_browser_open)
            except Exception:
                pass
            self._conn = None
        if self._entry is not None:
            try:
                self._provider.close(self._entry, keep_browser_open=keep_browser_open)
            except Exception:
                pass

    @staticmethod
    def _browser_closed_error(exc: BaseException) -> bool:
        err = str(exc).lower()
        return (
            "target page" in err
            or "browser has been closed" in err
            or "browser closed" in err
            or "context or browser has been closed" in err
        )

    def _open_multilogin_direct(self, entry, account_label: str) -> BrowserConnection:
        """Try DirectCDP, and leave the publisher as it was if it does not work.

        The provider moves only on success. It used to move on the way in, and
        a failed attempt left the publisher holding DirectCDP: the next line of
        `_ensure_browser` — the one the log calls "the normal chain" — then ran
        DirectCDP a second time and failed the same way, while the configured
        provider was never asked at all (measured on DQ501, 2026-09-16).
        """
        from src.fb_ads_rpa.browser import MultiloginDirectCDPProvider

        log.warning("Falling back to MultiloginDirectCDP (Mimic + CDP, no MLX API token).")
        previous = self._provider
        direct = MultiloginDirectCDPProvider()
        try:
            conn = direct.open(entry, account_label)
        except Exception:
            self._provider = previous
            raise
        self._provider = direct
        return conn

    @staticmethod
    def _profile_already_running(entry) -> bool:
        """Is this MLX profile currently open (started by hand from MLX)?

        Detected by a live chrome.exe whose command line carries the
        profile's own user-data-dir. MLX shortens the path on disk
        (…\\mlx\\profiles\\B18D03~1\\3EF7F4~1\\C21D4D~1), so match on the
        profile id's leading characters rather than the full uuid.
        """
        profile_id = (getattr(entry, "profile_id", "") or "").strip()
        if not profile_id:
            return False
        needle = profile_id.replace("-", "")[:6].lower()
        try:
            import psutil  # type: ignore

            for proc in psutil.process_iter(["name", "cmdline"]):
                try:
                    name = (proc.info.get("name") or "").lower()
                    if "chrome" not in name:
                        continue
                    cmd = " ".join(proc.info.get("cmdline") or []).lower()
                    if "mlx" in cmd and needle in cmd.replace("-", ""):
                        return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    def _open_playwright_profile(self, entry, account_label: str) -> BrowserConnection:
        # SAFETY GATE (user directive 2026-07-30, after this fallback fired
        # for real): opening the account's cookies in vanilla Chromium — no
        # Mimic fingerprint, no profile proxy — is exactly the signature
        # that gets ad accounts burned. It happened live: the DQ204 browser
        # wedged (CDP dead, profile lock held), DirectCDP failed, and the
        # chain silently dropped to a plain browser with the real session.
        # Never do that automatically again. If someone truly wants this
        # path, they must set FB_RPA_ALLOW_PLAIN_BROWSER=1 explicitly.
        import os as _os

        allow = (_os.environ.get("FB_RPA_ALLOW_PLAIN_BROWSER") or "").strip().lower()
        if allow not in ("1", "true", "yes", "on"):
            raise RuntimeError(
                "PlaywrightProfile fallback is DISABLED: it would open the "
                "account session in a vanilla (non-antidetect, non-proxied) "
                "browser. Re-open the profile in the Multilogin Desktop app "
                "and retry. (Override only if you understand the risk: "
                "FB_RPA_ALLOW_PLAIN_BROWSER=1.)"
            )
        log.warning(
            "Using PlaywrightProfileProvider — vanilla Chromium + MLX profile cookies "
            "(NOT Mimic/antidetect). Prefer opening the profile in MLX Desktop + DirectCDP, "
            "or refresh MULTILOGIN_AUTOMATION_TOKEN_A."
        )
        self._provider = PlaywrightProfileProvider()
        return self._provider.open(entry, account_label)

    @staticmethod
    def _mlx_fallback_error(direct_exc: BaseException, pw_exc: BaseException | None = None) -> RuntimeError:
        parts = [
            "Could not open Multilogin browser for RPA.",
            "1) Open this profile in the Multilogin (MLX) Desktop app.",
            "2) Retry publish (RPA will attach via CDP), or set FB_BROWSER_PROVIDER=multilogin_direct.",
            "3) Or refresh MULTILOGIN_AUTOMATION_TOKEN_A and use the Multilogin API provider.",
            f"DirectCDP error: {direct_exc}",
        ]
        if pw_exc is not None:
            parts.append(f"PlaywrightProfile error: {pw_exc}")
        return RuntimeError(" ".join(parts))

    def _ensure_browser(self, account_label: str) -> BrowserConnection:
        if self._conn is not None:
            require_profile_lease(physical_profile_key(
                getattr(self._entry, "profile_id", ""), provider=self.browser_provider_name))
            return self._conn
        entry = self._resolve_entry(account_label)
        if not entry:
            raise RuntimeError(f"Account {account_label!r} not in ad_account_map.csv")
        require_profile_lease(physical_profile_key(entry.profile_id, provider=self.browser_provider_name))
        self._entry = entry
        attach_only = get_mlx_attach_only()
        # When handing off / keeping the window, never hard-kill on provider switch.
        soft = bool(self.hand_off or self.dry_run)

        # If the user already has this profile open in MLX Desktop, go
        # straight to DirectCDP. The MLX automation API refuses to start a
        # profile that is already running ("HTTP 401: Profile Authorization
        # error", seen live 2026-07-31), so trying the API first just burns
        # time and then fails. DirectCDP is built for exactly this: it finds
        # the running profile and relaunches it with a debugging port.
        # Working on an already-open account is the normal case, not an
        # exception — the user should never have to close their browser
        # for a run to start.
        #
        # Not under attach-only, though, which is the default. There the
        # configured provider already attaches to a running profile before it
        # starts one, and when that fails it restarts the profile through the
        # MLX API — keeping the proxy and the cookies, which is precisely what
        # DirectCDP cannot do and rightly refuses to fake. Taking the shortcut
        # there only produced a refusal, and the buyer was told to close a
        # window that the RPA was able to take over by itself.
        if not attach_only and self._profile_already_running(entry):
            log.info("Profile already open in MLX — using DirectCDP instead of the API.")
            try:
                self._conn = self._open_multilogin_direct(entry, account_label)
                return self._conn
            except profile_trouble.ProfileStop:
                # ACL-1138: причина в профиле (например, нет прокси) — обычная
                # цепочка упрётся в то же, обходить нельзя.
                self._reset_browser(keep_browser_open=True)
                raise
            except Exception as direct_exc:
                log.warning("DirectCDP on running profile failed (%s); trying normal chain.", direct_exc)
                self._reset_browser(keep_browser_open=True)

        try:
            # ACL-1562: окно, открытое руками, провайдер переоткрывает сразу —
            # ожидания тишины ввода (ACL-1427) и его Stop больше нет.
            # ACL-1606: Stop слышен и между повторами запуска профиля.
            try:
                self._provider.cancel_check = lambda: bool(getattr(self, "_cancelled", False))
            except Exception:
                pass
            self._conn = self._provider.open(entry, account_label)
            log.info("Browser opened via %s", self._provider.__class__.__name__)
        except Exception as exc:
            err_str = str(exc)
            self._reset_browser(keep_browser_open=soft)
            if isinstance(exc, (profile_trouble.ProfileStop, ReopenCancelled)):
                # ACL-1320: причина в профиле (вход, сеть, открыт руками) —
                # её текст и есть сообщение; обходных путей нет.
                raise
            if attach_only:
                # The reason above is the one from the Multilogin automation
                # API, and it is the whole message worth reading. A profile
                # left open in MLX Desktop is no longer a reason to do
                # anything: that path restarts it by itself.
                # ACL-1320: профиль, открытый руками, робот больше не
                # перезапускает (это `profile_open_by_hand` выше) — хвост
                # сообщения не обещает того, чего не будет.
                raise RuntimeError(
                    f"{exc} "
                    "(MLX attach-only: RPA connects through the Multilogin automation API "
                    "on Publish.)"
                ) from exc
            if "401" in err_str or "Authorization" in err_str or "authorization" in err_str:
                log.warning("MLX API returned 401 (token expired). Trying DirectCDP.")
                try:
                    self._conn = self._open_multilogin_direct(entry, account_label)
                except Exception as direct_exc:
                    log.warning("DirectCDP failed (%s). Falling back to PlaywrightProfile.", direct_exc)
                    # Keep Multilogin open if it started; only disconnect CDP.
                    self._reset_browser(keep_browser_open=True)
                    try:
                        self._conn = self._open_playwright_profile(entry, account_label)
                    except Exception as pw_exc:
                        raise self._mlx_fallback_error(direct_exc, pw_exc) from pw_exc
            elif (
                "license check failed" in err_str.lower()
                or "mimic browser exited" in err_str.lower()
                or "econnrefused" in err_str.lower()
                or self._browser_closed_error(exc)
            ):
                log.warning("Multilogin launch failed (%s). Trying DirectCDP.", exc)
                try:
                    self._conn = self._open_multilogin_direct(entry, account_label)
                except Exception as direct_exc:
                    log.warning("DirectCDP failed (%s). Falling back to PlaywrightProfile.", direct_exc)
                    self._reset_browser(keep_browser_open=True)
                    try:
                        self._conn = self._open_playwright_profile(entry, account_label)
                    except Exception as pw_exc:
                        raise self._mlx_fallback_error(direct_exc, pw_exc) from pw_exc
            else:
                raise
        return self._conn

    def close(self, *, force: bool = False) -> None:
        # Always leave Multilogin/Mimic open so the user can verify FB login.
        # force/cancel only stop RPA + disconnect CDP — never stop_profile.
        _ = force  # kept for call-site compat; must not close MLX window
        self._account_stop = None
        if self._conn:
            try:
                self._conn.close(keep_browser_open=True)
            except Exception:
                pass
            self._conn = None
        if self._entry is not None:
            try:
                self._provider.close(self._entry, keep_browser_open=True)
            except Exception:
                pass
            self._entry = None

    def finish_job(self, account_label: str, *, clean: bool, other_job_waits: bool = False) -> str:
        """Конец задания: отпустить браузер и, если всё чисто, закрыть профиль (ACL-1444).

        `clean` — вердикт задания (все кампании годны к публикации, ошибок
        нет; решает `publish_jobs`). Профиль закрывается, только когда ВСЁ:
        выключатель `FB_RPA_KEEP_PROFILES_OPEN` не стоит, задание не отменяли,
        черновик сохранён (`_draft_saved`), а провайдер умеет и вправе закрыть
        этот профиль (`close_finished_profile`: только профиль робота, мягкая
        остановка через MLX API). Иначе окно остаётся, как раньше.

        `other_job_waits` — на этот же профиль уже стоит другое задание:
        закрывать незачем, оно поднимет профиль сразу (ревью PR #446, F6).

        CDP отпускается всегда (`close`), до остановки профиля. Строка
        телеметрии `profile_closed_after_job`: ok True — закрыт, False — MLX
        не остановил, None — оставлен намеренно (в т.ч. окно руками); `note` — почему.
        """
        entry, conn = self._entry, self._conn
        provider = self._provider
        outcome = ""
        if keep_profiles_open():
            outcome = "kept_switch"
        elif not clean:
            outcome = "kept_problem"
        elif self._cancelled:
            outcome = "kept_cancelled"
        elif entry is None or conn is None:
            outcome = "kept_no_session"
        elif not callable(getattr(provider, "close_finished_profile", None)):
            outcome = "kept_provider"
        else:
            try:
                saved = self._draft_saved(conn.page)
            except Exception:
                saved = False
            if not saved:
                outcome = "kept_unsaved"
        self.close()
        if not outcome and self._cancelled:
            # Stop нажали, пока ждали автосохранения (ревью PR #446, F2).
            outcome = "kept_cancelled"
        if not outcome and other_job_waits:
            outcome = "kept_next_job"
        if not outcome:
            try:
                outcome = str(provider.close_finished_profile(entry, account_label) or "stop_failed")
            except Exception as exc:
                log.warning("%s: closing the finished profile failed: %s", account_label,
                            _telemetry.scrub(str(exc))[:200])
                outcome = "stop_failed"
        ok = True if outcome == "closed" else (False if outcome == "stop_failed" else None)
        _telemetry.record("profile_closed_after_job", ok, note=outcome,
                          diag={"provider": type(provider).__name__})
        if outcome == "closed":
            log.info("%s: all campaigns are ready - closed the profile", account_label)
        return outcome

    @staticmethod
    def _draft_saved(page: Any) -> bool:
        """Черновик сохранён (ACL-1444). Закрыто по умолчанию: «нечего
        сохранять» — только на списке кампаний; любая другая страница
        (прежний редактор `/edit`, новый «в один экран» `simple_create`,
        чужая или непрочитанная) — дождаться автосохранения Meta и увидеть
        «All edits saved» (ревью PR #446, F1)."""
        from src.fb_ads_rpa import meta_dom

        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        if _on_campaign_list(url):
            return True
        try:
            meta_dom.wait_editor_saved(page, timeout_s=15)
        except Exception:
            pass
        return bool(meta_dom._all_edits_saved(page))

    def _drop_hung_tab(self) -> None:
        """Вкладку закрыл сторож (ACL-1702): снять метку и отпустить соединение.

        Отключение Playwright — из ЭТОГО потока (потока задания), окно
        профиля остаётся открытым (`keep_browser_open`). Следующая кампания
        задания подключается заново (`_ensure_browser`) и берёт живую вкладку;
        иначе страж «вкладки нет» отвечал бы `browser_closed` на каждую.
        """
        from src.fb_ads_rpa import tab_watchdog

        conn = getattr(self, "_conn", None)
        if conn is not None:
            tab_watchdog.clear_page_mark(getattr(conn, "page", None))
        self._reset_browser(keep_browser_open=True)

    def _flow(self, conn: BrowserConnection, account_label: str) -> AdsManagerFlow:
        """Поток кампании, который может сменить вкладку и перезапустить профиль."""
        return AdsManagerFlow(
            conn.page, selectors_path=self.selectors_path,
            on_page_replaced=self._adopt_page,
            restart_profile=lambda: self._restart_unresponsive_profile(account_label),
        )

    def _adopt_page(self, page: Any) -> None:
        """Поток сменил зависшую вкладку на свежую (ACL-1320): соединение
        дальше отдаёт её — и следующей кампании, и уборке в конце."""
        if self._conn is not None and page is not None:
            self._conn.page = page

    def _restart_unresponsive_profile(self, account_label: str) -> Any:
        """Перезапустить не отвечающий профиль через MLX API (ACL-1320).

        Только у провайдера, который это умеет (`restart_unresponsive`,
        attach-only по умолчанию): остановка и запуск идут через API
        рабочего пространства профиля, простой браузер не открывается
        никогда. Возвращает страницу нового соединения или None.
        """
        entry = self._entry
        restart = getattr(self._provider, "restart_unresponsive", None)
        if entry is None or restart is None:
            return None
        self._reset_browser(keep_browser_open=True)
        try:
            self._conn = restart(entry, account_label)
        except profile_trouble.ProfileStop:
            self._conn = None
            raise
        except Exception as exc:
            # ACL-1320, живой прогон 26.09 15:40 (Facebook_9943): остановка
            # прошла, подключение — `connect ECONNREFUSED`, и профиль остался
            # закрытым, а итог говорил только «профиль не отвечает». Теперь
            # это своя остановка с понятным текстом для человека.
            log.warning("Restart of unresponsive profile %s failed: %s", account_label, exc)
            self._conn = None
            raise profile_trouble.ProfileStop(
                profile_trouble.RESTART_FAILED, f"{type(exc).__name__}: {str(exc)[:120]}") from exc
        return self._conn.page

    @staticmethod
    def _report_profile_stop(stop: "profile_trouble.ProfileStop", campaign_name: str,
                             account: str, where: str, *, cancelled: bool = False) -> None:
        """Остановка из-за профиля до кампании (подключение): строка
        телеметрии и событие Sentry — как у остановки внутри кампании
        (ревью PR #323, F7: open-by-hand чаще всего случается именно здесь).

        Без раздела страницы (ревью PR #328, F2): страницы этой кампании не
        было, а раздел, оставшийся в памяти, снят другой кампанией или до
        падения вкладки."""
        try:
            _telemetry.record(stop.code, False, campaign=campaign_name, note=where,
                              diag={"detail": stop.detail, "message": profile_trouble.message(stop.code)})
        except Exception:
            pass
        if cancelled:
            # Ревью PR #428 (F3): задание остановил человек (Stop) — это не
            # сбой кампании, в Sentry не шлём (ACL-1318: туда — только
            # настоящие сбои).
            return
        try:
            from src.fb_ads_rpa import sentry_signal

            sentry_signal.report_campaign_stop(stop.code, campaign=campaign_name,
                                               account=account or "", attempt=1,
                                               attach_section=False)
        except Exception:
            pass
        try:
            # ACL-1447: и итог кампании — остановлена до первого шага.
            from src.fb_ads_rpa import run_summary

            run_summary.report_finished(campaign=campaign_name, account=account or "",
                                        stopped_at=stop.code, steps=[])
        except Exception:
            pass

    @staticmethod
    def _begin_campaign_context() -> None:
        """Забыть прогон предыдущей кампании в этом потоке (ревью PR #328, F2).

        Задание публикует кампании аккаунта по очереди в одном потоке, а
        номер прогона называет только поток кампании (`set_current_run`).
        Без этого остановка при подключении кампании B шла с номером прогона
        кампании A — и разбор шёл по чужому прогону. Номер задания (ACL-1315)
        сохраняется: он общий для всех кампаний задания.
        """
        try:
            job = _telemetry.current_job_id()
            _telemetry.clear_current_run()
            if job:
                _telemetry.set_current_job(job)
        except Exception:
            pass
        # ACL-1702 (ревью PR #704, B3): разблокировка, случившаяся вне кампании
        # (уборка окна, шаги после потока), не клеймит `tab_hung` следующую.
        try:
            from src.fb_ads_rpa import tab_watchdog

            tab_watchdog.take_hung()
        except Exception:
            pass

    @staticmethod
    def _profile_stop_result(campaign_name: str, stop: "profile_trouble.ProfileStop", *,
                             again: bool = False) -> RpaPublishResult:
        """Итог кампании, которую остановил профиль (ACL-1320): не собрана,
        блокирует, причина — текстом для человека."""
        result = RpaPublishResult(campaign_name=campaign_name, success=False)
        result.error = f"{stop.code}: {profile_trouble.message(stop.code)}"
        if stop.detail and not again:
            result.error += f" ({stop.detail})"
        result.blocking_failures.append(stop.code)
        result.steps_completed.append(
            f"profile_stop:{stop.code}" + (":earlier_campaign" if again else ""))
        return result

    @serialized_browser(key=lambda self, draft: profile_key_for(
        draft.account_label, provider=getattr(self, "browser_provider_name", None)))
    def publish_draft(self, draft: CampaignDraft) -> RpaPublishResult:
        if (draft.status or "").strip().upper() != "DRAFT":
            raise ValueError(
                "ACL is draft-only: ACTIVE campaigns are not accepted by the RPA publisher"
            )
        if draft.errors:
            return RpaPublishResult(
                campaign_name=draft.name,
                success=False,
                error="; ".join(draft.errors),
            )
        # hand_off: fill everything but leave browser open; treat as DRAFT status
        if self.hand_off:
            draft.status = "DRAFT"
        # ACL-1313: от конца сборки черновиков до сюда — ожидание очереди
        # профиля (publish_lease) и подготовки; замеряется один раз на сборку.
        campaign_name = str(getattr(draft, "name", "") or "")
        self._begin_campaign_context()
        built_at = getattr(self, "_built_at", None)
        if built_at is not None:
            self._built_at = None
            _telemetry.record("job_phase", True, campaign=campaign_name, note="profile_queue",
                              duration_s=time.monotonic() - built_at)
        # ACL-1320: профиль уже остановил задание (вход, сеть, открыт руками,
        # не поднялся) — следующая кампания упрётся в то же, браузер не трогаем.
        stopped = getattr(self, "_account_stop", None)
        if stopped is not None:
            return self._profile_stop_result(campaign_name, stopped, again=True)
        try:
            if getattr(self, "_conn", None) is None:
                with _telemetry.phase("browser_attach", campaign=campaign_name) as info:
                    conn = self._ensure_browser(draft.account_label)
                    info["provider"] = str(getattr(conn, "provider", "") or type(getattr(self, "_provider", None)).__name__)
            else:
                conn = self._ensure_browser(draft.account_label)
        except profile_trouble.ProfileStop as stop:
            self._account_stop = stop
            self._report_profile_stop(stop, campaign_name, draft.account_label, "attach",
                                      cancelled=bool(getattr(self, "_cancelled", False)))
            return self._profile_stop_result(campaign_name, stop)
        except ReopenCancelled:
            # ACL-1606: Stop нажат, пока профиль ждали; профиль не поднят.
            return RpaPublishResult(campaign_name=campaign_name, success=False,
                                    error="Stopped by the user before the campaign started",
                                    steps_completed=["cancelled_before_campaign"])
        if getattr(self, "_cancelled", False):
            # Ревью PR #563 (F2): Stop нажали, пока открывался браузер
            # (переоткрытие окна, открытого руками, идёт секунды и не
            # прерывается — ACL-1562). Кампанию не начинаем; окно остаётся
            # открытым, отключаемся только от CDP.
            self._reset_browser(keep_browser_open=True)
            return RpaPublishResult(campaign_name=campaign_name, success=False,
                                    error="Stopped by the user before the campaign started",
                                    steps_completed=["cancelled_before_campaign"])
        flow = self._flow(conn, draft.account_label)
        try:
            result = flow.publish_campaign(draft, dry_run=self.dry_run)
        except Exception as exc:
            # ACL-1702: вкладку разблокировал сторож (зависла в одном вызове) —
            # кампания кончена красным `tab_hung`, без переподключения и
            # повтора: черновик частичный, повтор наплодил бы дубли.
            from src.fb_ads_rpa import failure_policy, tab_watchdog

            hung = tab_watchdog.take_hung()
            if hung:
                step = f"tab_hung:{hung}"
                # Ревью PR #704 (B3): метку снять и соединение сбросить — вкладку
                # сторож закрыл, следующая кампания задания подключится заново.
                self._drop_hung_tab()
                return RpaPublishResult(
                    campaign_name=campaign_name, success=False,
                    error=f"Recorded flow failed at: {step} - {failure_policy.reason(step)}",
                    steps_completed=[f"recorded_flow_fail:{step}"],
                    blocking_failures=[step],
                    run_id=_telemetry.current_run_id() or None)
            if not self._browser_closed_error(exc):
                raise
            # A dead PAGE usually does not mean a dead BROWSER — confirmed
            # live 2026-07-31: a tab crashed mid-run while Mimic itself kept
            # running with its CDP port alive. Re-attach through the normal
            # provider chain first (which starts with attach-to-running);
            # the plain-browser fallback stays behind its v72 safety gate.
            log.warning("Browser target died mid-run (%s). Re-attaching.", exc)
            self._reset_browser(keep_browser_open=True)
            try:
                conn = self._ensure_browser(draft.account_label)
            except profile_trouble.ProfileStop as stop:
                self._account_stop = stop
                self._report_profile_stop(stop, campaign_name, draft.account_label, "reattach")
                return self._profile_stop_result(campaign_name, stop)
            except Exception as re_exc:
                raise self._mlx_fallback_error(exc, re_exc) from re_exc
            flow = self._flow(conn, draft.account_label)
            result = flow.publish_campaign(draft, dry_run=self.dry_run)
        # ACL-1702 (ревью PR #704, B3): кампания кончилась `tab_hung` — вкладку
        # закрыл сторож; соединение сбросить, следующая кампания — заново.
        if any(str(f).startswith("tab_hung") for f in (getattr(result, "blocking_failures", None) or [])):
            self._drop_hung_tab()
        # ACL-1437: номер прогона — в «Technical details» карточки.
        if isinstance(result, RpaPublishResult) and not result.run_id:
            result.run_id = _telemetry.current_run_id() or None
        for failure in result.blocking_failures or []:
            code = str(failure).split(":", 1)[0]
            if code in profile_trouble.ACCOUNT_STOPS:
                self._account_stop = profile_trouble.ProfileStop(code)
                break
        if self.hand_off and result.publishable:
            result.error = "Draft ready — browser left open, Meta Publish was not clicked"
        elif self.hand_off and result.success:
            # A draft can exist while being unsafe to publish.  Never replace
            # the blocking verdict with a reassuring hand-off message.
            note = "Browser left open for manual review; this draft is NOT publishable."
            result.error = f"{result.error}\n{note}" if result.error else note
        elif self.dry_run and result.publishable:
            provider = getattr(self._conn, "provider", "unknown") if self._conn else "unknown"
            result.error = (
                "Dry run complete — browser left open, Publish NOT clicked. "
                f"Browser: {provider}."
            )
        return result

    @serialized_browser(key=lambda self, slots, *, account_label, **kw: profile_key_for(
        account_label, provider=getattr(self, "browser_provider_name", None)))
    def publish_slots(
        self,
        slots: list[dict[str, Any]],
        *,
        account_label: str,
        country: str,
        activate: bool = False,
    ) -> list[RpaPublishResult]:
        if activate:
            raise ValueError(
                "ACL is draft-only: activate=True is disabled in the campaign-manager build"
            )
        drafts = self.build_drafts(
            slots,
            account_label=account_label,
            country=country,
            activate=activate,
            # Never weaken validation for a run that drives a browser. This
            # read `not self.dry_run`, which inverted the two: a REAL run got
            # preview leniency — creative paths synthesised without checking
            # the file exists, a missing Multilogin profile_id downgraded from
            # error to warning — while the supposedly safer dry run got the
            # strict checks. A dry run opens the same browser and needs the
            # same real files; only the callers that render a plan without a
            # browser (dashboard preview, `--preview-only`) pass True.
            preview_mode=False,
        )
        results: list[RpaPublishResult] = []
        try:
            for draft in drafts:
                if draft.errors:
                    results.append(
                        RpaPublishResult(
                            campaign_name=draft.name,
                            success=False,
                            error="; ".join(draft.errors),
                        )
                    )
                    continue
                results.append(self.publish_draft(draft))
        finally:
            # Park the tab on the campaigns LIST before letting go of the
            # browser. Confirmed live 2026-07-31 (Test152 server-truth
            # check): an ad editor left open after the run lets Meta's
            # "Instant form (suggested)" re-render eventually COMMIT via the
            # page's own autosave — the last ad of the run kept reverting
            # server-side even after the in-run sweep and tail watch had
            # verified it. The last ad of the FIRST campaign always
            # survived, because starting the next campaign navigated away —
            # this applies the same escape to the final campaign.
            try:
                if self._conn is not None:
                    self._goto_campaigns_list(self._conn.page)
            except Exception:
                pass
            self.close()
        return results

    @staticmethod
    def _goto_campaigns_list(page) -> None:
        import re as _re

        url = page.url or ""
        m = _re.search(r"[?&]act=(\d+)", url)
        if not m or "/adsmanager/" not in url:
            return
        page.goto(
            f"https://adsmanager.facebook.com/adsmanager/manage/campaigns?act={m.group(1)}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        # ACL-1313: без `wait_for_timeout(3000)`. Цель — уйти из редактора,
        # и она достигнута, когда goto вернулся: документ редактора выгружен
        # (domcontentloaded нового). Браузер профиля после close() остаётся
        # открытым и догружает список сам; 3 с ожидания ничего не добавляли.

    def __enter__(self) -> "RpaPublisher":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
