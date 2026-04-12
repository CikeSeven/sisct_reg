import io
import json
import re
import sqlite3
import sys
import unittest
import zipfile

sys.path.insert(0, 'backend')

from app.db import DB_PATH, create_codex_team_job, init_db, insert_codex_team_web_session
from app.codex_team_manager import codex_team_manager


class CodexTeamExportTests(unittest.TestCase):
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

    def test_export_cpa_bundle_contains_success_accounts_only(self):
        create_codex_team_job('job-1', request_payload={})
        insert_codex_team_web_session(
            job_id='job-1',
            email='ok@example.com',
            status='success',
            selected_workspace_id='acct-1',
            selected_workspace_kind='organization',
            account_id='acct-1',
            next_auth_session_token='st',
            access_token='at',
            refresh_token='rt',
            id_token='id',
            user_id='user-1',
            display_name='Ok User',
            info={'plan_type': 'team'},
            cookie_jar=[],
            error='',
        )
        insert_codex_team_web_session(
            job_id='job-1',
            email='bad@example.com',
            status='failed',
            selected_workspace_id='',
            selected_workspace_kind='',
            account_id='',
            next_auth_session_token='',
            access_token='',
            refresh_token='',
            id_token='',
            user_id='',
            display_name='',
            info={},
            cookie_jar=[],
            error='boom',
        )

        result = codex_team_manager.export_cpa_bundle(job_id='job-1')

        self.assertTrue(result['ok'])
        archive = zipfile.ZipFile(io.BytesIO(result['content']))
        names = sorted(archive.namelist())
        self.assertEqual(1, len(names))
        self.assertRegex(names[0], r'^CPA/cpa-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-1\.json$')
        payload = json.loads(archive.read(names[0]).decode())
        self.assertIsInstance(payload, list)
        self.assertEqual('ok@example.com', payload[0]['email'])
        self.assertEqual('at', payload[0]['access_token'])


if __name__ == '__main__':
    unittest.main()
