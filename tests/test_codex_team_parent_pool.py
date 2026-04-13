import json
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, 'backend')

from tests.db_isolation import IsolatedCodexTeamDbTestCase
from app.codex_team_manager import CodexTeamManager
from app.db import (
    DB_PATH,
    batch_import_codex_team_parent_accounts,
    get_codex_team_parent_pool_summary,
    init_db,
    list_codex_team_web_sessions,
)


class _FakeChildProvider:
    index = 0

    def create_email(self, config=None):
        type(self).index += 1
        email = f'child{type(self).index}@example.com'
        return {'email': email, 'account': {'email': email, 'password': 'child-pass'}}

    def get_invitation_link(self, email=None, timeout=120, invite_sent_at=None, **kwargs):
        return f'https://chatgpt.com/auth/login?inv_ws_name=TeamWorkspace&inv_email={email}&wId=team-1&accept_wId=team-1'


class CodexTeamParentPoolTests(IsolatedCodexTeamDbTestCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        _FakeChildProvider.index = 0

    def test_import_parent_pool_uses_outlook_line_format(self):
        result = batch_import_codex_team_parent_accounts(
            'parent@example.com----mail-pass----client-id----refresh-token',
            enabled=True,
        )

        self.assertEqual(1, result['success'])
        summary = get_codex_team_parent_pool_summary()
        self.assertEqual(1, summary['total'])
        self.assertEqual('parent@example.com', summary['items'][0]['email'])
        self.assertTrue(summary['items'][0]['has_oauth'])

    def test_import_parent_pool_supports_chatgpt_session_json(self):
        payload = {
            'user': {'email': 'parent@example.com'},
            'account': {'id': '123e4567-e89b-12d3-a456-426614174000', 'planType': 'team'},
            'accessToken': 'eyJaccess.payload.sig',
            'sessionToken': 'eyJsession.payload.sig',
        }
        result = batch_import_codex_team_parent_accounts(json.dumps(payload, indent=2), enabled=True)

        self.assertEqual(1, result['success'])
        item = result['summary']['items'][0]
        self.assertEqual('parent@example.com', item['email'])
        self.assertEqual('123e4567-e89b-12d3-a456-426614174000', item['team_account_id'])
        self.assertTrue(item['has_access_token'])
        self.assertTrue(item['has_session_token'])

    def test_import_parent_pool_supports_team_manage_style_tokens(self):
        token = 'eyJaaa.bbb.ccc'
        session_token = 'eyJsession.abc.def'
        result = batch_import_codex_team_parent_accounts(
            f'parent@example.com----{token}----123e4567-e89b-12d3-a456-426614174000----{session_token}----rt_token_value----app_ABC123',
            enabled=True,
        )

        self.assertEqual(1, result['success'])
        item = result['summary']['items'][0]
        self.assertEqual('parent@example.com', item['email'])
        self.assertTrue(item['has_access_token'])
        self.assertTrue(item['has_session_token'])
        self.assertTrue(item['has_oauth'])
        self.assertEqual('123e4567-e89b-12d3-a456-426614174000', item['team_account_id'])

    def test_delete_parent_account_removes_record(self):
        result = batch_import_codex_team_parent_accounts(
            'parent@example.com----mail-pass----client-id----refresh-token',
            enabled=True,
        )
        parent_id = result['summary']['items'][0]['id']

        from app.db import delete_codex_team_parent_account
        self.assertTrue(delete_codex_team_parent_account(parent_id))
        summary = get_codex_team_parent_pool_summary()
        self.assertEqual(0, summary['total'])

    def test_job_fills_first_parent_to_five_children_then_switches_parent(self):
        batch_import_codex_team_parent_accounts(
            '\n'.join([
                'parent1@example.com----eyJp1.bbb.ccc----team-1',
                'parent2@example.com----eyJp2.bbb.ccc----team-2',
            ]),
            enabled=True,
        )
        manager = CodexTeamManager()
        job_id = manager.create_job(
            {'parent_source': 'pool', 'target_children_per_parent': 5, 'max_parent_accounts': 2, 'executor_type': 'protocol'},
            merged_config={'use_proxy': False},
            start_immediately=False,
        )
        job = manager._jobs[job_id]

        counts = {'team-1': 3, 'team-2': 5}

        def fake_parent_context(parent, *, proxy_url=None, merged_config=None, executor_type=None, force_refresh=False, proxy_url_override=None):
            return {
                'success': True,
                'email': parent['email'],
                'access_token': f"at-{parent['email']}",
                'account_id': 'team-1' if parent['email'] == 'parent1@example.com' else 'team-2',
                'team_name': 'TeamWorkspace',
            }

        async def fake_get_members(*, access_token, account_id, identifier):
            return {'success': True, 'members': [{'role': 'standard-user'}] * counts[account_id], 'total': counts[account_id]}

        def fake_invite(*, access_token, account_id, email, identifier):
            counts[account_id] += 1
            return {'success': True, 'invite_id': 'invite-1', 'error': ''}

        with patch('app.codex_team_manager.build_mail_provider', return_value=_FakeChildProvider()), \
            patch('app.codex_team_manager.resolve_parent_invite_context', side_effect=fake_parent_context), \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls, \
            patch('app.codex_team_manager.RefreshTokenRegistrationEngine') as engine_cls:
            client = client_cls.return_value
            client.get_members = Mock(side_effect=lambda **kwargs: {'success': True, 'members': [{'role': 'standard-user'}] * counts[kwargs['account_id']], 'total': counts[kwargs['account_id']]})
            client.send_invite = Mock(side_effect=fake_invite)
            client.accept_invite = Mock(return_value={'success': True, 'error': ''})
            engine = engine_cls.return_value
            engine.run.return_value = type('R', (), {'success': True, 'email': 'child@example.com', 'password': 'mail-pass', 'workspace_id': 'team-1', 'account_id': 'team-1', 'session_token': 'st', 'access_token': 'child-at', 'refresh_token': '', 'id_token': '', 'error_message': '', 'metadata': {}})()
            manager._run_job(job)

        snapshot = manager.get_job_snapshot(job_id)
        self.assertEqual(2, snapshot['success'])
        self.assertEqual(0, snapshot['failed'])
        self.assertEqual(2, len(list_codex_team_web_sessions(job_id=job_id)))
        self.assertEqual(5, counts['team-1'])
        self.assertEqual(5, counts['team-2'])

    def test_job_uses_proxy_pool_when_no_direct_proxy_string(self):
        batch_import_codex_team_parent_accounts(
            'parent1@example.com----eyJaaa.bbb.ccc----123e4567-e89b-12d3-a456-426614174000',
            enabled=True,
        )
        manager = CodexTeamManager()
        job_id = manager.create_job(
            {'parent_source': 'pool', 'target_children_per_parent': 5, 'max_parent_accounts': 1, 'executor_type': 'protocol'},
            merged_config={'use_proxy': True, 'proxy': ''},
            start_immediately=False,
        )
        job = manager._jobs[job_id]

        with patch('app.codex_team_manager.acquire_proxy_pool_entry', return_value={'id': 7, 'proxy_url': 'http://127.0.0.1:7890'}), \
            patch('app.codex_team_manager.RegistrationManager._query_egress_info', return_value=('76.213.148.180', 'US')), \
            patch('app.codex_team_manager.resolve_parent_invite_context', return_value={'success': True, 'email': 'parent1@example.com', 'access_token': 'at-parent', 'account_id': 'team-1'}), \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls:
            client = client_cls.return_value
            client.get_members = Mock(return_value={'success': True, 'members': [{'role': 'standard-user'}] * 5, 'total': 5})
            manager._run_job(job)

        client_cls.assert_called_with(proxy_url='http://127.0.0.1:7890')

    def test_process_account_uses_team_manage_style_client_for_invite_and_accept(self):
        manager = CodexTeamManager()
        manager.create_job(
            {'parent_source': 'pool', 'target_children_per_parent': 5, 'max_parent_accounts': 1, 'executor_type': 'protocol'},
            merged_config={'use_proxy': True, 'proxy': 'http://127.0.0.1:7890'},
            start_immediately=False,
        )
        job_id = next(iter(manager._jobs.keys()))
        job = manager._jobs[job_id]

        with patch('app.codex_team_manager.build_mail_provider', return_value=_FakeChildProvider()), \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls, \
            patch('app.codex_team_manager.RefreshTokenRegistrationEngine') as engine_cls:
            client = client_cls.return_value
            client.send_invite = Mock(return_value={'success': True, 'invite_id': 'invite-1', 'error': ''})
            client.accept_invite = Mock(return_value={'success': True, 'error': ''})
            engine = engine_cls.return_value
            engine.run.return_value = type('R', (), {'success': True, 'email': 'child@example.com', 'password': 'mail-pass', 'workspace_id': 'team-1', 'account_id': 'team-1', 'session_token': 'st', 'access_token': 'child-at', 'refresh_token': '', 'id_token': '', 'error_message': '', 'metadata': {}})()
            manager._process_account(job, 1, parent_context={'access_token': 'at-parent', 'account_id': 'team-1', 'email': 'parent@example.com'})

        self.assertEqual('parent@example.com', client.send_invite.call_args.kwargs['identifier'])
        self.assertEqual('parent@example.com', client.accept_invite.call_args.kwargs['identifier'])
        client_cls.assert_called_with(proxy_url='http://127.0.0.1:7890')

    def test_parent_pool_job_stops_consuming_children_after_invite_failure(self):
        batch_import_codex_team_parent_accounts(
            'parent1@example.com----eyJaaa.bbb.ccc----123e4567-e89b-12d3-a456-426614174000',
            enabled=True,
        )
        manager = CodexTeamManager()
        job_id = manager.create_job(
            {'parent_source': 'pool', 'target_children_per_parent': 5, 'max_parent_accounts': 1, 'executor_type': 'protocol'},
            merged_config={'use_proxy': False},
            start_immediately=False,
        )
        job = manager._jobs[job_id]

        with patch('app.codex_team_manager.resolve_parent_invite_context', return_value={'success': True, 'email': 'parent1@example.com', 'access_token': 'at-parent', 'account_id': 'team-1'}), \
            patch.object(manager, '_process_account', return_value=False) as process_mock, \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls:
            client = client_cls.return_value
            client.get_members = Mock(return_value={'success': True, 'members': [], 'total': 0})
            manager._run_parent_pool_job(job)

        self.assertEqual(1, process_mock.call_count)

    def test_parent_pool_job_continues_after_mailbox_service_abuse_failure(self):
        batch_import_codex_team_parent_accounts(
            'parent1@example.com----eyJaaa.bbb.ccc----123e4567-e89b-12d3-a456-426614174000',
            enabled=True,
        )
        manager = CodexTeamManager()
        job_id = manager.create_job(
            {'parent_source': 'pool', 'target_children_per_parent': 1, 'max_parent_accounts': 1, 'executor_type': 'protocol'},
            merged_config={'use_proxy': False},
            start_immediately=False,
        )
        job = manager._jobs[job_id]

        results = [False, True]

        def fake_process(*args, **kwargs):
            current = results.pop(0)
            job._last_account_halt_parent = False if current is False else True
            return current

        with patch('app.codex_team_manager.resolve_parent_invite_context', return_value={'success': True, 'email': 'parent1@example.com', 'access_token': 'at-parent', 'account_id': 'team-1'}), \
            patch.object(manager, '_process_account', side_effect=fake_process) as process_mock, \
            patch('app.codex_team_manager.TeamManageStyleClient') as client_cls:
            client = client_cls.return_value
            client.get_members = Mock(side_effect=[
                {'success': True, 'members': [], 'total': 0},
                {'success': True, 'members': [{'role': 'standard-user'}], 'total': 1},
            ])
            manager._run_parent_pool_job(job)

        self.assertEqual(2, process_mock.call_count)


if __name__ == '__main__':
    unittest.main()
