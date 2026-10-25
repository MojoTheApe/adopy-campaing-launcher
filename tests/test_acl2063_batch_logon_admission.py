"""Permission boundaries for the real two-user Windows acceptance fixture."""
import pytest

from tests.acl2063_windows_batch_logon import BATCH, DENY_BATCH, WindowsBatchPolicy, admit_owned_batch

OWN = "S-1-5-21-111-222-333-1001"
OTHER = "S-1-5-21-111-222-333-1002"
USERS = "S-1-5-32-545"


class Policy:
    def __init__(self, allow=(), deny=(), *, refuse_after=False, before_error=None):
        self.allow, self.deny = set(allow), set(deny)
        self.refuse_after, self.before_error = refuse_after, before_error
        self.changes, self.calls = [], []
        self.identity = {"sid": OWN, "groups": [(USERS, 7), ("S-1-5-4", 7)]}

    def assignments(self, right):
        return set(self.allow if right == BATCH else self.deny)

    def logon(self, kind, missing_ok=False):
        self.calls.append((kind, missing_ok))
        if kind == 4:
            if self.before_error and missing_ok:
                raise OSError(self.before_error, "native authentication refusal")
            permitted = {OWN, USERS, "S-1-5-3"} & self.allow
            denied = {OWN, USERS, "S-1-5-3"} & self.deny
            if not permitted or denied or (self.refuse_after and self.changes):
                if missing_ok:
                    return None
                raise OSError(1385, "native batch admission still refused")
            return {"sid": OWN, "groups": [(USERS, 7), ("S-1-5-3", 7)]}
        assert kind == 2
        return self.identity

    def change(self, right, *, add):
        assert right == BATCH
        self.changes.append((OWN, right, add))
        if add:
            self.allow.add(OWN)
        else:
            self.allow.remove(OWN)


def test_missing_allow_is_added_only_to_created_sid_and_restored():
    policy = Policy(allow=[OTHER])
    with admit_owned_batch(policy) as receipt:
        assert receipt["batch_before"] is False and receipt["batch_after"] is True
        assert receipt["added_own_allow"] is True and receipt["sid"] == OWN
        assert policy.allow == {OWN, OTHER}
    assert policy.allow == {OTHER} and policy.deny == set()
    assert policy.changes == [(OWN, BATCH, True), (OWN, BATCH, False)]
    assert policy.calls == [(4, True), (2, False), (4, False)]


@pytest.mark.parametrize("denied_sid", [OWN, USERS, "S-1-5-3"])
def test_direct_group_or_batch_deny_is_never_bypassed(denied_sid):
    policy = Policy(deny=[denied_sid])
    with pytest.raises(AssertionError, match="explicitly denied"):
        with admit_owned_batch(policy):
            pytest.fail("denied identity was admitted")
    assert policy.changes == [] and policy.deny == {denied_sid}


def test_deny_only_group_is_checked_but_cannot_supply_an_allow():
    policy = Policy(deny=[OTHER])
    policy.identity["groups"].append((OTHER, 16))
    with pytest.raises(AssertionError, match="explicitly denied"):
        with admit_owned_batch(policy):
            pytest.fail("deny-only group was ignored")
    assert policy.changes == []


def test_existing_group_allow_needs_no_policy_write():
    policy = Policy(allow=[USERS])
    with admit_owned_batch(policy) as receipt:
        assert receipt["batch_before"] and not receipt["added_own_allow"]
    assert policy.allow == {USERS} and policy.changes == []


def test_native_refusal_after_grant_stays_failed_and_restores():
    policy = Policy(refuse_after=True)
    with pytest.raises(OSError, match="still refused"):
        with admit_owned_batch(policy):
            pytest.fail("native refusal was replaced by a simulated success")
    assert policy.allow == set() and policy.changes == [(OWN, BATCH, True), (OWN, BATCH, False)]


def test_test_body_failure_also_restores_the_exact_own_allow():
    policy = Policy()
    with pytest.raises(RuntimeError, match="actual test failure"):
        with admit_owned_batch(policy):
            raise RuntimeError("actual test failure")
    assert policy.allow == set() and policy.changes[-1] == (OWN, BATCH, False)


def test_unknown_authentication_error_never_triggers_a_grant():
    policy = Policy(before_error=1326)
    with pytest.raises(OSError, match="authentication refusal"):
        with admit_owned_batch(policy):
            pytest.fail("unknown authentication failure was ignored")
    assert policy.changes == []


def test_existing_allow_with_native_refusal_is_not_repaired_by_more_rights():
    policy = Policy(allow=[USERS])
    original = policy.logon

    def refused(kind, missing_ok=False):
        return None if kind == 4 and missing_ok else original(kind, missing_ok)

    policy.logon = refused
    with pytest.raises(AssertionError, match="despite an existing allow"):
        with admit_owned_batch(policy):
            pytest.fail("unexplained policy refusal was bypassed")
    assert policy.changes == []


def test_interactive_group_deny_is_not_confused_with_batch_group_deny():
    policy = Policy(deny=["S-1-5-4"])
    with admit_owned_batch(policy) as receipt:
        assert receipt["batch_after"]
    assert policy.deny == {"S-1-5-4"} and policy.allow == set()


@pytest.mark.parametrize("user,created", [("acl2063" + "a" * 12 + "1", []), ("existing-person", ["existing-person"])])
def test_unowned_or_non_synthetic_user_refused_before_native_api(user, created):
    with pytest.raises(AssertionError):
        WindowsBatchPolicy(user, "synthetic-only", created)


def test_native_write_interface_cannot_change_a_deny():
    policy = object.__new__(WindowsBatchPolicy)
    with pytest.raises(AssertionError):
        policy.change(DENY_BATCH, add=False)


def test_post_grant_identity_change_fails_and_restores():
    policy = Policy()
    original = policy.logon

    def changed(kind, missing_ok=False):
        result = original(kind, missing_ok)
        if kind == 4 and not missing_ok:
            result["sid"] = OTHER
        return result

    policy.logon = changed
    with pytest.raises(AssertionError):
        with admit_owned_batch(policy):
            pytest.fail("a different post-grant identity was accepted")
    assert policy.allow == set() and policy.changes[-1] == (OWN, BATCH, False)


def test_deny_only_group_does_not_count_as_an_effective_allow():
    policy = Policy(allow=[OTHER])
    policy.identity["groups"].append((OTHER, 16))
    with admit_owned_batch(policy) as receipt:
        assert receipt["added_own_allow"]
    assert policy.allow == {OTHER}


def test_failed_restoration_cannot_be_reported_as_success():
    policy = Policy()
    original = policy.change

    def not_restored(right, *, add):
        if add:
            original(right, add=add)

    policy.change = not_restored
    with pytest.raises(AssertionError, match="not restored"):
        with admit_owned_batch(policy):
            pass
