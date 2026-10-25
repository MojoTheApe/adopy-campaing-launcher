"""ACL-1782: наблюдатель за документацией Meta — без сети, с подменой загрузчика."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_weak_sources_flagged_in_digest(tmp_path):
    weak = dict(SRC_A, weak=True)
    watch.run_watch([weak], make_fetch({weak["url"]: PAGE_V1}), tmp_path / "s", None)
    d = watch.run_watch([weak], make_fetch({weak["url"]: PAGE_V2}), tmp_path / "s", None)
    assert d["weak_sources"] == [weak["url"]] and d["changes"][0]["weak"] is True
    md = watch.render_markdown(d)
    assert "WEAK" in md


def test_shipped_help_sources_are_weak():
    items = watch.load_sources(ROOT / "config" / "meta_rules" / "sources.json")
    helps = [s for s in items if "facebook.com/business/help" in s["url"]]
    assert helps and all(s.get("weak") for s in helps)


def _changed_digest(tmp_path):
    watch.run_watch([SRC_A], make_fetch({SRC_A["url"]: PAGE_V1}), tmp_path / "s", None)
    return watch.run_watch([SRC_A], make_fetch({SRC_A["url"]: PAGE_V2}), tmp_path / "s", None)


def test_obsidian_appends_dated_section(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    d = _changed_digest(tmp_path)
    assert watch.append_obsidian(d, vault) is True
    assert watch.append_obsidian(d, vault) is True
    text = (vault / watch.OBSIDIAN_NOTE).read_text(encoding="utf-8")
    assert text.count("# ACL — Meta изменения") == 1
    assert text.count(f"## {d['generated']}") == 2 and SRC_A["url"] in text


def test_obsidian_noop_when_empty_or_no_vault(tmp_path, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    watch.run_watch([SRC_A], make_fetch({SRC_A["url"]: PAGE_V1}), tmp_path / "s", None)
    empty = watch.run_watch([SRC_A], make_fetch({SRC_A["url"]: PAGE_V1}), tmp_path / "s", None)
    assert watch.append_obsidian(empty, vault) is False
    assert not (vault / watch.OBSIDIAN_NOTE).exists()
    assert watch.append_obsidian(_changed_digest(tmp_path), tmp_path / "missing") is False
    assert "ACL_OBSIDIAN_DIR" in capsys.readouterr().err


def _prepare_cli(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("ACL_OBSIDIAN_DIR", raising=False)
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps([SRC_A]), encoding="utf-8")
    snapshots = tmp_path / "snapshots"
    watch.run_watch([SRC_A], make_fetch({SRC_A["url"]: PAGE_V1}), snapshots, None)
    monkeypatch.setattr(watch, "http_fetch", make_fetch({SRC_A["url"]: PAGE_V2}))
    return ["--sources", str(sources), "--snapshots", str(snapshots),
            "--out", str(tmp_path / "digests"), "--delay", "0"]


@pytest.mark.parametrize("mode", ["default", "explicit", "missing-override"])
def test_cli_missing_vault_warns(tmp_path, monkeypatch, capfd, mode):
    args = _prepare_cli(tmp_path, monkeypatch)
    if mode == "explicit":
        args += ["--obsidian"]
    elif mode == "missing-override":
        default = tmp_path / "default"
        default.mkdir()
        monkeypatch.setenv("ACL_OBSIDIAN_DIR", str(default))
        args += ["--vault", str(tmp_path / "missing")]
    assert watch.main(args) == 0
    captured = capfd.readouterr()
    assert "Obsidian vault not found; set ACL_OBSIDIAN_DIR" in captured.err
    assert not list(tmp_path.rglob(watch.OBSIDIAN_NOTE))


def test_cli_default_appends_to_real_vault(tmp_path, monkeypatch, capfd):
    args = _prepare_cli(tmp_path, monkeypatch)
    vault = tmp_path / "home" / "Desktop" / "Obsidian" / "дешборд"
    vault.mkdir(parents=True)
    assert watch.main(args) == 0
    assert SRC_A["url"] in (vault / watch.OBSIDIAN_NOTE).read_text(encoding="utf-8")
    assert not capfd.readouterr().err


def test_cli_explicit_vault_overrides_default(tmp_path, monkeypatch, capfd):
    args = _prepare_cli(tmp_path, monkeypatch)
    default = tmp_path / "default"
    default.mkdir()
    monkeypatch.setenv("ACL_OBSIDIAN_DIR", str(default))
    vault = tmp_path / "chosen"
    vault.mkdir()
    assert watch.main(args + ["--obsidian", "--vault", str(vault)]) == 0
    assert SRC_A["url"] in (vault / watch.OBSIDIAN_NOTE).read_text(encoding="utf-8")
    assert not (default / watch.OBSIDIAN_NOTE).exists()
    assert not capfd.readouterr().err


def test_cli_disabled_obsidian_skips_lookup_and_write(tmp_path, monkeypatch, capfd):
    args = _prepare_cli(tmp_path, monkeypatch)

    def unexpected_lookup():
        pytest.fail("disabled Obsidian output must not look up a vault")

    monkeypatch.setattr(watch, "default_vault_dir", unexpected_lookup)
    assert watch.main(args + ["--no-obsidian"]) == 0
    assert not capfd.readouterr().err
    assert not list(tmp_path.rglob(watch.OBSIDIAN_NOTE))


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch, capfd):
    args = _prepare_cli(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    vault.mkdir()
    snapshot = watch._snap_path(tmp_path / "snapshots", SRC_A["url"])
    before = snapshot.read_bytes()
    assert watch.main(args + ["--dry-run", "--obsidian", "--vault", str(vault)]) == 0
    assert snapshot.read_bytes() == before
    assert not (tmp_path / "digests").exists()
    assert not (vault / watch.OBSIDIAN_NOTE).exists()
    assert "Meta docs digest" in capfd.readouterr().out


def test_direct_cli_resolves_config_from_another_cwd(tmp_path):
    sources = tmp_path / "sources.json"
    sources.write_text("[]", encoding="utf-8")  # no fetches
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = dict(os.environ, ACL_OBSIDIAN_DIR=str(tmp_path / "missing"),
               HOME=str(tmp_path / "home"), USERPROFILE=str(tmp_path / "home"))
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "meta_docs_watch.py"),
         "--sources", str(sources), "--snapshots", str(tmp_path / "snapshots"),
         "--out", str(tmp_path / "digests"), "--obsidian"],
        cwd=elsewhere, env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 1  # the empty source list has no successful fetch
    assert "Obsidian vault not found; set ACL_OBSIDIAN_DIR" in result.stderr
    assert "Traceback" not in result.stderr
