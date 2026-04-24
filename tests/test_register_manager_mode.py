import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, 'backend')

from tests.db_isolation import IsolatedCodexTeamDbTestCase
from app.db import get_task_run
from app.manager import RegistrationManager
from app.schemas import CreateRegisterTaskRequest


class RegistrationManagerModeTests(IsolatedCodexTeamDbTestCase, unittest.TestCase):
    def test_merge_runtime_config_sets_register_only_flags(self):
        manager = RegistrationManager()
        req = CreateRegisterTaskRequest(
            count=1,
            concurrency=1,
            register_delay_seconds=0,
            mail_provider='luckmail',
            registration_mode='register_only',
        )

        config = manager._merge_runtime_config({}, req)

        self.assertEqual('register_only', config['registration_mode'])
        self.assertEqual('register_only', config['chatgpt_registration_mode'])
        self.assertTrue(config['register_only_before_invite'])

    def test_create_task_persists_registration_mode(self):
        manager = RegistrationManager()
        req = CreateRegisterTaskRequest(
            count=1,
            concurrency=1,
            register_delay_seconds=0,
            mail_provider='luckmail',
            registration_mode='register_only',
        )

        with patch('app.manager.threading.Thread') as thread_cls:
            thread_cls.return_value.start = Mock()
            task_id = manager.create_task(req, {})

        snapshot = manager.get_task_snapshot(task_id)
        task_run = get_task_run(task_id)

        self.assertEqual('register_only', snapshot['meta']['mode'])
        self.assertEqual('register_only', task_run['request_json']['registration_mode'])


if __name__ == '__main__':
    unittest.main()
