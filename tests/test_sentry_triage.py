# -*- coding: utf-8 -*-
"""Сводка новых отказов из Sentry — `scripts/sentry_triage.py` (ACL-1325).

Sentry здесь — записанные ответы API (форма ответов organizations/…/issues/,
issues/…/events/latest/, projects/…/attachments/), подставленные вместо
HTTP. Проверяется: группировка по «шаг + причина», сверка с таблицей
известных задач, разбор вложения-раздела, курсор только при `--apply`,
разделы на диск — только в названную `--attachments-dir` и не больше
KEEP_SAVED_ISSUES папок (ревью PR #328, F3), только чтение и мягкий пропуск
без токена или прав.
"""
from __future__ import annotations

import io
import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import sentry_triage as st  # noqa: E402
from src.fb_ads_rpa import page_capture as pc  # noqa: E402

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
BASE = "https://de.sentry.io"
ORG = "adopy-7t"

FIXTURE = (ROOT / "tests" / "fixtures" / "ads_manager" / "audience_suggest_layout_2026_09_26.html").read_text(
    encoding="utf-8")
SUGGEST_FX = re.search(r"<!-- fragment: suggest_fx \| [^>]*-->\n(.*?)(?=\n<!-- fragment: |\Z)", FIXTURE, re.S).group(1)


def _section_bytes() -> bytes:
    """Вложение в том виде, в каком его кладёт робот."""
    pc.forget_sections()

    class _Page:
        # Раздел снимается ограниченным вызовом (`wait_for_function`).
        def wait_for_function(self, _expr, *, arg=None, timeout=None):
            parts = [{"name": "audience", "html": SUGGEST_FX}]
            return type("H", (), {"json_value": lambda _s: {"v": parts}, "dispose": lambda _s: None})()

    assert pc.remember_section(_Page(), step="pick_further_limit_radio", run_id="r1", campaign="CO_x")
    att = pc.section_attachment("further_limit_reach_radio", "r1",
                                related=["pick_further_limit_radio"])
    pc.forget_sections()
    return att["bytes"]


def _issue(issue_id, title, *, first="2026-09-26T10:00:00Z", last="2026-09-26T11:00:00Z", count=3, users=1):
    return {
        "id": issue_id, "shortId": f"ACL-{issue_id}", "title": title,
        "permalink": f"https://adopy-7t.sentry.io/issues/{issue_id}/",
        "project": {"slug": "acl"}, "count": str(count), "userCount": users,
        "firstSeen": first, "lastSeen": last, "status": "unresolved",
    }


def _event(event_id, step, reason, *, kind="blocking", machine="buyer-pc-1", account="FacebookDoc6540",
           snapshot=None):
    ev = {
        "eventID": event_id,
        "tags": [
            {"key": "step", "value": step}, {"key": "reason", "value": reason},
            {"key": "failure_kind", "value": kind}, {"key": "machine", "value": machine},
            {"key": "account", "value": account}, {"key": "release", "value": "acl-release-2026-09-26"},
        ],
        "contexts": {
            "run": {"run_id": "abc", "campaign": "CO_bnc_All_Video_FX_35_26.09", "attempt": 1},
            "failure": {"why": "not_selected"},
            "diag": {"url": "https://adsmanager.facebook.com/x?act=<hidden>"},
        },
    }
    if snapshot:
        ev["tags"].append({"key": "page_snapshot", "value": "section"})
        ev["contexts"]["page_snapshot"] = snapshot
    return ev


class FakeSentry:
    """Записанные ответы. Любой адрес вне списка — 404, как у настоящего API."""

    def __init__(self, *, new=(), regressed=(), events=None, attachments=None, blobs=None,
                 status=200, page_size=None):
        self.new = list(new)
        self.regressed = list(regressed)
        self.events = events or {}
        self.attachments = attachments or {}
        self.blobs = blobs or {}
        self.status = status
        self.page_size = page_size
        self.calls: list[str] = []
        self.headers_seen: list[dict] = []

    def __call__(self, url, headers):
        self.calls.append(url)
        self.headers_seen.append(headers)
        if self.status != 200:
            return self.status, b'{"detail": "no"}', {}
        parts = urllib.parse.urlsplit(url)
        assert parts.scheme + "://" + parts.netloc == BASE
        q = urllib.parse.parse_qs(parts.query)
        path = parts.path
        if path == f"/api/0/organizations/{ORG}/issues/":
            query = q["query"][0]
            rows = self.new if query.startswith("is:unresolved firstSeen:>") else self.regressed
            start = int(q.get("cursor", ["0"])[0])
            size = self.page_size or len(rows) or 1
            page = rows[start:start + size]
            headers_out = {}
            if start + size < len(rows):
                nxt = f"{BASE}{path}?query={urllib.parse.quote(query)}&cursor={start + size}"
                headers_out["Link"] = f'<{nxt}>; rel="next"; results="true"; cursor="{start + size}"'
            return 200, json.dumps(page).encode(), headers_out
        m = re.fullmatch(rf"/api/0/organizations/{ORG}/issues/(\w+)/events/latest/", path)
        if m and m.group(1) in self.events:
            return 200, json.dumps(self.events[m.group(1)]).encode(), {}
        m = re.fullmatch(rf"/api/0/projects/{ORG}/acl/events/(\w+)/attachments/", path)
        if m:
            return 200, json.dumps(self.attachments.get(m.group(1), [])).encode(), {}
        m = re.fullmatch(rf"/api/0/projects/{ORG}/acl/events/(\w+)/attachments/(\w+)/", path)
        if m and q.get("download") == ["1"] and m.group(2) in self.blobs:
            return 200, self.blobs[m.group(2)], {}
        return 404, b"{}", {}


def _run(tmp_path, fake, *args, env_token="tok-123", monkeypatch=None):
    if monkeypatch is not None:
        if env_token is None:
            monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
        else:
            monkeypatch.setenv("SENTRY_AUTH_TOKEN", env_token)
        for name in ("SENTRY_ORG", "SENTRY_BASE_URL", "SENTRY_PROJECT"):
            monkeypatch.delenv(name, raising=False)
    out = io.StringIO()
    code = st.main(["--state", str(tmp_path / "state.json"), "--format", "json", *args],
                   fetch=fake, now=NOW, out=out)
    assert code == 0
    return json.loads(out.getvalue())


def _world():
    snapshot = {"parts": ["audience"], "captured_at_step": "pick_further_limit_radio", "same_step": True}
    return FakeSentry(
        new=[_issue("101", "further_limit_reach_radio: not-selected", count=4, users=2),
             _issue("102", "ad2_copy: copy-fields-not-revealed"),
             _issue("103", "ad3_copy: copy-fields-not-revealed", count=2)],
        regressed=[_issue("104", "audience_location: location-rows-not-read", first="2026-09-20T10:00:00Z")],
        events={
            "101": _event("e101", "further_limit_reach_radio", "not-selected", kind="campaign_stop",
                          snapshot=snapshot),
            "102": _event("e102", "ad2_copy", "copy-fields-not-revealed", machine="buyer-pc-2"),
            "103": _event("e103", "ad3_copy", "copy-fields-not-revealed", machine="buyer-pc-3"),
            "104": _event("e104", "audience_location", "location-rows-not-read"),
        },
        attachments={"e101": [{"id": "9001", "name": "section_further_limit_reach_radio.html",
                               "mimetype": "text/html", "size": 1000}]},
        blobs={"9001": _section_bytes()},
    )


def test_the_digest_groups_by_step_and_reason_and_flags_new_kinds(tmp_path, monkeypatch):
    digest = _run(tmp_path, _world(), monkeypatch=monkeypatch)
    assert digest["issues"] == 4
    keys = [g["key"] for g in digest["groups"]]
    # ad2_copy и ad3_copy — одна поломка; новые виды — раньше известных.
    assert keys.index("ad_copy:copy-fields-not-revealed") < keys.index("further_limit_reach_radio:not-selected")
    copy = next(g for g in digest["groups"] if g["key"] == "ad_copy:copy-fields-not-revealed")
    assert copy["new_kind"] is True and copy["known_task"] is None
    assert [i["id"] for i in copy["issues"]] == ["102", "103"]
    assert copy["machines"] == ["buyer-pc-2", "buyer-pc-3"]
    assert copy["events"] == 5
    known = next(g for g in digest["groups"] if g["step"] == "further_limit_reach_radio")
    assert known["known_task"]["task"] == "ACL-1324"
    regressed = next(g for g in digest["groups"] if g["step"] == "audience_location")
    assert regressed["issues"][0]["change"] == "regressed"


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_the_section_snapshot_is_downloaded_and_outlined(tmp_path, monkeypatch):
    digest = _run(tmp_path, _world(), monkeypatch=monkeypatch)
    known = next(g for g in digest["groups"] if g["step"] == "further_limit_reach_radio")
    att = known["issues"][0]["attachment"]
    assert att["name"] == "section_further_limit_reach_radio.html"
    # Скелет построен из байтов в памяти: без `--attachments-dir` на диске
    # ничего (ревью PR #328, F3).
    assert "saved" not in att
    assert _files(tmp_path) == []
    assert "captured at step: pick_further_limit_radio" in att["header"]
    # По скелету раздела видно новую раскладку — без машины владельца.
    assert "Suggest an audience" in att["headings"]
    assert "button: Further limit the reach of your ads" in att["controls"]
    assert "button: Include these custom audiences (open)" in att["controls"]
    assert known["issues"][0]["latest_event"]["page_snapshot"]["parts"] == ["audience"]


def test_markdown_says_what_broke_where_and_what_is_known(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    out = io.StringIO()
    st.main(["--state", str(tmp_path / "s.json"), "--attachments-dir", str(tmp_path / "a"), "--format", "md"],
            fetch=_world(), now=NOW, out=out)
    md = out.getvalue()
    assert "### ad_copy: copy-fields-not-revealed — NEW KIND" in md
    assert "known: ACL-1324" in md
    assert "buyer-pc-2" in md and "page section:" in md


def test_it_only_reads(tmp_path, monkeypatch):
    fake = _world()
    _run(tmp_path, fake, "--apply", monkeypatch=monkeypatch)
    assert fake.calls and all(urllib.parse.urlsplit(u).path.startswith("/api/0/") for u in fake.calls)
    # Никаких адресов изменения: ни resolve, ни assign, ни комментариев.
    assert not any(re.search(r"/(comments|notes|assign|resolve|bulk)", u) for u in fake.calls)
    assert all(h["Authorization"] == "Bearer tok-123" for h in fake.headers_seen)
    assert not [name for name in dir(st.SentryClient) if re.match(r"(put|post|delete|update|resolve)", name)]


def test_the_snapshot_is_saved_only_into_a_named_folder(tmp_path, monkeypatch):
    save = tmp_path / "att"
    digest = _run(tmp_path, _world(), "--attachments-dir", str(save), monkeypatch=monkeypatch)
    att = next(g for g in digest["groups"] if g["step"] == "further_limit_reach_radio")["issues"][0]["attachment"]
    assert Path(att["saved"]).is_file() and Path(att["saved"]).parent.parent == save
    assert "Suggest an audience" in att["headings"]
    assert _files(tmp_path) == [str(Path("att") / "sentry_issue_101" / "section_further_limit_reach_radio.html")]


def test_apply_writes_only_the_state_file(tmp_path, monkeypatch):
    _run(tmp_path, _world(), "--apply", monkeypatch=monkeypatch)
    assert _files(tmp_path) == ["state.json"]


def test_the_saved_folder_keeps_only_the_latest_issues(tmp_path, monkeypatch):
    import os
    import time

    save = tmp_path / "att"
    old = time.time() - 3600
    for i in range(st.KEEP_SAVED_ISSUES + 5):
        folder = save / f"sentry_issue_{900 + i}"
        folder.mkdir(parents=True)
        (folder / "section_x.html").write_text("<div>x</div>", encoding="utf-8")
        os.utime(folder, (old + i, old + i))
    # Чужое в названной папке не трогается.
    (save / "notes").mkdir()
    (save / "notes" / "section_mine.html").write_text("mine", encoding="utf-8")
    (save / "sentry_issue_keep").mkdir()
    (save / "sentry_issue_keep" / "other.txt").write_text("mine", encoding="utf-8")
    os.utime(save / "sentry_issue_keep", (old - 10, old - 10))

    _run(tmp_path, _world(), "--attachments-dir", str(save), monkeypatch=monkeypatch)
    issues = sorted(d.name for d in save.iterdir() if d.name.startswith("sentry_issue_")
                    and any(d.glob("section_*.html")))
    assert len(issues) == st.KEEP_SAVED_ISSUES
    assert "sentry_issue_101" in issues
    assert "sentry_issue_900" not in issues
    assert (save / "notes" / "section_mine.html").is_file()
    assert (save / "sentry_issue_keep" / "other.txt").is_file()


def test_the_cursor_is_recorded_only_with_apply(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    _run(tmp_path, _world(), monkeypatch=monkeypatch)
    assert not state.exists()
    _run(tmp_path, _world(), "--apply", monkeypatch=monkeypatch)
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["cursor"] == "2026-09-26T12:00:00Z"
    assert saved["seen"]["101"] == "2026-09-26T11:00:00Z"


def test_the_next_run_starts_from_the_cursor_and_skips_what_was_seen(tmp_path, monkeypatch):
    (tmp_path / "state.json").write_text(json.dumps({
        "cursor": "2026-09-26T09:30:00Z", "seen": {"101": "2026-09-26T11:00:00Z"}}), encoding="utf-8")
    fake = _world()
    digest = _run(tmp_path, fake, monkeypatch=monkeypatch)
    first = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.calls[0]).query)["query"][0]
    assert first == "is:unresolved firstSeen:>2026-09-26T09:30:00"
    assert "101" not in [i["id"] for g in digest["groups"] for i in g["issues"]]


def test_without_a_cursor_it_looks_back_a_day(tmp_path, monkeypatch):
    fake = FakeSentry()
    digest = _run(tmp_path, fake, monkeypatch=monkeypatch)
    assert digest["since"] == "2026-09-25T12:00:00Z" and digest["groups"] == []


def test_pages_are_followed(tmp_path, monkeypatch):
    fake = FakeSentry(new=[_issue(str(200 + i), f"s{i}: other") for i in range(5)], page_size=2,
                      events={str(200 + i): _event(f"e{i}", f"step_{chr(97 + i)}", "other") for i in range(5)})
    digest = _run(tmp_path, fake, "--no-attachments", monkeypatch=monkeypatch)
    assert digest["issues"] == 5


def test_no_token_means_skipped_not_failed(tmp_path, monkeypatch):
    fake = _world()
    digest = _run(tmp_path, fake, "--apply", env_token=None, monkeypatch=monkeypatch)
    assert "SENTRY_AUTH_TOKEN" in digest["skipped"]
    assert fake.calls == []
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("status", [401, 403, 500])
def test_no_access_means_skipped_not_failed(tmp_path, monkeypatch, status):
    digest = _run(tmp_path, FakeSentry(status=status), "--apply", monkeypatch=monkeypatch)
    assert f"HTTP {status}" in digest["skipped"]
    assert not (tmp_path / "state.json").exists()


def test_a_network_error_means_skipped(tmp_path, monkeypatch):
    def offline(url, headers):
        raise OSError("no route")

    digest = _run(tmp_path, offline, monkeypatch=monkeypatch)
    assert digest["skipped"].startswith("network")


def test_the_known_failures_map_parses_and_matches():
    rows = st.load_map()
    assert rows, "config/sentry_triage_map.json пуст или не читается"
    assert st.known_task("further_limit_reach_radio", "timeout", rows)["task"] == "ACL-1324"
    assert st.known_task("profile_network", "proxy-network", rows)["task"] == "ACL-1320"
    assert st.known_task("ad_copy", "copy-fields-not-revealed", rows) is None


def test_a_resaved_old_issue_survives_pruning(tmp_path, monkeypatch):
    """Вернувшийся старый тикет перезаписывается в свою старую папку: перезапись
    файла на NTFS не двигает время папки, и без явного `utime` чистка тут же
    удаляла бы только что записанный раздел (повторное ревью PR #328, N1)."""
    import os
    import time

    save = tmp_path / "att"
    now = time.time()
    for i in range(st.KEEP_SAVED_ISSUES):
        f = save / f"sentry_issue_{900 + i}"
        f.mkdir(parents=True)
        (f / "section_x.html").write_text("x", encoding="utf-8")
        os.utime(f, (now - 100 + i, now - 100 + i))
    old = save / "sentry_issue_101"
    old.mkdir()
    (old / "section_further_limit_reach_radio.html").write_text("old", encoding="utf-8")
    os.utime(old, (now - 7200, now - 7200))
    digest = _run(tmp_path, _world(), "--attachments-dir", str(save), monkeypatch=monkeypatch)
    att = next(g for g in digest["groups"] if g["step"] == "further_limit_reach_radio")["issues"][0]["attachment"]
    assert Path(att["saved"]).is_file(), "the digest points at a file pruning just deleted"
    assert len([d for d in save.iterdir() if d.name.startswith("sentry_issue_")]) == st.KEEP_SAVED_ISSUES
