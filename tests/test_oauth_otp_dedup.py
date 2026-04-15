import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, 'backend')

from platforms.chatgpt.oauth_client import OAuthClient
from platforms.chatgpt.utils import FlowState


class _FakeMailClient:
    def __init__(self):
        self._used_codes = {'388019'}
        self.saved_code = ''

    def wait_for_verification_code(self, email, timeout=0, otp_sent_at=None, exclude_codes=None):
        return '388019'

    def remember_successful_code(self, code):
        self.saved_code = str(code or '')


class OAuthOtpDedupTests(unittest.TestCase):
    def test_oauth_otp_allows_same_code_in_new_phase(self):
        client = OAuthClient(config={'chatgpt_oauth_otp_wait_seconds': 30}, proxy=None, verbose=False)
        client._get_sentinel_token_with_retry = lambda **kwargs: 'sentinel-token'
        client._headers = lambda *args, **kwargs: {}
        client._browser_pause = lambda *args, **kwargs: None
        client._otp_suspend_settings = lambda prefix: (999, 120, 30)
        client.session = SimpleNamespace(
            post=lambda *args, **kwargs: SimpleNamespace(
                status_code=200,
                json=lambda: {
                    'continue_url': 'https://auth.openai.com/about-you',
                    'method': 'GET',
                    'page': {'type': 'about_you'},
                },
                url='https://auth.openai.com/api/accounts/email-otp/validate',
                text='{}',
            )
        )
        state = FlowState(page_type='email_otp_verification', current_url='https://auth.openai.com/email-verification')
        mail_client = _FakeMailClient()

        next_state = client._handle_otp_verification(
            'child@example.com',
            'device-1',
            None,
            None,
            None,
            mail_client,
            state,
            prefer_passwordless_login=True,
            allow_cached_code_retry=False,
        )

        self.assertIsNotNone(next_state)
        self.assertEqual('about_you', next_state.page_type)
        self.assertEqual('388019', mail_client.saved_code)


if __name__ == '__main__':
    unittest.main()
