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
        # ACL-1335: сохранённый вход заменяет токен автоматизации.
        from src.meta_campaign_agent.mlx_credentials import has_saved_session

        token_a = "signin" if has_saved_session("A") else ""
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

    Default ON since 2026-09-15. Disable with FB_RPA_REUSE_MEDIA=0.

    It was briefly off before, after a broken first version cost a whole run
    on 2026-07-29 (clicks landed on the filename caption instead of the tile,
    the grid stayed at "0 selected", ads ended up with no media), then turned
    back on once the pick learned to search by filename and verify the grid's
    own "N selected" counter.

    ВКЛЮЧЕНО СНОВА 15.09, и вот на каком основании — потому что выключали не
    от лени, и вернуть флаг, не сняв причину, было бы прямым вредом.

    Причина отключения была одна и конкретная: у поиска по медиатеке было
    ДВА исхода на ТРИ случая. «Не нашли» и «выбрали, но не смогли довести»
    возвращали одинаковое False, вызывающий в обоих случаях уходил грузить
    файл — и во втором клал его поверх уже выбранного. Теперь исхода три
    (`REUSE_ATTACHED` / `REUSE_ABSENT` / `REUSE_DIRTY`), отступление снимает
    за собой выбор и подтверждает это счётчиком сетки, а если снять не
    удалось — шаг честно отказывает вместо тихой загрузки поверх. Худший
    случай теперь «объявление без креатива, отказ видно», а не «объявление с
    шестью креативами, которое никто не заметит».

    Довод «оно ничего не экономит» тоже перестал быть верным. Он считался на
    креативах, где загрузка стоила 30-60 секунд. 15.09 живой креатив весил
    **247 МБ**, и за круг из шести кампаний по три объявления один и тот же
    файл уезжает в Meta восемнадцать раз — около 4,4 ГБ.

    История отключения ниже оставлена целиком: она объясняет, что именно
    проверять, если флаг снова начнёт вредить.

    Off again (24.08), and this time the claim that its worst case is "the old
    behaviour plus a couple of seconds" is simply not true. When the pick
    clicks tiles and then fails to advance the wizard, it returns False with
    those selections STILL APPLIED, and the upload path then adds its own on
    top: ads came out carrying 3-6 media instead of the one creative they
    were built from (measured runs 5bff134ea745 and 770bd7e5fb7b, "3 of 10
    selected" / "6 of 10 selected"). Meta spins the surplus into extra ad
    variations, so the published ad is not the one the operator assembled on
    the dashboard.

    It also buys nothing: every ad in every run of 24.08 shows an "upload is
    complete" toast, i.e. the reuse path falls through to a real upload
    anyway. Repeated test runs leave many identical tiles per filename, which
    is exactly the case it handles worst. A fresh upload auto-selects exactly
    the file it just sent — the behaviour we want — at a cost of ~30-60s per
    ad, which these runs were already paying.
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
