# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core.account_import import parse_import_text
from core import finished_batch_service as fbs


class FinishedBatchTests(unittest.TestCase):
    def test_parse_import_mixed_formats(self):
        text = "\n".join([
            "a@b.com----https://mail.example/code",
            "c@d.com----JBSWY3DPEHPK3PXP----https://mail.example/2",
            "not-a-line",
        ])
        records, errors = parse_import_text(text)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["source"], "generic_api")
        self.assertEqual(records[1]["source"], "generic_api")
        self.assertTrue(records[1].get("totp_secret"))
        self.assertGreaterEqual(len(errors), 1)

    def test_empty_summary_keys(self):
        s = fbs._empty_summary()
        for k in ("imported", "plus", "codex_success", "pool_pushed"):
            self.assertIn(k, s)

    def test_create_batch_without_autorun(self):
        with patch.object(fbs, "start_batch_pipeline", return_value={"ok": True}):
            batch = fbs.create_batch(
                name="单测批次",
                text="a@b.com----https://x.test/c",
                note="unit",
                auto_run=False,
                codex_workers=2,
            )
        self.assertTrue(str(batch.get("id") or "").startswith("fab-"))
        self.assertEqual(batch.get("name"), "单测批次")
        got = fbs.get_batch(batch["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got.get("pending_text", "")[:10], "a@b.com---")


if __name__ == "__main__":
    unittest.main()
