from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from verify_core_isolation import (
    BaselineManifest,
    IsolationError,
    is_path_allowed,
    load_manifest,
    verify_isolation,
)


def _run(root: Path, *args: str) -> str:
    result = subprocess.run(
        [*args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class PathAllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = BaselineManifest(
            baseline_sha="a" * 40,
            baseline_branch="feature/pilot",
            implementation_branch="codex/installer",
            allowed_exact_paths=frozenset({".github/workflows/windows-installer.yml"}),
            allowed_path_prefixes=("packaging/windows/",),
        )

    def test_allows_exact_workflow(self) -> None:
        self.assertTrue(
            is_path_allowed(".github/workflows/windows-installer.yml", self.manifest)
        )

    def test_allows_packaging_subtree(self) -> None:
        self.assertTrue(is_path_allowed("packaging/windows/build.py", self.manifest))

    def test_blocks_runtime_and_similar_prefix(self) -> None:
        self.assertFalse(is_path_allowed("src/web/app.py", self.manifest))
        self.assertFalse(is_path_allowed("packaging/windows-old/build.py", self.manifest))

    def test_rejects_parent_traversal(self) -> None:
        with self.assertRaises(IsolationError):
            is_path_allowed("packaging/windows/../../src/app.py", self.manifest)


class ManifestTests(unittest.TestCase):
    def test_loads_and_normalises_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "baseline.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "baseline_sha": "A" * 40,
                        "baseline_branch": "feature/pilot",
                        "implementation_branch": "codex/installer",
                        "allowed_exact_paths": [".github\\workflows\\installer.yml"],
                        "allowed_path_prefixes": ["packaging/windows"],
                    }
                ),
                encoding="utf-8",
            )
            manifest = load_manifest(path)

        self.assertEqual(manifest.baseline_sha, "a" * 40)
        self.assertIn(".github/workflows/installer.yml", manifest.allowed_exact_paths)
        self.assertEqual(manifest.allowed_path_prefixes, ("packaging/windows/",))


class RepositoryIsolationTests(unittest.TestCase):
    def test_reports_only_paths_outside_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _run(root, "git", "init", "-q")
            _run(root, "git", "config", "user.email", "acl-installer-test@example.invalid")
            _run(root, "git", "config", "user.name", "ACL Installer Test")

            (root / "src").mkdir()
            (root / "src" / "runtime.py").write_text("BASELINE = True\n", encoding="utf-8")
            _run(root, "git", "add", "src/runtime.py")
            _run(root, "git", "commit", "-q", "-m", "baseline")
            baseline = _run(root, "git", "rev-parse", "HEAD")

            (root / "packaging" / "windows").mkdir(parents=True)
            (root / "packaging" / "windows" / "build.py").write_text(
                "print('packaging')\n", encoding="utf-8"
            )
            (root / "src" / "runtime.py").write_text("BASELINE = False\n", encoding="utf-8")

            manifest = BaselineManifest(
                baseline_sha=baseline,
                baseline_branch="feature/pilot",
                implementation_branch="codex/installer",
                allowed_exact_paths=frozenset(),
                allowed_path_prefixes=("packaging/windows/",),
            )
            changed, blocked = verify_isolation(root, manifest)

        self.assertEqual(changed, ["packaging/windows/build.py", "src/runtime.py"])
        self.assertEqual(blocked, ["src/runtime.py"])


if __name__ == "__main__":
    unittest.main()
