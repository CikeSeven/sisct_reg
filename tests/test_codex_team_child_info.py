import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, 'backend')

from platforms.chatgpt.chatgpt_web_auth import build_child_session_summary


class CodexTeamChildInfoTests(unittest.TestCase):
    def test_build_child_session_summary_combines_session_and_profile(self):
        summary = build_child_session_summary(
            session_snapshot={
                'account_id': 'acct-team',
                'next_auth_session_token': 'st-1',
                'cookie_jar': [],
                'email': 'child@example.com',
                'selected_workspace_id': 'acct-team',
                'selected_workspace_kind': 'organization',
            },
            access_token='at-1',
            refresh_token='rt-1',
            id_token='id-1',
            account_info={'email': 'child@example.com', 'account_id': 'acct-team', 'claims': {'sub': 'auth0|abc'}},
            me_payload={'id': 'user-123', 'name': 'Child Name', 'email': 'child@example.com'},
            accounts_payload={'accounts': {'acct-team': {'account': {'plan_type': 'team', 'account_user_role': 'standard-user', 'name': 'TeamWorkspace'}}}},
        )
        self.assertEqual('user-123', summary['user_id'])
        self.assertEqual('Child Name', summary['display_name'])
        self.assertEqual('team', summary['plan_type'])
        self.assertEqual('standard-user', summary['account_role'])
        self.assertEqual('at-1', summary['access_token'])
        self.assertEqual('rt-1', summary['refresh_token'])


if __name__ == '__main__':
    unittest.main()
