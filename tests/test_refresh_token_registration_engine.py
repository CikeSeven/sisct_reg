import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, 'backend')

from platforms.chatgpt.refresh_token_registration_engine import RefreshTokenRegistrationEngine
from app.external_uploads import generate_cpa_token_json


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
        register_client.reuse_session_and_get_tokens.return_value = (
            True,
            {
                'access_token': 'chatgpt-at',
                'session_token': 'chatgpt-st',
                'account_id': 'acct-1',
                'workspace_id': 'ws-1',
            },
        )
        register_client.last_stage = 'about_you'
        register_client.session = object()
        engine._build_chatgpt_client = Mock(return_value=register_client)
        engine._build_oauth_client = Mock(side_effect=AssertionError('oauth should not run before invite'))

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual('child@example.com', result.email)
        self.assertEqual('chatgpt-pass', result.password)
        self.assertEqual('chatgpt-at', result.access_token)
        self.assertEqual('chatgpt-st', result.session_token)
        self.assertEqual('acct-1', result.account_id)
        self.assertEqual('ws-1', result.workspace_id)
        self.assertEqual('', result.refresh_token)
        self.assertTrue(result.metadata['register_only_before_invite'])
        self.assertTrue(result.metadata['refresh_token_skipped'])
        register_client.reuse_session_and_get_tokens.assert_called_once()
        _, register_kwargs = register_client.register_complete_flow.call_args
        self.assertFalse(register_kwargs['stop_before_about_you_submission'])
        engine._build_oauth_client.assert_not_called()


    def test_run_can_stop_after_register_when_registration_mode_is_register_only(self):
        email_service = Mock()
        email_service.service_type = SimpleNamespace(value='outlook_local')
        email_service.create_email.side_effect = AssertionError('should reuse prefetched mailbox')

        engine = RefreshTokenRegistrationEngine(
            email_service=email_service,
            extra_config={'registration_mode': 'register_only'},
        )
        engine.email = 'child@example.com'
        engine.password = 'chatgpt-pass'
        engine.email_info = {
            'email': 'child@example.com',
            'account': {'email': 'child@example.com', 'refresh_token': 'rt-1'},
        }

        register_client = Mock()
        register_client.register_complete_flow.return_value = (True, 'pending_about_you_submission')
        register_client.reuse_session_and_get_tokens.return_value = (
            True,
            {
                'access_token': 'chatgpt-at-2',
                'sessionToken': 'chatgpt-st-2',
                'account_id': 'acct-2',
                'workspace_id': 'ws-2',
            },
        )
        register_client.last_stage = 'about_you'
        register_client.session = object()
        engine._build_chatgpt_client = Mock(return_value=register_client)
        engine._build_oauth_client = Mock(side_effect=AssertionError('oauth should not run in register_only mode'))

        result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual('child@example.com', result.email)
        self.assertEqual('chatgpt-pass', result.password)
        self.assertEqual('chatgpt-at-2', result.access_token)
        self.assertEqual('chatgpt-st-2', result.session_token)
        self.assertEqual('acct-2', result.account_id)
        self.assertEqual('ws-2', result.workspace_id)
        self.assertEqual('', result.refresh_token)
        self.assertTrue(result.metadata['register_only_before_invite'])
        self.assertEqual('register_only', result.metadata['registration_mode'])
        self.assertTrue(result.metadata['refresh_token_skipped'])
        register_client.reuse_session_and_get_tokens.assert_called_once()
        _, register_kwargs = register_client.register_complete_flow.call_args
        self.assertFalse(register_kwargs['stop_before_about_you_submission'])
        engine._build_oauth_client.assert_not_called()

    def test_cpa_token_json_includes_sessionToken_when_refresh_token_skipped(self):
        payload = generate_cpa_token_json(
            SimpleNamespace(
                email='child@example.com',
                access_token='',
                refresh_token='',
                id_token='',
                account_id='acct-1',
                session_token='chatgpt-st',
            )
        )

        self.assertEqual('chatgpt-st', payload['sessionToken'])

        with_refresh_token = generate_cpa_token_json(
            SimpleNamespace(
                email='child@example.com',
                access_token='',
                refresh_token='oauth-rt',
                id_token='',
                account_id='acct-1',
                session_token='chatgpt-st',
            )
        )
        self.assertNotIn('sessionToken', with_refresh_token)
if __name__ == '__main__':
    unittest.main()
