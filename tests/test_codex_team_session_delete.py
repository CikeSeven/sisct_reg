import sys
import unittest

sys.path.insert(0, 'backend')

from tests.db_isolation import IsolatedCodexTeamDbTestCase
from app.db import create_codex_team_job, insert_codex_team_web_session, list_codex_team_web_sessions
from app.codex_team_manager import codex_team_manager


class CodexTeamSessionDeleteTests(IsolatedCodexTeamDbTestCase, unittest.TestCase):

    def test_delete_session_removes_specific_child_result(self):
        create_codex_team_job('job-1', request_payload={})
        first_id = insert_codex_team_web_session(
            job_id='job-1', email='a@example.com', status='success', selected_workspace_id='acct-1', selected_workspace_kind='organization',
            account_id='acct-1', next_auth_session_token='st1', access_token='at1', refresh_token='rt1', id_token='id1', user_id='u1', display_name='A', info={}, cookie_jar=[], error='',
        )
        second_id = insert_codex_team_web_session(
            job_id='job-1', email='b@example.com', status='success', selected_workspace_id='acct-2', selected_workspace_kind='organization',
            account_id='acct-2', next_auth_session_token='st2', access_token='at2', refresh_token='rt2', id_token='id2', user_id='u2', display_name='B', info={}, cookie_jar=[], error='',
        )

        result = codex_team_manager.delete_session(second_id)

        self.assertTrue(result['ok'])
        items = list_codex_team_web_sessions(job_id='job-1')
        self.assertEqual(1, len(items))
        self.assertEqual(first_id, items[0]['id'])

    def test_delete_sessions_removes_multiple_child_results(self):
        create_codex_team_job('job-1', request_payload={})
        first_id = insert_codex_team_web_session(
            job_id='job-1', email='a@example.com', status='success', selected_workspace_id='acct-1', selected_workspace_kind='organization',
            account_id='acct-1', next_auth_session_token='st1', access_token='at1', refresh_token='rt1', id_token='id1', user_id='u1', display_name='A', info={}, cookie_jar=[], error='',
        )
        second_id = insert_codex_team_web_session(
            job_id='job-1', email='b@example.com', status='success', selected_workspace_id='acct-2', selected_workspace_kind='organization',
            account_id='acct-2', next_auth_session_token='st2', access_token='at2', refresh_token='rt2', id_token='id2', user_id='u2', display_name='B', info={}, cookie_jar=[], error='',
        )
        third_id = insert_codex_team_web_session(
            job_id='job-1', email='c@example.com', status='success', selected_workspace_id='acct-3', selected_workspace_kind='organization',
            account_id='acct-3', next_auth_session_token='st3', access_token='at3', refresh_token='rt3', id_token='id3', user_id='u3', display_name='C', info={}, cookie_jar=[], error='',
        )

        result = codex_team_manager.delete_sessions([first_id, third_id])

        self.assertTrue(result['ok'])
        self.assertEqual(2, result['deleted'])
        items = list_codex_team_web_sessions(job_id='job-1')
        self.assertEqual(1, len(items))
        self.assertEqual(second_id, items[0]['id'])


if __name__ == '__main__':
    unittest.main()
