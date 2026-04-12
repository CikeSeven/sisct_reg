import base64
import json
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, 'backend')

from platforms.chatgpt.team_invite import (
    accept_team_invite,
    extract_account_id_from_access_token,
    get_team_child_member_count,
    refresh_parent_access_token,
    send_team_invite,
)


class CodexTeamInviteTests(unittest.TestCase):
    def _jwt(self, payload: dict) -> str:
        header = base64.urlsafe_b64encode(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode()).decode().rstrip('=')
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        return f"{header}.{body}.sig"

    def test_extract_account_id_from_access_token_reads_chatgpt_claim(self):
        token = self._jwt({
            'https://api.openai.com/auth': {
                'chatgpt_account_id': 'acct-123',
            }
        })
        self.assertEqual('acct-123', extract_account_id_from_access_token(token))

    def test_send_team_invite_posts_expected_payload(self):
        session = Mock()
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            'account_invites': [{'id': 'invite-1', 'email_address': 'child@example.com'}],
            'errored_emails': [],
        }
        session.post.return_value = response

        result = send_team_invite(
            access_token='token-1',
            account_id='acct-1',
            email='child@example.com',
            session=session,
        )

        self.assertTrue(result['success'])
        self.assertEqual('invite-1', result['invite_id'])
        _, kwargs = session.post.call_args
        self.assertEqual('Bearer token-1', kwargs['headers']['Authorization'])
        self.assertEqual('acct-1', kwargs['headers']['chatgpt-account-id'])
        self.assertEqual(
            {
                'email_addresses': ['child@example.com'],
                'role': 'standard-user',
                'seat_type': 'default',
                'resend_emails': True,
            },
            kwargs['json'],
        )

    def test_refresh_parent_access_token_passes_proxy(self):
        with unittest.mock.patch('platforms.chatgpt.team_invite.requests.post') as post_mock:
            response = Mock()
            response.status_code = 200
            response.content = b'{}'
            response.json.return_value = {'access_token': 'at-1', 'refresh_token': 'rt-2'}
            post_mock.return_value = response

            result = refresh_parent_access_token(client_id='cid', refresh_token='rt', proxy_url='http://127.0.0.1:7890')

        self.assertTrue(result['success'])
        self.assertEqual({'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}, post_mock.call_args.kwargs['proxies'])

    def test_send_team_invite_applies_proxy_to_session(self):
        with unittest.mock.patch('platforms.chatgpt.team_invite.requests.Session') as session_cls:
            session = Mock()
            response = Mock()
            response.status_code = 200
            response.content = b'{}'
            response.json.return_value = {'account_invites': [{'id': 'invite-1'}], 'errored_emails': []}
            session.post.return_value = response
            session_cls.return_value = session

            result = send_team_invite(access_token='token-1', account_id='acct-1', email='child@example.com', proxy_url='http://127.0.0.1:7890')

        self.assertTrue(result['success'])
        self.assertEqual({'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}, session.proxies)

    def test_accept_and_count_apply_proxy_to_session(self):
        with unittest.mock.patch('platforms.chatgpt.team_invite.requests.Session') as session_cls:
            session = Mock()
            ok_response = Mock()
            ok_response.status_code = 200
            ok_response.content = b'{"success": true}'
            ok_response.json.return_value = {'success': True}
            list_response = Mock()
            list_response.status_code = 200
            list_response.content = b'{"items": [], "total": 0}'
            list_response.json.return_value = {'items': [], 'total': 0}
            session.post.return_value = ok_response
            session.get.return_value = list_response
            session_cls.return_value = session

            accept_res = accept_team_invite(access_token='at', account_id='acct', proxy_url='http://127.0.0.1:7890')
            count_res = get_team_child_member_count(access_token='at', account_id='acct', proxy_url='http://127.0.0.1:7890')

        self.assertTrue(accept_res['success'])
        self.assertTrue(count_res['success'])
        self.assertEqual({'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}, session.proxies)


if __name__ == '__main__':
    unittest.main()
