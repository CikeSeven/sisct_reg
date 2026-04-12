import base64
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, 'backend')

from tests.db_isolation import IsolatedCodexTeamDbTestCase
from app.codex_team_manager import resolve_parent_invite_context
from app.codex_team_parent_login import login_parent_via_outlook, parse_outlook_parent_import_data


class CodexTeamParentLoginTests(IsolatedCodexTeamDbTestCase, unittest.TestCase):
    def _jwt(self, payload: dict) -> str:
        header = base64.urlsafe_b64encode(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode()).decode().rstrip('=')
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        return f"{header}.{body}.sig"

    def test_parent_context_prefers_existing_access_token_and_does_not_use_mail_login(self):
        access_token = self._jwt({
            'https://api.openai.com/auth': {'chatgpt_account_id': 'team-acct'},
            'https://api.openai.com/profile': {'email': 'parent@example.com'},
        })
        parent = {
            'email': 'parent@example.com',
            'access_token': access_token,
            'team_account_id': 'team-acct',
        }

        with patch('app.codex_team_manager.build_mail_provider') as provider_factory:
            result = resolve_parent_invite_context(
                parent,
                merged_config={'use_proxy': True, 'proxy': 'http://127.0.0.1:7890'},
                executor_type='protocol',
            )

        self.assertTrue(result['success'])
        self.assertEqual(access_token, result['access_token'])
        self.assertEqual('team-acct', result['account_id'])
        provider_factory.assert_not_called()

    def test_parent_context_can_refresh_with_session_token(self):
        parent = {
            'email': 'parent@example.com',
            'session_token': 'st-parent',
            'team_account_id': 'team-acct',
        }

        with patch('app.codex_team_manager.resolve_parent_invite_context_from_token', return_value={
            'success': True,
            'access_token': 'parent-at',
            'account_id': 'team-acct',
            'session_token': 'st-parent',
        }) as token_helper, \
            patch('app.codex_team_manager.build_mail_provider') as provider_factory:
            result = resolve_parent_invite_context(
                parent,
                merged_config={'use_proxy': True, 'proxy': 'http://127.0.0.1:7890'},
                executor_type='protocol',
            )

        self.assertTrue(result['success'])
        self.assertEqual('parent-at', result['access_token'])
        provider_factory.assert_not_called()
        self.assertEqual('http://127.0.0.1:7890', token_helper.call_args.kwargs['proxy_url'])

    def test_parent_context_force_refresh_prefers_session_token(self):
        parent = {
            'email': 'parent@example.com',
            'access_token': self._jwt({
                'https://api.openai.com/auth': {'chatgpt_account_id': 'team-acct'},
                'https://api.openai.com/profile': {'email': 'parent@example.com'},
            }),
            'session_token': 'st-parent',
            'team_account_id': 'team-acct',
        }

        with patch('app.codex_team_manager.resolve_parent_invite_context_from_token', return_value={
            'success': True,
            'access_token': 'fresh-at',
            'account_id': 'team-acct',
            'session_token': 'st-parent',
        }) as token_helper:
            result = resolve_parent_invite_context(
                parent,
                merged_config={'use_proxy': False},
                executor_type='protocol',
                force_refresh=True,
            )

        self.assertTrue(result['success'])
        self.assertEqual('fresh-at', result['access_token'])
        self.assertTrue(token_helper.call_args.kwargs['force_refresh'])

    def test_parse_outlook_parent_import_data_accepts_outlook_format(self):
        items = parse_outlook_parent_import_data(
            'KatherineHart8767@hotmail.com----ldmhwwxc37664----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C523'
        )
        self.assertEqual(1, len(items))
        self.assertEqual('katherinehart8767@hotmail.com', items[0]['email'])
        self.assertEqual('9e5f94bc-e8a4-4e73-b8be-63364c29d753', items[0]['client_id'])

    def test_login_parent_via_outlook_imports_session_json_into_parent_pool(self):
        class _FakeProvider:
            def create_email(self, config=None):
                return {
                    'email': 'parent@example.com',
                    'account': {
                        'email': 'parent@example.com',
                        'password': 'mail-pass',
                        'client_id': 'client-id',
                        'refresh_token': 'rt',
                    },
                }

        with patch('app.codex_team_parent_login.build_mail_provider', return_value=_FakeProvider()), \
            patch('app.codex_team_parent_login.ChatGPTWebAuthClient') as auth_cls, \
            patch('app.codex_team_parent_login.ChatGPTClient') as session_cls:
            auth = auth_cls.return_value
            auth.login_and_get_tokens.return_value = {
                'access_token': self._jwt({
                    'https://api.openai.com/auth': {'chatgpt_account_id': 'team-acct'},
                    'https://api.openai.com/profile': {'email': 'parent@example.com'},
                }),
                'refresh_token': 'openai-rt',
                'id_token': 'id-token',
            }
            auth.last_workspace_id = 'team-acct'
            session_client = session_cls.return_value
            session_client.get_next_auth_session_token.return_value = 'st-parent'
            session_client.fetch_chatgpt_session.return_value = (True, {
                'accessToken': self._jwt({
                    'https://api.openai.com/auth': {'chatgpt_account_id': 'team-acct'},
                    'https://api.openai.com/profile': {'email': 'parent@example.com'},
                }),
                'sessionToken': 'st-parent',
                'account': {'id': 'team-acct', 'planType': 'team'},
                'info': {'plan_type': 'team'},
            })

            result = login_parent_via_outlook(
                {
                    'email': 'parent@example.com',
                    'password': 'mail-pass',
                    'client_id': 'client-id',
                    'refresh_token': 'ms-rt',
                },
                merged_config={'use_proxy': False},
                executor_type='protocol',
            )

        self.assertTrue(result['success'])
        self.assertEqual('team-acct', result['account_id'])
        self.assertGreater(int(result['parent_id'] or 0), 0)


if __name__ == '__main__':
    unittest.main()
