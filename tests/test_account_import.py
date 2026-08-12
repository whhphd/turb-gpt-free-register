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

    def test_generic_api_email_mfa_code_url(self):
        # 邮箱----MFA----接码地址（无登录密码）
        line = (
            "balsas_forth.8l@icloud.com----JBSWY3DPEHPK3PXP"
            "----https://icloud-api.top/s/xxx/balsas_forth.8l@icloud.com"
        )
        rec = parse_import_account_line(line)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["kind"], "generic_api")
        self.assertEqual(rec["email"], "balsas_forth.8l@icloud.com")
        self.assertEqual(rec["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertTrue(str(rec["code_url"]).startswith("https://"))
        self.assertNotIn("password", rec)

    def test_generic_api_email_mfa_url_token(self):
        line = (
            "u@x.com----JBSWY3DPEHPK3PXP----https://mail.example.com/c"
            "----sk-test-access-token"
        )
        rec = parse_import_account_line(line)
        self.assertEqual(rec["kind"], "generic_api")
        self.assertEqual(rec["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(rec["access_token"], "sk-test-access-token")

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
            "e@f.com----JBSWY3DPEHPK3PXP----https://mail.example.com/x",
            "# comment",
            "bad-line",
        ])
        records, errors = parse_import_text(text)
        self.assertEqual(len(records), 3)
        self.assertTrue(any("无法识别" in e for e in errors))
        kinds = {r["kind"] for r in records}
        self.assertEqual(kinds, {"generic_api", "password_totp"})
        mfa_url = next(r for r in records if r["email"] == "e@f.com")
        self.assertEqual(mfa_url["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertIn("https://", mfa_url["code_url"])

    def test_detect_kind_helpers(self):
        self.assertEqual(
            detect_import_kind(split_account_line("a@b.com====https://x.com")),
            "generic_api",
        )


if __name__ == "__main__":
    unittest.main()
