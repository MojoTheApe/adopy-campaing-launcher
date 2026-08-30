"""Make console output survive a Windows console that is not UTF-8.

Windows still opens consoles in a legacy code page (cp1251, cp866, cp1252
depending on the machine). Printing a Russian line there raises
UnicodeEncodeError and kills the script mid-run — not a garbled character, an
abort with a traceback.

That is not theoretical. The CI runner failed exactly this way on 2026-08-30:
scripts/acl_scan_secrets.py found nothing, then died printing "Секретов и
настоящих идентификаторов ... нет". The scan passed and the build went red.
Worse, install_acl.bat runs the doctor as its last step — so on a buyer's
machine the check meant to tell them what is wrong could itself crash instead
of reporting.

errors="replace" rather than "strict": a diagnostic that cannot print one
character should print a question mark and keep going, never stop.
"""
from __future__ import annotations

import sys


def use_utf8() -> None:
    """Force UTF-8 on stdout/stderr. Safe to call more than once."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            # Redirected to something without reconfigure — output is going to a
            # file or a pipe, which is not where this problem happens.
            pass
