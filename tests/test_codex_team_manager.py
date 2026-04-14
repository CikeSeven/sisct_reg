import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, 'backend')

from tests.db_isolation import IsolatedCodexTeamDbTestCase
from app.codex_team_manager import CodexTeamManager, CodexTeamParentImportJob
from app.defaults import DEFAULT_CONFIG
from app.db import batch_import_codex_team_parent_accounts, get_codex_team_parent_pool_summary, list_codex_team_web_sessions


class _FakeMailProvider:
    def create_email(self, config=None):
        return {'email': 'child@example.com', 'account': {'email': 'child@example.com', 'password': 'mail-password'}}

    def get_invitation_link(self, email=None, timeout=120, invite_sent_at=None, **kwargs):
        return 'https://chatgpt.com/auth/login?inv_ws_name=TeamWorkspace&inv_email=child%40example.com&wId=parent-acct&accept_wId=parent-acct'


class CodexTeamManagerTests(IsolatedCodexTeamDbTestCase, unittest.TestCase):

    def _make_job(self):
        manager = CodexTeamManager()
        manager.create_job(
            {'parent_credentials': {'email': 'parent@example.com'}, 'child_count': 1, 'concurrency': 1, 'executor_type': 'protocol'},
            merged_config={'use_proxy': False},
            start_immediately=False,
        )
        job_id = next(iter(manager._jobs.keys()))
        return manager, job_id, manager._jobs[job_id]

    def test_process_account_success_persists_session_and_updates_snapshot(self):
        manager, job_id, job = self._make_job()

        with patch('app.codex_team_manager.build_mail_provider', return_value=_FakeMailProvider()), \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls, \
            patch('app.codex_team_manager.RefreshTokenRegistrationEngine') as engine_cls:
            client = client_cls.return_value
            client.send_invite = Mock(return_value={'success': True, 'invite_id': 'invite-1', 'error': ''})
            client.accept_invite = Mock(return_value={'success': True, 'error': ''})
            engine = engine_cls.return_value
            engine.run.return_value = Mock(
                success=True,
                email='child@example.com',
                password='mail-pass',
                account_id='acct-1',
                workspace_id='org-1',
                access_token='child-at',
                refresh_token='child-rt',
                id_token='child-id',
                session_token='child-st',
                error_message='',
                metadata={'plan_type': 'team', 'account_role': 'standard-user'},
            )
            manager._process_account(job, 1, parent_context={'access_token': 'at-1', 'account_id': 'parent-acct', 'email': 'parent@example.com'})

        engine_cls.assert_called_once()
        snapshot = manager.get_job_snapshot(job_id)
        sessions = list_codex_team_web_sessions(job_id=job_id)
        self.assertEqual('done', snapshot['status'])
        self.assertEqual(1, snapshot['success'])
        self.assertEqual(1, len(sessions))
        self.assertEqual('org-1', sessions[0]['selected_workspace_id'])
        self.assertEqual('child-at', sessions[0]['access_token'])

    def test_process_account_failure_without_team_workspace_marks_failed(self):
        manager, job_id, job = self._make_job()

        with patch('app.codex_team_manager.build_mail_provider', return_value=_FakeMailProvider()), \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls, \
            patch('app.codex_team_manager.RefreshTokenRegistrationEngine') as engine_cls:
            client = client_cls.return_value
            client.get_invites = Mock(return_value={'success': True, 'items': [], 'total': 0, 'error': ''})
            client.send_invite = Mock(return_value={'success': True, 'invite_id': 'invite-1', 'error': ''})
            engine = engine_cls.return_value
            engine.run.return_value = Mock(
                success=False,
                email='child@example.com',
                password='mail-pass',
                account_id='',
                workspace_id='',
                access_token='',
                refresh_token='',
                id_token='',
                session_token='',
                error_message='没有可用的 team workspace',
                metadata={},
            )
            manager._process_account(job, 1, parent_context={'access_token': 'at-1', 'account_id': 'parent-acct', 'email': 'parent@example.com'})

        snapshot = manager.get_job_snapshot(job_id)
        sessions = list_codex_team_web_sessions(job_id=job_id)
        self.assertEqual('done', snapshot['status'])
        self.assertEqual(0, snapshot['success'])
        self.assertEqual(1, snapshot['failed'])
        self.assertIn('team workspace', sessions[0]['error'])

    def test_process_account_invite_failure_marks_failed_without_login(self):
        manager, job_id, job = self._make_job()

        with patch('app.codex_team_manager.build_mail_provider', return_value=_FakeMailProvider()), \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls, \
            patch('app.codex_team_manager.RefreshTokenRegistrationEngine') as engine_cls:
            client = client_cls.return_value
            client.get_invites = Mock(return_value={'success': True, 'items': [], 'total': 0, 'error': ''})
            client.send_invite = Mock(return_value={'success': False, 'invite_id': '', 'error': 'invite denied'})
            manager._process_account(job, 1, parent_context={'access_token': 'at-1', 'account_id': 'parent-acct', 'email': 'parent@example.com'})

        snapshot = manager.get_job_snapshot(job_id)
        sessions = list_codex_team_web_sessions(job_id=job_id)
        self.assertEqual(0, snapshot['success'])
        self.assertEqual(1, snapshot['failed'])
        self.assertIn('invite denied', sessions[0]['error'])
        engine_cls.assert_not_called()

    def test_process_account_reuses_existing_pending_invite_after_send_failure(self):
        manager, job_id, job = self._make_job()

        with patch('app.codex_team_manager.build_mail_provider', return_value=_FakeMailProvider()), \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls, \
            patch('app.codex_team_manager.RefreshTokenRegistrationEngine') as engine_cls:
            client = client_cls.return_value
            client.get_invites = Mock(side_effect=[
                {'success': True, 'items': [], 'total': 0, 'error': ''},
                {'success': True, 'items': [{'id': 'inv-existing', 'email_address': 'child@example.com'}], 'total': 1, 'error': ''},
            ])
            client.send_invite = Mock(return_value={'success': False, 'invite_id': '', 'error': 'invite denied'})
            client.accept_invite = Mock(return_value={'success': True, 'error': ''})
            engine = engine_cls.return_value
            engine.run.return_value = Mock(
                success=True,
                email='child@example.com',
                password='mail-pass',
                account_id='acct-1',
                workspace_id='org-1',
                access_token='child-at',
                refresh_token='child-rt',
                id_token='child-id',
                session_token='child-st',
                error_message='',
                metadata={'plan_type': 'team', 'account_role': 'standard-user'},
            )
            manager._process_account(job, 1, parent_context={'access_token': 'at-1', 'account_id': 'parent-acct', 'email': 'parent@example.com'})

        snapshot = manager.get_job_snapshot(job_id)
        self.assertEqual(1, snapshot['success'])
        self.assertEqual(0, snapshot['failed'])
        engine_cls.assert_called_once()

    def test_process_account_retries_registration_on_http_429(self):
        manager, job_id, job = self._make_job()

        with patch('app.codex_team_manager.build_mail_provider', return_value=_FakeMailProvider()), \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls, \
            patch('app.codex_team_manager.RefreshTokenRegistrationEngine') as engine_cls, \
            patch('app.codex_team_manager.time.sleep', return_value=None):
            client = client_cls.return_value
            client.get_invites = Mock(return_value={'success': True, 'items': [], 'total': 0, 'error': ''})
            client.send_invite = Mock(return_value={'success': True, 'invite_id': 'invite-1', 'error': ''})
            client.accept_invite = Mock(return_value={'success': True, 'error': ''})
            engine = engine_cls.return_value
            engine.run.side_effect = [
                Mock(
                    success=False,
                    email='child@example.com',
                    password='mail-pass',
                    account_id='',
                    workspace_id='',
                    access_token='',
                    refresh_token='',
                    id_token='',
                    session_token='',
                    error_message='注册状态机失败: 注册失败: HTTP 429: Just a moment...',
                    metadata={},
                ),
                Mock(
                    success=True,
                    email='child@example.com',
                    password='mail-pass',
                    account_id='acct-1',
                    workspace_id='org-1',
                    access_token='child-at',
                    refresh_token='child-rt',
                    id_token='child-id',
                    session_token='child-st',
                    error_message='',
                    metadata={'plan_type': 'team', 'account_role': 'standard-user'},
                ),
            ]
            manager._process_account(job, 1, parent_context={'access_token': 'at-1', 'account_id': 'parent-acct', 'email': 'parent@example.com'})

        snapshot = manager.get_job_snapshot(job_id)
        self.assertEqual(1, snapshot['success'])
        self.assertEqual(0, snapshot['failed'])
        self.assertEqual(2, engine.run.call_count)




    def test_parent_import_job_syncs_child_count_after_login(self):
        manager = CodexTeamManager()
        job = CodexTeamParentImportJob(
            id='parent-import-1',
            raw_data='parent@example.com----mail-pass----123e4567-e89b-12d3-a456-426614174000----M.C523',
            merged_config={'use_proxy': False},
            executor_type='protocol',
        )

        def fake_login_parent(*args, **kwargs):
            import_result = batch_import_codex_team_parent_accounts(
                'parent@example.com----eyJaaa.bbb.ccc----123e4567-e89b-12d3-a456-426614174000',
                enabled=True,
            )
            parent_id = int(import_result['summary']['items'][0]['id'])
            return {
                'success': True,
                'parent_id': parent_id,
                'account_id': '123e4567-e89b-12d3-a456-426614174000',
            }

        with patch('app.codex_team_manager.login_parent_via_outlook', side_effect=fake_login_parent), \
            patch('app.codex_team_manager.resolve_parent_invite_context', return_value={
                'success': True,
                'email': 'parent@example.com',
                'access_token': 'at-parent',
                'account_id': '123e4567-e89b-12d3-a456-426614174000',
                'team_name': 'Team Workspace',
            }), \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls:
            client = client_cls.return_value
            client.get_members = Mock(return_value={
                'success': True,
                'members': [
                    {'role': 'account-owner'},
                    {'role': 'standard-user'},
                ],
                'total': 2,
            })
            manager._run_parent_import_job(job)

        summary = get_codex_team_parent_pool_summary()
        self.assertEqual('done', job.status)
        self.assertEqual(1, job.success)
        self.assertEqual(1, summary['items'][0]['child_member_count'])
        self.assertTrue(any('当前子号数=1' in str(item.get('message') or '') for item in job.events))

    def test_default_register_otp_wait_seconds_is_60(self):
        self.assertEqual(60, int(DEFAULT_CONFIG['chatgpt_register_otp_wait_seconds']))

    def test_parent_pool_job_stops_when_outlook_pool_is_empty(self):
        manager = CodexTeamManager()
        job_id = manager.create_job(
            {
                'parent_source': 'pool',
                'target_children_per_parent': 5,
                'max_parent_accounts': 1,
                'executor_type': 'protocol',
            },
            merged_config={'use_proxy': False},
            start_immediately=False,
        )
        job = manager._jobs[job_id]

        class _EmptyMailProvider:
            def create_email(self, config=None):
                raise RuntimeError('本地微软邮箱池为空，请先导入 Outlook 账号')

        with patch('app.codex_team_manager.list_enabled_codex_team_parent_accounts', return_value=[{'id': 1, 'email': 'parent@example.com'}]), \
            patch('app.codex_team_manager.resolve_parent_invite_context', return_value={'success': True, 'access_token': 'at', 'account_id': 'acct', 'email': 'parent@example.com'}), \
            patch('app.codex_team_manager.build_mail_provider', return_value=_EmptyMailProvider()), \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls:
            client = client_cls.return_value
            client.get_members.return_value = {'success': True, 'members': []}
            manager._run_parent_pool_job(job)

        snapshot = manager.get_job_snapshot(job_id)
        self.assertTrue(job.stop_requested)
        self.assertEqual('failed', snapshot['status'])
        self.assertEqual(1, snapshot['failed'])
        self.assertEqual(1, len(snapshot['sessions']))
        self.assertIn('邮箱池为空', snapshot['sessions'][0]['error'])


if __name__ == '__main__':
    unittest.main()
