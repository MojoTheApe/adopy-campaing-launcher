#!/usr/bin/env python3
"""Entry point for Agent 8: build upload Excel from plan CSV."""
import os
import sys
_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)
from src.upload_agent.build_upload_excel import main
if __name__ == "__main__":
    main()
