import json
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.stock_analysis import router
from app.services.stock_analyzer import analyze_stock_stream, existing_analysis_context


class ExistingNewsTests(unittest.IsolatedAsyncioTestCase):
    def repo(self):
        repo = Mock()
        repo.get_etf_instruments.return_value = pl.DataFrame({'symbol': [], 'name': []})
        repo.get_instruments.return_value = pl.DataFrame({'symbol': ['601988.SH'], 'name': ['中国银行']})
        return repo

    def bars(self):
        return pl.DataFrame({'date': [date(2026, 11, 23)], 'open': [7.4], 'high': [7.5], 'low': [7.3], 'close': [7.39], 'volume': [10000.]})

    async def test_disabled_news_does_not_fetch(self):
        with patch('app.services.market_news.fetch_news_context', new=AsyncMock()) as fetch:
            context = await existing_analysis_context(self.repo(), '601988.SH', False, df=self.bars())
        fetch.assert_not_awaited()
        self.assertFalse(context['news_enabled'])
        self.assertEqual([], context['news'])

    async def test_news_reaches_model_without_changing_day_mode_or_dropping_levels(self):
        item = {'title': '公司公告', 'url': 'https://example.com/news', 'published_at': '2026-11-24T12:00:00+08:00'}
        bundle = {'items': [item], 'warnings': [], 'source': 'eastmoney', 'fetched_at': '2026-11-24T15:00:00+08:00'}
        async def fake_ai(messages, **kwargs):
            content = messages[1]['content']
            self.assertIn('公司公告', content)
            self.assertIn('"mode": "existing"', content)
            self.assertIn('"period": "day"', content)
            self.assertIn('"levels": {"support": []}', content)
            self.assertIn('外部不可信数据', messages[0]['content'])
            yield '完整报告'
        with patch('app.services.stock_analyzer._load_kline', return_value=self.bars()), \
             patch('app.services.stock_analyzer._load_financials', return_value={}), \
             patch('app.services.stock_analyzer.compute_levels', return_value={'support': []}) as levels, \
             patch('app.services.stock_analyzer.summarize_levels', return_value='summary'), \
             patch('app.services.market_news.fetch_news_context', new=AsyncMock(return_value=bundle)) as fetch, \
             patch('app.services.ai_provider.stream_ai_text', new=fake_ai):
            events = [json.loads(e) async for e in analyze_stock_stream(self.repo(), Path('.'), '601988.SH', include_news=True)]
        self.assertEqual(['meta', 'delta', 'done'], [e['type'] for e in events])
        levels.assert_called_once()
        self.assertEqual('中国银行', fetch.call_args.kwargs['name'])
        context = events[0]['data_context']
        self.assertEqual('2026-11-23', context['as_of'])
        self.assertEqual(bundle['fetched_at'], context['news_fetched_at'])
        self.assertNotIn('rows', context)

    async def test_news_failure_preserves_existing_daily_context(self):
        with patch('app.services.market_news.fetch_news_context', new=AsyncMock(side_effect=TimeoutError())):
            context = await existing_analysis_context(self.repo(), '601988.SH', True, df=self.bars())
        self.assertEqual(7.39, context['rows'][0]['close'])
        self.assertEqual([], context['news'])
        self.assertIn('新闻获取失败', ' '.join(context['warnings']))

    async def test_existing_preview_accepts_news_switch_and_never_calls_ai(self):
        app = FastAPI()
        app.include_router(router)
        app.state.repo = self.repo()
        with patch('app.services.stock_analyzer._load_kline', return_value=self.bars()), \
             patch('app.services.market_news.fetch_news_context', new=AsyncMock(return_value={'items': [], 'warnings': [], 'source': 'none'})) as fetch, \
             patch('app.services.ai_provider.stream_ai_text') as ai, TestClient(app) as client:
            response = client.get('/api/stock-analysis/context?symbol=601988.SH&mode=existing&include_news=true')
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()['news_enabled'])
        fetch.assert_awaited_once()
        ai.assert_not_called()
