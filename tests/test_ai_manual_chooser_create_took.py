# -*- coding: utf-8 -*-
"""ACL-1726: выбор «Meta AI-guided setup / Manual campaign setup» после Create.

Прогон 4db2039f760d (9403A, 08.10): Create открывал этот выбор, а
`_create_took` отвечал «nothing» — робот жал Create снова и 103 с ждал окно
цели. Проверка на подставной странице, без браузера.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fb_ads_rpa import meta_dom as md  # noqa: E402


class _Loc:
    def __init__(self, visible):
        self.visible = visible
        self.first = self

    def count(self):
        return 1 if self.visible else 0

    def is_visible(self, *a, **k):
        return self.visible

    def wait_for(self, state="visible", timeout=0):
        if not self.visible:
            raise TimeoutError("not visible")


class _Page:
    url = "https://example.test/adsmanager/manage/campaigns"

    def __init__(self, texts=(), leads=False):
        self.texts = texts
        self.leads = leads

    def get_by_role(self, role, name=None, **kw):
        return _Loc(self.leads if role == "radio" else False)

    def get_by_text(self, pattern, **kw):
        probe = getattr(pattern, "pattern", pattern)
        return _Loc(any(t.lower() in probe.lower() or
                        pattern.search(t) for t in self.texts)
                    if hasattr(pattern, "search") else False)


CHOOSER = ("Meta AI-guided setup", "Manual campaign setup")


def test_chooser_detected():
    assert md._ai_manual_chooser_visible(_Page(CHOOSER))


def test_chooser_not_detected_without_manual_or_with_leads_radio():
    assert not md._ai_manual_chooser_visible(_Page(("Meta AI-guided setup",)))
    assert not md._ai_manual_chooser_visible(_Page(()))
    assert not md._ai_manual_chooser_visible(_Page(CHOOSER, leads=True))


def test_create_took_counts_chooser(monkeypatch):
    monkeypatch.setattr(md, "_tab_gone", lambda p: False)
    monkeypatch.setattr(md, "_tab_frozen", lambda p: False)
    monkeypatch.setattr(md, "_open_left", lambda: 5.0)
    assert md._create_took(_Page(CHOOSER), "https://example.test/x") == "ai_manual_chooser"


def test_wait_objective_returns_at_once_on_chooser(monkeypatch):
    rows = []
    monkeypatch.setattr(md._telemetry, "record",
                        lambda step, ok=None, **kw: rows.append((step, kw.get("note"))))
    monkeypatch.setattr(md, "_tab_gone", lambda p: False)
    monkeypatch.setattr(md, "_StallWatch",
                        lambda *a, **k: SimpleNamespace(check=lambda **kw: False))
    page = _Page(CHOOSER)
    page.get_by_role = lambda role, name=None, **kw: SimpleNamespace(first=_Loc(False))
    out = {}
    assert md._wait_objective_dialog(page, first_s=5, out=out) is True
    assert out["outcome"] == "ai_manual_chooser"
    assert ("objective_wait", "ai_manual_chooser") in rows
