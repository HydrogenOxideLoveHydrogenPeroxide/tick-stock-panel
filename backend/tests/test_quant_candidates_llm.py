"""Selection is read-only and model explanations use server-computed quantities."""
import unittest
from unittest.mock import patch, AsyncMock
from app.services import quant_assistant as q

class CandidateAndExplanationTests(unittest.IsolatedAsyncioTestCase):
    def payload(self):
        return q.SuggestRequest(portfolio=q.Portfolio(cash=10000, holdings=[], strategy=q.Strategy(screener_strategy_id='context_sentiment_bull_core')), mode='morning')

    async def test_candidates_preserve_portfolio(self):
        payload = self.payload()
        before = payload.model_dump()
        data = dict(strategy_id='context_sentiment_bull_core', name='研究', as_of='2026-11-05', total=1,
                    candidates=[dict(symbol='600000.SH', name='浦发银行', score=70)], matched_symbols=['600000.SH'], overrides={})
        with patch.object(q, '_evaluate_screener', return_value=data):
            result = await q.screen_candidates(None, None, payload)
        self.assertEqual(result['candidates'][0]['symbol'], '600000.SH')
        self.assertNotIn('matched_symbols', result)
        self.assertEqual(payload.model_dump(), before)

    async def test_no_silent_fallback(self):
        payload = self.payload(); payload.portfolio.strategy.screener_strategy_id = ''
        with self.assertRaisesRegex(ValueError, '选择选股策略'):
            await q.screen_candidates(None, None, payload)

    async def test_missing_configuration_does_not_call_provider(self):
        with patch('app.services.ai_provider.ai_configured', return_value=False), patch.object(q, 'suggest_portfolio', new_callable=AsyncMock) as suggest:
            with self.assertRaisesRegex(ValueError, '尚未配置'):
                await q.explain_portfolio(None, None, self.payload())
        suggest.assert_not_called()

    async def test_llm_keeps_server_quantities_omits_raw_bars(self):
        snapshot = dict(generated_at='2026-11-06', suggestions=[dict(symbol='600000.SH', quantity=300, action='buy',
            data_context=dict(bars=['NEVER_SEND_RAW_BARS'], as_of='2026-11-05', screener={'matched': True}))])
        with patch('app.services.ai_provider.ai_configured', return_value=True), patch.object(q, 'suggest_portfolio', new_callable=AsyncMock, return_value=snapshot), patch('app.services.ai_provider.current_ai_model', return_value='configured-model'), patch('app.services.ai_provider.generate_ai_text', new_callable=AsyncMock, return_value='参考数量300股') as llm:
            response = await q.explain_portfolio(None, None, self.payload())
        self.assertIs(response['result'], snapshot)
        self.assertIn('300', llm.call_args.args[0][1]['content'])
        self.assertNotIn('NEVER_SEND_RAW_BARS', llm.call_args.args[0][1]['content'])
        self.assertEqual(response['result']['suggestions'][0]['quantity'], 300)
