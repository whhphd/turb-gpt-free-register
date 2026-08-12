# -*- coding: utf-8 -*-
"""手机号填写/校验纯函数（OpenAI add-phone 自动格式化兼容）。"""
from __future__ import annotations

import re

# 常见国际区号，长的优先匹配（避免 1 抢掉 12x 等；美加统一用 1）
_DIAL_CODES: tuple[str, ...] = tuple(
    sorted(
        {
            "1", "7", "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45",
            "46", "47", "48", "49", "51", "52", "53", "54", "55", "56", "57", "58", "60", "61", "62", "63",
            "64", "65", "66", "81", "82", "84", "86", "90", "91", "92", "93", "94", "95", "98",
            "211", "212", "213", "216", "218", "220", "221", "222", "223", "224", "225", "226", "227",
            "228", "229", "230", "231", "232", "233", "234", "235", "236", "237", "238", "239", "240",
            "241", "242", "243", "244", "245", "248", "249", "250", "251", "252", "253", "254", "255",
            "256", "257", "258", "260", "261", "262", "263", "264", "265", "266", "267", "268", "269",
            "290", "291", "297", "298", "299", "350", "351", "352", "353", "354", "355", "356", "357",
            "358", "359", "370", "371", "372", "373", "374", "375", "376", "377", "378", "380", "381",
            "382", "383", "385", "386", "387", "389", "420", "421", "423", "500", "501", "502", "503",
            "504", "505", "506", "507", "508", "509", "590", "591", "592", "593", "594", "595", "596",
            "597", "598", "599", "670", "672", "673", "674", "675", "676", "677", "678", "679", "680",
            "681", "682", "683", "685", "686", "687", "688", "689", "690", "691", "692", "850", "852",
            "853", "855", "856", "880", "886", "960", "961", "962", "963", "964", "965", "966", "967",
            "968", "970", "971", "972", "973", "974", "975", "976", "977", "992", "993", "994", "995",
            "996", "998",
        },
        key=len,
        reverse=True,
    )
)


def digits_only(value: str | None) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def normalize_e164(phone: str | None) -> str:
    raw = str(phone or "").strip()
    if not raw:
        return ""
    if raw.startswith("+"):
        d = digits_only(raw)
        return f"+{d}" if d else ""
    d = digits_only(raw)
    return f"+{d}" if d else ""


def guess_dial_code(phone_or_digits: str | None) -> str:
    """从 E.164/纯数字猜测国际区号。"""
    digits = digits_only(phone_or_digits)
    if not digits:
        return ""
    for code in _DIAL_CODES:
        if digits.startswith(code) and len(digits) >= len(code) + 6:
            return code
    return ""


def national_digits(phone_or_digits: str | None, dial_code: str | None = None) -> str:
    digits = digits_only(phone_or_digits)
    code = str(dial_code or "").strip() or guess_dial_code(digits)
    if code and digits.startswith(code) and len(digits) > len(code):
        return digits[len(code):]
    return digits


# ISO 3166-1 alpha-2 → 国际区号（覆盖接码常用国；美加同为 1）
_ISO_TO_DIAL: dict[str, str] = {
    "US": "1", "CA": "1", "PR": "1", "DO": "1", "JM": "1",
    "GB": "44", "UK": "44", "IE": "353", "FR": "33", "DE": "49", "IT": "39", "ES": "34",
    "PT": "351", "NL": "31", "BE": "32", "CH": "41", "AT": "43", "SE": "46", "NO": "47",
    "DK": "45", "FI": "358", "PL": "48", "CZ": "420", "RO": "40", "HU": "36", "GR": "30",
    "TR": "90", "RU": "7", "UA": "380", "KZ": "7",
    "CN": "86", "HK": "852", "MO": "853", "TW": "886", "JP": "81", "KR": "82",
    "IN": "91", "PK": "92", "BD": "880", "LK": "94", "NP": "977",
    "ID": "62", "MY": "60", "SG": "65", "TH": "66", "VN": "84", "PH": "63", "MM": "95",
    "KH": "855", "LA": "856", "BN": "673",
    "AU": "61", "NZ": "64",
    "BR": "55", "AR": "54", "CL": "56", "CO": "57", "PE": "51", "MX": "52", "VE": "58",
    "EC": "593", "UY": "598", "PY": "595", "BO": "591",
    "SA": "966", "AE": "971", "QA": "974", "KW": "965", "BH": "973", "OM": "968",
    "IL": "972", "JO": "962", "LB": "961", "IQ": "964", "IR": "98", "EG": "20",
    "ZA": "27", "NG": "234", "KE": "254", "GH": "233", "MA": "212", "TN": "216",
    "DZ": "213", "ET": "251", "TZ": "255", "UG": "256",
}

# 日/英/中常见国家名片段 → 区号（OpenAI 日文界面常见「アメリカ合衆国」无 +1）
_COUNTRY_NAME_TO_DIAL: tuple[tuple[str, str], ...] = (
    ("united states", "1"), ("usa", "1"), ("america", "1"), ("アメリカ", "1"), ("美国", "1"), ("美國", "1"),
    ("canada", "1"), ("カナダ", "1"), ("加拿大", "1"),
    ("united kingdom", "44"), ("great britain", "44"), ("england", "44"), ("イギリス", "44"), ("英国", "44"), ("英國", "44"),
    ("indonesia", "62"), ("インドネシア", "62"), ("印尼", "62"), ("印度尼西亚", "62"),
    ("thailand", "66"), ("タイ", "66"), ("泰国", "66"), ("泰國", "66"),
    ("philippines", "63"), ("フィリピン", "63"), ("菲律宾", "63"), ("菲律賓", "63"),
    ("malaysia", "60"), ("マレーシア", "60"), ("马来西亚", "60"), ("馬來西亞", "60"),
    ("vietnam", "84"), ("ベトナム", "84"), ("越南", "84"),
    ("singapore", "65"), ("シンガポール", "65"), ("新加坡", "65"),
    ("india", "91"), ("インド", "91"), ("印度", "91"),
    ("china", "86"), ("中国", "86"), ("中國", "86"), ("中国大陆", "86"),
    ("hong kong", "852"), ("香港", "852"),
    ("taiwan", "886"), ("台湾", "886"), ("台灣", "886"), ("タイペイ", "886"),
    ("japan", "81"), ("日本", "81"),
    ("korea", "82"), ("south korea", "82"), ("韓国", "82"), ("韩国", "82"), ("韓國", "82"),
    ("brazil", "55"), ("ブラジル", "55"), ("巴西", "55"),
    ("mexico", "52"), ("メキシコ", "52"), ("墨西哥", "52"),
    ("saudi", "966"), ("サウジアラビア", "966"), ("沙特", "966"), ("沙烏地", "966"),
    ("united arab emirates", "971"), ("uae", "971"), ("アラブ首長国", "971"), ("阿联酋", "971"),
    ("turkey", "90"), ("türkiye", "90"), ("トルコ", "90"), ("土耳其", "90"),
    ("poland", "48"), ("ポーランド", "48"), ("波兰", "48"), ("波蘭", "48"),
    ("germany", "49"), ("ドイツ", "49"), ("德国", "49"), ("德國", "49"),
    ("france", "33"), ("フランス", "33"), ("法国", "33"), ("法國", "33"),
    ("australia", "61"), ("オーストラリア", "61"), ("澳大利亚", "61"), ("澳洲", "61"),
    ("russia", "7"), ("ロシア", "7"), ("俄罗斯", "7"), ("俄羅斯", "7"),
    ("nigeria", "234"), ("ナイジェリア", "234"), ("尼日利亚", "234"),
    ("egypt", "20"), ("エジプト", "20"), ("埃及", "20"),
    ("pakistan", "92"), ("パキスタン", "92"), ("巴基斯坦", "92"),
    ("bangladesh", "880"), ("バングラデシュ", "880"), ("孟加拉", "880"),
)


def iso_to_dial(iso_code: str | None) -> str:
    code = str(iso_code or "").strip().upper()
    if not code:
        return ""
    return _ISO_TO_DIAL.get(code, "")


def dial_to_iso_candidates(dial_code: str | None) -> list[str]:
    want = digits_only(dial_code)
    if not want:
        return []
    return sorted({iso for iso, dial in _ISO_TO_DIAL.items() if dial == want})


def extract_option_dial_code(
    text: str | None = None,
    *,
    value: str | None = None,
    data_key: str | None = None,
    data_value: str | None = None,
) -> str:
    """从 option/combobox 文案或 value/ISO 中提取国际区号。"""
    blobs = [
        str(text or ""),
        str(value or ""),
        str(data_key or ""),
        str(data_value or ""),
    ]
    joined = " ".join(blobs).replace("\u00a0", " ")
    compact = re.sub(r"\s+", " ", joined).strip()
    if not compact:
        return ""

    # (+62) / +62 / 62 结尾
    m = (
        re.search(r"\(\s*\+(\d{1,4})\s*\)", compact)
        or re.search(r"\+(\d{1,4})\b", compact)
        or re.search(r"(?:^|[^\d])(\d{1,4})\s*$", compact)
    )
    if m:
        code = m.group(1)
        if code in _DIAL_CODES or len(code) <= 3:
            return code

    # value/data 本身是区号
    for raw in (value, data_key, data_value):
        s = str(raw or "").strip()
        if re.fullmatch(r"\+?\d{1,4}", s):
            return digits_only(s)

    # ISO alpha-2
    for raw in (value, data_key, data_value, text):
        s = str(raw or "").strip().upper()
        if re.fullmatch(r"[A-Z]{2}", s):
            dial = iso_to_dial(s)
            if dial:
                return dial
        # value 形如 US / US:+1 / country-US
        m_iso = re.search(r"(?:^|[^A-Z])([A-Z]{2})(?:$|[^A-Z])", s)
        if m_iso:
            dial = iso_to_dial(m_iso.group(1))
            if dial:
                return dial

    # 国家名
    low = compact.lower()
    for name, dial in _COUNTRY_NAME_TO_DIAL:
        if name in low or name in compact:
            return dial
    return ""


def country_dial_matches(actual_dial: str | None, expected_dial: str | None) -> bool:
    a = digits_only(actual_dial)
    e = digits_only(expected_dial)
    if not e:
        return True
    return bool(a) and a == e


def phone_visible_matches_expected(
    actual_visible: str | None,
    expected_e164: str | None,
    *,
    dial_code: str | None = None,
    hidden_value: str | None = None,
    selected_country_dial: str | None = None,
    require_country_match: bool = False,
) -> bool:
    """可见框/隐藏域是否“有效代表”目标号码。

    OpenAI React-Aria 会把号码格式化成 "+62 858 ..." 或错误地按美号 (xxx) xxx-xxxx。
    表单提交通常以隐藏域 phoneNumber 为准：
      - 若 hidden 存在且 digits == E.164，默认通过（可见框可被 UI 截断/美化）
      - 若 require_country_match=True，还必须 selected_country_dial 与目标区号一致
      - 若无 hidden，则要求可见框 digits 等于 E.164 或国内号
    """
    expected = digits_only(expected_e164)
    if not expected:
        return False

    code = str(dial_code or "").strip() or guess_dial_code(expected)
    if require_country_match and code:
        if not country_dial_matches(selected_country_dial, code):
            return False

    hidden = digits_only(hidden_value) if hidden_value is not None else ""
    # 隐藏域存在且错误 → 必失败（国家码选错时常见 +1 改写）
    if hidden_value is not None and str(hidden_value).strip() != "":
        if hidden == expected:
            return True
        return False

    actual = digits_only(actual_visible)
    if not actual:
        return False
    if actual == expected:
        return True

    national = national_digits(expected, code)
    if national and actual == national:
        return True
    if national and actual == ("0" + national):
        return True
    if code and actual == (code + national):
        return True
    return False


def classify_phone_failure_reason(
    *,
    error_message: str = "",
    body_text: str = "",
    whatsapp_checked: bool | None = None,
    sms_checked: bool | None = None,
    radios_present: bool = False,
) -> str:
    """分类手机号失败原因，供冷却/换号策略使用。

    注意：
    - 页面上常驻 SMS/WhatsApp 切换文案，不能因 body 含 "whatsapp" 就判 whatsapp_channel；
    - 异常 message 里若 dump 了 state，也会含 whatsapp 字样，必须用结构化前缀优先。
    """
    msg = str(error_message or "").strip()
    msg_l = msg.lower()
    body = str(body_text or "").lower()

    # 1) 显式错误码前缀（我们自己 raise 的）
    for code in (
        "send_not_accepted",
        "whatsapp_channel",
        "invalid_phone",
        "invalid_auth_step",
        "delivery_refused",
        "send_limited",
        "phone_fill_mismatch",
        "country_select_failed",
        "verify_submit_missing",
    ):
        if msg_l.startswith(code + ":") or msg_l.startswith(code + " "):
            if code in ("phone_fill_mismatch", "country_select_failed"):
                return "invalid_phone"
            return code

    # 2) WhatsApp 仅在 radio 被勾选且 SMS 未勾选，或明确只提供 WhatsApp
    if whatsapp_checked is True and sms_checked is not True:
        return "whatsapp_channel"
    if "whatsapp_channel" in msg_l and "仅提供" in msg:
        return "whatsapp_channel"
    if "页面仅提供 whatsapp" in msg_l or "only" in msg_l and "whatsapp" in msg_l and "sms" not in msg_l:
        return "whatsapp_channel"

    # 3) 明确业务错误文案（body 或 message 前半段，避免 state dump）
    head = msg_l.split("state=", 1)[0]
    probe = f"{head}\n{body}"
    # 日文/英文 add-phone 页常驻说明「続行するには電話番号を追加… / Phone number is required」
    # 不能单凭这句判 invalid_phone；只有带 invalid/not valid 等才算。
    if any(k in probe for k in (
        "invalid phone", "not a valid phone", "phone number is not valid",
        "号码无效", "手机号无效", "無効な電話", "電話番号が無効",
    )):
        if whatsapp_checked is True and sms_checked is not True:
            return "whatsapp_channel"
        return "invalid_phone"
    # “Phone number required / 電話番号が必要” 单独出现时：
    # - 若 WhatsApp 勾选且 SMS 未勾选 → 通道问题
    # - 否则当作普通页面说明，不归类（交给上层继续等/换策略）
    if any(k in probe for k in ("phone number required", "電話番号が必要", "phone number is required")):
        if whatsapp_checked is True and sms_checked is not True:
            return "whatsapp_channel"
        return ""
    if any(k in probe for k in (
        "cannot send", "could not send", "unable to send", "failed to send", "send failed",
        "发送失败", "无法发送", "不能发送", "送信できません", "送信に失敗",
    )):
        return "delivery_refused"
    if any(k in probe for k in ("too many", "rate limit", "throttle", "频繁", "限流")):
        return "send_limited"
    if "invalid_auth_step" in probe or "invalid auth step" in probe:
        return "invalid_auth_step"

    # 4) 仍停留 add-phone 且未识别到更具体错误
    if "send_not_accepted" in msg_l:
        return "send_not_accepted"

    # 5) 最后：不要因为页面有 WhatsApp 文案就判 whatsapp
    if radios_present and whatsapp_checked is True and sms_checked is False:
        return "whatsapp_channel"
    return ""
