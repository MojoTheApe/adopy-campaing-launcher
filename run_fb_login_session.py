#!/usr/bin/env python3
"""Entry point: save a logged-in Facebook browser session for Agent 10 to reuse."""
import os
import sys
_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)
from src.fb_publish_agent.login_session import main
if __name__ == "__main__":
    main()
