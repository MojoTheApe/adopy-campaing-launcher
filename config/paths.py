"""Paths and file locations. Excel paths can be overridden via env vars."""
import os

EXCEL_PATH_DEFAULT = r"C:\Users\EvgeniSedin\Desktop\cursor example.xlsx"
UPLOAD_FB_EXCEL_DEFAULT = r"C:\Users\EvgeniSedin\Desktop\Upluad FB.xlsx"


def get_excel_path() -> str:
    return os.environ.get("FB_STATS_EXCEL", "").strip() or EXCEL_PATH_DEFAULT


def get_upload_fb_excel_path() -> str:
    """Path to Upload FB.xlsx (Accounts, Main, offers) — for agent 5 (Jira tasks)."""
    return os.environ.get("UPLOAD_FB_EXCEL", "").strip() or UPLOAD_FB_EXCEL_DEFAULT
