import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, 'backend')

from platforms.chatgpt.team_manage_style_client import TeamManageStyleClient


class TeamManageStyleClientTests(unittest.TestCase):
    def test_get_members_uses_persistent_impersonated_session(self):
        with patch('platforms.chatgpt.team_manage_style_client.Session') as session_cls:
            session = Mock()
            response = Mock()
            response.status_code = 200
            response.json.return_value = {'items': [{'role': 'standard-user'}], 'total': 1}
            session.get.return_value = response
            session_cls.return_value = session

            client = TeamManageStyleClient(proxy_url='http://127.0.0.1:7890')
            result = client.get_members(access_token='at', account_id='acct-1', identifier='parent@example.com')

        self.assertTrue(result['success'])
        session_cls.assert_called_once()
        self.assertEqual('chrome110', session_cls.call_args.kwargs['impersonate'])
        self.assertEqual({'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}, session_cls.call_args.kwargs['proxies'])
        self.assertEqual('Bearer at', session.get.call_args.kwargs['headers']['Authorization'])

    def test_send_invite_and_accept_invite_use_same_session(self):
        with patch('platforms.chatgpt.team_manage_style_client.Session') as session_cls:
            session = Mock()
            invite_resp = Mock(); invite_resp.status_code = 200; invite_resp.json.return_value = {'account_invites': [{'id': 'inv-1'}], 'errored_emails': []}
            accept_resp = Mock(); accept_resp.status_code = 200; accept_resp.json.return_value = {'success': True}
            session.post.side_effect = [invite_resp, accept_resp]
            session_cls.return_value = session

            client = TeamManageStyleClient(proxy_url='http://127.0.0.1:7890')
            invite = client.send_invite(access_token='at', account_id='acct-1', email='child@example.com', identifier='parent@example.com')
            accept = client.accept_invite(access_token='at', account_id='acct-1', identifier='parent@example.com')

        self.assertTrue(invite['success'])
        self.assertTrue(accept['success'])
        self.assertEqual(2, session.post.call_count)
        self.assertIs(client._sessions['parent@example.com'], session)


if __name__ == '__main__':
    unittest.main()
