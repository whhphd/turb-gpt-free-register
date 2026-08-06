# -*- coding: utf-8 -*-
import unittest

from core.account_import import (
    detect_import_kind,
    parse_import_account_line,
    parse_import_text,
    split_account_line,
)


class AccountImportParseTests(unittest.TestCase):
    def test_generic_api_url(self):
        rec = parse_import_account_line("a@b.com----https://mail.example.com/api/code?e=a@b.com")
        self.assertEqual(rec["kind"], "generic_api")
        self.assertEqual(rec["email"], "a@b.com")
        self.assertIn("https://", rec["code_url"])

    def test_password_totp_triple_dash(self):
        rec = parse_import_account_line("user@icloud.com----MyPass123----JBSWY3DPEHPK3PXP")
        self.assertEqual(rec["kind"], "password_totp")
        self.assertEqual(rec["password"], "MyPass123")
        self.assertEqual(rec["totp_secret"], "JBSWY3DPEHPK3PXP")

    def test_password_totp_single_dash_style(self):
        # 中文常用「邮箱-密码-2FA」
        rec = parse_import_account_line("user@icloud.com-MyPass123-JBSWY3DPEHPK3PXP")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["kind"], "password_totp")
        self.assertEqual(rec["email"], "user@icloud.com")
        self.assertEqual(rec["password"], "MyPass123")
        self.assertEqual(rec["totp_secret"], "JBSWY3DPEHPK3PXP")

    def test_outlook_four_fields(self):
        line = (
            "u@outlook.com----pwd----11111111-2222-3333-4444-555555555555"
            "----M.R3_BAY.0.U.-refreshTOKENVALUE_long_enough_for_detect"
        )
        rec = parse_import_account_line(line)
        self.assertEqual(rec["kind"], "outlook")
        self.assertEqual(rec["client_id"], "11111111-2222-3333-4444-555555555555")

    def test_mixed_text_auto(self):
        text = "\n".join([
            "a@b.com----https://x.com/c",
            "c@d.com----pw----JBSWY3DPEHPK3PXP",
            "# comment",
            "bad-line",
        ])
        records, errors = parse_import_text(text)
        self.assertEqual(len(records), 2)
        self.assertTrue(any("无法识别" in e for e in errors))
        kinds = {r["kind"] for r in records}
        self.assertEqual(kinds, {"generic_api", "password_totp"})

    def test_detect_kind_helpers(self):
        self.assertEqual(
            detect_import_kind(split_account_line("a@b.com====https://x.com")),
            "generic_api",
        )


if __name__ == "__main__":
    unittest.main()
