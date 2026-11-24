import unittest
from unittest.mock import AsyncMock, patch

from app.services.ai_errors import ai_error_message


class AIConnectionErrorTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_status_is_actionable_without_leaking_response(self):
        for status, expected in [(401, '认证失败'), (403, '模型权限'), (404, '不存在'), (429, '限流'), (503, '供应商')]:
            error = Exception('private response Authorization Bearer secret-token')
            error.status_code = status
            text = ai_error_message(error)
            self.assertIn(expected, text)
            self.assertIn(str(status), text)
            self.assertNotIn('secret-token', text)
            self.assertNotIn('Authorization', text)

    def test_timeout_is_distinct_from_model_permissions(self):
        self.assertIn('超时', ai_error_message(TimeoutError('sensitive')))
        error = type('APIConnectionError', (Exception,), {})('secret')
        self.assertIn('网络连接失败', ai_error_message(error))

    async def test_empty_answer_is_not_connection_success(self):
        from app.api.strategy import ai_test
        with patch('app.services.ai_provider.generate_ai_text', new=AsyncMock(return_value='')) as generate:
            result = await ai_test(None)
        self.assertFalse(result['ok'])
        self.assertEqual(90, generate.call_args.kwargs['timeout'])

    async def test_test_button_reports_model_denial_without_changing_model(self):
        from app.api.strategy import ai_test
        error = Exception('secret provider response')
        error.status_code = 403
        with patch('app.services.ai_provider.generate_ai_text', new=AsyncMock(side_effect=error)):
            result = await ai_test(None)
        self.assertFalse(result['ok'])
        self.assertIn('模型权限', result['error'])
        self.assertNotIn('secret', result['error'])
