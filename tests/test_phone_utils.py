# -*- coding: utf-8 -*-
import unittest

from core.phone_utils import (
    classify_phone_failure_reason,
    country_dial_matches,
    dial_to_iso_candidates,
    digits_only,
    extract_option_dial_code,
    guess_dial_code,
    iso_to_dial,
    national_digits,
    phone_visible_matches_expected,
)


class PhoneUtilsTests(unittest.TestCase):
    def test_indonesia_formatted_visible_matches(self):
        # 日志里的真实形态：空格格式化后数字应完整对齐
        self.assertTrue(
            phone_visible_matches_expected(
                "+62 858 8178 9782",
                "+6285881789782",
                dial_code="62",
                hidden_value="+6285881789782",
            )
        )
        # 可见框只显示国内号
        self.assertTrue(
            phone_visible_matches_expected(
                "85881789782",
                "+6285881789782",
                dial_code="62",
                hidden_value="+6285881789782",
            )
        )
        # 可见框被美号格式截断，但 hidden 正确 → 通过（提交靠 hidden）
        self.assertTrue(
            phone_visible_matches_expected(
                "(838) 225-0866",
                "+6283822508663",
                dial_code="62",
                hidden_value="+6283822508663",
            )
        )
        # hidden 被改成 +1 → 必须失败
        self.assertFalse(
            phone_visible_matches_expected(
                "(571) 946-756",
                "+966571946756",
                dial_code="966",
                hidden_value="+1571946756",
            )
        )
        # 无 hidden 且可见截断 → 失败
        self.assertFalse(
            phone_visible_matches_expected(
                "+62 858 8178 978",
                "+6285881789782",
                dial_code="62",
                hidden_value=None,
            )
        )

    def test_us_number_formats(self):
        self.assertEqual(guess_dial_code("+13186509853"), "1")
        self.assertEqual(national_digits("+13186509853"), "3186509853")
        self.assertTrue(
            phone_visible_matches_expected(
                "(318) 650-9853",
                "+13186509853",
                dial_code="1",
            )
        )

    def test_hidden_mismatch_rejects(self):
        self.assertFalse(
            phone_visible_matches_expected(
                "+62 858 8178 9782",
                "+6285881789782",
                hidden_value="+6285881789780",  # wrong
            )
        )

    def test_whatsapp_not_inferred_from_page_label_alone(self):
        # 页面常驻 SMS/WhatsApp 文案，不能仅凭 body 判 whatsapp
        reason = classify_phone_failure_reason(
            body_text="SMS WhatsApp Phone number Continue",
            whatsapp_checked=False,
            sms_checked=True,
            radios_present=True,
        )
        self.assertEqual(reason, "")

        reason2 = classify_phone_failure_reason(
            body_text="SMS WhatsApp Phone number Continue",
            whatsapp_checked=True,
            sms_checked=False,
            radios_present=True,
        )
        self.assertEqual(reason2, "whatsapp_channel")

    def test_state_dump_with_whatsapp_not_misclassify_fill_error(self):
        msg = (
            "phone_fill_mismatch: 手机号可见输入框校验失败 expected_digits=6285881789782 "
            "actual=+62 858 8178 978 result={...} state={'radios': [{'value': 'whatsapp'}]}"
        )
        reason = classify_phone_failure_reason(error_message=msg)
        self.assertEqual(reason, "invalid_phone")

    def test_send_not_accepted_prefix(self):
        reason = classify_phone_failure_reason(
            error_message="send_not_accepted: 提交后仍停留在 add-phone state={'radios':[{'value':'whatsapp'}]}"
        )
        self.assertEqual(reason, "send_not_accepted")

    def test_digits_only(self):
        self.assertEqual(digits_only("+62 858 8178 9782"), "6285881789782")

    def test_extract_option_dial_from_iso_and_names(self):
        # OpenAI 日文界面：select 文案无 +1
        self.assertEqual(extract_option_dial_code("アメリカ合衆国", value="US"), "1")
        self.assertEqual(extract_option_dial_code("タイ", value="TH"), "66")
        self.assertEqual(extract_option_dial_code("Indonesia (+62)"), "62")
        self.assertEqual(extract_option_dial_code("Saudi Arabia +966"), "966")
        self.assertEqual(extract_option_dial_code(value="ID"), "62")
        self.assertEqual(iso_to_dial("TH"), "66")
        self.assertIn("ID", dial_to_iso_candidates("62"))
        self.assertTrue(country_dial_matches("62", "62"))
        self.assertFalse(country_dial_matches("1", "62"))

    def test_require_country_match_rejects_us_default_for_non_us(self):
        # hidden 正确但国家仍是美国 → 必须失败（旧逻辑会放行导致 invalid_phone）
        self.assertFalse(
            phone_visible_matches_expected(
                "(638) 360-146",
                "+66638360146",
                dial_code="66",
                hidden_value="+66638360146",
                selected_country_dial="1",
                require_country_match=True,
            )
        )
        self.assertTrue(
            phone_visible_matches_expected(
                "+66 638 360 146",
                "+66638360146",
                dial_code="66",
                hidden_value="+66638360146",
                selected_country_dial="66",
                require_country_match=True,
            )
        )

    def test_country_select_failed_classifies_invalid_phone(self):
        reason = classify_phone_failure_reason(
            error_message="country_select_failed: 国家/区号选择失败 want=+66 detail={...}"
        )
        self.assertEqual(reason, "invalid_phone")

    def test_japanese_page_instruction_not_invalid_phone(self):
        # add-phone 页常驻「電話番号が必要です」不是错误
        reason = classify_phone_failure_reason(
            body_text="続行するには電話番号を追加してください。電話番号が必要です。SMS WhatsApp",
            whatsapp_checked=False,
            sms_checked=True,
            radios_present=True,
        )
        self.assertEqual(reason, "")
        # 仅当 WhatsApp 勾选时才因「必要」判通道问题
        reason2 = classify_phone_failure_reason(
            body_text="電話番号が必要です",
            whatsapp_checked=True,
            sms_checked=False,
            radios_present=True,
        )
        self.assertEqual(reason2, "whatsapp_channel")


if __name__ == "__main__":
    unittest.main()
