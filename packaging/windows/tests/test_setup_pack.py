from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from setup_pack import SetupPackError, apply_setup_pack, load_setup_pack


def valid_document() -> dict:
    return {
        "schema": 1,
        "campaign_manager": {
            "name": "Dmitry Operator",
            "accounts_sheet": "1WlSGohkev4h_n0gw2vl0jlJEDFpTGl306fF7bIiRt9k",
        },
        "accounts": [
            {
                "account_name": "FacebookACL574 EC",
                "ad_account_id": "act_987654321098765",
                "page_id": "987654321098765",
                "multilogin_instance": "A",
                "folder_id": "11111111-1111-4111-8111-111111111111",
                "profile_id": "22222222-2222-4222-8222-222222222222",
            }
        ],
    }


def write_pack(path: Path, document: dict, *, extra: dict[str, bytes] | None = None) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("setup.json", json.dumps(document))
        for name, content in (extra or {}).items():
            archive.writestr(name, content)


class SetupPackTests(unittest.TestCase):
    def test_valid_pack_loads_and_normalises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dmitry.aclsetup"
            write_pack(path, valid_document())
            pack = load_setup_pack(path)

        self.assertEqual(pack.campaign_manager_name, "Dmitry Operator")
        self.assertEqual(pack.accounts[0].multilogin_instance, "A")
        self.assertEqual(pack.accounts[0].ad_account_id, "act_987654321098765")

    def test_unknown_fields_and_duplicate_accounts_are_rejected(self):
        document = valid_document()
        document["forbidden_safety_override"] = "recipe_player"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.aclsetup"
            write_pack(path, document)
            with self.assertRaisesRegex(SetupPackError, "unexpected fields"):
                load_setup_pack(path)

        document = valid_document()
        document["accounts"].append(dict(document["accounts"][0]))
        document["accounts"][1]["profile_id"] = "33333333-3333-4333-8333-333333333333"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate.aclsetup"
            write_pack(path, document)
            with self.assertRaisesRegex(SetupPackError, "duplicate account label"):
                load_setup_pack(path)

    def test_archive_paths_and_unexpected_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsafe.aclsetup"
            write_pack(path, valid_document(), extra={"../token_accounts.json": b"secret"})
            with self.assertRaisesRegex(SetupPackError, "unsafe path"):
                load_setup_pack(path)

    def test_invalid_pack_does_not_touch_existing_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            config.mkdir()
            env = config / ".env"
            env.write_text("MY_CM_NAME=Original\n", encoding="utf-8")
            document = valid_document()
            document["accounts"][0]["ad_account_id"] = "wrong"
            path = root / "bad.aclsetup"
            write_pack(path, document)

            with self.assertRaises(SetupPackError):
                load_setup_pack(path)
            self.assertEqual(env.read_text(encoding="utf-8"), "MY_CM_NAME=Original\n")
            self.assertFalse((config / "ad_account_map.csv").exists())

    def test_apply_preserves_other_env_and_sets_fixed_safety_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "valid.aclsetup"
            write_pack(path, valid_document())
            pack = load_setup_pack(path)
            config = root / "config"
            config.mkdir()
            (config / ".env").write_text("KEEP_ME=unchanged\nFB_RPA_ENGINE=unsafe\n", encoding="utf-8")

            apply_setup_pack(
                pack,
                config,
                stats_workbook=r"C:\Data\stats.xlsx",
                upload_workbook=r"C:\Data\upload.xlsx",
                creatives_directory=r"C:\Data\Creatives",
            )

            env = (config / ".env").read_text(encoding="utf-8")
            self.assertIn("KEEP_ME=unchanged", env)
            self.assertIn('FB_RPA_ENGINE="recorded_flow"', env)
            self.assertIn('FB_PUBLISH_BACKEND="rpa"', env)
            account_map = (config / "ad_account_map.csv").read_text(encoding="utf-8")
            self.assertIn("FacebookACL574 EC,act_987654321098765", account_map)


if __name__ == "__main__":
    unittest.main()
