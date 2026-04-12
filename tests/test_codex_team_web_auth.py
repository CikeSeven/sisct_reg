import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, 'backend')

from platforms.chatgpt.chatgpt_web_auth import (
    ChatGPTWebAuthClient,
    choose_team_workspace,
    extract_web_session_from_cookies,
)


class CodexTeamWebAuthTests(unittest.TestCase):
    def test_choose_team_workspace_prefers_organization(self):
        workspace = choose_team_workspace(
            [
                {'id': 'personal-1', 'kind': 'personal', 'name': 'Me'},
                {'id': 'org-1', 'kind': 'organization', 'name': 'Team A'},
            ]
        )

        self.assertEqual('org-1', workspace['id'])
        self.assertEqual('organization', workspace['kind'])

    def test_choose_team_workspace_returns_none_when_no_organization(self):
        workspace = choose_team_workspace(
            [
                {'id': 'personal-1', 'kind': 'personal', 'name': 'Me'},
            ]
        )

        self.assertIsNone(workspace)

    def test_extract_web_session_from_cookies_merges_chunked_next_auth(self):
        cookies = [
            SimpleNamespace(name='oai-did', value='device-1', domain='.chatgpt.com', path='/'),
            SimpleNamespace(name='_account', value='acct-123', domain='chatgpt.com', path='/'),
            SimpleNamespace(name='__Secure-next-auth.session-token.0', value='part-aaa', domain='.chatgpt.com', path='/'),
            SimpleNamespace(name='__Secure-next-auth.session-token.1', value='part-bbb', domain='.chatgpt.com', path='/'),
            SimpleNamespace(name='_account_residency_region', value='no_constraint', domain='chatgpt.com', path='/'),
        ]

        snapshot = extract_web_session_from_cookies(cookies)

        self.assertEqual('acct-123', snapshot['account_id'])
        self.assertEqual('part-aaapart-bbb', snapshot['next_auth_session_token'])
        self.assertEqual('device-1', snapshot['device_id'])
        self.assertEqual('no_constraint', snapshot['account_residency_region'])
        self.assertEqual(5, len(snapshot['cookie_jar']))

    def test_login_and_get_session_uses_oauth_client_login_flow_for_child(self):
        client = ChatGPTWebAuthClient(config={'use_proxy': False})
        fake_provider = object()
        with patch.object(ChatGPTWebAuthClient, 'login_and_get_tokens', return_value={'access_token': 'child-at', 'refresh_token': 'child-rt', 'id_token': 'child-id'}) as login_mock, \
            patch.object(ChatGPTWebAuthClient, '_enrich_child_session', return_value={'success': True, 'account_id': 'team-1', 'next_auth_session_token': 'st', 'selected_workspace_id': 'team-1', 'selected_workspace_kind': 'organization', 'info': {}}):
            result = client.login_and_get_session(
                email='child@example.com',
                password='mail-pass',
                skymail_client=fake_provider,
                executor_type='protocol',
                invite_link='https://chatgpt.com/auth/login?inv_ws_name=TeamWorkspace&inv_email=child%40example.com&wId=team-1&accept_wId=team-1',
            )

        self.assertTrue(result['success'])
        _, kwargs = login_mock.call_args
        self.assertEqual('child@example.com', kwargs['email'])
        self.assertEqual(fake_provider, kwargs['skymail_client'])
        self.assertTrue(kwargs['force_chatgpt_entry'])
        self.assertTrue(kwargs['prefer_passwordless_login'])
        self.assertTrue(kwargs['complete_about_you_if_needed'])


if __name__ == '__main__':
    unittest.main()
