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
from platforms.chatgpt.utils import FlowState


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

    def test_complete_chatgpt_web_callback_does_not_require_chatgpt_session_cookie(self):
        client = ChatGPTWebAuthClient(config={'use_proxy': False})
        client.session = SimpleNamespace(
            cookies=[],
            get=lambda *args, **kwargs: SimpleNamespace(status_code=200, url='https://chatgpt.com/'),
        )
        result = client._complete_chatgpt_web_callback(
            'https://chatgpt.com/api/auth/callback/openai?code=cb-code',
            referer='https://auth.openai.com/about-you',
        )
        self.assertTrue(result['success'])
        self.assertTrue(result['callback_completed'])
        self.assertFalse(result['workspace_selected'])
        self.assertEqual([], result['cookie_jar'])

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


    def test_login_and_get_session_can_succeed_without_web_cookie_when_optional(self):
        client = ChatGPTWebAuthClient(config={'use_proxy': False})
        with patch.object(ChatGPTWebAuthClient, 'login_and_get_tokens', return_value={'access_token': 'child-at', 'refresh_token': 'child-rt', 'id_token': 'child-id'}), \
            patch.object(ChatGPTWebAuthClient, '_enrich_child_session', return_value={
                'success': False,
                'access_token': 'child-at',
                'refresh_token': 'child-rt',
                'id_token': 'child-id',
                'account_id': '',
                'selected_workspace_id': 'team-1',
                'next_auth_session_token': '',
                'info': {},
                'error': '缺少 chatgpt.com 会话 Cookie',
            }):
            result = client.login_and_get_session(
                email='child@example.com',
                password='mail-pass',
                require_web_session=False,
            )

        self.assertTrue(result['success'])
        self.assertEqual('team-1', result['selected_workspace_id'])
        self.assertEqual('', result['error'])

    def test_login_and_select_workspace_uses_password_login_then_callback(self):
        client = ChatGPTWebAuthClient(config={'use_proxy': False})
        fake_login_client = SimpleNamespace(
            visit_homepage=lambda: True,
            get_csrf_token=lambda: 'csrf-1',
            signin=lambda email, csrf: 'https://auth.openai.com/api/accounts/authorize?client_id=app_X8',
            authorize=lambda url: 'https://auth.openai.com/log-in/password',
            session=SimpleNamespace(headers={}, cookies=[]),
            device_id='device-1',
            ua='ua-1',
            sec_ch_ua='sec-1',
            impersonate='chrome131',
        )
        with patch.object(client, '_build_chatgpt_web_login_client', return_value=fake_login_client), \
            patch.object(client, 'adopt_browser_context'), \
            patch.object(client, '_submit_password_verify', return_value=FlowState(page_type='workspace', continue_url='https://auth.openai.com/workspace', current_url='https://auth.openai.com/api/accounts/password/verify')), \
            patch.object(client, '_oauth_submit_workspace_and_org', return_value=('cb-code', FlowState(current_url='https://chatgpt.com/api/auth/callback/openai?code=cb-code'))), \
            patch.object(client, '_complete_chatgpt_web_callback', return_value={'success': True, 'account_id': 'team-1', 'next_auth_session_token': 'st'}):
            result = client.login_and_select_workspace(
                email='child@example.com',
                password='mail-pass',
                preferred_workspace_id='team-1',
            )

        self.assertTrue(result['success'])
        self.assertEqual('child@example.com', result['email'])
        self.assertEqual('team-1', result['preferred_workspace_id'])


    def test_login_and_select_workspace_follows_authorize_before_password(self):
        client = ChatGPTWebAuthClient(config={'use_proxy': False})
        fake_login_client = SimpleNamespace(
            visit_homepage=lambda: True,
            get_csrf_token=lambda: 'csrf-1',
            signin=lambda email, csrf: 'https://auth.openai.com/api/accounts/authorize?client_id=app_X8',
            authorize=lambda url: 'https://auth.openai.com/api/accounts/authorize?client_id=app_X8',
            session=SimpleNamespace(headers={}, cookies=[]),
            device_id='device-1',
            ua='ua-1',
            sec_ch_ua='sec-1',
            impersonate='chrome131',
        )
        with patch.object(client, '_build_chatgpt_web_login_client', return_value=fake_login_client), \
            patch.object(client, 'adopt_browser_context'), \
            patch.object(client, '_decode_oauth_session_cookie', return_value={'workspaces': [{'id': 'team-1', 'kind': 'organization'}]}), \
            patch.object(client, '_follow_flow_state', return_value=(None, FlowState(page_type='login_password', current_url='https://auth.openai.com/log-in/password'))), \
            patch.object(client, '_submit_password_verify', return_value=FlowState(page_type='workspace', continue_url='https://auth.openai.com/workspace', current_url='https://auth.openai.com/api/accounts/password/verify')), \
            patch.object(client, '_oauth_submit_workspace_and_org', return_value=('cb-code', FlowState(current_url='https://chatgpt.com/api/auth/callback/openai?code=cb-code'))), \
            patch.object(client, '_complete_chatgpt_web_callback', return_value={'success': True, 'account_id': 'team-1', 'next_auth_session_token': 'st'}):
            result = client.login_and_select_workspace(
                email='child@example.com',
                password='mail-pass',
                preferred_workspace_id='team-1',
            )

        self.assertTrue(result['success'])

    def test_login_and_select_workspace_submits_about_you_before_workspace(self):
        client = ChatGPTWebAuthClient(config={'use_proxy': False})
        fake_login_client = SimpleNamespace(
            visit_homepage=lambda: True,
            get_csrf_token=lambda: 'csrf-1',
            signin=lambda email, csrf: 'https://auth.openai.com/api/accounts/authorize?client_id=app_X8',
            authorize=lambda url: 'https://auth.openai.com/log-in/password',
            session=SimpleNamespace(headers={}, cookies=[]),
            device_id='device-1',
            ua='ua-1',
            sec_ch_ua='sec-1',
            impersonate='chrome131',
        )
        with patch.object(client, '_build_chatgpt_web_login_client', return_value=fake_login_client), \
            patch.object(client, 'adopt_browser_context'), \
            patch.object(client, '_submit_password_verify', return_value=FlowState(page_type='about_you', current_url='https://auth.openai.com/about-you')), \
            patch.object(client, '_submit_about_you_create_account', return_value=FlowState(page_type='workspace', continue_url='https://auth.openai.com/workspace')), \
            patch.object(client, '_oauth_submit_workspace_and_org', return_value=('cb-code', FlowState(current_url='https://chatgpt.com/api/auth/callback/openai?code=cb-code'))), \
            patch.object(client, '_complete_chatgpt_web_callback', return_value={'success': True, 'account_id': 'team-1', 'next_auth_session_token': 'st'}):
            result = client.login_and_select_workspace(
                email='child@example.com',
                password='mail-pass',
                preferred_workspace_id='team-1',
            )

        self.assertTrue(result['success'])

    def test_login_and_select_workspace_allows_direct_callback_without_workspace(self):
        client = ChatGPTWebAuthClient(config={'use_proxy': False})
        fake_login_client = SimpleNamespace(
            visit_homepage=lambda: True,
            get_csrf_token=lambda: 'csrf-1',
            signin=lambda email, csrf: 'https://auth.openai.com/api/accounts/authorize?client_id=app_X8',
            authorize=lambda url: 'https://auth.openai.com/log-in/password',
            session=SimpleNamespace(headers={}, cookies=[]),
            device_id='device-1',
            ua='ua-1',
            sec_ch_ua='sec-1',
            impersonate='chrome131',
        )
        with patch.object(client, '_build_chatgpt_web_login_client', return_value=fake_login_client), \
            patch.object(client, 'adopt_browser_context'), \
            patch.object(client, '_submit_password_verify', return_value=FlowState(page_type='about_you', current_url='https://auth.openai.com/about-you')), \
            patch.object(client, '_submit_about_you_create_account', return_value=FlowState(page_type='external_url', continue_url='https://chatgpt.com/api/auth/callback/openai?code=cb-code')), \
            patch.object(client, '_complete_chatgpt_web_callback', return_value={'success': True, 'account_id': 'team-1', 'next_auth_session_token': 'st'}):
            result = client.login_and_select_workspace(
                email='child@example.com',
                password='mail-pass',
                preferred_workspace_id='team-1',
            )

        self.assertTrue(result['success'])
        self.assertFalse(result['workspace_selected'])
if __name__ == '__main__':
    unittest.main()
