import base64
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, 'backend')

from app.oauth_cpa import create_oauth_cpa_link, exchange_oauth_callback_for_cpa


def _jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode()).decode().rstrip('=')
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return f'{header}.{body}.sig'


class OAuthCpaTests(unittest.TestCase):
    def test_create_oauth_cpa_link_returns_session_parameters(self):
        with patch('app.oauth_cpa.generate_oauth_url', return_value=SimpleNamespace(
            auth_url='https://auth.example/authorize?state=s1',
            state='s1',
            code_verifier='verifier-1',
            redirect_uri='http://localhost:1455/auth/callback',
        )):
            result = create_oauth_cpa_link()

        self.assertEqual('https://auth.example/authorize?state=s1', result['auth_url'])
        self.assertEqual('s1', result['state'])
        self.assertEqual('verifier-1', result['code_verifier'])
        self.assertEqual('http://localhost:1455/auth/callback', result['redirect_uri'])

    def test_exchange_oauth_callback_for_cpa_returns_cpa_json(self):
        access_token = _jwt({
            'exp': 1800000000,
            'https://api.openai.com/auth': {
                'chatgpt_account_id': 'acct-1',
                'chatgpt_user_id': 'user-1',
            },
            'https://api.openai.com/profile': {'email': 'user@example.com'},
        })
        id_token = _jwt({
            'email': 'user@example.com',
            'https://api.openai.com/auth': {
                'chatgpt_account_id': 'acct-1',
                'organization_id': 'org-1',
            },
        })
        oauth_payload = {
            'type': 'codex',
            'email': 'user@example.com',
            'account_id': 'acct-1',
            'access_token': access_token,
            'refresh_token': 'rt-1',
            'id_token': id_token,
            'expired': '2027-01-15T08:00:00Z',
            'last_refresh': '2026-04-14T00:00:00Z',
        }

        with patch('app.oauth_cpa.submit_callback_url', return_value=json.dumps(oauth_payload)) as submit_mock:
            result = exchange_oauth_callback_for_cpa(
                callback_url='http://localhost:1455/auth/callback?code=abc&state=s1',
                state='s1',
                code_verifier='verifier-1',
                proxy_url='http://127.0.0.1:7890',
            )

        self.assertEqual('user@example.com', result['email'])
        self.assertEqual('acct-1', result['account_id'])
        self.assertEqual('rt-1', result['cpa_account']['refresh_token'])
        self.assertIn('"type": "codex"', result['cpa_json'])
        submit_mock.assert_called_once()
        self.assertEqual('http://127.0.0.1:7890', submit_mock.call_args.kwargs['proxy_url'])


if __name__ == '__main__':
    unittest.main()
