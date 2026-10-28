"""Known synthetic secrets must survive no export's size boundary."""
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

from config import token_vault
from src.fb_ads_rpa import page_capture, telemetry_sync

SECRET = "FAKE-boundary-credential-abcdefghijklmnopqrstuvwxyz-0123456789"


@pytest.fixture
def snapshot(monkeypatch):
    # Never collect the host's configured secrets, even if the cache TTL expires.
    monkeypatch.setattr(token_vault, "_secrets_snapshot", lambda: (frozenset({SECRET}), (SECRET,)))


@pytest.mark.parametrize("offset", [-len(SECRET), -20, 0])
def test_telemetry_redacts_before_900_character_boundary(snapshot, offset):
    start = 900 + offset
    prefix = len(json.dumps({"detail": ""})) - 2
    row = telemetry_sync.to_row({"diag": {"detail": "x" * (start - prefix) + SECRET + "z" * 100}}, "L", "M", "v")
    assert len(row[9]) <= 900
    assert SECRET not in row[9] and SECRET[:20] not in row[9] and SECRET[-20:] not in row[9]
    assert row[9].startswith('{"detail": "xxx')


@pytest.mark.parametrize("unicode_padding", [False, True])
def test_capture_actual_byte_cap_hides_crossing_secret(snapshot, unicode_padding):
    limit = page_capture.MAX_HTML_BYTES
    padding = "x" * (limit - 20)
    if unicode_padding:
        padding = "é" * ((limit - 20) // 2)
    capture, truncated = page_capture.scrub_html(padding + SECRET + "z" * 100)
    assert truncated and len(capture.encode("utf-8")) <= limit
    assert SECRET not in capture and SECRET[:20] not in capture and SECRET[-20:] not in capture


def test_capture_whole_value_control(snapshot):
    capture, truncated = page_capture.scrub_html("<p>" + SECRET + "</p>")
    assert not truncated and SECRET not in capture and "<hidden>" in capture


@pytest.mark.parametrize("offset", [-len(SECRET), -20, 0])
def test_capture_retains_cap_and_safe_boundary_controls(snapshot, monkeypatch, offset):
    monkeypatch.setattr(page_capture, "MAX_HTML_BYTES", 200)
    capture, truncated = page_capture.scrub_html("x" * (200 + offset) + SECRET + "z" * 100)
    assert truncated and len(capture.encode()) <= 200
    assert SECRET not in capture and SECRET[:20] not in capture and SECRET[-20:] not in capture


def test_prefix_replacement_never_pulls_an_unchecked_overlap_fragment(snapshot):
    # The first replacement shrinks; a second secret crosses the overlap's end.
    # Its prefix must stay outside the original retained range.
    text = SECRET + "x" * (200 - 20 - len(SECRET)) + SECRET
    limit = 200 - len(SECRET) + 1
    safe, truncated = token_vault.hide_configured_secrets_prefix(text, limit)
    assert truncated and safe == "<hidden>" + "x" * (limit - len(SECRET))


@pytest.fixture
def diagnostics():
    path = Path(__file__).resolve().parents[1] / "packaging/windows/diagnostics.py"
    spec = importlib.util.spec_from_file_location("diagnostics_boundary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("suffix_length", [0, 20, len(SECRET)])
def test_real_archive_tail_redacts_known_value(diagnostics, monkeypatch, tmp_path, suffix_length):
    limit = diagnostics.MAX_SOURCE_FILE_BYTES
    # Whole secret, crossing boundary, or entirely outside the retained tail.
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "run.log").write_text("p" * 100 + SECRET + "x" * (limit - suffix_length), encoding="utf-8")
    monkeypatch.setattr(diagnostics, "local_values_to_redact", lambda _: {SECRET})
    monkeypatch.setattr(diagnostics, "build_summary", lambda *_: {})
    archive = diagnostics.export_diagnostics(tmp_path, tmp_path / "unused.json")
    with zipfile.ZipFile(archive) as bundle:
        text = bundle.read("logs/01.txt").decode("utf-8")
    assert SECRET not in text and SECRET[:20] not in text and SECRET[-20:] not in text
    assert len(text.encode("utf-8")) <= limit


def test_log_reader_keeps_bounded_io(diagnostics, tmp_path, monkeypatch):
    import io
    reads = []

    class Stream(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            assert 0 <= size <= 100 + len(SECRET.encode("utf-8")) - 1
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: Stream(b"p" * 1000 + SECRET.encode() + b"x" * 80))
    text = diagnostics._read_bounded(tmp_path / "unused.log", limit=100, local_values={SECRET})
    assert reads and SECRET[-20:] not in text and len(text.encode()) <= 100
