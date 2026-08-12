# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from core import db


class DirectCardStatusTests(unittest.TestCase):
    def test_normalize_missing_as_unused(self):
        self.assertEqual(db.normalize_direct_card_status(None), "未用")
        self.assertEqual(db.normalize_direct_card_status(""), "未用")
        self.assertEqual(db.normalize_direct_card_status("unused"), "未用")
        self.assertEqual(db.normalize_direct_card_status("random"), "未用")

    def test_normalize_used(self):
        self.assertEqual(db.normalize_direct_card_status("已用"), "已用")
        self.assertEqual(db.normalize_direct_card_status("used"), "已用")
        self.assertEqual(db.normalize_direct_card_status("USED"), "已用")

    def test_decorate_defaults_unused(self):
        out = db._decorate_account({"id": 1, "email": "a@b.com"})
        self.assertEqual(out["direct_card_status"], "未用")

    def test_update_marks_used(self):
        rows = [{"id": 7, "email": "x@y.com", "access_token": "tok"}]
        with patch.object(db, "_load_accounts", return_value=rows), \
             patch.object(db, "_save_accounts") as save:
            updated = db.update_account_direct_card_status(7, "已用")
        self.assertIsNotNone(updated)
        self.assertEqual(updated["direct_card_status"], "已用")
        self.assertEqual(rows[0]["direct_card_status"], "已用")
        self.assertTrue(rows[0].get("direct_card_updated_at"))
        save.assert_called_once()

    def test_plan_status_snapshot_includes_direct_card(self):
        rows = [
            {"id": 1, "email": "a@b.com", "access_token": "t1"},
            {"id": 2, "email": "c@d.com", "access_token": "t2", "direct_card_status": "已用"},
        ]
        with patch.object(db, "_load_accounts", return_value=rows):
            snap = db.list_account_plan_check_statuses(limit=10, offset=0)
        items = {int(x["id"]): x for x in snap["items"]}
        self.assertEqual(items[1]["direct_card_status"], "未用")
        self.assertEqual(items[2]["direct_card_status"], "已用")


if __name__ == "__main__":
    unittest.main()
