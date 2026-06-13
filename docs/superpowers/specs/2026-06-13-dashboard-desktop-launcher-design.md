# Desktop Dashboard Launcher — Design Spec

**Date:** 2026-06-13
**Status:** Implemented

## Problem

Starting the morning dashboard required opening a terminal, navigating to the project folder, and running `start_dashboard.bat` manually. There was no quick-launch entry point accessible from the Windows desktop.

## Solution

A desktop shortcut that runs `start_dashboard.bat` directly. The batch file starts `python serve.py`, which opens the browser at http://127.0.0.1:9090/. Closing the console window stops the server.

An initial design routed the shortcut through Cursor automatic tasks (`runOn: folderOpen`), but that flow was unreliable in practice (tasks often did not run), so the shortcut now targets the batch file directly.

## Files Added

| File | Purpose |
|------|---------|
| `assets/dashboard.ico` | Multi-size icon (256/48/32/16 px) for the desktop shortcut |
| `scripts/convert_favicon_to_ico.py` | Generates `dashboard.ico` from the pirate smiley design via Pillow |
| `scripts/install_dashboard_shortcut.ps1` | Creates the desktop shortcut pointing at `start_dashboard.bat` |
| `install_dashboard_shortcut.bat` | Convenience wrapper — double-click to run the PowerShell script |
| `morning-dashboard.code-workspace` | Optional Cursor workspace (not used by the shortcut) |

## Launch Flow

```
Desktop shortcut (Morning Dashboard.lnk)
  → start_dashboard.bat
    → python serve.py
      → http://127.0.0.1:9090/  (browser opens automatically)
```

## Design Decisions

**Direct batch launch** — reliable on Windows; no dependency on Cursor automatic tasks or workspace trust prompts.

**No system tray / background process** — closing the console window kills uvicorn. Matches the requirement: "closing X just closes it."

**Pillow-drawn ICO** — neither `cairosvg` nor `svglib` is installed in this environment. The icon is generated programmatically using Pillow drawing primitives that replicate the SVG design.

## Out of Scope

- System tray / background service
- Faster initial report build time
- Changes to `serve.py` or report logic

## Setup Instructions (for new machines)

1. Clone the repo.
2. Install Python dependencies: `pip install -r requirements.txt`
3. Run `install_dashboard_shortcut.bat` (double-click).
4. Double-click **Morning Dashboard** on the desktop.
