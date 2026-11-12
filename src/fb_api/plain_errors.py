"""ACL-2392: one table of short plain-English failure texts for the buyer.

The panel shows `Failure.line()` ("short - next step"); the raw Meta / exception text,
codes and ids live only in `Failure.details` (collapsed "Technical details").
Pure module, no I/O. `graph.py` does not import it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.fb_api import error_catalog as _catalog
from src.fb_api import mlx_session as _mlx
from src.fb_api import telemetry as _tm
from src.fb_api.graph import (
    REJECTION_SUBCODES, CallBudget, Checkpoint, GraphError, GraphStop, LoggedOut, PageUnresponsive,
    PolicyBlocked, RateLimited, WriteUnknown, WritesBlocked,
)

DETAILS_HEADING = "Technical details (what Meta said, codes, run id)"
#: Where the full text lives (the header "Recent runs" popup); the builder only links to it.
WHERE = "see details in Recent runs"
DETAILS_LINK = "Details in Recent runs"
COPY_LABEL = "Copy details"
NO_DETAILS = "Nothing more to show."
#: Marker of the one-line readback summary (publish_api tells it from other blocking failures).
READBACK_MARK = "Meta saved different values than planned"
#: Keys whose line names the stage ("... (while creating ad set "X")"): the owner asked for what AND where.
STAGED = ("unknown", "graph_write_unconfirmed", "graph_read_timeout")


@dataclass(frozen=True)
class Plain:
    short: str
    next_step: str = ""
    severity: str = "blocking"   # blocking | hold | info
    retry: str = "after_fix"     # after_fix | later | never | now


TABLE: dict[str, Plain] = {
    "logged_out": Plain("Account logged out", "log in and republish"),
    "checkpoint": Plain("Facebook asks to confirm this account", "open the profile, finish the check and republish"),
    "targeting_merged": Plain("Targeting options were merged by Meta", "replace the interests and republish"),
    "targeting_term": Plain("Meta did not accept a targeting term{names}", "replace it and republish"),
    "page_unavailable": Plain("The Facebook Page is not available for this ad account",
                              "choose another Page and republish"),
    "pixel_unavailable": Plain("The pixel is not on this ad account", "choose another pixel and republish"),
    "form_unavailable": Plain("The Instant Form is not available for this Page",
                              "refresh the forms, choose another one and republish"),
    "account_blocked": Plain("This ad account cannot run ads right now (disabled, restricted or at its spending limit)",
                             "check it in Ads Manager"),
    "profile_locked": Plain("The Multilogin profile is busy or locked",
                            "wait for the other action or close the window, then republish", "hold", "later"),
    "profile_not_started": Plain("The Multilogin profile did not start", "open it in Multilogin, then republish",
                                 "blocking", "later"),
    "mlx_token_rejected": Plain("", "", "blocking", "after_fix"),  # text comes from mlx_session
    "tab_unresponsive": Plain("The Ads Manager tab stopped answering",
                              "wait {wait}, then republish", "hold", "later"),
    "proxy_error": Plain("The profile's proxy or secure connection failed",
                         "check the proxy of this profile in Multilogin and republish"),
    "graph_read_timeout": Plain("Meta did not answer in time{stage}", "nothing was changed, republish in a few minutes",
                                "blocking", "later"),
    "graph_write_unconfirmed": Plain("Meta did not confirm this step{stage}",
                                     "check the campaign in Ads Manager, then republish (it will not duplicate)",
                                     "blocking", "later"),
    "rate_limited": Plain("Meta is limiting requests", "wait {wait} and republish", "hold", "later"),
    "policy_block": Plain("Meta temporarily blocked this action", "wait before trying again, do not retry now",
                          "blocking", "never"),
    # ACL-2400: the same words as warm.MEMORY_HOLD_MESSAGE, so the two can never show different texts.
    "memory_hold": Plain("Not enough free memory", "close a profile and retry", "blocking", "later"),
    "account_hold": Plain("Another action is still running", "wait for it to finish and republish", "hold",
                          "later"),
    "creative_file": Plain("A creative file cannot be used{ad}", "replace the file and republish"),
    "unknown": Plain("Meta refused this step{stage}", WHERE),
}

#: Not table entries: our own text passed through (`meta_rule`, `plan_text`) or the catalog (`catalog`).
PASS_THROUGH = ("meta_rule", "plan_text", "catalog")


@dataclass
class Failure:
    key: str
    short: str
    next_step: str = ""
    left: list[str] = field(default_factory=list)
    severity: str = "blocking"
    retry: str = "after_fix"
    details: dict = field(default_factory=dict)
    sep: str = " - "
    stage: str = ""

    def line(self) -> str:
        return f"{self.short}{self.sep}{self.next_step}" if self.next_step else self.short

    def text(self) -> str:
        """The line plus the "Left in Meta" sentence: what the panel prints."""
        return ". ".join([self.line(), *self.left]).strip() if self.left else self.line()

    def as_dict(self) -> dict:
        return {"key": self.key, "short": self.short, "next": self.next_step, "line": self.line(),
                "left": list(self.left), "stage": self.stage, "severity": self.severity, "retry": self.retry,
                "details": dict(self.details)}


def _make(key: str, ctx: dict | None = None, **over: Any) -> Failure:
    ctx = ctx or {}
    plain = TABLE[key]
    names = ctx.get("names") or []
    ad = ctx.get("ad")
    fill = {"names": (": " + ", ".join(f'"{n}"' for n in names[:3])) if names else "",
            "ad": f": ad {ad}" if ad else "", "stage": f" ({ctx['stage']})" if ctx.get("stage") else "",
            "wait": f"about {ctx['minutes']} min" if ctx.get("minutes") else "a few minutes"}
    short, nxt = plain.short, plain.next_step
    for k, v in fill.items():
        short = short.replace("{" + k + "}", str(v))
        nxt = nxt.replace("{" + k + "}", str(v))
    if key == "mlx_token_rejected":
        short = str(ctx.get("text") or _mlx.SESSION_LOST_MESSAGE)
    return Failure(key=key, short=short, next_step=nxt, severity=plain.severity, retry=plain.retry,
                   stage=str(ctx.get("stage") or ""), **over)


# ───────────────────────── details (scrubbed) ─────────────────────────

def build_details(exc: BaseException | None = None, *, raw: str = "", kind: str = "",
                  extra: dict | None = None) -> dict:
    """What Meta said and where: the only place the raw text may live. Always scrubbed."""
    out: dict[str, Any] = {}
    text = raw or (str(exc) if exc is not None else "")
    if text:
        out["raw"] = text
    for name in ("code", "subcode", "status"):
        value = getattr(exc, name, None)
        if value is not None:
            out["http_status" if name == "status" else name] = value
    det = getattr(exc, "details", None)
    if isinstance(det, dict):
        for k, v in det.items():
            out.setdefault(k, v)
    if exc is not None:
        out["type"] = det.get("type") if isinstance(det, dict) and det.get("type") else type(exc).__name__
    if kind:
        out["stop_kind"] = kind
    out.update(extra or {})
    return _tm.scrub_value(out)


def offending_note(planned: list[str], user_msg: str) -> tuple[list[str], str]:
    """Planned interest names that occur in Meta's message; none -> the "did not name" note."""
    hit = [n for n in planned if n and n.lower() in (user_msg or "").lower()]
    if hit:
        return hit, "Meta named these options: " + ", ".join(hit)
    return [], "Meta did not name the options."


# ───────────────────────── classification ─────────────────────────

#: Only (sub)codes backed by a RECORDED Meta payload (tests/fixtures/acl2392/targeting_merged.json) are mapped here;
#: every other code goes to the error catalog or the generic line, never to a guessed class.
_CODE_KEYS = {
    1870226: "targeting_merged",
}
_POLICY_CODES = {368}
#: Codes WE put on ApiPublishError / WarmError / GraphStop.kind (not Meta codes).
_API_CODE_KEYS = {
    "page_unresponsive": "tab_unresponsive", "profile_closed": "profile_locked", "profile_locked": "profile_locked",
    "gate_wait": "account_hold", "below_reserve": "memory_hold", "memory_low": "memory_hold",
    "limit": "account_hold", "cdp": "profile_not_started", "launch": "profile_not_started",
    "cdp_timeout": "graph_read_timeout",
    # GraphStop.kind carried by ApiPublishError (publish_api._halt)
    "rate_limited": "rate_limited", "call_budget": "rate_limited", "policy_block": "policy_block",
    "page_failed": "unknown", "logged_out": "logged_out", "checkpoint": "checkpoint",
}

_SNIFFERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ERR_(PROXY|TUNNEL|CERT|SSL)|PROXY_AUTH|proxy", re.I), "proxy_error"),
    (re.compile(r"logged out|no session token|token is no longer valid", re.I), "logged_out"),
    (re.compile(r"checkpoint", re.I), "checkpoint"),
    (re.compile(r"stopped answering|did not finish loading|page_unresponsive", re.I), "tab_unresponsive"),
    (re.compile(r"ad account is not active|account_status", re.I), "account_blocked"),
    (re.compile(r"is not among the pages|belongs to the profile|validation account differs|"
                r"require a Facebook Page|Pages could not be fully loaded", re.I), "page_unavailable"),
    (re.compile(r"pixel.*(not on this ad account|datasets)", re.I), "pixel_unavailable"),
    (re.compile(r"Instant Form|form template|lead_gen_form_id", re.I), "form_unavailable"),
    (re.compile(r"creative file|Stories / Reels file", re.I), "creative_file"),
    (re.compile(r"was not found by Meta targeting search", re.I), "targeting_term"),
    (re.compile(r"earlier write has an unknown result|write result is unknown|readback", re.I),
     "graph_write_unconfirmed"),
    (re.compile(r"timed out|timeout", re.I), "graph_read_timeout"),
    (re.compile(r"profile .*(locked|already used)|Multilogin profile .*busy", re.I), "profile_locked"),
    (re.compile(r"could not be opened with automation|profile .*did not start", re.I), "profile_not_started"),
]
#: Own-text exceptions (PlanError, ApiPublishError, ...) carry user data (interest, ad set, Page names), so they are
#: matched ONLY against these phrases that exist solely in our own messages, after every quoted fragment was
#: removed. The generic words (proxy, checkpoint, timeout, Instant Form, readback) are for opaque exceptions only.
_OWN_SNIFFERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ERR_(PROXY|TUNNEL|CERT|SSL)\w*|PROXY_AUTH"), "proxy_error"),
    (re.compile(r"no session token|token is no longer valid"), "logged_out"),
    (re.compile(r"Ads Manager tab stopped answering|did not finish loading"), "tab_unresponsive"),
    (re.compile(r"ad account is not active"), "account_blocked"),
    (re.compile(r"is not among the pages|belongs to the profile|validation account differs|"
                r"require a Facebook Page|Pages could not be fully loaded"), "page_unavailable"),
    (re.compile(r"pixel from the draft .*is not on this ad account"), "pixel_unavailable"),
    (re.compile(r"is not a form of this Page"), "form_unavailable"),
    (re.compile(r"creative file is empty|Stories / Reels file"), "creative_file"),
    (re.compile(r"was not found by Meta targeting search"), "targeting_term"),
    (re.compile(r"earlier write has an unknown result|write result is unknown"), "graph_write_unconfirmed"),
    (re.compile(r"the profile could not be opened with automation|profile did not start"), "profile_not_started"),
]
_QUOTED_ANY = re.compile(r"'[^']*'|\"[^\"]*\"|\[[^\]]*\]")
#: Exceptions whose message is already our own English text (no Meta wording): passed through scrubbed.
_OWN_TEXT = ("PlanError", "ApiPublishError", "CreationStopped", "ProfileLeaseError", "LoadRequeue", "RulesViolation")
_QUOTED = re.compile(r"'([^']{1,60})'")


def _by_text(text: str, ctx: dict, *, own: bool = False) -> str:
    """Last-resort free-text match. `own`: our message with user data inside: quoted parts removed, phrases only."""
    if own:
        text = _QUOTED_ANY.sub(" ", text)
    code = _mlx.classify(text, "")
    if code:
        ctx.setdefault("text", _mlx.message_for(code))
        return "mlx_token_rejected" if ctx["text"] else ""
    for pat, key in (_OWN_SNIFFERS if own else _SNIFFERS):
        if pat.search(text):
            return key
    return ""


def classify(exc: BaseException | str | None, *, kind: str = "", ctx: dict | None = None) -> Failure:
    """Our exception (or legacy text) -> the buyer-facing `Failure`. Order is fixed, see the spec."""
    ctx = dict(ctx or {})
    raw = exc if isinstance(exc, str) else (str(exc) if exc is not None else "")
    explicit = getattr(exc, "failure", None)
    if isinstance(explicit, Failure):
        return explicit
    stage = str(ctx.get("stage") or getattr(exc, "stage", "") or "")
    if stage:
        ctx["stage"] = stage
    details = build_details(None if isinstance(exc, str) else exc, raw=raw, kind=kind,
                            extra={**(ctx.pop("details", None) or {}), **({"stage": stage} if stage else {})})
    key = getattr(exc, "key", "") or ""
    code, sub = getattr(exc, "code", None), getattr(exc, "subcode", None)
    if not key and isinstance(exc, Checkpoint):
        key = "checkpoint"
    elif not key and isinstance(exc, LoggedOut):
        key = "logged_out"
    elif not key and isinstance(exc, PageUnresponsive):
        key = "tab_unresponsive"
    elif not key and isinstance(exc, (RateLimited, CallBudget)):
        key = "rate_limited"
        wait = getattr(exc, "retry_after_s", None)
        if wait:
            ctx.setdefault("minutes", int(wait // 60) + 1)
        elif isinstance(exc, CallBudget):
            ctx.setdefault("minutes", 5)  # our own per-run call ceiling, no Meta cooldown to quote
    elif not key and isinstance(exc, PolicyBlocked):
        key = "policy_block"
    elif not key and isinstance(exc, (WriteUnknown, WritesBlocked)) and sub not in REJECTION_SUBCODES:
        key = "graph_write_unconfirmed"
    if not key and isinstance(code, str):
        key = _API_CODE_KEYS.get(code, "")
    if not key and sub in _CODE_KEYS:
        key = _CODE_KEYS[sub]
    if not key and isinstance(code, int):
        key = "policy_block" if code in _POLICY_CODES else _CODE_KEYS.get(code, "")
    if not key and kind in ("page_unresponsive", "logged_out", "checkpoint"):
        key = {"page_unresponsive": "tab_unresponsive"}.get(kind, kind)
    names = [c.__name__ for c in type(exc).__mro__] if exc is not None and not isinstance(exc, str) else []
    own = any(n in _OWN_TEXT for n in names)
    if not key and (isinstance(exc, (GraphError, GraphStop)) or isinstance(code, int)):
        # The catalog (Meta's own code) wins over any free-text match: error_user_title is Meta/user data.
        x = _catalog.explain(code if isinstance(code, int) else None, sub, raw)
        if x.known:
            return Failure("catalog", x.message_en, x.action_en, sep=" ", details=details, stage=stage)
    if not key and raw and "TargetingUnresolved" not in names:
        key = _by_text(raw, ctx, own=own)
    if key in ("tab_unresponsive", "rate_limited") and not ctx.get("minutes"):
        m = re.search(r"about (\d+) min", raw)
        if m:
            ctx["minutes"] = m.group(1)
    if key == "targeting_term" and not ctx.get("names"):
        ctx["names"] = _QUOTED.findall(raw)[:3]
    if key == "creative_file" and not ctx.get("ad"):
        m = re.search(r"\bad (\d+)\b", raw, re.I)
        ctx["ad"] = m.group(1) if m else ""
    if key == "mlx_token_rejected":
        ctx.setdefault("text", _mlx.message_for(_mlx.classify(raw, "")) or _mlx.SESSION_LOST_MESSAGE)
    if key in TABLE:
        return _make(key, ctx, details=details)
    if key == "meta_rule":
        return Failure("meta_rule", _tm.scrub(raw), details=details, stage=stage)
    if isinstance(exc, (GraphError, GraphStop)) or isinstance(code, int):
        x = _catalog.explain(code if isinstance(code, int) else None, sub, raw)
        if x.known:
            return Failure("catalog", x.message_en, x.action_en, sep=" ", details=details, stage=stage)
        return _make("unknown", ctx, details=details)
    if exc is not None and raw and own:
        return Failure("plan_text", _tm.scrub(raw).strip().rstrip("."), details=details, stage=stage)
    if exc is not None and not isinstance(exc, str) and not isinstance(exc, (GraphError, GraphStop)):
        # Not a Meta answer at all (a bug or a local failure): do not blame Meta.
        return Failure("internal", "Publishing stopped unexpectedly", WHERE, severity="blocking",
                       retry="later", details=details, stage=stage)
    return _make("unknown", ctx, details=details)


_AD = re.compile(r"^(ad \d+)")


def stage_of(what: str) -> str:
    """The `what` of a creator write ("ad set 'X'", "ad 2: image upload") in plain words."""
    w = (what or "").strip()
    m = _AD.match(w)
    if w == "campaign":
        return "while creating the campaign"
    if w.startswith("ad set"):
        return f'while creating ad set "{w[6:].strip().strip(chr(39) + chr(34))}"'
    if "upload" in w:
        return f"while uploading {m.group(1)} creative" if m else "while uploading a creative file"
    if w.startswith("form"):
        return "while creating the Instant Form"
    if m and "creative" in w:
        return f"while creating {m.group(1)} creative"
    if m:
        return f"while creating {m.group(1)}"
    return "while writing to Meta"


def left_in_meta(*, campaign: bool, adsets: int = 0, ads: int = 0, empty_note: bool = False,
                 not_created: list[int] | None = None, name: str = "") -> list[str]:
    """What a stop left in Meta (English). Nothing created -> []."""
    if not campaign:
        return []
    if empty_note and not adsets and not ads:
        return ["Left in Meta: an empty paused campaign. Publishing again reuses it, you do not need to delete it."]
    parts = ["a paused campaign"]
    if adsets:
        parts.append(f"{adsets} ad set{'s' if adsets != 1 else ''}")
    if ads:
        parts.append(f"{ads} ad{'s' if ads != 1 else ''}")
    # ACL-2578: the real naming is the ACL-2250 copy counter ("X copy 1", then the next free number), never "_2".
    again = (f'otherwise the new one is published as a new copy, "{name} copy 1" (or the next free number)'
             if name else "otherwise a new one is published as a new copy")
    text = ("Left in Meta: " + ", ".join(parts)
            + f". Publishing again reuses it if its settings match; {again}.")
    if not_created:
        text += " Not created: " + ", ".join(f"ad {n}" for n in not_created) + "."
    return [text]


_FIELD_NAMES = {"daily_budget": "daily budget", "lifetime_budget": "lifetime budget", "targeting": "targeting",
                "optimization_goal": "optimization goal", "bid_strategy": "bid strategy",
                "start_time": "start time", "end_time": "end time", "status": "status", "name": "name"}


def readback_line(mismatches: list[dict]) -> str:
    """One panel line for readback mismatches; the full lines stay in the details."""
    names: list[str] = []
    for m in mismatches:
        raw = str(m.get("field") or "").strip()
        label = _FIELD_NAMES.get(raw, raw.replace("_", " ")) or "settings"
        if label not in names:
            names.append(label)
    return f"{READBACK_MARK}: {', '.join(names) or 'settings'} - {WHERE}"


__all__ = ["Failure", "Plain", "TABLE", "classify", "build_details", "left_in_meta", "readback_line",
           "offending_note", "REJECTION_SUBCODES", "READBACK_MARK", "WHERE", "DETAILS_LINK", "STAGED", "stage_of", "DETAILS_HEADING", "COPY_LABEL",
           "NO_DETAILS"]
