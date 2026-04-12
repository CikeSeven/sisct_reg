import base64
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, 'backend')

from app.codex_team_manager import resolve_parent_invite_context


class CodexTeamParentLoginTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
