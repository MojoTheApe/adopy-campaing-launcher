"""Offline apt fixtures: never read/write the machine's apt or download packages."""
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from scripts.ci_ubuntu_mirror import redirect_sources

ROOT = Path(__file__).resolve().parents[1]


def put(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    return path


def test_observed_indirect_hosted_source_changes_effective_mirror_not_source_settings(tmp_path):
    # The native failed job shows this URI and actual downloads from Azure.
    # Other fields/metadata are synthetic fixtures, not claimed runner provenance.
    source_text = ("Types: deb\nURIs: mirror+file:/etc/apt/apt-mirrors.txt\n"
                   "Suites: noble noble-updates noble-backports\nComponents: main universe\n"
                   "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n\n"
                   "Types: deb\nURIs: mirror+file:/etc/apt/apt-mirrors.txt\n"
                   "Suites: noble-security\nComponents: main universe\n"
                   "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n")
    source = put(tmp_path, "sources.list.d/ubuntu.sources", source_text)
    mirrors_text = ("# archive choices\nhttp://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
                    "http://security.ubuntu.com/ubuntu/\tpriority:2 suite:noble-security\n"
                    "https://packages.example.test/ubuntu/\tarch:amd64\n")
    mirrors = put(tmp_path, "apt-mirrors.txt", mirrors_text)
    unrelated = put(tmp_path, "unreferenced-mirrors.txt", "http://azure.archive.ubuntu.com/ubuntu/\n")

    assert redirect_sources(tmp_path) == [mirrors]
    assert mirrors.read_text() == mirrors_text.replace("azure.archive.ubuntu.com", "archive.ubuntu.com")
    assert source.read_bytes() == source_text.encode(), "suites, security stanza and signing are preserved"
    assert unrelated.read_text() == "http://azure.archive.ubuntu.com/ubuntu/\n"
    assert redirect_sources(tmp_path) == [], "preparation is idempotent"


@pytest.mark.parametrize("scheme,slash", [("http", ""), ("https", "/")])
def test_direct_deb822_preserves_security_other_uris_comments_crlf_and_options(tmp_path, scheme, slash):
    original = ("# Example: http://azure.archive.ubuntu.com/ubuntu/\r\n"
                "Types: deb deb-src\r\n"
                f"URIs: {scheme}://azure.archive.ubuntu.com/ubuntu{slash}\r\n"
                " https://security.ubuntu.com/ubuntu/ https://packages.example.test/ubuntu/\r\n"
                "Suites: noble noble-updates\r\nComponents: main universe\r\n"
                "Architectures: amd64\r\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\r\n")
    source = put(tmp_path, "sources.list.d/ubuntu.sources", original)
    assert redirect_sources(tmp_path) == [source]
    expected = original.replace(f"URIs: {scheme}://azure.archive.ubuntu.com/ubuntu{slash}",
                                f"URIs: {scheme}://archive.ubuntu.com/ubuntu{slash}")
    assert source.read_bytes() == expected.encode()


def test_legacy_source_resolves_indirection_and_changes_only_active_archive_uri(tmp_path):
    original = ("# deb http://azure.archive.ubuntu.com/ubuntu noble main\n"
                "deb [arch=amd64 signed-by=/usr/share/keyrings/ubuntu.gpg] "
                "https://azure.archive.ubuntu.com/ubuntu/ noble main # keep this comment\n"
                "deb-src mirror+file:/etc/apt/apt-mirrors.txt noble universe\n"
                "deb http://security.ubuntu.com/ubuntu noble-security main\n"
                "deb https://packages.example.test/ubuntu noble main\n")
    source = put(tmp_path, "sources.list", original)
    mirrors = put(tmp_path, "apt-mirrors.txt", "https://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n")
    assert redirect_sources(tmp_path) == [source, mirrors]
    assert source.read_text() == original.replace("https://azure.archive.ubuntu.com", "https://archive.ubuntu.com")
    assert mirrors.read_text() == "https://archive.ubuntu.com/ubuntu/\tpriority:1\n"


def test_disabled_source_and_similar_unrelated_urls_are_unchanged(tmp_path):
    original = ("Types: deb\nEnabled: no\nURIs: http://azure.archive.ubuntu.com/ubuntu/\n"
                "Suites: noble\n\nTypes: deb\nURIs: http://azure.archive.ubuntu.com/other "
                "https://azure.archive.ubuntu.com.example.test/ubuntu/\nSuites: noble\n")
    source = put(tmp_path, "sources.list.d/ubuntu.sources", original)
    third_party = put(tmp_path, "sources.list.d/vendor.sources", "Types: deb\nURIs: https://vendor.example.test/\n")
    assert redirect_sources(tmp_path) == []
    assert source.read_text() == original
    assert third_party.read_text() == "Types: deb\nURIs: https://vendor.example.test/\n"


def test_empty_configuration_does_not_create_or_modify_sources(tmp_path):
    assert redirect_sources(tmp_path) == []
    assert list(tmp_path.iterdir()) == []


def test_reference_cannot_escape_fixture_apt_directory(tmp_path):
    apt = tmp_path / "apt"
    outside = put(tmp_path, "outside.txt", "http://azure.archive.ubuntu.com/ubuntu/\n")
    put(apt, "sources.list", "deb mirror+file:/etc/apt/../outside.txt noble main\n")
    with pytest.raises(ValueError, match="outside the apt directory"):
        redirect_sources(apt)
    assert outside.read_text() == "http://azure.archive.ubuntu.com/ubuntu/\n"


def test_cli_updates_only_passed_temporary_apt_root(tmp_path):
    source = put(tmp_path, "sources.list", "deb http://azure.archive.ubuntu.com/ubuntu noble main\n")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/ci_ubuntu_mirror.py"),
                             "--apt-root", str(tmp_path)], check=True, capture_output=True, text=True)
    assert source.read_text() == "deb http://archive.ubuntu.com/ubuntu noble main\n"
    assert "Ubuntu archive mirror updated:" in result.stdout


def test_real_linux_preparation_calls_helper_before_both_existing_bounded_install_paths():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(s for s in ci["jobs"]["test-linux"]["steps"] if s.get("name") == "Install Chromium for isolated DOM tests")
    script = step["run"]
    helper = 'sudo -- "$(command -v python)" scripts/ci_ubuntu_mirror.py'
    assert helper in script
    assert script.index(helper) < script.index('if [ "${{ steps.pw-cache.outputs.cache-hit }}"')
    assert "bash scripts/ci_retry.sh 3 300 15 -- python -m playwright install-deps chromium" in script
    assert "bash scripts/ci_retry.sh 3 300 15 -- python -m playwright install --with-deps chromium" in script
    assert step["timeout-minutes"] == 16
