import asyncio
import unittest
from unittest.mock import patch
from app.services import ai_provider as ai

class AISecurityTests(unittest.TestCase):
    def test_url_allowlist(self):
        self.assertEqual(ai.normalize_openai_base_url('https://api.llm.ustc.edu.cn/v1/chat/completions'), 'https://api.llm.ustc.edu.cn/v1')
        for url in ['http://api.llm.ustc.edu.cn/v1', 'https://localhost/v1', 'https://api.llm.ustc.edu.cn.evil/v1', 'https://user:pass@api.llm.ustc.edu.cn/v1', 'https://api.llm.ustc.edu.cn/v1?x=1']:
            with self.assertRaises(ValueError): ai.normalize_openai_base_url(url)

    def test_codex_blocked(self):
        with patch('asyncio.create_subprocess_exec') as proc:
            with self.assertRaises(RuntimeError): asyncio.run(ai._run_codex_cli([], max_tokens=8, timeout=1))
            proc.assert_not_called()

    def test_client_guards(self):
        async def check():
            import httpx
            with patch.object(ai, 'current_ai_provider', return_value='openai_compat'), patch.object(ai.secrets_store, 'get_ai_config', return_value='https://api.llm.ustc.edu.cn/v1'):
                client = ai._openai_client('test-key-not-real', 5)
                try:
                    self.assertFalse(client._client.follow_redirects)
                    self.assertFalse(client._client._trust_env)
                    self.assertEqual(client.max_retries, 0)
                    guard = client._client.event_hooks['request'][0]
                    with self.assertRaises(RuntimeError): await guard(httpx.Request('POST', 'https://evil.example/v1/chat/completions'))
                    with patch('app.services.safe_http.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
                        with self.assertRaises(ValueError): await guard(httpx.Request('POST', 'https://api.llm.ustc.edu.cn/v1/chat/completions'))
                finally: await client.close()
        asyncio.run(check())

if __name__ == '__main__': unittest.main()
