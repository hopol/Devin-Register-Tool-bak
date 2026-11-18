"""收件规则与取码的单测（离线，不碰网络）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.mailrules import RULES, diagnose, extract_otp, get  # noqa: E402
from src.tempemail import Mail  # noqa: E402

# 实测样本：信封发件人是 VERP 回弹地址，主题固定，正文重复 4 遍同一段文案。
REAL_MAIL = Mail(
    id="1",
    to="oai-test@mail.test",
    sender="bounces+44927189-54bb-oai-test=mail.test@em3779.cognition.ai",
    subject="Your Devin Login Code",
    body=(
        "Verify your email for Devin - 517536\n\n"
        "To start using Devin, please enter the verification code below in the original window.\n"
        "517536\nThis code expires in 10 minutes.\n"
        "Cognition AI, Inc. 1875 Mission Street, 103, San Francisco, CA 94103\n"
        "517536\n517536\n"
    ),
    received_at=1793435567000,
)


class RuleTests(unittest.TestCase):
    def test_real_mail_matches(self):
        self.assertTrue(get("login_code").matches(REAL_MAIL))

    def test_wrong_sender_rejected(self):
        mail = Mail("2", REAL_MAIL.to, "news@example.com",
                    "Your Devin Login Code", "123456", 1)
        self.assertFalse(get("login_code").matches(mail))

    def test_subject_without_code_word_rejected(self):
        mail = Mail("3", REAL_MAIL.to, REAL_MAIL.sender,
                    "Devin product update", "123456", 1)
        self.assertFalse(get("login_code").matches(mail))

    def test_diagnose_lists_unmatched(self):
        junk = Mail("4", REAL_MAIL.to, "no-reply@other.tld", "Hello", "", 2)
        self.assertEqual(len(diagnose([REAL_MAIL, junk])), 1)
        self.assertIn("Hello", diagnose([REAL_MAIL, junk])[0])

    def test_rule_table_nonempty(self):
        self.assertIn("login_code", RULES)


class OtpTests(unittest.TestCase):
    def test_anchored_from_body(self):
        code, how = extract_otp(REAL_MAIL.subject, REAL_MAIL.body)
        self.assertEqual((code, how), ("517536", "anchored"))

    def test_subject_suffix_wins(self):
        code, how = extract_otp("Your Devin Login Code - 123456", "code below 999999")
        self.assertEqual((code, how), ("123456", "subject"))

    def test_loose_most_common(self):
        body = "no kw here 111111 222222 111111 111111"
        code, how = extract_otp("Devin", body)
        self.assertEqual((code, how), ("111111", "loose"))

    def test_verp_eight_digit_not_treated_as_six(self):
        """VERP 地址里的 44927189 不能切成 449271（否则 401 会伪装成"过期"）。"""
        body = "bounces+44927189-54bb@em3779.cognition.ai"
        code, how = extract_otp("Devin", body)
        self.assertEqual((code, how), ("", "none"))

    def test_no_code(self):
        self.assertEqual(extract_otp("Devin", "nothing here"), ("", "none"))


if __name__ == "__main__":
    unittest.main()
