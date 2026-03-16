"""Paths and file locations. Excel paths can be overridden via env vars."""
import os

# Единственный док с отчётом (фиксируем): cursor example.xlsx на рабочем столе
EXCEL_PATH_DEFAULT = r"C:\Users\EvgeniSedin\Desktop\cursor example.xlsx"
REPORT_EXCEL_DEFAULT = EXCEL_PATH_DEFAULT  # тот же файл — док с отчётом
STATS_FOLDER_DEFAULT = os.path.dirname(EXCEL_PATH_DEFAULT)
STATS_FILENAME_DEFAULT = os.path.basename(EXCEL_PATH_DEFAULT)

UPLOAD_FB_EXCEL_DEFAULT = r"C:\Users\EvgeniSedin\Desktop\Upluad FB.xlsx"


def get_excel_path() -> str:
    """Путь к доку с отчётом (cursor example.xlsx). Переменная FB_STATS_EXCEL переопределяет путь."""
    return os.environ.get("FB_STATS_EXCEL", "").strip() or EXCEL_PATH_DEFAULT


def get_stats_folder() -> str:
    """Папка, куда заливаются данные (cursor example). Для подстановки имени файла."""
    return os.environ.get("FB_STATS_FOLDER", "").strip() or STATS_FOLDER_DEFAULT


def get_upload_fb_excel_path() -> str:
    """Path to Upload FB.xlsx (Accounts, Main, offers) — for agent 5 (Jira tasks)."""
    return os.environ.get("UPLOAD_FB_EXCEL", "").strip() or UPLOAD_FB_EXCEL_DEFAULT
