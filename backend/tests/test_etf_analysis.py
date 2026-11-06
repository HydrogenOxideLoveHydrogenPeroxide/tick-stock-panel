import asyncio
import json
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from datetime import date
from pathlib import Path
import polars as pl
from app.services.stock_analyzer import _load_kline, analyze_stock_stream

class ETFAnalysisTests(unittest.TestCase):
    def test_etf_stream(self):
        from datetime import timedelta
        repo=Mock()
        repo.store.data_dir=Path(".")
        repo.get_etf_instruments.return_value=pl.DataFrame({"symbol":["513100.SH"]})
        repo.get_etf_daily.return_value=pl.DataFrame({"date":[date.today()-timedelta(days=40-i) for i in range(40)],"open":[2.]*40,"high":[2.2]*40,"low":[1.9]*40,"close":[2.1]*40,"volume":[1000.]*40})
        df=_load_kline(repo,'513100.SH')
        self.assertFalse(df.is_empty())
        async def fake(messages, **kwargs):
            self.assertIn('资产类型：ETF',messages[1]['content'])
            self.assertIn('公司财报',messages[0]['content'])
            yield 'ETF分析测试'
        async def run():
            with patch('app.services.ai_provider.stream_ai_text',fake), patch('app.services.stock_analyzer._load_financials') as fin:
                result=[json.loads(s) async for s in analyze_stock_stream(repo,repo.store.data_dir,'513100.SH')]
                fin.assert_not_called()
            self.assertEqual([x['type'] for x in result],['meta','delta','done'])
        asyncio.run(run())
    def test_stock_routing(self):
        repo=Mock()
        repo.get_etf_instruments.return_value=pl.DataFrame({'symbol':['513100.SH']})
        repo.get_daily.return_value=pl.DataFrame({'date':[date.today()],'close':[1.]})
        self.assertEqual(_load_kline(repo,'000001.SZ').height,1)
        repo.get_etf_daily.assert_not_called()

if __name__=='__main__': unittest.main()
