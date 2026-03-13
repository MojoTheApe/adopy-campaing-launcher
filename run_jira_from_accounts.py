#!/usr/bin/env python3
"""Entry point for Agent 5: create Jira tasks from Upluad FB.xlsx Accounts."""
import os
import sys

_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)

from src.jira_agent.jira_from_accounts import main

if __name__ == "__main__":
    main()
