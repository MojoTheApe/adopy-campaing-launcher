"""RPA publish configuration from environment."""
from __future__ import annotations

import os
from pathlib import Path


def ensure_playwright_browsers_path() -> None:
    """Use a real Playwright browser install, not Cursor's empty sandbox cache."""
    default_root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    default_has_chromium = any(default_root.glob("chromium-*/chrome-win64/chrome.exe"))

    custom = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if custom:
        custom_path = Path(custom)
        sandboxish = "cursor-sandbox-cache" in custom.lower()
        has_chromium = any(custom_path.glob("chromium-*/chrome-win64/chrome.exe"))
        if sandboxish or not has_chromium:
            custom = ""

    if custom:
        return

    if default_has_chromium:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(default_root)
    else:
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


def get_publish_backend() -> str:
    """rpa (default) = Playwright Ads Manager; api = legacy Marketing API."""
    return (os.environ.get("FB_PUBLISH_BACKEND") or "rpa").strip().lower()


def get_mlx_attach_only() -> bool:
    """When True (default), RPA uses MLX automation API on Publish only (no DirectCDP/Playwright fallback)."""
    val = (os.environ.get("FB_MLX_ATTACH_ONLY") or "1").strip().lower()
    return val not in ("0", "false", "no", "off")


def get_browser_provider() -> str:
    """multilogin_attach (default), multilogin (API), multilogin_direct, playwright_profile, dolphin."""
    explicit = (os.environ.get("FB_BROWSER_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    if get_mlx_attach_only():
        return "multilogin_attach"
    token_a = (os.environ.get("MULTILOGIN_AUTOMATION_TOKEN_A") or "").strip()
    if not token_a:
        return "multilogin_direct"
    return "multilogin"


def get_dolphin_api_base() -> str:
    return (os.environ.get("DOLPHIN_API_BASE") or "http://127.0.0.1:3001").strip().rstrip("/")


def get_dolphin_api_token() -> str:
    return (os.environ.get("DOLPHIN_API_TOKEN") or "").strip()


def use_rpa_publish() -> bool:
    return get_publish_backend() != "api"


def get_use_recipe_player() -> bool:
    """When True, RPA runs the compiled golden recipe (config/rpa_recipes/campaign_golden.yaml)
    via RecipePlayer, falling back to the hardcoded AdsManagerFlow steps for any gap.

    Default ON. Disable with FB_RPA_RECIPE_PLAYER=0.
    """
    val = (os.environ.get("FB_RPA_RECIPE_PLAYER") or "1").strip().lower()
    return val in ("1", "true", "yes", "on")


def get_reuse_library_media() -> bool:
    """When True, the ad-creative step first tries to attach an existing copy
    of the file from the account's media library instead of re-uploading it.

    Default ON. Disable with FB_RPA_REUSE_MEDIA=0.

    It was briefly off by default after a broken first version cost a whole
    run on 2026-07-29 (clicks landed on the filename caption instead of the
    tile, the grid stayed at "0 selected", ads ended up with no media). Two
    things changed since: the pick now searches the library by filename and
    verifies the grid's own "N selected" counter actually moved, and — if
    it can't select anything — it bails before touching the Media step, so
    the caller falls straight through to the proven upload path. Worst case
    is therefore the old behaviour plus a couple of seconds.
    """
    val = (os.environ.get("FB_RPA_REUSE_MEDIA") or "1").strip().lower()
    return val in ("1", "true", "yes", "on")


def get_use_llm_agent() -> bool:
    """When True, RPA delegates to LLMAgentPlayer (src/fb_ads_rpa/llm_agent.py) —
    a live Claude-driven loop that reads the accessibility tree and decides each
    click itself, instead of replaying the static recorded recipe. Requires
    ANTHROPIC_API_KEY. Takes priority over FB_RPA_RECIPE_PLAYER when set.

    Default OFF (opt-in until a live key is configured and it's been smoke-tested).
    Enable with FB_RPA_ENGINE=llm_agent.
    """
    val = (os.environ.get("FB_RPA_ENGINE") or "").strip().lower()
    return val == "llm_agent"


def get_use_recorded_flow() -> bool:
    """When True, RPA runs meta_dom.run_recorded_campaign_flow — a direct,
    linear replay of a verified human Playwright codegen recording (2026-07-21)
    for Create -> Website, instead of the label/breadcrumb/coordinate cascade
    in recipe_player.py. Takes priority over both the LLM agent and the recipe
    player when set. Enable with FB_RPA_ENGINE=recorded_flow.
    """
    val = (os.environ.get("FB_RPA_ENGINE") or "").strip().lower()
    return val == "recorded_flow"


def get_recipe_stop_after() -> str:
    """Optional step id to stop RecipePlayer after that step (or its delegated section).

    Empty = run full recipe to handoff.
    Default gate (serve.py): s12_conversion_location —
    Ad set name + Conversion location → Single → Website (exact).
    Older gate: s06_campaign (name + budget only).
    """
    return (os.environ.get("FB_RPA_RECIPE_STOP_AFTER") or "").strip()
