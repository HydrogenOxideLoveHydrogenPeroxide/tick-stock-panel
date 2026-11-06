"""Analysis integration tests: no real AI or network calls."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock
import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.stock_analysis import router
from app.services.stock_analyzer import analyze_stock_stream

class AnalysisModesTests(unittest.IsolatedAsyncioTestCase):
    def repo(self):
        repo = Mock()
        repo.get_etf_instruments.return_value = pl.DataFrame({'symbol':['513100.SH']})
        repo.store.data_dir = Path('.')
        return repo

    async def test_noon_context_not_daily(self):
        context = dict(mode='noon',symbol='513100.SH',period='60min',as_of='2026-11-04T11:30:00+08:00',source='腾讯',adjust='none',rows=[dict(date='2026-11-04 11:30',open=2.,high=2.2,low=1.9,close=2.1,volume=100.)],snapshot={},quote=None,news=[],warnings=['来源原始量'])
        async def fake(messages, **kwargs):
            self.assertIn('60min',messages[1]['content'])
            self.assertNotIn('最近日 K 数据',messages[1]['content'])
            self.assertIn('外部不可信数据',messages[0]['content'])
            yield '测试报告'
        repo=self.repo()
        with patch('app.services.market_sources.fetch_market_context',AsyncMock(return_value=context)), patch('app.services.ai_provider.stream_ai_text',fake):
            events=[json.loads(x) async for x in analyze_stock_stream(repo,Path('.'),'513100.SH',mode='noon')]
        self.assertEqual([x['type'] for x in events],['meta','delta','done'])
        self.assertEqual(events[0]['levels'],{})
        self.assertEqual(events[0]['data_context']['as_of'],context['as_of'])
        self.assertNotIn('rows',events[0]['data_context'])
        repo.get_daily.assert_not_called()

    async def test_missing_minute_never_calls_ai(self):
        with patch('app.services.market_sources.fetch_market_context',AsyncMock(side_effect=ValueError('上午分钟数据不足'))), patch('app.services.ai_provider.stream_ai_text') as ai:
            events=[json.loads(x) async for x in analyze_stock_stream(self.repo(),Path('.'),'513100.SH',mode='noon')]
        ai.assert_not_called()
        self.assertEqual(events,[{'type':'error','message':'上午分钟数据不足'}])

    async def test_preview_and_mode_validation(self):
        app=FastAPI(); app.include_router(router); app.state.repo=self.repo()
        with TestClient(app) as client, patch('app.services.market_sources.fetch_market_context',AsyncMock(return_value={'as_of':'2026-11-04','rows':[]})), patch('app.services.ai_provider.stream_ai_text') as ai:
            self.assertEqual(client.get('/api/stock-analysis/context?symbol=513100.SH&mode=noon').status_code,200)
            self.assertEqual(client.post('/api/stock-analysis/analyze',json={'symbol':'513100.SH','mode':'invalid'}).status_code,422)
            ai.assert_not_called()

if __name__=='__main__': unittest.main()
