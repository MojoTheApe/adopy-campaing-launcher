"""Paths and file locations. Excel path can be overridden via env FB_STATS_EXCEL."""
import os

EXCEL_PATH_DEFAULT = r"C:\Users\EvgeniSedin\Desktop\cursor example.xlsx"


def get_excel_path() -> str:
    return os.environ.get("FB_STATS_EXCEL", "").strip() or EXCEL_PATH_DEFAULT
