"""Paths and file locations. Excel paths can be overridden via env vars."""
import os

# Единственный док с отчётом (фиксируем): cursor example.xlsx на рабочем столе
EXCEL_PATH_DEFAULT = r"C:\Users\EvgeniSedin\Desktop\cursor example.xlsx"
REPORT_EXCEL_DEFAULT = EXCEL_PATH_DEFAULT  # тот же файл — док с отчётом
STATS_FOLDER_DEFAULT = os.path.dirname(EXCEL_PATH_DEFAULT)
STATS_FILENAME_DEFAULT = os.path.basename(EXCEL_PATH_DEFAULT)

UPLOAD_FB_EXCEL_DEFAULT = r"C:\Users\EvgeniSedin\Desktop\Upluad FB.xlsx"
CREATIVES_ROOT_DEFAULT = r"C:\Users\EvgeniSedin\Desktop\Creatives"
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


def get_ad_account_map_path() -> str:
    """CSV mapping Excel sheet name (account) -> act_* ad account id."""
    return os.environ.get("META_AD_ACCOUNT_MAP_PATH", "").strip() or AD_ACCOUNT_MAP_DEFAULT


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
