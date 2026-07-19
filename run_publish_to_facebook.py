#!/usr/bin/env python3
"""Entry point for Agent 10: publish upload_plan.xlsx into Facebook Ads Manager via Playwright."""
import os
import sys
_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)
from src.fb_publish_agent.publish_from_excel import main
if __name__ == "__main__":
    main()
