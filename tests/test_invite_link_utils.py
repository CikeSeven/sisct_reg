import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, 'backend')

from platforms.chatgpt.invite_link import (
    extract_invite_workspace_ids,
    extract_invite_urls,
    resolve_invite_link,
)


class InviteLinkUtilsTests(unittest.TestCase):
    def test_extract_invite_urls_prefers_direct_chatgpt_login_link(self):
        html = '''<a href="https://chatgpt.com/auth/login?inv_ws_name=TeamWorkspace&inv_email=a%40b.com&wId=wid-1&accept_wId=wid-1">Join</a>'''
        urls = extract_invite_urls(html)
        self.assertEqual('https://chatgpt.com/auth/login?inv_ws_name=TeamWorkspace&inv_email=a%40b.com&wId=wid-1&accept_wId=wid-1', urls[0])

    def test_extract_invite_workspace_ids_reads_wid_and_accept_wid(self):
        payload = extract_invite_workspace_ids('https://chatgpt.com/auth/login?inv_ws_name=TeamWorkspace&inv_email=a%40b.com&wId=wid-1&accept_wId=wid-2')
        self.assertEqual('wid-1', payload['workspace_id'])
        self.assertEqual('wid-2', payload['accept_workspace_id'])

    def test_resolve_invite_link_follows_sendgrid_redirect(self):
        session = Mock()
        response = Mock()
        response.url = 'https://chatgpt.com/auth/login?inv_ws_name=TeamWorkspace&inv_email=a%40b.com&wId=wid-1&accept_wId=wid-1'
        response.status_code = 200
        session.get.return_value = response

        resolved = resolve_invite_link('https://u20216706.ct.sendgrid.net/ls/click?upn=abc', session=session)
        self.assertEqual('https://chatgpt.com/auth/login?inv_ws_name=TeamWorkspace&inv_email=a%40b.com&wId=wid-1&accept_wId=wid-1', resolved)


if __name__ == '__main__':
    unittest.main()
