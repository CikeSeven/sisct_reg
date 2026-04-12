import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, 'backend')

from app.manager import QueuedAttempt, RegistrationManager, TaskExecutionState


class RetryQueueBehaviorTests(unittest.TestCase):
    def test_prepare_initial_attempts_assigns_sequential_indexes(self):
        manager = RegistrationManager()
        prepared = manager._prepare_initial_attempts(
            [
                QueuedAttempt(
                    attempt_index=0,
                    req_overrides={'email': 'first@example.com'},
                    merged_config_overrides={'retry_from_task_id': 'task_a', 'retry_from_attempt_index': 3},
                ),
                QueuedAttempt(
                    attempt_index=0,
                    req_overrides={'email': 'second@example.com'},
                    merged_config_overrides={'retry_from_task_id': 'task_b', 'retry_from_attempt_index': 5},
                ),
            ]
        )

        self.assertEqual([1, 2], [item.attempt_index for item in prepared])
        self.assertEqual('first@example.com', prepared[0].req_overrides.get('email'))
        self.assertEqual('task_b', prepared[1].merged_config_overrides.get('retry_from_task_id'))

    def test_enqueue_attempts_keeps_cold_queue_items_out_of_live_accounts(self):
        manager = RegistrationManager()
        state = TaskExecutionState(total=2)

        with patch('app.manager.upsert_task_account_state'):
            enqueued = manager._enqueue_attempts_with_pending_accounts(
                'retry_task',
                state,
                [
                    QueuedAttempt(
                        attempt_index=1,
                        req_overrides={'email': 'first@example.com'},
                        merged_config_overrides={'retry_from_task_id': 'task_a', 'retry_from_attempt_index': 4},
                    ),
                    QueuedAttempt(
                        attempt_index=2,
                        req_overrides={'email': 'second@example.com'},
                        merged_config_overrides={'retry_from_task_id': 'task_b', 'retry_from_attempt_index': 5},
                    ),
                ],
            )

        self.assertEqual(2, enqueued)
        self.assertEqual(2, len(state.pending_attempts))
        self.assertEqual([], manager._get_live_accounts('retry_task'))

    def test_retry_attempt_uses_requested_concurrency_when_creating_new_retry_task(self):
        manager = RegistrationManager()
        captured: dict[str, object] = {}

        def fake_create_task(self, req, merged_config, **kwargs):
            captured['concurrency'] = req.concurrency
            captured['count'] = req.count
            captured['meta'] = kwargs.get('meta')
            return 'retry_task_new'

        task_row = {
            'status': 'done',
            'request_json': {
                'concurrency': 8,
                'mail_provider': 'luckmail',
                'merged_config': {},
                'provider_config': {},
                'phone_config': {},
                'executor_type': 'protocol',
            },
        }
        accounts = [
            {
                'attempt_index': 3,
                'status': 'failed',
                'email': 'alice@example.com',
                'failure_stage': 'otp',
                'failure_origin': 'oauth',
                'retry_email_binding': {'email': 'alice@example.com'},
            }
        ]

        with patch('app.manager.get_task_run', return_value=task_row), \
            patch.object(RegistrationManager, '_build_db_accounts', return_value=accounts), \
            patch.object(RegistrationManager, '_get_current_runtime_defaults', return_value={'use_proxy': False}), \
            patch.object(RegistrationManager, '_enabled_proxy_pool_exists', return_value=False), \
            patch.object(RegistrationManager, 'create_task', new=fake_create_task):
            result = manager.retry_attempt('task_source', 3, retry_concurrency=6)

        self.assertEqual({'ok': True, 'task_id': 'retry_task_new'}, result)
        self.assertEqual(6, captured['concurrency'])
        self.assertEqual(1, captured['count'])

    def test_retry_result_uses_requested_concurrency_when_creating_new_retry_task(self):
        manager = RegistrationManager()
        captured: dict[str, object] = {}

        def fake_create_task(self, req, merged_config, **kwargs):
            captured['concurrency'] = req.concurrency
            captured['count'] = req.count
            captured['meta'] = kwargs.get('meta')
            return 'retry_task_new'

        result_row = {
            'id': 42,
            'task_id': 'task_source',
            'attempt_index': 5,
            'status': 'failed',
            'email': 'bob@example.com',
            'password': '',
            'extra_json': {
                'failure_stage': 'otp',
                'failure_origin': 'oauth',
                'metadata': {'email_binding': {'email': 'bob@example.com'}},
            },
        }
        task_row = {
            'status': 'done',
            'request_json': {
                'concurrency': 4,
                'mail_provider': 'luckmail',
                'merged_config': {},
                'provider_config': {},
                'phone_config': {},
                'executor_type': 'protocol',
            },
        }

        def fake_get_task_run(task_id):
            return task_row if task_id == 'task_source' else None

        with patch('app.manager.get_task_result', return_value=result_row), \
            patch('app.manager.get_task_run', side_effect=fake_get_task_run), \
            patch.object(RegistrationManager, '_get_current_runtime_defaults', return_value={'use_proxy': False}), \
            patch.object(RegistrationManager, '_enabled_proxy_pool_exists', return_value=False), \
            patch.object(RegistrationManager, 'create_task', new=fake_create_task):
            result = manager.retry_result(42, retry_concurrency=7)

        self.assertEqual({'ok': True, 'task_id': 'retry_task_new'}, result)
        self.assertEqual(7, captured['concurrency'])
        self.assertEqual(1, captured['count'])

    def test_appended_attempt_keeps_retry_origin_in_queue_without_materializing_live_account(self):
        manager = RegistrationManager()
        manager._task_store.create('retry_task', platform='chatgpt', total=0, source='retry', meta={})
        state = TaskExecutionState(total=0)
        manager._set_task_execution_state('retry_task', state)

        with patch('app.manager.upsert_task_account_state'), \
            patch('app.manager.update_task_run'), \
            patch('app.manager.update_task_request_count'):
            result = manager._append_attempts_to_active_task(
                'retry_task',
                [
                    QueuedAttempt(
                        attempt_index=0,
                        req_overrides={'email': 'queued@example.com'},
                        merged_config_overrides={
                            'retry_from_task_id': 'task_a',
                            'retry_from_attempt_index': 2,
                            'retry_from_result_id': 18,
                        },
                    )
                ],
            )

        self.assertTrue(result.get('ok'))
        self.assertEqual([], manager._get_live_accounts('retry_task'))
        self.assertEqual(1, len(state.pending_attempts))
        queued_item = state.pending_attempts[0]
        self.assertEqual('task_a', queued_item.merged_config_overrides.get('retry_from_task_id'))
        self.assertEqual(2, queued_item.merged_config_overrides.get('retry_from_attempt_index'))
        self.assertEqual(18, queued_item.merged_config_overrides.get('retry_from_result_id'))

    def test_build_db_accounts_exposes_retry_origin_from_result_metadata(self):
        manager = RegistrationManager()
        request_payload = {'mail_provider': 'luckmail'}

        with patch('app.manager.get_task_run', return_value={'status': 'done'}), \
            patch('app.manager.get_task_account_states', return_value=[]), \
            patch('app.manager.get_task_events', return_value=[]), \
            patch('app.manager.get_task_results', return_value=[
                {
                    'id': 100,
                    'attempt_index': 1,
                    'status': 'failed',
                    'email': 'done@example.com',
                    'error': 'boom',
                    'created_at': 1.0,
                    'extra_json': {
                        'failure_stage': 'otp',
                        'failure_origin': 'oauth',
                        'retry_from_task_id': 'task_done',
                        'retry_from_attempt_index': 9,
                        'retry_from_result_id': 88,
                        'metadata': {'email_binding': {'email': 'done@example.com'}},
                    },
                }
            ]):
            items = manager._build_db_accounts('retry_task', request_payload=request_payload)

        self.assertEqual(1, len(items))
        self.assertEqual('task_done', items[0].get('retry_from_task_id'))
        self.assertEqual(9, items[0].get('retry_from_attempt_index'))
        self.assertEqual(88, items[0].get('retry_from_result_id'))

    def test_build_db_accounts_ignores_corrupted_attempt_index_zero_rows(self):
        manager = RegistrationManager()
        request_payload = {'mail_provider': 'luckmail'}

        with patch('app.manager.get_task_run', return_value={'status': 'done'}), \
            patch('app.manager.get_task_account_states', return_value=[
                {'attempt_index': 0, 'status': 'failed', 'email': 'bad@example.com', 'error': 'bad'},
                {'attempt_index': 2, 'status': 'failed', 'email': 'good@example.com', 'error': 'good'},
            ]), \
            patch('app.manager.get_task_events', return_value=[]), \
            patch('app.manager.get_task_results', return_value=[
                {
                    'id': 1,
                    'attempt_index': 0,
                    'status': 'failed',
                    'email': 'bad@example.com',
                    'error': 'bad',
                    'created_at': 1.0,
                    'extra_json': {},
                },
                {
                    'id': 2,
                    'attempt_index': 2,
                    'status': 'failed',
                    'email': 'good@example.com',
                    'error': 'good',
                    'created_at': 2.0,
                    'extra_json': {},
                },
            ]):
            items = manager._build_db_accounts('retry_task', request_payload=request_payload)

        self.assertEqual([2], [item.get('attempt_index') for item in items])

    def test_build_db_accounts_ignores_corrupted_event_attempt_index_zero_rows(self):
        manager = RegistrationManager()
        request_payload = {'mail_provider': 'luckmail'}

        with patch('app.manager.get_task_run', return_value={'status': 'done'}), \
            patch('app.manager.get_task_account_states', return_value=[]), \
            patch('app.manager.get_task_events', return_value=[
                {'attempt_index': 0, 'message': '[00:00:00] 开始注册第 0/4 个账号'},
                {'attempt_index': 2, 'message': '[00:00:01] 开始注册第 2/4 个账号'},
            ]), \
            patch('app.manager.get_task_results', return_value=[]):
            items = manager._build_db_accounts('retry_task', request_payload=request_payload)

        self.assertEqual([2], [item.get('attempt_index') for item in items])


if __name__ == '__main__':
    unittest.main()
