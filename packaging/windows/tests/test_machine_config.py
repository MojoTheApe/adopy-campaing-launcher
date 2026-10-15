from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from machine_config import (
    AccountAssignment,
    MachineConfiguration,
    apply_machine_configuration,
)


class MachineConfigTests(unittest.TestCase):
    def test_apply_is_atomic_and_preserves_unrelated_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            (config / ".env").write_text(
                "KEEP_ME=unchanged\nFB_RPA_ENGINE=unsafe\n"
                'MULTILOGIN_AUTOMATION_TOKEN_A="saved-by-the-dashboard"\n',
                encoding="utf-8")
            configuration = MachineConfiguration(
                "Anna Smith",
                (AccountAssignment("FacebookAnna1", "act_" + ("8" * 15)),),
            )

            apply_machine_configuration(
                configuration,
                config,
                stats_workbook=r"C:\Data\stats.xlsx",
                creatives_directory=r"C:\Data\Creatives",
            )

            env = (config / ".env").read_text(encoding="utf-8")
            self.assertIn("KEEP_ME=unchanged", env)
            self.assertIn('FB_RPA_ENGINE="recorded_flow"', env)
            self.assertIn('FB_BROWSER_PROVIDER="multilogin_attach"', env)
            # ACL-1447: имя менеджера — и подпись событий Sentry этой машины.
            self.assertIn('ACL_MACHINE_LABEL="Anna Smith"', env)
            # ACL-1303: мастер больше не получает токен — его сохраняет
            # дешборд, когда человек входит в Multilogin. Повторный проход
            # установки обязан оставить эту строку в покое: затерев её, мы
            # отняли бы у человека подключение, а заново выдать токен может
            # только Multilogin, и выдаст он его, погасив предыдущий (24.09).
            self.assertIn('MULTILOGIN_AUTOMATION_TOKEN_A="saved-by-the-dashboard"', env)
            account_map = (config / "ad_account_map.csv").read_text(encoding="utf-8")
            self.assertIn("FacebookAnna1,act_" + ("8" * 15), account_map)
            self.assertIn('FB_CREATIVES_ROOT="C:\\\\Data\\\\Creatives"', env)
            for gone in ("GSHEET_ACCOUNTS_ID", "UPLOAD_FB_EXCEL"):
                self.assertNotIn(gone, env)
            self.assertFalse((config / "oauth_client.json").exists())


if __name__ == "__main__":
    unittest.main()
