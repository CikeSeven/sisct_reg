import sqlite3
import sys
import unittest

sys.path.insert(0, 'backend')

from app.db import DB_PATH, create_codex_team_job, init_db, insert_codex_team_web_session, list_codex_team_web_sessions
from app.codex_team_manager import codex_team_manager


class CodexTeamSessionDeleteTests(unittest.TestCase):
    def setUp(self):
        init_db()
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute('DELETE FROM codex_team_web_sessions')
            conn.execute('DELETE FROM codex_team_job_events')
            conn.execute('DELETE FROM codex_team_jobs')
            conn.commit()
        finally:
            conn.close()

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


if __name__ == '__main__':
    unittest.main()
