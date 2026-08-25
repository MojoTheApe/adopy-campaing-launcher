"""Paths and file locations. Excel paths can be overridden via env vars."""
import os

# Единственный док с отчётом (фиксируем): cursor example.xlsx на рабочем столе.
#
# Defaults are built from the CURRENT user's Desktop, not a literal
# C:\Users\EvgeniSedin\... — on this machine they resolve to exactly the same
# paths as before, but on a media buyer's machine running ACL they point at
# that buyer's own Desktop instead of at a stranger's, which cannot exist. A
# missing file is then an honest "your file isn't there" rather than a
# confusing error naming someone else's account.
_DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")

EXCEL_PATH_DEFAULT = os.path.join(_DESKTOP, "cursor example.xlsx")
REPORT_EXCEL_DEFAULT = EXCEL_PATH_DEFAULT  # тот же файл — док с отчётом
STATS_FOLDER_DEFAULT = os.path.dirname(EXCEL_PATH_DEFAULT)
STATS_FILENAME_DEFAULT = os.path.basename(EXCEL_PATH_DEFAULT)

UPLOAD_FB_EXCEL_DEFAULT = os.path.join(_DESKTOP, "Upluad FB.xlsx")
CREATIVES_ROOT_DEFAULT = os.path.join(_DESKTOP, "Creatives")
AD_ACCOUNT_MAP_DEFAULT = os.path.join(os.path.dirname(__file__), "ad_account_map.csv")


def get_excel_path() -> str:
    """Путь к доку с отчётом (cursor example.xlsx). Переменная FB_STATS_EXCEL переопределяет путь."""
    return os.environ.get("FB_STATS_EXCEL", "").strip() or EXCEL_PATH_DEFAULT


def get_stats_folder() -> str:
    """Папка, куда заливаются данные (cursor example). Для подстановки имени файла."""
    return os.environ.get("FB_STATS_FOLDER", "").strip() or STATS_FOLDER_DEFAULT


def get_upload_fb_excel_path() -> str:
    """Path to Upload FB.xlsx (Accounts, Main, offers) — for agent 5 (Jira tasks)."""
    return os.environ.get("UPLOAD_FB_EXCEL", "").strip() or UPLOAD_FB_EXCEL_DEFAULT


def get_creatives_root() -> str:
    """Root folder for creative files (.mp4 / .jpeg); searched recursively by filename."""
    return os.environ.get("FB_CREATIVES_ROOT", "").strip() or CREATIVES_ROOT_DEFAULT


def get_local_config_dir() -> str:
    """Per-machine config that must NEVER travel with the code.

    ACL is moving from "one repo on Evgeni's machine" to a launcher installed
    on every media buyer's computer, updated from the shared work repo. That
    only works if the repo carries code and the machine carries its own
    accounts: ten buyers have ten different account maps, and a file living in
    git would both overwrite theirs on update and push theirs to a repository
    outside developers can read.

    Override with ACL_CONFIG_DIR. Default is the OS's own per-user data
    location, deliberately OUTSIDE the checkout so that deleting or re-cloning
    the repo cannot take a buyer's configuration with it.
    """
    explicit = os.environ.get("ACL_CONFIG_DIR", "").strip()
    if explicit:
        return explicit
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if base:  # Windows — every buyer machine today
        return os.path.join(base, "ACL")
    return os.path.join(os.path.expanduser("~"), ".config", "acl")


def get_ad_account_map_path() -> str:
    """CSV mapping Excel sheet name (account) -> act_* ad account id.

    Resolution order, first hit wins:
      1. META_AD_ACCOUNT_MAP_PATH — explicit override, unchanged behaviour.
      2. <local config dir>/ad_account_map.csv — where a buyer's own map lives.
      3. config/ad_account_map.csv in the checkout — how it has always worked;
         kept so existing installs keep running untouched after this change.

    When none exists, returns the local-config path rather than the repo one:
    the file is meant to be created there, so callers' "not found" errors point
    a new install at the right place instead of back into git.
    """
    explicit = os.environ.get("META_AD_ACCOUNT_MAP_PATH", "").strip()
    if explicit:
        return explicit
    local = os.path.join(get_local_config_dir(), "ad_account_map.csv")
    if os.path.exists(local):
        return local
    if os.path.exists(AD_ACCOUNT_MAP_DEFAULT):
        return AD_ACCOUNT_MAP_DEFAULT
    return local


# ── Agent 10: Facebook Ads Manager publish (Playwright) ──────────────────────

FB_ADS_AUTH_STATE_DEFAULT = ".auth/fb_state.json"
FB_ADS_MANAGER_URL_DEFAULT = "https://adsmanager.facebook.com/adsmanager/manage/campaigns"
FB_PUBLISH_OUTPUT_DIR_DEFAULT = "output/fb_publish_runs"


def get_fb_ads_auth_state_path() -> str:
    """Путь к сохранённой сессии браузера (storage_state), созданной run_fb_login_session.py.
    Переменная FB_ADS_AUTH_STATE переопределяет путь. Файл в git не попадает (см. .gitignore)."""
    return os.environ.get("FB_ADS_AUTH_STATE", "").strip() or FB_ADS_AUTH_STATE_DEFAULT


def get_fb_ads_manager_url() -> str:
    """URL Ads Manager, с которого стартует флоу создания кампании."""
    return os.environ.get("FB_ADS_MANAGER_URL", "").strip() or FB_ADS_MANAGER_URL_DEFAULT


def get_fb_publish_output_dir() -> str:
    """Папка для скриншотов, trace-файлов и отчётов по прогонам агента 10."""
    return os.environ.get("FB_PUBLISH_OUTPUT_DIR", "").strip() or FB_PUBLISH_OUTPUT_DIR_DEFAULT
