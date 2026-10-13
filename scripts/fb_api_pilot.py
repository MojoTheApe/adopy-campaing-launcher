"""Read-only Graph API pilot inside the account's own Multilogin profile (ACL-1756).

Opens (or attaches to) the account's Multilogin profile, opens ONE own tab
with Ads Manager, lets the page's helper pick up the session token inside the
page, and reads:

  GET /me/adaccounts
  GET /act_X/campaigns
  GET /act_X/insights   (yesterday, last 7 days)

Never writes to Meta. The token never leaves the tab: this script only sees
JSON answers. Output shows counts and spend numbers; ids are masked to the
last 4 digits.

Default is a DRY RUN that contacts nothing. Real calls need --run.

Usage:
  python scripts/fb_api_pilot.py --account KLD_BFL_B93
  python scripts/fb_api_pilot.py --account KLD_BFL_B93 --run
  python scripts/fb_api_pilot.py --profile-id <uuid> --act act_123 [--folder-id <uuid>] [--instance A] --run

Diagnosis options (default: mirror Ads Manager's own token request):
  --graph-host captured|public       captured = the Graph host Ads Manager uses; public = graph.facebook.com
  --credentials captured|include|omit   cookie mode of our GET calls
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.fb_api import telemetry  # noqa: E402
from src.fb_api.graph import GraphClient, GraphStop, LoggedOut, Checkpoint, PageFailed, normalize_act  # noqa: E402
from src.fb_api.telemetry import mask_id, scrub  # noqa: E402

LEAD_ACTIONS = ("lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead")


class Target:
    def __init__(self, label: str, act: str, profile_id: str, folder_id: str, instance: str):
        self.label = label
        self.act = act
        self.profile_id = profile_id
        self.folder_id = folder_id
        self.instance = instance


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Read-only Graph API pilot inside a Multilogin profile.")
    p.add_argument("--account", help="account name from ad_account_map.csv")
    p.add_argument("--profile-id", help="Multilogin profile id (instead of --account)")
    p.add_argument("--act", help="ad account id act_<digits> (instead of --account)")
    p.add_argument("--folder-id", default="", help="Multilogin folder id (with --profile-id)")
    p.add_argument("--instance", default="A", help="Multilogin slot A or B (with --profile-id)")
    p.add_argument("--run", action="store_true", help="really open the profile and call Graph API")
    p.add_argument("--timeout", type=float, default=90.0,
                   help="seconds to wait for Ads Manager to load and show its token (default 90)")
    p.add_argument("--graph-host", choices=["captured", "public"], default="captured",
                   help="captured: the Graph host Ads Manager itself uses; public: graph.facebook.com")
    p.add_argument("--credentials", choices=["captured", "include", "omit"], default="captured",
                   help="cookie mode of our call; captured mirrors Ads Manager's own token request")
    return p.parse_args(argv)


def resolve_target(args: argparse.Namespace) -> Target:
    if args.account:
        from config.paths import get_ad_account_map_path
        from src.meta_campaign_agent.ad_account_map import load_ad_account_map, resolve_account_entry

        entry = resolve_account_entry(load_ad_account_map(get_ad_account_map_path()), args.account)
        if entry is None:
            raise SystemExit(f"Account {args.account!r} is not in the account map (or the name is ambiguous).")
        if not entry.profile_id:
            raise SystemExit(f"Account {args.account!r} has no Multilogin profile id in the account map.")
        if not entry.ad_account_id:
            raise SystemExit(f"Account {args.account!r} has no ad account id in the account map.")
        return Target(args.account, normalize_act(entry.ad_account_id), entry.profile_id,
                      entry.folder_id or "", entry.multilogin_instance or "A")
    if not (args.profile_id and args.act):
        raise SystemExit("Give --account, or both --profile-id and --act.")
    try:
        act = normalize_act(args.act)
    except ValueError as exc:
        raise SystemExit(str(exc))
    return Target("(manual)", act, args.profile_id.strip(), args.folder_id.strip(),
                  (args.instance or "A").strip().upper())


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def summarize_insights(row: dict) -> dict:
    leads = {}
    for action in row.get("actions") or []:
        kind = str(action.get("action_type") or "")
        if kind in LEAD_ACTIONS:
            leads[kind] = int(_num(action.get("value")))
    return {
        "spend": round(_num(row.get("spend")), 2),
        "impressions": int(_num(row.get("impressions"))),
        "clicks": int(_num(row.get("clicks"))),
        "currency": str(row.get("account_currency") or ""),
        "leads": leads,
    }


class Steps:
    """Какой шаг пилота идёт: строка на каждый шаг и имя шага в отказе."""

    def __init__(self, emit: Callable[[str], None], clock: Callable[[], float] | None = None):
        import time as _time

        self._emit = emit
        self._clock = clock or _time.monotonic
        self._t0 = self._clock()
        self.current = ""
        self.started_at = self._t0

    def enter(self, name: str) -> None:
        self.current = name
        self.started_at = self._clock()
        self._emit(f"Step: {name} (t+{self.started_at - self._t0:.1f} s)")

    def mark(self, exc: BaseException) -> BaseException:
        """Приписать к исключению шаг и сколько он шёл (для вывода и журнала)."""
        try:
            exc.pilot_step = self.current  # type: ignore[attr-defined]
            exc.pilot_step_s = round(self._clock() - self.started_at, 1)  # type: ignore[attr-defined]
        except Exception:
            pass
        return exc


def run_pilot(target: Target, *, emit: Callable[[str], None], timeout: float = 90.0,
              open_profile: Callable[..., Any] | None = None,
              release_profile: Callable[..., Any] | None = None,
              open_tab: Callable[[str], Any] | None = None,
              make_graph: Callable[[Any], Any] | None = None,
              make_client: Callable[[Any], GraphClient] | None = None,
              graph_host: str = "captured", credentials: str = "captured",
              steps: Steps | None = None) -> dict:
    """Один проход пилота. Зависимости подменяются в тестах."""
    steps = steps or Steps(emit)
    try:
        return _run_pilot(target, emit=emit, timeout=timeout, open_profile=open_profile,
                          release_profile=release_profile, open_tab=open_tab, make_graph=make_graph,
                          make_client=make_client, graph_host=graph_host, credentials=credentials,
                          steps=steps)
    except BaseException as exc:
        raise steps.mark(exc)


def _run_pilot(target: Target, *, emit, timeout, open_profile, release_profile, open_tab,
               make_graph, make_client, graph_host, credentials, steps: Steps) -> dict:
    from src.fb_api import profile as _profile
    from src.fb_api.cdp import PageTab
    from src.fb_api.token_capture import InPageGraph

    open_profile = open_profile or _profile.open_profile
    release_profile = release_profile or _profile.release_profile
    open_tab = open_tab or PageTab.open
    make_graph = make_graph or (lambda tab: InPageGraph(tab, graph_host=graph_host, credentials=credentials))
    make_client = make_client or GraphClient

    summary: dict[str, Any] = {"account": target.label, "act": mask_id(target.act)}
    steps.enter("open_profile (MLX launcher)")
    opened = open_profile(target.profile_id, target.folder_id, target.instance)
    summary["profile_started_by_pilot"] = bool(getattr(opened, "started_by_us", False))
    emit(f"Profile {mask_id(target.profile_id)}: "
         + ("started by the pilot" if summary["profile_started_by_pilot"] else "already running, attached"))
    try:
        steps.enter("open_tab (own background tab over CDP)")
        tab = open_tab(opened.cdp_url)
        throttle = getattr(tab, "throttle", None)
        if isinstance(throttle, dict):
            emit(f"Background throttling off: focus={throttle.get('focus')}, "
                 f"lifecycle={throttle.get('lifecycle')}")
        try:
            graph = make_graph(tab)
            steps.enter("install_helper")
            graph.install()
            steps.enter("navigate (Ads Manager)")
            graph.navigate(target.act, timeout=min(60.0, timeout))
            steps.enter(f"wait_for_token (up to {timeout:.0f} s)")
            state = graph.wait_ready(timeout=timeout)
            summary["page"] = {k: state.get(k) for k in ("kind", "has_token", "source", "graph_host",
                                                         "version", "credentials", "am_request")}
            if state.get("kind") == "checkpoint":
                raise Checkpoint("the profile is on a Facebook checkpoint")
            if state.get("kind") == "login":
                raise LoggedOut("the profile is logged out of Facebook")
            if not state.get("has_token"):
                raise PageFailed("Ads Manager made no Graph API request with a token "
                                 f"(page: {state.get('kind')}); the profile may be logged out")
            emit(f"Ads Manager loaded; session token found in the page ({state.get('source')}), "
                 "it stays there")
            emit(f"Ads Manager's own Graph request: {state.get('am_request')}; our calls go to "
                 f"{state.get('graph_host')}/{state.get('version') or '(unversioned)'} "
                 f"with credentials={state.get('credentials')}")

            client = make_client(graph)
            steps.enter("read /me/adaccounts")
            accounts = client.ad_accounts()
            act_row = next((a for a in accounts if a.get("id") == target.act), None)
            summary["ad_accounts"] = len(accounts)
            summary["act_visible"] = act_row is not None
            summary["act_status"] = (act_row or {}).get("account_status")
            emit(f"/me/adaccounts: {len(accounts)} ad accounts; {mask_id(target.act)} "
                 + ("is among them" if act_row else "is NOT among them")
                 + (f" (account_status {summary['act_status']})" if act_row else ""))

            steps.enter("read campaigns")
            campaigns = client.campaigns(target.act)
            by_status: dict[str, int] = {}
            for c in campaigns:
                key = str(c.get("effective_status") or c.get("status") or "?")
                by_status[key] = by_status.get(key, 0) + 1
            summary["campaigns"] = len(campaigns)
            summary["campaigns_by_status"] = by_status
            emit(f"/{mask_id(target.act)}/campaigns: {len(campaigns)} campaigns {by_status}")

            for preset in ("yesterday", "last_7d"):
                steps.enter(f"read insights {preset}")
                ins = summarize_insights(client.insights(target.act, preset))
                summary[f"insights_{preset}"] = ins
                emit(f"/{mask_id(target.act)}/insights {preset}: spend {ins['spend']} {ins['currency']}, "
                     f"impressions {ins['impressions']}, clicks {ins['clicks']}, leads {ins['leads']}")

            summary["calls"] = client.calls
            summary["usage_max_pct"] = (client.last_usage or {}).get("max_pct")
            summary["usage_headers_seen"] = bool(client.usage_headers_seen)
            emit(f"Graph calls: {client.calls}; usage_headers_seen: {summary['usage_headers_seen']}; "
                 f"usage_max_pct: {summary['usage_max_pct']}")
            if client.stopped is not None:
                summary["stopped"] = client.stopped.kind
                emit(f"Stopped early: {client.stopped}")
        finally:
            # Закрываем нашу вкладку на любом исходе, не затирая шаг отказа:
            # не steps.enter (он сменил бы current на close_tab перед mark).
            emit("Closing our tab")
            tab.close()
    finally:
        summary["profile_release"] = release_profile(opened)
        if summary["profile_release"] == "left_open_started_by_pilot":
            emit("Profile left open — close it in Multilogin when done.")
        else:
            emit("Profile was already running; left as it was.")
    return summary


def _at(exc: BaseException) -> str:
    step = getattr(exc, "pilot_step", "")
    if not step:
        return ""
    secs = getattr(exc, "pilot_step_s", None)
    return f" at step '{step}'" + (f" after {secs} s" if secs is not None else "")


def main(argv: list[str] | None = None) -> int:
    try:
        from scripts._console import use_utf8

        use_utf8()
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(telemetry.ScrubFilter())

    args = _parse(argv)
    try:
        from config.paths import load_env

        load_env()
    except Exception:
        pass
    target = resolve_target(args)

    def emit(line: str) -> None:
        print(scrub(line), flush=True)

    emit(f"Account {target.label}: ad account {mask_id(target.act)}, "
         f"Multilogin profile {mask_id(target.profile_id)} (slot {target.instance})")
    emit("Plan: open/attach the profile via the MLX launcher; open one own tab with Ads Manager; "
         "read /me/adaccounts, campaigns, insights (yesterday, last 7 days); close the tab; "
         "never stop the profile. Read-only, no writes to Meta.")
    if not args.run:
        emit("DRY RUN: nothing was contacted. Add --run to execute.")
        return 0

    try:
        summary = run_pilot(target, emit=emit, timeout=args.timeout,
                            graph_host=args.graph_host, credentials=args.credentials)
    except GraphStop as stop:
        emit(f"STOP ({stop.kind}){_at(stop)}: {stop}"
             + (f" Retry after ~{int(stop.retry_after_s)} s." if stop.retry_after_s else ""))
        telemetry.record({"event": "fb_api_pilot", "ok": False, "stop": stop.kind,
                          "code": stop.code, "message": str(stop),
                          "step": getattr(stop, "pilot_step", ""),
                          "step_s": getattr(stop, "pilot_step_s", None)})
        telemetry.report_stop(stop.kind, str(stop), {"account": target.label, "code": stop.code})
        return 2
    except Exception as exc:
        emit(f"FAILED{_at(exc)}: {type(exc).__name__}: {exc}")
        telemetry.record({"event": "fb_api_pilot", "ok": False, "error": type(exc).__name__,
                          "message": str(exc), "step": getattr(exc, "pilot_step", ""),
                          "step_s": getattr(exc, "pilot_step_s", None)})
        return 1
    telemetry.record({"event": "fb_api_pilot", "ok": "stopped" not in summary, **summary})
    emit("Done.")
    return 0 if "stopped" not in summary else 2


if __name__ == "__main__":
    raise SystemExit(main())
