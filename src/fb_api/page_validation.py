"""ACL-2105: a fixed no-creation creative probe, separate from ordinary writes.

Meta's official AdAccount.create_ad_creative accepts execution_options and
AdCreative.ExecutionOptions defines validate_only. No caller supplies that option
or arbitrary creative parameters here; both transports build the same fixed probe.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


NAME = "ACL Page access validation"
DEFAULT_LINK = "https://www.facebook.com/"


def params(act: str, page: str, link: str = "") -> dict:
    if not isinstance(act, str) or not re.fullmatch(r"act_[0-9]{5,}", act):
        raise ValueError("Page validation needs the verified ad account")
    if not isinstance(page, str) or not re.fullmatch(r"[0-9]{1,25}", page):
        raise ValueError("Page validation needs a numeric Page id")
    link = link or DEFAULT_LINK
    if (not isinstance(link, str) or len(link) > 2048
            or any(c.isspace() or ord(c) < 32 for c in link)):
        raise ValueError("Page validation needs an HTTP(S) link")
    url = urlsplit(link)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ValueError("Page validation needs an HTTP(S) link")
    return {"name": NAME, "object_story_spec": {"page_id": page, "link_data": {"link": link}},
            "execution_options": ["validate_only"]}
