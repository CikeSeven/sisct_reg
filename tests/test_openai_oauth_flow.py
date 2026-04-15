import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, 'backend')

from platforms.chatgpt.oauth import generate_oauth_url, submit_callback_url
from platforms.chatgpt.oauth_client import choose_codex_consent_workspace


class OAuthFlowTests(unittest.TestCase):
    def test_generate_oauth_url_matches_har_shape(self):
        start = generate_oauth_url()
        parsed = urlparse(start.auth_url)
        query = parse_qs(parsed.query)

        self.assertEqual('https', parsed.scheme)
        self.assertEqual('auth.openai.com', parsed.netloc)
        self.assertEqual('/oauth/authorize', parsed.path)
        self.assertEqual('app_EMoamEEZ73f0CkXaXp7hrann', query['client_id'][0])
        self.assertEqual('code', query['response_type'][0])
        self.assertEqual('http://localhost:1455/auth/callback', query['redirect_uri'][0])
        self.assertEqual('openid email profile offline_access', query['scope'][0])
        self.assertEqual('S256', query['code_challenge_method'][0])
        self.assertEqual('login', query['prompt'][0])
        self.assertEqual('true', query['id_token_add_organizations'][0])
        self.assertEqual('true', query['codex_cli_simplified_flow'][0])
        self.assertTrue(query['state'][0])
        self.assertTrue(query['code_challenge'][0])

    def test_submit_callback_url_uses_har_aligned_headers(self):
        id_token_payload = {
            'email': 'user@example.com',
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-1'},
        }
        header = 'eyJhbGciOiAiUlMyNTYifQ'
        body = json.dumps(id_token_payload).encode('utf-8')
        import base64
        id_token = f"{header}.{base64.urlsafe_b64encode(body).decode().rstrip('=')}.sig"

        fake_response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                'access_token': 'access-token',
                'refresh_token': 'refresh-token',
                'id_token': id_token,
                'expires_in': 3600,
            },
        )

        with patch('platforms.chatgpt.oauth.tracked_request', return_value=fake_response) as tracked_request:
            result = submit_callback_url(
                callback_url='http://localhost:1455/auth/callback?code=abc123&scope=openid+email+profile+offline_access&state=har-state',
                expected_state='har-state',
                code_verifier='verifier-123',
                proxy_url='http://127.0.0.1:7890',
            )

        payload = json.loads(result)
        self.assertEqual('user@example.com', payload['email'])
        self.assertEqual('acct-1', payload['account_id'])
        kwargs = tracked_request.call_args.kwargs
        self.assertEqual('http://127.0.0.1:7890', kwargs['proxy_url'])
        headers = kwargs['headers']
        self.assertEqual('https://auth.openai.com', headers['Origin'])
        self.assertEqual('https://auth.openai.com/sign-in-with-chatgpt/codex/consent', headers['Referer'])
        self.assertEqual('same-origin', headers['Sec-Fetch-Site'])
        self.assertEqual('cors', headers['Sec-Fetch-Mode'])
        self.assertEqual('empty', headers['Sec-Fetch-Dest'])

    def test_choose_codex_consent_workspace_prefers_organization(self):
        workspace = choose_codex_consent_workspace([
            {'id': 'personal-1', 'kind': 'personal', 'is_default': True},
            {'id': 'org-1', 'kind': 'organization', 'name': 'Org Workspace'},
        ])
        self.assertEqual('org-1', workspace['id'])


if __name__ == '__main__':
    unittest.main()
