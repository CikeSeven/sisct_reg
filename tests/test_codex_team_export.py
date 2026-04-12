import io
import json
import re
import sys
import unittest
import zipfile

sys.path.insert(0, 'backend')

from tests.db_isolation import IsolatedCodexTeamDbTestCase
from app.db import create_codex_team_job, insert_codex_team_web_session
from app.codex_team_manager import codex_team_manager


class CodexTeamExportTests(IsolatedCodexTeamDbTestCase, unittest.TestCase):

    def test_export_cpa_bundle_contains_success_accounts_only(self):
        create_codex_team_job('job-1', request_payload={})
        success_id = insert_codex_team_web_session(
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

    def test_export_cpa_bundle_can_limit_to_selected_session_ids(self):
        create_codex_team_job('job-1', request_payload={})
        selected_id = insert_codex_team_web_session(
            job_id='job-1',
            email='selected@example.com',
            status='success',
            selected_workspace_id='acct-1',
            selected_workspace_kind='organization',
            account_id='acct-1',
            next_auth_session_token='st1',
            access_token='at1',
            refresh_token='rt1',
            id_token='id1',
            user_id='user-1',
            display_name='Selected User',
            info={'plan_type': 'team'},
            cookie_jar=[],
            error='',
        )
        insert_codex_team_web_session(
            job_id='job-1',
            email='other@example.com',
            status='success',
            selected_workspace_id='acct-2',
            selected_workspace_kind='organization',
            account_id='acct-2',
            next_auth_session_token='st2',
            access_token='at2',
            refresh_token='rt2',
            id_token='id2',
            user_id='user-2',
            display_name='Other User',
            info={'plan_type': 'team'},
            cookie_jar=[],
            error='',
        )

        result = codex_team_manager.export_cpa_bundle(job_id='job-1', session_ids=[selected_id])

        self.assertTrue(result['ok'])
        archive = zipfile.ZipFile(io.BytesIO(result['content']))
        names = sorted(archive.namelist())
        self.assertEqual(1, len(names))
        payload = json.loads(archive.read(names[0]).decode())
        self.assertEqual('selected@example.com', payload[0]['email'])
        self.assertEqual('at1', payload[0]['access_token'])


if __name__ == '__main__':
    unittest.main()
