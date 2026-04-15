import sys
import unittest

sys.path.insert(0, 'backend')

from app.mail_providers import OutlookLocalProvider


RAW_OPENAI_MAIL = b"""Subject: Your OpenAI code is 882191\r\nFrom: noreply@tm.openai.com\r\nDate: Tue, 14 Apr 2026 16:29:06 +0000\r\nContent-Transfer-Encoding: quoted-printable\r\nContent-Type: text/html; charset=iso-8859-1\r\n\r\n<html><body><p>Enter this temporary verification code to continue:</p><p>882191</p></body></html>\r\n"""

RAW_OPENAI_MAIL_SPACED = b"""Subject: Your OpenAI code is 123 456\r\nFrom: noreply@tm.openai.com\r\nDate: Tue, 14 Apr 2026 16:29:06 +0000\r\nContent-Transfer-Encoding: quoted-printable\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nYour authentication code is 123 456.\r\n"""


class OutlookLocalMailParsingTests(unittest.TestCase):
    def test_extract_message_bundle_from_raw_reads_subject_and_code(self):
        provider = OutlookLocalProvider(fixed_account={})
        subject, text, from_header, sent_at = provider._extract_message_bundle_from_raw(RAW_OPENAI_MAIL)

        self.assertIn('Your OpenAI code is 882191', subject)
        self.assertIn('noreply@tm.openai.com', from_header)
        self.assertIsNotNone(sent_at)
        self.assertEqual('882191', provider._extract_code(text))

    def test_extract_code_accepts_spaced_digits_from_raw_mail(self):
        provider = OutlookLocalProvider(fixed_account={})
        _subject, text, _from_header, _sent_at = provider._extract_message_bundle_from_raw(RAW_OPENAI_MAIL_SPACED)
        self.assertEqual('123456', provider._extract_code(text))


if __name__ == '__main__':
    unittest.main()
