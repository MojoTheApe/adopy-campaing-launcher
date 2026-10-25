"""Приёмка обновления находит дешборд и нового, и старого (до ACL-1309) пакета.

Complete Delivery ставит как «старую» версию последний опубликованный релиз: его
/health без `config`, а порт всегда 9090. Новый поиск по отпечатку такой дешборд
не видел бы, и шаг падал бы через 90 секунд.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parents[1]
ROOT = PACKAGING_DIR.parents[1]
for entry in (str(PACKAGING_DIR), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import local_dashboard  # noqa: E402
import test_update_cycle as cycle  # noqa: E402


def _probe(answers):
    return lambda port: answers.get(port)


class FindDashboardTests(unittest.TestCase):
    def setUp(self):
        self.config = tempfile.mkdtemp()
        self.mine = local_dashboard.identity(self.config)
        # Порт «занят» для find_own, если по нему кто-то отвечает.
        self.answers = {}

    def find(self):
        return cycle.find_dashboard(self.config, _probe(self.answers), in_use=lambda port: port in self.answers)

    def test_own_fingerprint_on_a_later_port_is_found(self):
        self.answers[9091] = {"app": "ACL", "ok": True, "config": self.mine, "version": "2"}
        self.assertEqual(self.find()[0], 9091)

    def test_pre_acl_1309_payload_without_config_on_9090_is_found(self):
        self.answers[9090] = {"app": "ACL", "ok": True, "version": "1"}
        self.assertEqual(self.find()[0], 9090)

    def test_someone_elses_fingerprint_on_9090_is_not_ours(self):
        self.answers[9090] = {"app": "ACL", "ok": True, "config": "0" * 16, "version": "2"}
        self.assertIsNone(self.find())

    def test_nothing_answering_is_none(self):
        self.assertIsNone(self.find())


if __name__ == "__main__":
    unittest.main()
