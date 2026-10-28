"""Synthetic transport worlds keep independent receipts without moving form/config data."""
import hashlib
from pathlib import Path

_selected = None


def select(root):
    global _selected
    _selected = root


def root_for(config_root):
    if _selected is None:
        return config_root
    # Explicit simulated machine changes still select a different receipt universe.
    key = hashlib.sha256(str(config_root).encode()).hexdigest()[:12]
    return Path(_selected) / key
