#!/usr/bin/env python3
"""Compatibility entry point; shared implementation is in .workflow/tools."""
from pathlib import Path
import runpy
import sys
_tools = Path(__file__).resolve().parents[1] / '.workflow/tools'
sys.path.insert(0, str(_tools))
globals().update(runpy.run_path(str(_tools / 'tracker_process_instructions.py'), run_name=__name__))
