import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, 'backend')

from platforms.chatgpt.refresh_token_registration_engine import RefreshTokenRegistrationEngine


class RefreshTokenRegistrationEngineTests(unittest.TestCase):
    def test_create_email_reuses_prefetched_email_info(self):
        email_service = Mock()
        email_service.service_type = SimpleNamespace(value='outlook_local')
        email_service.create_email.side_effect = AssertionError('should not create a second mailbox')

        engine = RefreshTokenRegistrationEngine(email_service=email_service)
        engine.email = 'child@example.com'
        engine.email_info = {
            'email': 'child@example.com',
            'account': {'email': 'child@example.com', 'refresh_token': 'rt-1'},
        }

        self.assertTrue(engine._create_email())
        email_service.create_email.assert_not_called()
        self.assertEqual('child@example.com', engine.email)
        self.assertEqual('child@example.com', engine.email_info['account']['email'])

    def test_run_can_stop_after_register_before_oauth(self):
        email_service = Mock()
        email_service.service_type = SimpleNamespace(value='outlook_local')
        email_service.create_email.side_effect = AssertionError('should reuse prefetched mailbox')

        engine = RefreshTokenRegistrationEngine(
            email_service=email_service,
            extra_config={'codex_team_register_only_before_invite': True},
        )
        engine.email = 'child@example.com'
        engine.password = 'chatgpt-pass'
        engine.email_info = {
            'email': 'child@example.com',
            'account': {'email': 'child@example.com', 'refresh_token': 'rt-1'},
        }

        register_client = Mock()
        register_client.register_complete_flow.return_value = (True, 'pending_about_you_submission')
        register_client.last_stage = 'about_you'
        engine._build_chatgpt_client = Mock(return_value=register_client)
        engine._build_oauth_client = Mock(side_effect=AssertionError('oauth should not run before invite'))

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual('child@example.com', result.email)
        self.assertEqual('chatgpt-pass', result.password)
        self.assertTrue(result.metadata['register_only_before_invite'])
        engine._build_oauth_client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
