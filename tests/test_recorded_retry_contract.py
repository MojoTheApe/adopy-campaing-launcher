"""The recorded flow must not gain an implicit or unbounded retry path."""
from __future__ import annotations

import ast
from pathlib import Path

from src.fb_ads_rpa import failure_policy, retry_policy

_ROOT = Path(__file__).resolve().parents[1]


class _TopLevelReturns(ast.NodeVisitor):
    def __init__(self) -> None:
        self.values: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Do not treat returns from helper functions nested inside the recorded
        # flow as hard-failure results from the flow itself.
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Return(self, node: ast.Return) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            if node.value.value:
                self.values.add(node.value.value)


def _recorded_flow_hard_failures() -> set[str]:
    tree = ast.parse((_ROOT / "src/fb_ads_rpa/meta_dom.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_recorded_campaign_flow"
    )
    visitor = _TopLevelReturns()
    for statement in function.body:
        visitor.visit(statement)
    return visitor.values


def test_every_hard_failure_has_an_explicit_campaign_retry_contract() -> None:
    assert _recorded_flow_hard_failures() == set(retry_policy.CAMPAIGN_RETRY_STEPS | retry_policy.NON_RETRYABLE_FLOW_STEPS)


def test_campaign_retry_is_bounded_without_an_off_by_one() -> None:
    step = "campaign_name"
    assert retry_policy.should_retry_campaign(step, completed_attempts=1) is True
    assert retry_policy.should_retry_campaign(step, completed_attempts=2) is True
    assert retry_policy.should_retry_campaign(step, completed_attempts=3) is False


def test_late_or_unknown_failure_never_replays_the_whole_campaign() -> None:
    for step in (*retry_policy.BLOCKING_REVIEW_STEPS, "future_meta_redesign_step", ""):
        decision = retry_policy.disposition(step)
        assert decision.action == retry_policy.BLOCK_AND_REVIEW
        assert retry_policy.should_retry_campaign(step, completed_attempts=0) is False


def test_every_known_late_failure_is_classified_with_a_human_reason() -> None:
    for step in retry_policy.BLOCKING_REVIEW_STEPS:
        assert failure_policy.severity(step) == failure_policy.BLOCKING, step
        assert "unclassified" not in failure_policy.reason(step), step


def test_numbered_ads_use_the_same_explicit_policy() -> None:
    for step in (
        "ad2_create",
        "ad3_name",
        "ad4_creative_upload",
        "ad5_copy",
        "ad6_destination",
    ):
        assert failure_policy.severity(step) == failure_policy.BLOCKING
        assert "unclassified" not in failure_policy.reason(step), step
