"""台账的单测：等级合并、幂等、失败重跑不抹凭据。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import config, ledger  # noqa: E402


class RankTests(unittest.TestCase):
    def test_keyed_highest(self):
        self.assertGreater(ledger.rank("keyed"), ledger.rank("registered"))
        self.assertGreater(ledger.rank("registered"), ledger.rank("failed"))

    def test_unknown_is_zero(self):
        self.assertEqual(ledger.rank("whatever"), 0)
        self.assertEqual(ledger.rank(None), 0)


class MergeTests(unittest.TestCase):
    def test_failed_rerun_does_not_wipe_key(self):
        prev = {"email": "a@b.c", "status": "keyed", "api_key": "apk_user_x",
                "user_id": "u1"}
        new = {"email": "a@b.c", "status": "failed", "error": "boom",
               "api_key": "", "last_attempt": "2026-10-31T00:00:00+00:00"}
        merged = ledger._merge(prev, new)  # noqa: SLF001
        self.assertEqual(merged["api_key"], "apk_user_x")
        self.assertEqual(merged["status"], "keyed")
        self.assertEqual(merged["error"], "boom")

    def test_upgrade_fills_empty_from_old(self):
        prev = {"email": "a@b.c", "status": "registered", "org_id": "org-1"}
        new = {"email": "a@b.c", "status": "keyed", "api_key": "apk_user_y"}
        merged = ledger._merge(prev, new)  # noqa: SLF001
        self.assertEqual(merged["status"], "keyed")
        self.assertEqual(merged["org_id"], "org-1")

    def test_new_error_kept_even_when_old_wins(self):
        prev = {"email": "a@b.c", "status": "keyed", "api_key": "k"}
        new = {"email": "a@b.c", "status": "failed", "error": "rate_limited",
               "last_stage": "send_code"}
        merged = ledger._merge(prev, new)  # noqa: SLF001
        self.assertEqual(merged["last_stage"], "send_code")


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(config, "LEDGER_PATH", root / "ledger.jsonl"),
            mock.patch.object(config, "SUCCESS_PATH", root / "success.jsonl"),
            mock.patch.object(config, "RESULT_DIR", root),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_upsert_is_idempotent(self):
        ledger.upsert_many([
            {"email": "a@b.c", "status": "mailbox"},
            {"email": "a@b.c", "status": "code_sent"},
        ])
        rows = ledger.load()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows["a@b.c"]["status"], "code_sent")

    def test_persist_only_writes_success_with_key(self):
        ledger.persist({"email": "a@b.c", "status": "registered"})
        ledger.persist({"email": "b@b.c", "status": "keyed", "api_key": "apk_user_z"})
        self.assertEqual(len(ledger.load()), 2)
        success = config.SUCCESS_PATH.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(success), 1)
        self.assertIn("b@b.c", success[0])
        self.assertEqual(len(ledger.keyed_records()), 1)

    def test_keyed_records_merges_success_ledger(self):
        """清掉运行台账后，交付物仍能从成功台账重建（实测踩过的坑）。"""
        ledger.append({"email": "a@b.c", "status": "keyed", "api_key": "k1"},
                      config.LEDGER_PATH)
        ledger.append({"email": "b@b.c", "status": "keyed", "api_key": "k2"},
                      config.SUCCESS_PATH)
        rows = ledger.keyed_records()
        self.assertEqual(set(rows), {"a@b.c", "b@b.c"})

    def test_bad_jsonl_line_skipped(self):
        config.LEDGER_PATH.write_text(
            '{"email":"ok@b.c","status":"keyed","api_key":"k"}\n{half\n',
            encoding="utf-8")
        self.assertEqual(list(ledger.load()), ["ok@b.c"])


if __name__ == "__main__":
    unittest.main()
