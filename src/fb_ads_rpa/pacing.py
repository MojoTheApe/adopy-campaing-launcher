"""Human-like delays between RPA steps."""
from __future__ import annotations

import random
import time


def pause_short() -> None:
    time.sleep(random.uniform(0.4, 0.9))


def pause_medium() -> None:
    time.sleep(random.uniform(1.0, 2.2))


def pause_long() -> None:
    time.sleep(random.uniform(2.5, 4.5))


def pause_after_navigation() -> None:
    time.sleep(random.uniform(2.0, 3.5))
