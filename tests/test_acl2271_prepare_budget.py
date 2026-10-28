"""ACL-2271: the connection preparation budget is capped at 150 s, and never above the caller's own timeout."""
import pytest

from src.fb_api import warm
from tests.test_acl1812_warm_sessions import ACT, PID, Rig


def test_cap_is_150_seconds():
    assert warm.PREPARE_BUDGET_CAP_S == 150.0


@pytest.mark.parametrize("timeout,expected", [(600, 150.0), (150, 150.0), (100, 100.0), (45, 45.0)])
def test_preparation_budget_is_the_timeout_up_to_the_cap(tmp_path, timeout, expected):
    rig = Rig(tmp_path)
    seen = []

    def owned(sess, act, mode, make_graph):
        seen.append(warm._PREPARE_DEADLINE.get() - rig.reg._mono())
        return object(), {}
    rig.reg._prepare_owned = owned
    sess = rig.warm_up()
    rig.reg._prepare(sess, ACT, "read", rig.make, timeout)
    assert seen == [pytest.approx(expected)]
