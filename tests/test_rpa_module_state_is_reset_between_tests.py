# -*- coding: utf-8 -*-
"""ACL-2078: каждый тест начинает с чистого состояния `meta_dom`/`crop_step`.

Первый тест пачкает каждый кусок состояния уровня модуля, второй (он идёт
следом в том же файле, значит в том же воркере) проверяет, что autouse-фикстура
`_crop_and_media_refusal_state_per_test` всё вернула. Ещё два теста ловят
новое состояние, которое забыли вписать в `tests/rpa_module_state.py`, и
расхождение записанных там начальных значений с исходником модуля.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from src.fb_ads_rpa import crop_step
from src.fb_ads_rpa import meta_dom as md
from src.fb_ads_rpa import run_telemetry as rt
from src.fb_ads_rpa import page_capture
from tests import rpa_module_state as state

MODULES = {state.META_DOM: md, state.CROP_STEP: crop_step}
PROBE = "_acl2078_probe"


def _real_capture_and_stall_functions():
    functions = (page_capture.capture, md._stall_evidence)
    for function, module, name in ((functions[0], page_capture, "capture"),
                                   (functions[1], md, "_stall_evidence")):
        # A previous test may legitimately reload meta_dom after collection.
        # Accept that new real definition, never an already leaked test fake.
        assert inspect.isfunction(function)
        assert function.__module__ == module.__name__
        assert function.__name__ == name
        assert Path(inspect.getsourcefile(function)).resolve() == Path(module.__file__).resolve()
    return functions


@pytest.fixture(scope="module")
def _capture_and_stall_baseline():
    # Requested by test3 BEFORE its patches, then retained unchanged for test4.
    # Capturing during collection would become stale after a legal module reload.
    return _real_capture_and_stall_functions()


def _dirty_everything() -> None:
    for module in MODULES.values():
        for local in state.module_thread_locals(module).values():
            setattr(local, PROBE, "left by the previous test")
    md._set_upload_why(md.LIBRARY_HAS_NO_COPY)
    md._note_media_refused("media_refused_on_ad")
    md._set_create_block("tree_row_missing")
    md._LAST_UPLOAD_DIAG["file"] = "a.mp4"
    md._UNSTUCK_RELOADED.add("ad2_name")
    md._STALL_EVIDENCE_TAKEN["run"] = 3
    md._OTHER_REVIEW_ERRORS_SEEN.add("Review 1 error")
    md._reuse_state["counter_missing"] = True
    md._failed_uploads[("run", "a.mp4")] = md.LIBRARY_HAS_NO_COPY
    md._uploaded_ok.add(("run", "b.mp4"))
    md._last_destination.update(why="url_field_missing", diag={"x": 1}, instant_form={"y": 2})
    md._ADOPTED_SHELL_IDS.add("123")
    crop_step._tls.failure = "crop_square_card_missing"
    crop_step._tls.warning = "not_in_library"
    rt.set_current_run("run_left_behind", "Campaign")


def _assert_clean() -> None:
    for name, module in MODULES.items():
        for attr, local in state.module_thread_locals(module).items():
            assert not hasattr(local, PROBE), f"{name}.{attr} kept the previous test's value"
        for attr, initial in state.MUTABLE[name].items():
            assert getattr(module, attr) == initial, f"{name}.{attr} kept the previous test's value"
    assert md.last_upload_why() == ""
    assert getattr(md._media_refusal_tls, "why", None) is None
    assert md.last_create_block() == ""
    assert md.last_crop_warning() == ""
    assert crop_step._tls.failure is None
    assert crop_step._tls.warning == ""
    assert rt.current_run_id() == ""


def test_1_a_test_leaves_every_piece_of_module_state_dirty():
    _assert_clean()  # the fixture also ran before this test
    _dirty_everything()


def test_2_the_next_test_starts_clean():
    _assert_clean()


def test_3_patched_missing_thread_local_does_not_leak_other_mocks(monkeypatch, _capture_and_stall_baseline):
    # Undo must restore the earlier mocks before reset removes a newly added
    # TLS field. Otherwise delattr(outcome) aborts the rest of monkeypatch undo.
    monkeypatch.setattr(page_capture, "capture", lambda *a, **k: None)
    monkeypatch.setattr(md, "_stall_evidence", lambda *a, **k: None)
    monkeypatch.setattr(md._LAST_OPEN, "outcome", "fixture-probe", raising=False)


def test_4_the_next_test_has_the_real_capture_and_stall_functions(_capture_and_stall_baseline):
    capture, stall_evidence = _capture_and_stall_baseline
    assert page_capture.capture is capture
    assert md._stall_evidence is stall_evidence
    assert not hasattr(md._LAST_OPEN, "outcome")


def test_reset_clears_everything_on_its_own():
    _dirty_everything()
    state.reset()
    _assert_clean()


def test_every_mutable_module_container_is_registered():
    # A new module-level dict/set/list in these modules is state until shown
    # otherwise: register it in `MUTABLE` (reset between tests) or `CONSTANTS`.
    for name, module in MODULES.items():
        found = set(state.module_containers(module))
        known = set(state.MUTABLE[name]) | state.CONSTANTS[name]
        assert found - known == set(), f"{name}: register in tests/rpa_module_state.py"
        assert known - found == set(), f"{name}: stale names in tests/rpa_module_state.py"


def test_registered_initial_values_match_the_module_source():
    for name, module in MODULES.items():
        tree = ast.parse(inspect.getsource(module))
        source: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
                target, value = node.target.id, node.value
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                target, value = node.targets[0].id, node.value
            else:
                continue
            if target not in state.MUTABLE[name]:
                continue
            if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "set" and not value.args:
                source[target] = set()
            else:
                source[target] = ast.literal_eval(value)
        assert source == state.MUTABLE[name]
