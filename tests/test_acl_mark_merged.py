import copy

import pytest

from scripts import acl_mark_merged as merged


SHA = "a" * 40
REPO = "owner/repo"
ROOT = "repos/" + REPO


class FakeGitHub:
    def __init__(self):
        self.issue = {"number": 12, "title": "acl-102: Account map", "body": "- [x] Completed part",
                      "labels": [{"name": "status:review"}, {"name": "area:1"}, {"name": "track:dashboard"}],
                      "html_url": "https://github.com/owner/repo/issues/12"}
        self.pr = {"number": 7, "title": "Complete acl-102", "body": "", "merged": True,
                   "merge_commit_sha": SHA, "base": {"ref": "master"}, "head": {"ref": "feature/accounts"}}
        self.comparison = "ahead"
        self.writes = []
        self.fail_delete = False

    def request(self, path, method="GET", payload=None, paginate=False):
        if method != "GET":
            self.writes.append((method, path, payload))
        if path == ROOT + "/pulls/7":
            return copy.deepcopy(self.pr)
        if path == ROOT + f"/compare/{SHA}...master":
            return {"status": self.comparison}
        if path == ROOT + "/pulls/7/commits?per_page=100":
            assert paginate
            return [{"commit": {"message": "Implement accounts"}}]
        if path == ROOT + "/issues/12":
            return copy.deepcopy(self.issue)
        if path == ROOT + "/labels/status%3Amerged":
            return {"name": merged.MERGED}
        if method == "POST" and path == ROOT + "/issues/12/labels":
            for label in payload["labels"]:
                self.issue["labels"].append({"name": label})
            return copy.deepcopy(self.issue["labels"])
        if method == "DELETE" and path == ROOT + "/issues/12/labels/status%3Areview":
            if self.fail_delete:
                raise RuntimeError("temporary failure")
            self.issue["labels"] = [x for x in self.issue["labels"] if x["name"] != merged.REVIEW]
            return copy.deepcopy(self.issue["labels"])
        raise AssertionError((method, path, payload))


def test_dry_run_checks_merge_without_writing():
    api = FakeGitHub()
    plan = merged.prepare(api, REPO, [12], [7])
    assert plan[0]["prs"] == [7] and plan[0]["change"]
    assert not api.writes


def test_transition_preserves_other_labels_and_is_idempotent():
    api = FakeGitHub()
    merged.apply(api, REPO, merged.prepare(api, REPO, [12], [7]))
    assert {x["name"] for x in api.issue["labels"]} == {"status:merged", "area:1", "track:dashboard"}
    assert [x[0] for x in api.writes] == ["POST", "DELETE"]
    api.writes.clear()
    merged.apply(api, REPO, merged.prepare(api, REPO, [12], [7]))
    assert not api.writes


@pytest.mark.parametrize("mutation", ["open_pr", "wrong_base", "reverted_merge", "unchecked", "unrelated", "other_status"])
def test_unsafe_transition_is_rejected_before_writes(mutation):
    api = FakeGitHub()
    if mutation == "open_pr": api.pr["merged"] = False
    elif mutation == "wrong_base": api.pr["base"]["ref"] = "develop"
    elif mutation == "reverted_merge": api.comparison = "diverged"
    elif mutation == "unchecked": api.issue["body"] = "- [ ] Unfinished part"
    elif mutation == "unrelated": api.pr["title"] = "Change acl-1029"
    elif mutation == "other_status": api.issue["labels"].append({"name": "status:blocked"})
    with pytest.raises(ValueError):
        merged.prepare(api, REPO, [12], [7])
    assert not api.writes


def test_resume_after_partial_label_failure():
    api = FakeGitHub()
    plan = merged.prepare(api, REPO, [12], [7])
    api.fail_delete = True
    with pytest.raises(RuntimeError):
        merged.apply(api, REPO, plan)
    api.fail_delete = False
    api.writes.clear()
    merged.apply(api, REPO, merged.prepare(api, REPO, [12], [7]))
    assert [x[0] for x in api.writes] == ["DELETE"]
    assert merged.REVIEW not in {x["name"] for x in api.issue["labels"]}


def test_rechecks_issue_before_applying_plan():
    api = FakeGitHub()
    plan = merged.prepare(api, REPO, [12], [7])
    api.issue["body"] += "\n- [ ] Newly discovered work"
    with pytest.raises(ValueError):
        merged.apply(api, REPO, plan)
    assert not api.writes


def test_issue_url_is_valid_evidence():
    api = FakeGitHub()
    api.pr["title"] = "Complete account map"
    api.pr["body"] = "Fixes https://github.com/owner/repo/issues/12."
    assert merged.prepare(api, REPO, [12], [7])[0]["prs"] == [7]


def test_code_examples_do_not_count_as_unfinished_subtasks():
    assert not merged.unchecked_subtasks("```md\n- [ ] example\n```\n- [x] actual task")
    assert merged.unchecked_subtasks("```md\n- [ ] example\n```\n- [ ] actual task")


def test_apply_requires_explicit_reviewer_completeness_confirmation():
    with pytest.raises(SystemExit) as error:
        merged.main(["12", "--pr", "7", "--apply"])
    assert error.value.code == 2
