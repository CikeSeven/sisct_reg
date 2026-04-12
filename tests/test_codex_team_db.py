import json
import sys
import unittest

sys.path.insert(0, 'backend')

from tests.db_isolation import IsolatedCodexTeamDbTestCase
from app.db import (
    DB_PATH,
    append_codex_team_job_event,
    create_codex_team_job,
    get_codex_team_job,
    init_db,
    insert_codex_team_web_session,
    list_codex_team_job_events,
    list_codex_team_web_sessions,
    update_codex_team_job,
)


class CodexTeamDbTests(IsolatedCodexTeamDbTestCase, unittest.TestCase):

    def test_create_and_update_job_snapshot(self):
        create_codex_team_job('job-1', request_payload={'child_count': 3, 'concurrency': 2})
        update_codex_team_job('job-1', status='running', total=3, success=1, failed=1, progress='2/3')

        snapshot = get_codex_team_job('job-1')

        self.assertEqual('running', snapshot['status'])
        self.assertEqual(3, snapshot['total'])
        self.assertEqual(1, snapshot['success'])
        self.assertEqual(1, snapshot['failed'])
        self.assertEqual('2/3', snapshot['progress'])
        self.assertEqual(3, snapshot['request_json']['child_count'])

    def test_events_are_sequenced(self):
        create_codex_team_job('job-2', request_payload={})
        append_codex_team_job_event('job-2', 'first')
        append_codex_team_job_event('job-2', 'second', account_email='a@example.com')

        events = list_codex_team_job_events('job-2')

        self.assertEqual([1, 2], [item['seq'] for item in events])
        self.assertEqual('a@example.com', events[1]['account_email'])

    def test_insert_and_list_web_sessions(self):
        create_codex_team_job('job-3', request_payload={})
        insert_codex_team_web_session(
            job_id='job-3',
            email='child@example.com',
            status='success',
            selected_workspace_id='org-1',
            selected_workspace_kind='organization',
            account_id='acct-1',
            next_auth_session_token='token-1',
            cookie_jar=[{'name': '_account', 'value': 'acct-1'}],
            error='',
        )

        items = list_codex_team_web_sessions(job_id='job-3')

        self.assertEqual(1, len(items))
        self.assertEqual('child@example.com', items[0]['email'])
        self.assertEqual('org-1', items[0]['selected_workspace_id'])
        self.assertEqual('organization', items[0]['selected_workspace_kind'])
        self.assertEqual('_account', items[0]['cookie_jar'][0]['name'])


if __name__ == '__main__':
    unittest.main()
