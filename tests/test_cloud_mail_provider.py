import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, 'backend')

from app.mail_providers import CloudMailProvider, build_mail_provider


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class CloudMailProviderTests(unittest.TestCase):
    def setUp(self):
        CloudMailProvider._shared_tokens.clear()
        CloudMailProvider._shared_seen_ids.clear()

    def test_build_mail_provider_supports_cloud_mail(self):
        provider = build_mail_provider(
            'cloud_mail',
            config={
                'cloud_mail_base_url': 'https://mail.example.com',
                'cloud_mail_admin_email': 'admin@example.com',
                'cloud_mail_admin_password': 'secret',
                'cloud_mail_domain': 'example.com',
                'cloud_mail_subdomain': 'mx',
            },
        )

        self.assertIsInstance(provider, CloudMailProvider)
        created = provider.create_email({'name': 'demo'})
        self.assertEqual('demo@mx.example.com', created['email'])
        self.assertEqual('demo@mx.example.com', created['account']['email'])

    def test_get_verification_code_reads_cloud_mail_messages(self):
        provider = CloudMailProvider(
            base_url='https://mail.example.com',
            admin_email='admin@example.com',
            admin_password='secret',
            domain='example.com',
        )
        sent_at = time.time() - 5

        with patch(
            'app.mail_providers.tracked_request',
            side_effect=[
                _FakeResponse({'code': 200, 'data': {'token': 'cm-token'}}),
                _FakeResponse(
                    {
                        'code': 200,
                        'data': [
                            {
                                'emailId': 'msg-1',
                                'toEmail': 'user@example.com',
                                'subject': 'Your OpenAI code is 654321',
                                'content': '<p>654321</p>',
                                'sendTime': int((sent_at + 1) * 1000),
                            }
                        ],
                    }
                ),
            ],
        ):
            code = provider.get_verification_code(
                email='user@example.com',
                timeout=5,
                otp_sent_at=sent_at,
                exclude_codes=set(),
            )

        self.assertEqual('654321', code)

    def test_get_verification_code_prefers_contextual_code_over_hidden_digits(self):
        provider = CloudMailProvider(
            base_url='https://mail.example.com',
            admin_email='admin@example.com',
            admin_password='secret',
            domain='example.com',
        )
        sent_at = time.time() - 5

        with patch(
            'app.mail_providers.tracked_request',
            side_effect=[
                _FakeResponse({'code': 200, 'data': {'token': 'cm-token'}}),
                _FakeResponse(
                    {
                        'code': 200,
                        'data': [
                            {
                                'emailId': 'msg-ctx',
                                'toEmail': 'user@example.com',
                                'sendEmail': 'noreply@tm.openai.com',
                                'sendName': 'OpenAI',
                                'subject': 'Your temporary ChatGPT verification code',
                                'content': '<div style="display:none">202123</div><p>Your temporary ChatGPT verification code is <strong>654321</strong></p>',
                                'sendTime': int((sent_at + 1) * 1000),
                            }
                        ],
                    }
                ),
            ],
        ):
            code = provider.get_verification_code(
                email='user@example.com',
                timeout=5,
                otp_sent_at=sent_at,
                exclude_codes=set(),
            )

        self.assertEqual('654321', code)


if __name__ == '__main__':
    unittest.main()
