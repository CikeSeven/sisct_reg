import base64
import io
import json
import sys
import unittest
import zipfile
from unittest.mock import Mock, patch

sys.path.insert(0, 'backend')

from app.codex_auth_batch import CodexAuthBatchManager, authorize_codex_account_via_outlook
from app.mail_providers import ProviderBase


def _jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode()).decode().rstrip('=')
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'{header}.{body}.sig'


class CodexAuthBatchTests(unittest.TestCase):
    def test_authorize_codex_account_uses_config_proxy(self):
        account = {
            'email': 'parent@example.com',
            'password': 'mail-pass',
            'client_id': 'client-id',
            'refresh_token': 'ms-rt',
        }
        access_token = _jwt({
            'exp': 1800000000,
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-1'},
            'https://api.openai.com/profile': {'email': 'parent@example.com'},
        })
        id_token = _jwt({
            'email': 'parent@example.com',
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-1'},
        })

        class _FakeProvider:
            def create_email(self, config=None):
                return {'email': 'parent@example.com'}

        with patch('app.codex_auth_batch.build_mail_provider', return_value=_FakeProvider()) as provider_factory, \
            patch('app.codex_auth_batch.ChatGPTWebAuthClient') as client_cls:
            client = client_cls.return_value
            client.login_and_get_tokens = Mock(return_value={
                'access_token': access_token,
                'refresh_token': 'openai-rt',
                'id_token': id_token,
            })
            client.last_workspace_id = 'acct-1'

            result = authorize_codex_account_via_outlook(
                account,
                merged_config={'use_proxy': True, 'proxy': 'http://127.0.0.1:7890'},
                executor_type='protocol',
            )

        self.assertTrue(result['success'])
        self.assertEqual('acct-1', result['account_id'])
        self.assertIn('"type": "codex"', result['cpa_json'])
        self.assertEqual('http://127.0.0.1:7890', provider_factory.call_args.kwargs['proxy'])
        self.assertEqual('http://127.0.0.1:7890', client_cls.call_args.kwargs['proxy'])

    def test_authorize_codex_account_falls_back_to_proxy_pool(self):
        account = {
            'email': 'parent@example.com',
            'password': 'mail-pass',
            'client_id': 'client-id',
            'refresh_token': 'ms-rt',
        }
        access_token = _jwt({
            'exp': 1800000000,
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-1'},
            'https://api.openai.com/profile': {'email': 'parent@example.com'},
        })
        id_token = _jwt({
            'email': 'parent@example.com',
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-1'},
        })

        class _FakeProvider:
            def create_email(self, config=None):
                return {'email': 'parent@example.com'}

        with patch('app.codex_auth_batch.get_proxy_pool_summary', return_value={'enabled': 1}), \
            patch('app.codex_auth_batch.acquire_proxy_pool_entry', return_value={'proxy_url': 'http://pool-proxy:7890'}), \
            patch('app.codex_auth_batch.build_mail_provider', return_value=_FakeProvider()) as provider_factory, \
            patch('app.codex_auth_batch.ChatGPTWebAuthClient') as client_cls:
            client = client_cls.return_value
            client.login_and_get_tokens = Mock(return_value={
                'access_token': access_token,
                'refresh_token': 'openai-rt',
                'id_token': id_token,
            })
            client.last_workspace_id = 'acct-1'

            result = authorize_codex_account_via_outlook(
                account,
                merged_config={'use_proxy': True, 'proxy': ''},
                executor_type='protocol',
            )

        self.assertTrue(result['success'])
        self.assertEqual('http://pool-proxy:7890', provider_factory.call_args.kwargs['proxy'])
        self.assertEqual('http://pool-proxy:7890', client_cls.call_args.kwargs['proxy'])


    def test_extract_code_accepts_spaced_digits(self):
        provider = ProviderBase()
        code = provider._extract_code('Your authentication code is 123 456. Use it to sign in.')
        self.assertEqual('123456', code)


    def test_batch_manager_records_success_and_failure_results(self):
        manager = CodexAuthBatchManager()

        with patch('app.codex_auth_batch.authorize_codex_account_via_outlook', side_effect=[
            {'success': True, 'account_id': 'acct-a', 'cpa_account': {'email': 'a@example.com'}, 'cpa_json': '{"email":"a@example.com"}'},
            {'success': False, 'error': 'login failed'},
        ]):
            job_id = manager.create_job(
                data='\n'.join([
                    'a@example.com----pass-a----client-a----rt-a',
                    'b@example.com----pass-b----client-b----rt-b',
                ]),
                merged_config={'use_proxy': False},
                executor_type='protocol',
            )
            thread = manager._threads[job_id]
            thread.join(timeout=3)

        snapshot = manager.get_job_snapshot(job_id)
        self.assertIsNotNone(snapshot)
        self.assertEqual(2, snapshot['total'])
        self.assertEqual(2, snapshot['completed'])
        self.assertEqual(1, snapshot['success'])
        self.assertEqual(1, snapshot['failed'])
        self.assertEqual(2, len(snapshot['results']))
        self.assertTrue(any(item['status'] == 'success' for item in snapshot['results']))
        self.assertTrue(any(item['status'] == 'failed' for item in snapshot['results']))


    def test_batch_manager_exports_selected_results_as_zip(self):
        manager = CodexAuthBatchManager()
        with patch('app.codex_auth_batch.authorize_codex_account_via_outlook', side_effect=[
            {'success': True, 'account_id': 'acct-a', 'cpa_account': {'email': 'a@example.com'}, 'cpa_json': '{"email":"a@example.com"}'},
            {'success': True, 'account_id': 'acct-b', 'cpa_account': {'email': 'b@example.com'}, 'cpa_json': '{"email":"b@example.com"}'},
        ]):
            job_id = manager.create_job(
                data='\n'.join([
                    'a@example.com----pass-a----client-a----rt-a',
                    'b@example.com----pass-b----client-b----rt-b',
                ]),
                merged_config={'use_proxy': False},
                executor_type='protocol',
            )
            manager._threads[job_id].join(timeout=3)

        result = manager.export_results(job_id, emails=['b@example.com'])
        self.assertTrue(result['ok'])
        archive = zipfile.ZipFile(io.BytesIO(result['content']))
        self.assertEqual(['b_example.com.json'], archive.namelist())
        self.assertIn('b@example.com', archive.read('b_example.com.json').decode())


if __name__ == '__main__':
    unittest.main()
