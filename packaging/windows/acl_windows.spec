# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir bundle for the frozen Windows pilot baseline."""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


PROJECT_ROOT = Path(SPECPATH).parents[1]
MANIFEST_PATH = Path(
    os.environ.get(
        "ACL_BUILD_MANIFEST",
        str(PROJECT_ROOT / "packaging" / "windows" / ".build" / "acl-build-manifest.json"),
    )
)

if not MANIFEST_PATH.is_file():
    raise SystemExit(f"Build manifest not found: {MANIFEST_PATH}")


playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")

datas = [
    (str(PROJECT_ROOT / ".env.example"), "."),
    (str(PROJECT_ROOT / "config" / "ad_account_map.example.csv"), "config"),
    (str(PROJECT_ROOT / "config" / "ads_manager_selectors.yaml"), "config"),
    (str(PROJECT_ROOT / "config" / "analysis_reading_rules.json"), "config"),
    (str(PROJECT_ROOT / "config" / "benchmarks.json"), "config"),
    (str(PROJECT_ROOT / "config" / "content_plan_week.example.json"), "config"),
    (str(PROJECT_ROOT / "config" / "knowledge_base.json"), "config"),
    (str(PROJECT_ROOT / "config" / "rpa_forbidden_zones.yaml"), "config"),
    (str(PROJECT_ROOT / "config" / "meta_rules"), "config/meta_rules"),
    (str(PROJECT_ROOT / "config" / "rpa_intents"), "config/rpa_intents"),
    (str(PROJECT_ROOT / "config" / "rpa_recipes"), "config/rpa_recipes"),
    (str(PROJECT_ROOT / "config" / "rpa_variations"), "config/rpa_variations"),
    (str(PROJECT_ROOT / "knowledge_base"), "knowledge_base"),
    (str(PROJECT_ROOT / "src" / "web" / "static"), "src/web/static"),
    (str(PROJECT_ROOT / "src" / "web" / "templates"), "src/web/templates"),
    (str(MANIFEST_PATH), "."),
    (str(MANIFEST_PATH.with_name("acl-update-channel.json")), "."),
] + playwright_datas

# Uvicorn chooses its event loop, HTTP implementation and lifespan handler at
# runtime. PyInstaller cannot infer those string-based imports statically.
hiddenimports = collect_submodules("uvicorn") + playwright_hiddenimports + [
    "diagnostics",
    "machine_config",
    "setup_wizard",
    "updater",
    "tkinter",
    "scripts.acl_runtime_manager",
    "scripts.acl_smoke",
    "src.analysis_agent.morning_report",
    "src.fb_ads_rpa.ads_manager_flow",
    "src.fb_ads_rpa.retry_policy",
    "src.web.app",
]


a = Analysis(
    [str(PROJECT_ROOT / "packaging" / "windows" / "launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=playwright_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ACL",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "assets" / "dashboard.ico"),
)

console_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ACLConsole",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "assets" / "dashboard.ico"),
)

coll = COLLECT(
    exe,
    console_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ACL",
)
