import base64
import io
import json
import sys
import unittest
import zipfile
from unittest.mock import Mock, patch

sys.path.insert(0, 'backend')

from app.gpt_password_codex import (
    GptPasswordCodexManager,
    NissanSerenaOtpProvider,
    authorize_codex_account_via_password,
    parse_gpt_password_accounts,
)


def _jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode()).decode().rstrip('=')
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'{header}.{body}.sig'


class GptPasswordCodexTests(unittest.TestCase):
    def test_parse_accounts_supports_header_lines_and_custom_delimiter(self):
        parsed = parse_gpt_password_accounts(
            '\n'.join([
                '=== 使用说明 ===',
                'api接码网站 https://nissanserena.my.id/otp',
                '=== 卡密内容 ===',
                'user1@example.com|pass-1',
                'user2@example.com|pass-2',
            ]),
            delimiter='|',
        )
        self.assertEqual(
            [
                {'email': 'user1@example.com', 'password': 'pass-1'},
                {'email': 'user2@example.com', 'password': 'pass-2'},
            ],
            parsed,
        )

        parsed_custom = parse_gpt_password_accounts('user3@example.com----pass-3', delimiter='----')
        self.assertEqual([{'email': 'user3@example.com', 'password': 'pass-3'}], parsed_custom)

    def test_parse_search_result_extracts_openai_sender_code_and_time(self):
        html = '''
        <div>
          <p class="text-4xl font-bold">508636</p>
          <div class="text-xs break-all">noreply@tm.openai.com</div>
          <span>23 Apr 2026, 17:35</span>
        </div>
        '''
        parsed = NissanSerenaOtpProvider._parse_search_result(html)
        self.assertEqual('508636', parsed['code'])
        self.assertEqual('noreply@tm.openai.com', parsed['sender'])
        self.assertEqual('23 Apr 2026, 17:35', parsed['sent_at_text'])
        self.assertIsNotNone(parsed['sent_at'])

    def test_authorize_codex_account_via_password_uses_proxy_and_returns_cpa(self):
        access_token = _jwt({
            'exp': 1800000000,
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-1'},
            'https://api.openai.com/profile': {'email': 'user@example.com'},
        })
        id_token = _jwt({
            'email': 'user@example.com',
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-1'},
        })
        with patch('app.gpt_password_codex.ChatGPTWebAuthClient') as client_cls:
            client = client_cls.return_value
            client.login_and_get_tokens = Mock(return_value={
                'access_token': access_token,
                'refresh_token': 'rt-1',
                'id_token': id_token,
                'session_token': 'st-1',
            })
            client.last_workspace_id = 'acct-1'

            result = authorize_codex_account_via_password(
                {'email': 'user@example.com', 'password': 'pass-1'},
                merged_config={'use_proxy': True, 'proxy': 'http://127.0.0.1:7890'},
                executor_type='protocol',
                otp_site_url='https://nissanserena.my.id/otp',
            )

        self.assertTrue(result['success'])
        self.assertEqual('acct-1', result['account_id'])
        self.assertEqual('http://127.0.0.1:7890', client_cls.call_args.kwargs['proxy'])
        self.assertEqual('rt-1', result['cpa_account']['refresh_token'])
        self.assertEqual('st-1', result['oauth_account']['session_token'])

    def test_manager_exports_cpa_and_sub2api_zip(self):
        manager = GptPasswordCodexManager()
        access_token = _jwt({
            'exp': 1800000000,
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-a'},
            'https://api.openai.com/profile': {'email': 'a@example.com'},
        })
        id_token = _jwt({
            'email': 'a@example.com',
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-a'},
        })
        success_result = {
            'success': True,
            'account_id': 'acct-a',
            'oauth_account': {
                'email': 'a@example.com',
                'account_id': 'acct-a',
                'access_token': access_token,
                'refresh_token': 'rt-a',
                'id_token': id_token,
                'session_token': 'st-a',
            },
            'cpa_account': {'email': 'a@example.com', 'account_id': 'acct-a'},
            'cpa_json': '{"email":"a@example.com"}',
        }
        with patch('app.gpt_password_codex.authorize_codex_account_via_password', side_effect=[success_result]):
            job_id = manager.create_job(
                data='a@example.com|pass-a',
                delimiter='|',
                otp_site_url='https://nissanserena.my.id/otp',
                merged_config={'use_proxy': False},
                executor_type='protocol',
            )
            manager._threads[job_id].join(timeout=3)

        result = manager.export_results(job_id, emails=['a@example.com'])
        self.assertTrue(result['ok'])
        archive = zipfile.ZipFile(io.BytesIO(result['content']))
        names = sorted(archive.namelist())
        self.assertEqual(['CPA/a_example.com.json', 'sub2api/sub2api_accounts.json'], names)
        self.assertIn('a@example.com', archive.read('CPA/a_example.com.json').decode())
        self.assertIn('a@example.com', archive.read('sub2api/sub2api_accounts.json').decode())



    def test_manager_deletes_converted_results_by_email(self):
        manager = GptPasswordCodexManager()
        success_result = {
            'success': True,
            'account_id': 'acct-a',
            'oauth_account': {'email': 'a@example.com'},
            'cpa_account': {'email': 'a@example.com', 'account_id': 'acct-a'},
            'cpa_json': '{"email":"a@example.com"}',
        }
        with patch('app.gpt_password_codex.authorize_codex_account_via_password', side_effect=[success_result]):
            job_id = manager.create_job(
                data='a@example.com|pass-a',
                delimiter='|',
                otp_site_url='https://nissanserena.my.id/otp',
                merged_config={'use_proxy': False},
                executor_type='protocol',
            )
            manager._threads[job_id].join(timeout=3)

        snapshot = manager.get_job_snapshot(job_id)
        self.assertEqual(1, len(snapshot['results']))

        delete_result = manager.delete_results(job_id, emails=['a@example.com'])
        self.assertTrue(delete_result['ok'])
        self.assertEqual(1, delete_result['deleted'])
        snapshot = manager.get_job_snapshot(job_id)
        self.assertEqual([], snapshot['results'])
if __name__ == '__main__':
    unittest.main()
