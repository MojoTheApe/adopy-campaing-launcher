"""ACL-1782: наблюдатель за документацией Meta — без сети, с подменой загрузчика."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("meta_docs_watch", ROOT / "scripts" / "meta_docs_watch.py")
watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch)

SRC_A = {"url": "https://example.test/a", "title": "A", "category": "changelog"}
SRC_B = {"url": "https://example.test/b", "title": "B", "category": "guide"}

PAGE_V1 = """<html><body><nav>menu noise</nav><main><h1>Changelog</h1>
<p>optimization_goal LEAD_GENERATION is generally available.</p></main>
<script>var x=1</script></body></html>"""
PAGE_V2 = PAGE_V1.replace("</main>", "<p>New field OUTCOME_SALES_X is in beta rollout.</p></main>")


def make_fetch(pages):
    def fetch(url):
        v = pages[url]
        if isinstance(v, Exception):
            raise v
        return v
    return fetch


def test_extract_main_text_prefers_main_and_drops_scripts():
    t = watch.extract_main_text(PAGE_V1)
    assert "generally available" in t
    assert "menu noise" not in t and "var x" not in t


def test_first_run_baselines_then_unchanged_is_empty(tmp_path):
    pages = {SRC_A["url"]: PAGE_V1, SRC_B["url"]: PAGE_V1}
    kw = dict(snap_dir=tmp_path / "s", out_dir=tmp_path / "d", today="2026-10-11")
    d1 = watch.run_watch([SRC_A, SRC_B], make_fetch(pages), **kw)
    assert d1["baselined"] == 2 and d1["changes"] == []
    d2 = watch.run_watch([SRC_A, SRC_B], make_fetch(pages), **kw)
    assert d2["changes"] == [] and d2["errors"] == [] and d2["unchanged"] == 2
    assert "No changes." in (tmp_path / "d" / "latest.md").read_text(encoding="utf-8")


def test_changed_page_yields_one_entry_with_status_and_fields(tmp_path):
    kw = dict(snap_dir=tmp_path / "s", out_dir=tmp_path / "d", today="2026-10-11")
    watch.run_watch([SRC_A, SRC_B], make_fetch({SRC_A["url"]: PAGE_V1, SRC_B["url"]: PAGE_V1}), **kw)
    d = watch.run_watch([SRC_A, SRC_B], make_fetch({SRC_A["url"]: PAGE_V2, SRC_B["url"]: PAGE_V1}), **kw)
    assert len(d["changes"]) == 1
    c = d["changes"][0]
    assert c["source_url"] == SRC_A["url"] and c["status_label"] == "beta"
    assert "OUTCOME_SALES_X" in c["affected_fields"]
    assert "+1 / -0" in c["summary"]
    saved = json.loads((tmp_path / "d" / "latest.json").read_text(encoding="utf-8"))
    assert saved["changes"][0]["date_seen"] == "2026-10-11"


def test_fetch_error_is_reported_and_others_continue(tmp_path):
    pages = {SRC_A["url"]: OSError("boom"), SRC_B["url"]: PAGE_V1}
    d = watch.run_watch([SRC_A, SRC_B], make_fetch(pages), tmp_path / "s", tmp_path / "d")
    assert d["failed"] == 1 and d["ok"] == 1 and d["baselined"] == 1
    assert d["errors"][0]["source_url"] == SRC_A["url"] and "boom" in d["errors"][0]["error"]
    assert "Fetch errors" in (tmp_path / "d" / "latest.md").read_text(encoding="utf-8")


def test_empty_page_counts_as_error(tmp_path):
    d = watch.run_watch([SRC_A], make_fetch({SRC_A["url"]: "<html><script>x</script></html>"}),
                        tmp_path / "s", None)
    assert d["failed"] == 1


def test_dry_run_writes_nothing(tmp_path):
    d = watch.run_watch([SRC_A], make_fetch({SRC_A["url"]: PAGE_V1}), tmp_path / "s", tmp_path / "d", dry_run=True)
    assert d["baselined"] == 1
    assert not (tmp_path / "s").exists() and not (tmp_path / "d").exists()


def test_status_classification_keeps_labels_distinct():
    c = watch.classify_status
    assert c(["Feature X is now generally available"]) == "ga"
    assert c(["Meta announced a coming change"]) == "announced"
    assert c(["Field Y is deprecated"]) == "deprecated"
    assert c(["Testing with select advertisers"]) == "test"
    assert c(["nothing special"]) == "unknown"


def test_shipped_sources_json_is_official_only_and_categorised():
    items = watch.load_sources(ROOT / "config" / "meta_rules" / "sources.json")
    assert len(items) > 50
    allowed = {"versions", "changelog", "reference", "guide", "announcements", "help"}
    banned = ("fbtool", "dolphin", "nooklz", "saint", "bir.ch", "multilogin", "ls.app")
    for s in items:
        assert s["category"] in allowed
        assert not any(b in s["url"] for b in banned)
        assert s["url"].startswith("https://")
