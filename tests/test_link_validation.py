from src.campaign_bridge.link_validation import classify_link, link_error_message


def test_classify_link_empty() -> None:
    assert classify_link("") == "empty"
    assert classify_link(None) == "empty"
    assert link_error_message("empty") == "Open Jira Task — provision asset/link in Jira first"


def test_classify_link_jira_atlassian() -> None:
    assert classify_link("https://company.atlassian.net/browse/MED-123") == "jira"
    assert link_error_message("jira") == "Open Keitaro — replace Jira URL with tracking link"


def test_classify_link_tracking() -> None:
    assert classify_link("https://track.example.com/click?x=1") == "tracking"
    assert link_error_message("tracking") is None
